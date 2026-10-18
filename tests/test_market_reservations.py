"""Reservation-order pre-check and US regular-session confirmation. No network."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrader.core import D, Engine, Journal, ReservationLookup, Settings, Stop
from autotrader.paper import Paper
from autotrader.schedule import Schedule, broker_segment, confirm_regular_session, regular_session_check
import test_doctor
from test_doctor import KRW_MORNING, US_OPEN, Harness

NOW = datetime(2026, 10, 15, 13, 31, tzinfo=timezone.utc)
LIVE = {"symbol": "QQQ", "side": "buy", "quantity": "1"}


def reservation_row(**kw):
    row = dict(ovrs_stck_symb_iscd="QQQ", iscd="QQQ", ovrs_stck_mrkt_tr_cls_code="01", seln_byov_clcd="2",
               odqt="1", acml_cntg_qty="0", ovrs_stck_oder_prc="500", rsev_aspr_tpcd="1", rsev_cncl_yn="",
               oder_sta_date="20261016", oder_fin_date="20261016", cncl_able_yn="Y",
               rsev_acpt_date="20261015", rsev_acpt_srno="9")
    row.update(kw)
    return row


class EngineReservationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / "journal.sqlite")
        self.broker = Paper(usd="1000")
        self.lookups = 0
        original = self.broker.active_reservations

        def counted(now, since=None):
            self.lookups += 1
            return original(now, since)
        self.broker.active_reservations = counted
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper"))

    def tearDown(self):
        self.journal.db.close()
        self.temp.cleanup()

    def tick(self, **kw):
        args = dict(target="TQQQ", signal_date="2026-10-14", expected_signal_date="2026-10-14",
                    session="2026-10-15", now=NOW, may_exchange=False, may_trade=True)
        args.update(kw)
        return self.engine.tick(**args)

    def test_no_reservations_allows_new_order(self):
        self.assertEqual(self.tick(), "submitted: order")
        self.assertEqual(self.lookups, 1)
        self.assertEqual(self.broker.calls[0][:2], ("buy", "TQQQ"))

    def test_active_reservation_blocks_order_and_is_not_cancelled(self):
        self.broker.reservations = [dict(LIVE)]
        with self.assertRaisesRegex(Stop, "1 active reservation order.*QQQ buy 1.*never cancels"):
            self.tick()
        self.assertEqual(self.broker.calls, [])          # no order, no cancel of any kind
        self.assertEqual(self.journal.rows(), [])
        self.assertEqual(self.broker.reservations, [LIVE])

    def test_lookup_failure_blocks_order(self):
        self.broker.reservations = Stop("Incomplete paginated response; no trading on a partial snapshot")
        with self.assertRaisesRegex(Stop, "reservation lookup failed or incomplete.*Incomplete paginated"):
            self.tick()
        self.assertEqual(self.broker.calls, [])

    def test_unexpected_lookup_error_blocks_without_leaking_text(self):
        self.broker.reservations = KeyError("secret-looking-detail")
        with self.assertRaises(Stop) as ctx:
            self.tick()
        self.assertIn("KeyError", str(ctx.exception))
        self.assertNotIn("secret-looking-detail", str(ctx.exception))

    def test_broker_without_lookup_fails_closed(self):
        class NoLookup:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                if name == "active_reservations":
                    raise AttributeError(name)
                return getattr(self.inner, name)
        self.engine.broker = NoLookup(self.broker)
        with self.assertRaisesRegex(Stop, "reservation lookup failed \\(AttributeError\\)"):
            self.tick()
        self.assertEqual(self.broker.calls, [])

    def test_fx_is_also_blocked_but_only_when_fx_would_be_sent(self):
        self.broker.reservations = [dict(LIVE)]
        self.assertEqual(self.tick(may_exchange=True, may_trade=False), "idle: outside execution window")
        self.assertEqual(self.lookups, 0)   # nothing to convert: no lookup, no block
        self.broker.krw = D("1000000")
        with self.assertRaisesRegex(Stop, "active reservation"):
            self.tick(may_exchange=True, may_trade=False)
        self.assertEqual(self.broker.calls, [])

    def test_outside_window_does_not_query(self):
        self.broker.reservations = [dict(LIVE)]
        self.assertEqual(self.tick(may_trade=False), "idle: outside execution window")
        self.assertEqual(self.lookups, 0)

    def test_tracked_order_status_reads_continue_while_new_orders_blocked(self):
        self.assertEqual(self.tick(), "submitted: order")
        self.broker.reservations = [dict(LIVE)]
        lookups = self.lookups
        # Read-only reconciliation of our own accepted order is not blocked.
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=10)), "waiting: order fill")
        self.broker.fill(1)
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=20)), "confirmed: filled")
        self.assertEqual(self.lookups, lookups)
        # Only the NEXT new action is gated.
        with self.assertRaisesRegex(Stop, "active reservation"):
            self.tick(now=NOW + timedelta(seconds=30))
        self.assertEqual([c[0] for c in self.broker.calls], ["buy"])

    def test_market_block_reason_is_raised_after_reconciliation(self):
        self.assertEqual(self.tick(), "submitted: order")
        # An accepted order is still reconciled even when the market is unconfirmed.
        self.assertEqual(self.tick(now=NOW + timedelta(seconds=10), may_trade=False, may_manage_orders=False,
                                   market_block="US regular session not confirmed: x"), "waiting: order fill")
        self.broker.fill(1)
        self.tick(now=NOW + timedelta(seconds=20), may_trade=False, may_manage_orders=False)
        with self.assertRaisesRegex(Stop, "New orders blocked: US regular session not confirmed"):
            self.tick(now=NOW + timedelta(seconds=30), may_trade=False, market_block="US regular session not confirmed: x")


class ReservationCoverageGateTests(unittest.TestCase):
    """A lookup that cannot prove it saw every acceptance date blocks live only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, mode, complete):
        broker = Paper(usd="1000")
        broker.active_reservations = lambda now, since=None: ReservationLookup(
            [], complete=complete, searched_from="20251016", reason="searched only acceptances since 20251016")
        journal = Journal(Path(self.temp.name) / f"{mode}-{complete}.sqlite")
        self.addCleanup(journal.db.close)
        return Engine(broker, journal, Settings(mode=mode)), broker, journal

    def tick(self, engine, **kw):
        return engine.tick(target="TQQQ", signal_date="2026-10-14", expected_signal_date="2026-10-14",
                           session="2026-10-15", now=NOW, may_trade=True, **kw)

    def test_live_blocks_when_coverage_unverified(self):
        engine, broker, journal = self.engine("live", complete=False)
        with self.assertRaisesRegex(Stop, "New orders blocked: reservation coverage unverified: searched only"):
            self.tick(engine)
        self.assertEqual((broker.calls, journal.rows()), ([], []))

    def test_live_fx_also_blocked_when_coverage_unverified(self):
        engine, broker, _ = self.engine("live", complete=False)
        broker.krw = D("1000000")
        with self.assertRaisesRegex(Stop, "coverage unverified"):
            self.tick(engine, may_exchange=True)
        self.assertEqual(broker.calls, [])

    def test_live_proceeds_when_coverage_complete(self):
        engine, broker, _ = self.engine("live", complete=True)
        self.assertEqual(self.tick(engine), "submitted: order")

    def test_observe_plans_but_records_unverified(self):
        engine, broker, journal = self.engine("observe", complete=False)
        self.assertEqual(self.tick(engine), "observe: order")
        events = [json.loads(r[0]) for r in journal.db.execute("SELECT body FROM events WHERE category='unverified'")]
        self.assertEqual(events[0]["check"], "reservations")
        self.assertEqual(broker.calls, [])

    def test_start_date_setting_is_validated(self):
        for bad in ("20261332", "2026-10-15", "202610", "19891231"):
            with self.assertRaises(ValueError):
                Settings(reservation_lookup_start=bad)
        self.assertEqual(Settings(reservation_lookup_start="20200102").reservation_lookup_start, "20200102")


class MeritzReservationLookupTests(Harness):
    """Adapter path through Client._http with schema-checked GET fields."""

    def lookup(self, response, now=KRW_MORNING, since=None):
        from unittest.mock import patch
        from autotrader.meritz import Client, Meritz
        from test_doctor import FakeMeritz
        fake = FakeMeritz(now, **{"/trading/v1/overseas/reserved-orders": response})
        queries = []
        def http(_self, method, path, query=None, *a, **k):
            if path.endswith("reserved-orders"):
                queries.append(dict(query))
            return fake(method, path, query, *a, **k)
        with patch.object(Client, "_http", http):
            broker = Meritz(Client(self.state, allow_mutations=False), self.journal)
            return broker.active_reservations(now, since), queries, fake

    def test_none_sends_all_filter_fields(self):
        lookup, queries, fake = self.lookup(dict(rsp_cd="5820", tr_cont="0"))
        self.assertEqual(lookup.live, [])
        q = queries[0]
        self.assertEqual((q["iqry_sta_date"], q["iqry_fin_date"], q["seln_byov_clcd"], q["iqry_idnt_clcd"]),
                         ("20251016", "20261015", "0", "2"))
        self.assertEqual(len(queries), 1)
        self.assertTrue(all(m == "GET" or p == "/oauth2/token" for m, p in fake.calls))

    def test_default_lookback_is_not_complete(self):
        lookup, _, _ = self.lookup(dict(rsp_cd="5820", tr_cont="0"))
        self.assertFalse(lookup.complete)
        self.assertIn("reservation_lookup_start", lookup.reason)

    def test_active_listed_and_cancelled_excluded(self):
        lookup, _, _ = self.lookup(dict(rsp_cd="5766", tr_cont="0", data=[
            reservation_row(), reservation_row(ovrs_stck_symb_iscd="TQQQ", rsev_cncl_yn="Y", rsev_acpt_srno="10")]))
        self.assertEqual([(r["symbol"], r["side"], r["quantity"]) for r in lookup.live], [("QQQ", "buy", "1")])
        # Reservation serial numbers (used only for cancellation) are not carried.
        self.assertNotIn("rsev_acpt_srno", json.dumps(lookup.live))

    def test_old_active_reservation_missed_by_lookback_found_from_start_date(self):
        old = dict(rsp_cd="5766", tr_cont="0", data=[reservation_row(
            rsev_acpt_date="20230502", oder_sta_date="20230502", oder_fin_date="20271231", rsev_aspr_tpcd="1")])
        # A fixed 365-day look-back misses it, and says so.
        lookup, _, _ = self.lookup(old)
        self.assertEqual(lookup.live, [])
        self.assertFalse(lookup.complete)
        # Searching from the account opening date finds it.
        lookup, queries, _ = self.lookup(old, since="20200101")
        self.assertTrue(lookup.complete)
        self.assertEqual([(r["symbol"], r["accepted"], r["end"]) for r in lookup.live],
                         [("QQQ", "20230502", "20271231")])
        self.assertEqual(lookup.searched_from, "20200101")

    def test_start_date_is_split_into_contiguous_windows(self):
        _, queries, _ = self.lookup(dict(rsp_cd="5820", tr_cont="0"), since="20200101")
        windows = [(q["iqry_sta_date"], q["iqry_fin_date"]) for q in queries]
        self.assertEqual(windows[0][0], "20200101")
        self.assertEqual(windows[-1][1], "20261015")
        days = lambda d: datetime.strptime(d, "%Y%m%d").date()
        for (a, b), (c, _) in zip(windows, windows[1:]):
            self.assertEqual(days(c) - days(b), timedelta(days=1))   # no gap, no overlap
        for a, b in windows:
            self.assertLessEqual((days(b) - days(a)).days + 1, 365)
        self.assertEqual(len(windows), 7)

    def test_ignored_date_filter_is_incomplete(self):
        # Broker returning the same old row for every window = filter not applied.
        raw = lambda q: dict(rsp_cd="5766", tr_cont="0", data=[reservation_row(rsev_acpt_date="20230502")])
        with self.assertRaisesRegex(Stop, "filter not applied"):
            self.lookup(raw, since="20200101")

    def test_repeated_row_is_incomplete(self):
        with self.assertRaisesRegex(Stop, "repeated"):
            self.lookup(dict(rsp_cd="5766", tr_cont="0", data=[reservation_row(), reservation_row()]))

    def test_future_start_date_stops(self):
        with self.assertRaisesRegex(Stop, "future"):
            self.lookup(dict(rsp_cd="5820", tr_cont="0"), since="20261016")

    def test_missing_cursor_is_incomplete(self):
        with self.assertRaisesRegex(Stop, "cursor"):
            self.lookup(dict(rsp_cd="5762", tr_cont="1", data=[reservation_row()]))

    def test_api_error_is_failure(self):
        with self.assertRaisesRegex(Stop, "API read failed"):
            self.lookup(dict(rsp_cd="IGW50004", tr_cont="0", data=[]))

    def test_row_without_documented_fields_is_incomplete(self):
        row = reservation_row()
        del row["rsev_cncl_yn"]
        with self.assertRaisesRegex(Stop, "missing documented fields"):
            self.lookup(dict(rsp_cd="5766", tr_cont="0", data=[row]))

    def test_doctor_reports_coverage(self):
        report, _ = self.run_doctor()
        self.assertFalse(report["reserved_orders"]["coverage"]["complete"])
        self.assertIn("reserved_orders_coverage", self.codes(report))
        report, _ = self.run_doctor(settings=Settings(reservation_lookup_start="20200101"))
        self.assertTrue(report["reserved_orders"]["coverage"]["complete"])
        self.assertEqual(report["reserved_orders"]["coverage"]["requests"], 7)
        self.assertNotIn("reserved_orders_coverage", self.codes(report))

    def test_doctor_lists_lookup_failure_as_engine_blocker(self):
        report, _ = self.run_doctor(**{"/trading/v1/overseas/reserved-orders": dict(rsp_cd="5762", tr_cont="1", data=[])})
        self.assertEqual(report["reserved_orders"]["status"], "error")
        blocker = [b for b in report["blockers"] if b["code"] == "reserved_orders_lookup"]
        self.assertEqual(len(blocker), 1)
        self.assertTrue(blocker[0]["engine_checks"])


class RegularSessionCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schedule = Schedule(NOW)

    def check(self, now, code, begin, end, market="01"):
        status = {"market": market, "code": code, "begin_kst": begin, "end_kst": end}
        return regular_session_check(self.schedule.trading_session(now), status, now)

    def assertRegular(self, result):
        self.assertTrue(result["regular"], result["reasons"])
        self.assertEqual(result["reasons"], [])

    def assertNotRegular(self, result, fragment):
        self.assertFalse(result["regular"])
        self.assertTrue(any(fragment in r for r in result["reasons"]), result["reasons"])

    def test_regular_summer_session(self):
        result = self.check(NOW, "004", "2230", "0500")
        self.assertRegular(result)
        self.assertTrue(result["unverified"])   # always carries unverified conditions

    def test_segment_crossing_midnight(self):
        after_midnight = datetime(2026, 10, 15, 15, 30, tzinfo=timezone.utc)   # 00:30 KST next day
        result = self.check(after_midnight, "004", "2230", "0500")
        self.assertRegular(result)
        self.assertEqual(result["broker_segment_kst"],
                         ["2026-10-15T22:30:00+09:00", "2026-10-16T05:00:00+09:00"])
        start, end = broker_segment("2230", "0500", after_midnight)
        self.assertLess(start, after_midnight)
        self.assertLess(after_midnight, end)

    def test_kst_dates_around_midnight_for_2230_0500_segment(self):
        kst = timezone(timedelta(hours=9))
        cases = [  # (KST now, expected segment start or None, regular?)
            (datetime(2026, 10, 15, 22, 31, tzinfo=kst), "2026-10-15T22:30:00+09:00", True),
            (datetime(2026, 10, 16, 0, 30, tzinfo=kst), "2026-10-15T22:30:00+09:00", True),
            (datetime(2026, 10, 15, 21, 0, tzinfo=kst), None, False),
            (datetime(2026, 10, 16, 5, 0, tzinfo=kst), None, False),   # end is exclusive: closed at 05:00
        ]
        for now, start, regular in cases:
            with self.subTest(now=now.isoformat()):
                segment = broker_segment("2230", "0500", now)
                self.assertEqual(segment[0].isoformat() if segment else None, start)
                if segment:
                    self.assertEqual(segment[1].isoformat(), "2026-10-16T05:00:00+09:00")
                result = self.check(now, "004", "2230", "0500")
                self.assertEqual(result["regular"], regular, result["reasons"])
        last_minute = datetime(2026, 10, 16, 4, 59, tzinfo=kst)
        self.assertTrue(self.check(last_minute, "004", "2230", "0500")["regular"])

    def test_011_alone_is_not_regular(self):
        self.assertNotRegular(self.check(NOW, "011", "0000", "0000"), "0000/0000")
        self.assertNotRegular(self.check(NOW, "011", "1700", "0900"), "differs from the calendar")

    def test_011_with_matching_segment_is_regular_but_noted(self):
        result = self.check(NOW, "011", "2230", "0500")
        self.assertRegular(result)
        self.assertIn("unverified", result["note"])

    def test_pre_market(self):
        pre = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)   # 21:00 KST
        self.assertNotRegular(self.check(pre, "003", "1700", "2230"), "Exchange calendar")
        # Broker wrongly claiming 004 before the open still fails both ways.
        result = self.check(pre, "004", "2230", "0500")
        self.assertNotRegular(result, "Exchange calendar")
        self.assertNotRegular(result, "outside the broker")

    def test_after_market(self):
        after = datetime(2026, 10, 15, 20, 30, tzinfo=timezone.utc)   # 05:30 KST
        self.assertNotRegular(self.check(after, "005", "0500", "0900"), "not a regular-session code")
        self.assertNotRegular(self.check(after, "004", "0500", "0900"), "Exchange calendar")

    def test_holiday(self):
        thanksgiving = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
        self.assertNotRegular(self.check(thanksgiving, "013", "0000", "0000"), "Exchange calendar")
        self.assertNotRegular(self.check(thanksgiving, "004", "2330", "0600"), "Exchange calendar")

    def test_early_close(self):
        during = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)    # 00:00 KST, closes 03:00 KST
        self.assertRegular(self.check(during, "004", "2330", "0300"))
        self.assertNotRegular(self.check(during, "004", "2330", "0600"), "differs from the calendar")
        after_early_close = datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc)
        self.assertNotRegular(self.check(after_early_close, "004", "2330", "0600"), "Exchange calendar")

    def test_daylight_saving_switches(self):
        winter = datetime(2026, 11, 2, 14, 31, tzinfo=timezone.utc)    # first Monday after DST ended
        self.assertRegular(self.check(winter, "004", "2330", "0600"))
        self.assertNotRegular(self.check(winter, "004", "2230", "0500"), "differs from the calendar")
        spring = datetime(2026, 3, 9, 13, 31, tzinfo=timezone.utc)     # first Monday after DST began
        self.assertRegular(self.check(spring, "004", "2230", "0500"))
        self.assertNotRegular(self.check(spring, "004", "2330", "0600"), "outside the broker")

    def test_malformed_and_mismatched_responses(self):
        for begin, end in (("2560", "0500"), ("ab", "0500"), ("", ""), ("223", "0500")):
            self.assertNotRegular(self.check(NOW, "004", begin, end), "not valid KST HHMM")
        self.assertNotRegular(self.check(NOW, "004", "2230", "0500", market="02"), "not for the US market")
        self.assertNotRegular(self.check(NOW, "007", "2230", "0500"), "not a regular-session code")

    def test_read_failure_is_not_open(self):
        def failing():
            raise Stop("API read failed: 0760")
        result = confirm_regular_session(self.schedule, failing, NOW)
        self.assertNotRegular(result, "API read failed: 0760")

        def crashing():
            raise KeyError("raw-detail")
        result = confirm_regular_session(self.schedule, crashing, NOW)
        self.assertNotRegular(result, "KeyError")
        self.assertNotIn("raw-detail", json.dumps(result))


class RunAndDoctorMarketTests(Harness):
    cli_run = test_doctor.RunObserveTests.cli_run

    def test_run_blocks_with_reason_when_011_segment_mismatches(self):
        market = dict(rsp_cd="5766", data=dict(ovrs_stck_mrkt_tr_cls_code="01", ovrs_mkop_cls_code="011",
                                               mrkt_begn_time="1700", mrkt_fin_time="0900"))
        code, out, plans = self.cli_run(US_OPEN, **{"/reference/v1/overseas/market-hours": market})
        self.assertEqual(code, 1)
        self.assertTrue(out["error"].startswith("New orders blocked: US regular session not confirmed"))
        self.assertEqual(plans, [])

    def test_run_blocks_order_plan_with_active_reservation(self):
        reserved = dict(rsp_cd="5766", tr_cont="0", data=[reservation_row()])
        code, out, plans = self.cli_run(US_OPEN, **{"/trading/v1/overseas/reserved-orders": reserved})
        self.assertEqual(code, 1)
        self.assertIn("active reservation", out["error"])
        self.assertEqual(plans, [])

    def test_run_finds_old_reservation_only_with_start_date(self):
        old = dict(rsp_cd="5766", tr_cont="0", data=[reservation_row(rsev_acpt_date="20230502", oder_fin_date="20271231")])
        code, out, plans = self.cli_run(US_OPEN, **{"/trading/v1/overseas/reserved-orders": old})
        self.assertEqual((code, out["result"]), (0, "observe: order"))   # observe: planned, coverage unverified
        code, out, plans = self.cli_run(US_OPEN, execution={"reservation_lookup_start": "20200101"},
                                        **{"/trading/v1/overseas/reserved-orders": old})
        self.assertEqual(code, 1)
        self.assertIn("active reservation", out["error"])

    def test_run_blocks_fx_plan_when_reservation_lookup_incomplete(self):
        incomplete = dict(rsp_cd="5762", tr_cont="1", data=[reservation_row()])
        code, out, plans = self.cli_run(KRW_MORNING, **{"/trading/v1/overseas/reserved-orders": incomplete})
        self.assertEqual(code, 1)
        self.assertIn("reservation lookup failed or incomplete", out["error"])
        self.assertEqual(plans, [])

    def test_doctor_reports_session_check_and_blocker(self):
        market = dict(rsp_cd="5766", data=dict(ovrs_stck_mrkt_tr_cls_code="01", ovrs_mkop_cls_code="004",
                                               mrkt_begn_time="2330", mrkt_fin_time="0600"))
        report, _ = self.run_doctor(now=US_OPEN, **{"/reference/v1/overseas/market-hours": market})
        check = report["market"]["regular_session_check"]
        self.assertFalse(check["regular"])
        self.assertTrue(check["unverified"])
        self.assertIn("market_not_confirmed", self.codes(report))
        self.assertFalse(report["market"]["order_possible_now"]["engine_would_place_new_order"])

    def test_doctor_confirms_matching_session(self):
        report, _ = self.run_doctor(now=US_OPEN)
        self.assertTrue(report["market"]["regular_session_check"]["regular"])
        self.assertNotIn("market_not_confirmed", self.codes(report))


if __name__ == "__main__":
    unittest.main()
