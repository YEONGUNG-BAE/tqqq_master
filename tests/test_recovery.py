from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from autotrader.core import D, Engine, Journal, OrderStatus, Settings, Stop
from autotrader.history import refresh
from autotrader.meritz import Client, Meritz
from autotrader.paper import Paper
from autotrader.signal import decisions
from autotrader.signal import DEFENSIVE_SYMBOL
from test_automation import NOW


class SignalBoundaryTests(unittest.TestCase):
    def test_exact_decimal_thresholds(self):
        rows = decisions(["11.13"]*252+["9.4605", "9.794399999999999", "9.7944"])
        self.assertEqual([r.target for r in rows[-3:]], [DEFENSIVE_SYMBOL, DEFENSIVE_SYMBOL, "TQQQ"])

    def test_just_above_exit_does_not_exit(self):
        self.assertEqual(decisions(["11.13"]*252+["9.460500000000001"])[-1].target, "TQQQ")


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name)/"j.sqlite")
        self.broker = Paper(usd="1000")
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper"))

    def tearDown(self):
        self.journal.db.close()
        self.tmp.cleanup()

    def tick(self, **kw):
        args = dict(target="TQQQ", signal_date="2026-10-14", expected_signal_date="2026-10-14",
                    session="2026-10-15", now=NOW, may_trade=True)
        return self.engine.tick(**dict(args, **kw))

    def test_no_cancel_mutation_outside_market(self):
        self.tick()
        result = self.tick(now=NOW+timedelta(minutes=10), may_trade=False, may_manage_orders=False)
        self.assertEqual(result, "waiting: market permits cancellation")
        self.assertEqual(len(self.broker.calls), 1)

    def test_manage_existing_order_after_new_order_window(self):
        self.tick()
        result = self.tick(now=NOW+timedelta(minutes=20), may_trade=False, may_manage_orders=True)
        self.assertEqual(result, "submitted: cancel")

    def test_excess_fill_quantity_blocks_next_action(self):
        self.tick()
        self.broker.order_status = lambda receipt: OrderStatus("filled", D(1000))
        with self.assertRaises(Stop):
            self.tick()
        self.assertEqual(len(self.broker.calls), 1)

    def test_small_deposit_can_buy_one_share_with_fee_buffer(self):
        self.broker.usd = D(100000)/D(1400)
        self.tick()
        order = next(iter(self.broker.orders.values()))
        self.assertEqual(order["qty"], D(1))
        self.assertLessEqual(order["qty"]*order["price"]*(1+self.engine.cfg.fee_buffer), self.broker.usd)


class AdapterRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = Journal(Path(self.tmp.name)/"j.sqlite")

    def tearDown(self):
        self.j.db.close()
        self.tmp.cleanup()

    def client(self, **kw):
        with patch.dict("os.environ", {"MERITZ_CLIENT_ID":"dummy", "MERITZ_CLIENT_SECRET":"dummy"}):
            return Client(self.tmp.name, **kw)

    def test_pages_preserve_cursor_whitespace_and_request_all_records(self):
        c = self.client()
        with patch.object(c, "call", side_effect=[
            dict(rsp_cd="5762", data=[dict(oder_no=1)], tr_cont="1", tr_cont_key="cursor   "),
            dict(rsp_cd="5766", data=[dict(oder_no=2)], tr_cont="0")]) as call:
            result = c.pages("/history", query=dict(cntg_cls_code="0"))
        self.assertEqual(len(result), 2)
        self.assertEqual(call.call_args_list[1].kwargs["query"]["tr_cont_key"], "cursor   ")

    def test_repeated_cursor_stops_instead_of_returning_partial_ledger(self):
        c = self.client()
        page = dict(rsp_cd="5762", data=[dict(oder_no=1)], tr_cont="1", tr_cont_key="same")
        with patch.object(c, "call", return_value=page) as call:
            with self.assertRaises(Stop):
                c.pages("/history", query={})
        self.assertEqual(call.call_count, 2)

    def test_token_from_different_key_is_not_reused(self):
        c = self.client()
        c.token_file.write_text(json.dumps(dict(token="wrong", expires=9999999999, key_fingerprint="other")))
        with patch.object(c, "_http", return_value=dict(access_token="fresh", expires_in=3600)) as http:
            self.assertEqual(c.token(), "fresh")
            self.assertEqual(http.call_count, 1)

    def test_state_cannot_bind_to_different_api_key(self):
        Meritz(self.client(), self.j)
        c = self.client()
        c.key_fingerprint = "different"
        with self.assertRaises(Stop):
            Meritz(c, self.j)

    def historical_row(self, **extra):
        row = dict(oder_date="20251015", oder_no=12, oder_rctf_cncl_cls_code="0",
                   iscd="TQQQ.OQ", seln_byov_clcd="2", oder_dqty="10.000000",
                   cntg_dqty="10.000000", ovrs_stck_oder_stat_cls_code="0")
        return dict(row, **extra)

    def test_prior_date_fully_filled_order_recovered_from_detail(self):
        c = self.client()
        c.call = Mock(return_value=dict(rsp_cd="5766", data=[self.historical_row()]))
        a = Meritz(c, self.j)
        result = a.order_status(dict(date="20251015", number=12, symbol="TQQQ", side="buy", qty="10"))
        self.assertEqual(result.state, "filled")
        self.assertEqual(result.filled, D(10))
        self.assertEqual(c.call.call_args.kwargs["query"], dict(oder_sta_date="20251015", oder_no=12))

    def test_prior_date_partial_is_not_assumed_cancelled(self):
        c = self.client()
        c.call = Mock(return_value=dict(rsp_cd="5766", data=[self.historical_row(cntg_dqty="3")]))
        with self.assertRaises(Stop):
            Meritz(c, self.j).order_status(dict(date="20251015", number=12))

    def test_same_number_wrong_symbol_is_not_reconciled(self):
        c = self.client()
        c.call = Mock(return_value=dict(rsp_cd="5766", data=[self.historical_row()]))
        with self.assertRaises(Stop):
            Meritz(c, self.j).order_status(dict(date="20251015", number=12, symbol="QQQ", side="buy", qty="10"))

    def test_unknown_order_code_stops(self):
        with self.assertRaises(Stop):
            Meritz._state(dict(odqt=10, cntg_qty=0, ovrs_stck_oder_stat_cls_code="9"))


class HistoryGapTests(unittest.TestCase):
    def test_more_than_252_missing_sessions_are_all_replayed(self):
        # Synthetic numbered sessions still use real ascending calendar dates.
        from datetime import date
        dates = [(date(2024, 1, 1)+timedelta(days=i)).isoformat() for i in range(600)]
        old_dates, gap = dates[:252], dates[252:]
        all_prices = {d:D(100) for d in dates}
        all_prices[gap[0]] = D(84)
        # Gap's first exit falls outside the current 252 rows. Later closes stay
        # below reentry while the 100 high remains in the rolling window.
        for d in gap[1:]:
            all_prices[d] = D(86)
        # Keep a recent high that prevents rolling expiry from masking the gap.
        all_prices[gap[200]] = D(99)
        class Calendar:
            sessions = [(d, None, None) for d in dates]
            def recent_dates(self, last):return dates[-252:]
        class Feed:
            def completed_candles(self, first, last):
                return {d:p for d,p in all_prices.items() if first<=d<=last}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"history.csv"
            p.write_text("date,close\n"+"".join(d+",100\n" for d in old_dates))
            actual = refresh(Feed(), p, Calendar(), dates[-1])
            self.assertEqual(len(p.read_text().splitlines()), 601)
        expected = decisions(all_prices.values())[-1]
        self.assertEqual(actual["target"], expected.target)


if __name__ == "__main__":
    unittest.main()
