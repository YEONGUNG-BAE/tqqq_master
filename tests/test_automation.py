import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader.core import D, Engine, FXQuote, Journal, Quote, Rejected, Settings, Snapshot, Stop, UnknownOutcome
from autotrader.history import merge_history, read_history, refresh
from autotrader.meritz import Meritz, Client
from autotrader.paper import Paper
from autotrader.schedule import Schedule
from autotrader.signal import decisions

NOW = datetime(2026, 10, 15, 13, 31, tzinfo=timezone.utc)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite"
        self.journal = Journal(self.path)
        self.broker = Paper(usd="1000")
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper"))

    def tearDown(self):
        self.journal.db.close()
        self.temp.cleanup()

    def tick(self, **kw):
        args = dict(target="TQQQ", signal_date="2026-10-14", expected_signal_date="2026-10-14",
                    session="2026-10-15", now=NOW, may_exchange=False, may_trade=True)
        args.update(kw)
        return self.engine.tick(**args)

    def test_fx_checkpoint_and_restart_no_duplicate(self):
        self.broker.krw = D("1000000")
        self.tick(may_exchange=True, may_trade=False)
        self.journal.db.close()
        self.journal = Journal(self.path)
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper"))
        self.tick(may_exchange=True, may_trade=False)
        self.assertEqual([c[0] for c in self.broker.calls], ["fx"])
        self.assertEqual(self.journal.rows()[0]["status"], "done")

    def test_fx_timeout_may_succeed_and_never_repeats(self):
        self.broker.krw = D("1000000")
        def interrupted(amount, quote):
            Paper.exchange(self.broker, amount, quote)
            raise TimeoutError()
        self.broker.exchange = interrupted
        with self.assertRaises(UnknownOutcome):
            self.tick(may_exchange=True)
        self.assertEqual(self.journal.rows()[0]["status"], "unknown")
        # Recovered from the broker's own FX record, never resent.
        self.assertEqual(self.tick(may_exchange=True), "recovered: fx")
        self.tick(may_exchange=True)
        self.assertEqual([c[0] for c in self.broker.calls].count("fx"), 1)
        self.assertEqual(self.journal.rows()[0]["status"], "done")

    def test_crash_before_receipt_checkpoint_prevents_resubmit(self):
        self.journal.create("order", {"symbol": "TQQQ"}, NOW, "2026-10-15")
        with self.assertRaises(UnknownOutcome):
            self.tick()
        self.assertEqual(self.broker.calls, [])

    def test_order_timeout_may_succeed_and_never_repeats(self):
        def interrupted(*args):
            Paper.order(self.broker, *args)
            raise TimeoutError()
        self.broker.order = interrupted
        with self.assertRaises(UnknownOutcome):
            self.tick()
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper"))
        self.assertEqual(self.tick(), "recovered: order")
        self.assertEqual(self.tick(), "waiting: order fill")
        self.assertEqual(len(self.broker.orders), 1)

    def test_partial_sale_waits_then_cancel_and_sells_remainder(self):
        self.broker.positions = {"QQQ": D(10)}
        self.tick()
        n = next(iter(self.broker.orders))
        self.broker.fill(n, 3)
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=30)), "waiting: order fill")
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=121)), "submitted: cancel")
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=122)), "confirmed: cancellation")
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=123)), "confirmed: cancelled")
        self.tick(now=NOW + timedelta(seconds=124))
        self.assertEqual(self.broker.calls[-1], ("sell", "QQQ", D(7)))

    def test_full_sale_confirms_before_buy(self):
        self.broker.positions = {"QQQ": D(10)}
        self.tick()
        self.broker.fill(next(iter(self.broker.orders)))
        self.assertEqual(self.tick(), "confirmed: filled")
        self.assertEqual(len(self.broker.orders), 1)
        self.tick()
        self.assertEqual(self.broker.calls[-1][0:2], ("buy", "TQQQ"))

    def test_pending_buy_not_duplicated_after_restart(self):
        self.tick()
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper"))
        self.tick()
        self.assertEqual(len(self.broker.orders), 1)

    def test_holdings_lag_does_not_duplicate(self):
        self.tick()
        n = next(iter(self.broker.orders))
        self.broker.orders[n]["filled"] = self.broker.orders[n]["qty"]
        self.broker.orders[n]["state"] = "filled"
        self.assertEqual(self.tick(), "waiting: holdings reflection")
        self.assertEqual(len(self.broker.orders), 1)

    def test_new_money_buys_qqq_in_defensive_state(self):
        self.tick(target="QQQ")
        self.assertEqual(self.broker.calls[-1][0:2], ("buy", "QQQ"))

    def test_real_buying_power_caps_quantity(self):
        self.broker.power_fraction = D("0.95")
        self.tick()
        r = next(iter(self.broker.orders.values()))
        self.assertLessEqual(r["qty"] * r["price"] * (1 + self.engine.cfg.fee_buffer), D(945))

    def test_krw_reserve_remains(self):
        self.broker.krw = D("1000000")
        self.engine.cfg.krw_reserve = D("300000")
        self.tick(may_exchange=True)
        self.assertEqual(self.broker.krw, D("300000"))

    def test_fx_daily_cap_and_later_deposit(self):
        self.broker.krw = D("1000000")
        self.engine.cfg.max_fx_krw_per_day = D("200000")
        self.tick(may_exchange=True, may_trade=False)
        self.tick(may_exchange=True, may_trade=False)
        self.assertEqual(self.broker.krw, D("800000"))
        self.assertEqual(len(self.broker.calls), 1)
        self.tick(session="2026-10-16", may_exchange=True, may_trade=False)
        self.assertEqual(self.broker.krw, D("600000"))

    def test_stale_signal_no_actions(self):
        with self.assertRaises(Stop):
            self.tick(signal_date="2026-10-13")
        self.assertEqual(self.broker.calls, [])

    def test_stale_fx_no_actions(self):
        self.broker.krw = D("1000000")
        self.broker.fx_quote = lambda now: FXQuote(D(1400), D(1400), NOW - timedelta(minutes=5), "stale")
        with self.assertRaises(Stop):
            self.tick(may_exchange=True)
        self.assertEqual(self.broker.calls, [])

    def test_fx_discount_regression_stops(self):
        self.broker.krw = D("1000000")
        self.broker.fx_quote = lambda now: FXQuote(D(1414), D(1400), NOW, "fresh")
        with self.assertRaises(Stop):
            self.tick(may_exchange=True)
        self.assertEqual(self.broker.calls, [])

    def test_stale_or_crossed_quote_stops(self):
        for quote in [Quote(D(70), D(70), D("0.01"), NOW - timedelta(minutes=2)),
                      Quote(D(71), D(70), D("0.01"), NOW)]:
            self.broker.quote = lambda symbol, now: quote
            with self.assertRaises(Stop):
                self.tick()
        self.assertEqual(self.broker.calls, [])

    def test_warning_not_automatically_accepted(self):
        def warning(*args):
            raise Rejected("warning")
        self.broker.order = warning
        with self.assertRaises(Rejected):
            self.tick()
        with self.assertRaises(Stop):
            self.tick()
        self.assertEqual(len(self.journal.rows()), 1)

    def test_unknown_positions_untouched(self):
        self.broker.positions = {"QQQM": D(29), "BRK.B": D(4)}
        with self.assertRaises(Stop):
            self.tick()
        self.assertEqual(self.broker.calls, [])

    def test_migration_requires_allowlist(self):
        self.broker.positions = {"QQQM": D(29)}
        self.engine.cfg.migrate_symbols = ("QQQM",)
        self.tick()
        self.assertEqual(self.broker.calls[-1], ("sell", "QQQM", D(29)))

    def test_observe_never_calls_mutations(self):
        self.journal.db.close()
        self.journal = Journal(Path(self.temp.name) / "observe.sqlite")
        self.engine = Engine(self.broker, self.journal, Settings(mode="observe"))
        self.broker.krw = D("1000000")
        self.tick(may_exchange=True)
        self.tick(may_exchange=False)
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.journal.rows(), [])

    def test_cannot_reuse_paper_state_for_live(self):
        with self.assertRaises(Stop):
            Engine(self.broker, self.journal, Settings(mode="live"))

    def test_external_untracked_order_stops(self):
        orig = self.broker.snapshot
        def external():
            s = orig(); s.external_open_orders = True; return s
        self.broker.snapshot = external
        with self.assertRaises(Stop):
            self.tick()
        self.assertEqual(self.broker.calls, [])


class CalendarHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schedule = Schedule(NOW)
        cls.dates = cls.schedule.recent_dates("2026-10-14")
        cls.prices = {d: D(100) for d in cls.dates}

    def test_dst_and_early_close(self):
        rows = {s[0]: s for s in self.schedule.sessions}
        self.assertEqual(rows["2026-10-15"][1].hour, 13)
        self.assertEqual(rows["2026-11-03"][1].hour, 14)
        self.assertEqual(rows["2026-11-27"][2].hour, 18)
        self.assertNotIn("2026-11-26", rows)
        self.assertNotIn("2026-10-17", rows)

    def test_completed_close_not_current_intraday(self):
        self.assertEqual(self.schedule.expected_close(NOW), "2026-10-14")
        during = datetime(2026, 10, 14, 19, 59, tzinfo=timezone.utc)
        self.assertEqual(self.schedule.expected_close(during), "2026-10-13")
        shortly = datetime(2026, 10, 14, 20, 2, tzinfo=timezone.utc)
        self.assertEqual(self.schedule.expected_close(shortly), "2026-10-13")
        self.assertEqual(self.schedule.expected_close(shortly + timedelta(minutes=3)), "2026-10-14")

    def test_execution_window(self):
        self.assertTrue(self.schedule.execution_window(NOW))
        self.assertFalse(self.schedule.execution_window(NOW - timedelta(minutes=1)))
        self.assertFalse(self.schedule.execution_window(NOW + timedelta(minutes=15)))

    def test_price_overlap_rounding_and_missing_session(self):
        fresh = {day: price + D("0.005") for day, price in self.prices.items()}
        out, rebase = merge_history(self.prices, fresh, self.dates)
        self.assertIsNone(rebase)
        self.assertEqual(out[self.dates[-1]], D("100.005"))
        old = dict(self.prices); fresh = dict(self.prices)
        del old[self.dates[50]]; del fresh[self.dates[50]]
        with self.assertRaises(Stop):
            merge_history(old, fresh, self.dates)

    def test_dividend_adjustment_mismatch_stops(self):
        fresh = dict(self.prices)
        for day in self.dates[:150]:
            fresh[day] *= D("0.999")
        with self.assertRaises(Stop):
            merge_history(self.prices, fresh, self.dates)

    def test_uniform_split_rebases_all_older_history(self):
        old = dict(self.prices); old["2024-01-02"] = D(90)
        fresh = {day: price / 2 for day, price in self.prices.items()}
        result, rebase = merge_history(old, fresh, self.dates)
        self.assertEqual(result["2024-01-02"], D(45))
        self.assertEqual(rebase["divide_by"], "2")

    def test_nonuniform_split_does_not_rebase(self):
        fresh = {day: price / 2 for day, price in self.prices.items()}
        fresh[self.dates[80]] = D(49)
        with self.assertRaises(Stop):
            merge_history(self.prices, fresh, self.dates)

    def test_refresh_checks_each_chunk_and_excludes_future_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.csv"
            all_dates = self.dates + ["2026-10-15"]
            path.write_text("date,close\n" + "".join(day + ",100\n" for day in all_dates))
            class Feed:
                def completed_candles(_, first, last):
                    return {day: D(100) for day in self.dates if first <= day <= last}
            signal = refresh(Feed(), path, self.schedule, "2026-10-14")
            self.assertEqual(signal["target"], "TQQQ")
            self.assertEqual(max(read_history(path)), "2026-10-14")

    def test_rule_hysteresis_and_expiry_diagnostic(self):
        prices = [100] * 252 + [84] + [85] * 252
        result = decisions(prices)
        self.assertEqual(result[252].target, "QQQ")
        self.assertEqual(result[253].target, "QQQ")
        expiry = [r for r in result if r.expiry_driven]
        self.assertEqual(len(expiry), 1)
        self.assertEqual(expiry[0].target, "TQQQ")


class AdapterTests(unittest.TestCase):
    def test_partial_cancel_does_not_finish_original_order(self):
        row = dict(odqt=10, cntg_qty=3, cncl_cnfr_qty=4,
                   ovrs_stck_oder_stat_cls_code="0", rctf_cncl_cls_code="3")
        self.assertEqual(Meritz._state(row).state, "open")

    def test_cancel_confirmed_vs_cancel_requested(self):
        base = dict(odqt=10, cntg_qty=3, ovrs_stck_oder_stat_cls_code="0", rctf_cncl_cls_code="1")
        self.assertEqual(Meritz._state(dict(base, cncl_odqt=7, cncl_cnfr_qty=0)).state, "open")
        self.assertEqual(Meritz._state(dict(base, cncl_odqt=7, cncl_cnfr_qty=7)).state, "cancelled")

    def test_no_automatic_warning_ack(self):
        with self.assertRaises(Rejected):
            Meritz._receipt({"data": {"oder_no": 1, "warn_cls_code": "1"}}, NOW)

    def test_observe_transport_blocks_post_before_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"MERITZ_CLIENT_ID": "dummy", "MERITZ_CLIENT_SECRET": "dummy"}):
            client = Client(tmp, allow_mutations=False)
            with patch.object(client, "_http") as http:
                with self.assertRaises(Stop):
                    client.call("POST", "/trading/v1/overseas/orders/buy", body={})
                http.assert_not_called()

    def test_successful_post_is_not_retried_on_expired_token(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"MERITZ_CLIENT_ID": "dummy", "MERITZ_CLIENT_SECRET": "dummy"}):
            client = Client(tmp, allow_mutations=True)
            with patch.object(client, "token", return_value="dummy"), patch.object(client, "_http", return_value={"rsp_cd": "EGW00123"}) as http:
                with self.assertRaises(UnknownOutcome):
                    client.call("POST", "/forex/v1/exchanges", body={})
                self.assertEqual(http.call_count, 1)


if __name__ == "__main__":
    unittest.main()
