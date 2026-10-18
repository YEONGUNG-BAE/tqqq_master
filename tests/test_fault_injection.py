"""Fault injection for automatic recovery. Fake brokers, temp state, fixed clocks.

No real API call is made: Paper/FaultBroker simulate broker-side effects, and the
REST-adapter cases replace Client._http with a schema-checked fake. Each test
asserts invariants, not HTTP success: no duplicate financial request, no double
counting of fills/holdings, no order beyond available cash, no unexpected open
bot order, no disposal of other assets or other orders, and either the target
reached or an explicit hold/lock.
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from autotrader import __main__ as cli
from autotrader.core import (ACCEPTED, CANCELLING, DONE, NOT_SENT, PARTIAL, PREPARED, REFUSED, SUBMITTING, UNKNOWN,
                             D, Engine, Journal, Locked, NotSent, OrderStatus, Quote, Refused, RefusedUnconfirmed,
                             Settings, Stop, UnknownOutcome, set_lock)
from autotrader.meritz import AuthError, Client, KST, Meritz
from autotrader.notify import Notifier, scrub, telegram_channel
from autotrader.paper import Paper
from test_doctor import APIS, KRW_MORNING, SECRET_ENV, TOKEN, US_OPEN, FakeMeritz, Harness

NOW = datetime(2026, 10, 15, 13, 31, tzinfo=timezone.utc)
SIGNAL = dict(target="TQQQ", signal_date="2026-10-14", expected_signal_date="2026-10-14", session="2026-10-15")


class FaultBroker(Paper):
    """Paper broker with injectable faults. Broker-side effects always happen
    before an injected lost response, as with a real timeout after acceptance."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.lose = set()            # kinds whose response is lost after the broker acted
        self.refuse = None           # Refused/RefusedUnconfirmed raised after the send started
        self.deferred_cancel = False
        self.status_script = {}      # order number -> [(state, filled)] reported before the truth
        self.candidates_script = []  # overrides for find_candidates: list | Exception | "dup"
        self.power = None            # buying-power override (sale-proceeds lag)
        self.usd_shown = self.krw_shown = None   # balance reflection lag
        self.external = False
        self.lookups = 0

    def snapshot(self):
        s = super().snapshot()
        if self.power is not None:
            s.buying_power = self.power
        if self.usd_shown is not None:
            s.usd = s.buying_power = self.usd_shown
        if self.krw_shown is not None:
            s.krw = self.krw_shown
        s.external_open_orders = self.external
        return s

    def _maybe_refuse(self):
        if self.refuse is not None:
            self._sending()
            raise self.refuse

    def exchange(self, krw, quote):
        self._maybe_refuse()
        receipt = super().exchange(krw, quote)
        if "fx" in self.lose:
            raise TimeoutError()
        return receipt

    def order(self, symbol, side, qty, price, now):
        self._maybe_refuse()
        receipt = super().order(symbol, side, qty, price, now)
        if "order" in self.lose:
            raise TimeoutError()
        return receipt

    def cancel(self, original, qty):
        if not self.deferred_cancel:
            receipt = super().cancel(original, qty)
        else:
            self._sending()
            self.calls.append(("cancel", original["number"], qty))
            number = len(self.calls)
            self.cancel_log.append({"number": number, "original": original["number"], "at": None})
            receipt = {"number": number, "original": original}
        if "cancel" in self.lose:
            raise TimeoutError()
        return receipt

    def confirm_cancel(self, number):
        if self.orders[number]["state"] == "open":
            self.orders[number]["state"] = "cancelled"

    def order_status(self, receipt):
        script = self.status_script.get(receipt["number"])
        if script:
            state, filled = script.pop(0)
            return OrderStatus(state, D(filled), {})
        return super().order_status(receipt)

    def find_candidates(self, kind, payload, created, tracked):
        self.lookups += 1
        if self.candidates_script:
            item = self.candidates_script.pop(0)
            if isinstance(item, Exception):
                raise item
            if item == "dup":
                found = super().find_candidates(kind, payload, created, tracked)
                return found + found
            return item
        return super().find_candidates(kind, payload, created, tracked)


class Base(unittest.TestCase):
    mode = "paper"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "journal.sqlite"
        self.broker = FaultBroker(usd="1000")
        self.journal = None
        self.open()

    def tearDown(self):
        self.journal.db.close()

    def open(self, **cfg):
        if self.journal is not None:
            self.journal.db.close()
        self.journal = Journal(self.path)
        self.engine = Engine(self.broker, self.journal, Settings(mode=self.mode, **cfg))
        self.engine.rng.seed(7)

    restart = open

    def tick(self, at=0, **kw):
        args = dict(SIGNAL, now=NOW + timedelta(seconds=at), may_exchange=False, may_trade=True)
        args.update(kw)
        return self.engine.tick(**args)

    def sent(self, *kinds):
        return [c for c in self.broker.calls if c[0] in kinds]

    def statuses(self, kind=None):
        return [r["status"] for r in self.journal.rows() if kind in (None, r["kind"])]


# 1 --------------------------------------------------------------------------
class CrashPointTests(Base):
    def test_crash_before_intent_saved_sends_nothing(self):
        with patch.object(self.journal, "create", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                self.tick()
        self.assertEqual(self.broker.calls, [])
        self.restart()
        self.assertEqual(self.tick(), "submitted: order")
        self.assertEqual(len(self.sent("buy")), 1)

    def test_intent_saved_but_send_never_started_is_abandoned_then_redone_once(self):
        self.journal.create("order", dict(symbol="TQQQ", side="buy", qty="14", price="70.08", held_before="0",
                                          target="TQQQ"), NOW, "2026-10-15", status=PREPARED, run_id="crashed")
        self.restart()
        with self.assertRaisesRegex(Stop, "Another process may be about to send"):
            self.tick(at=60)            # too fresh: could still be another process
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.tick(at=301), "submitted: order")
        self.assertEqual(self.statuses("order"), [NOT_SENT, ACCEPTED])
        self.assertEqual(len(self.sent("buy")), 1)

    def test_send_started_without_broker_record_is_never_resent(self):
        self.journal.create("order", dict(symbol="TQQQ", side="buy", qty="14", price="70.08", held_before="0",
                                          target="TQQQ"), NOW, "2026-10-15", status=SUBMITTING, run_id="crashed")
        self.restart()
        for at in (10, 600, 3 * 3600):
            with self.assertRaises(UnknownOutcome):
                self.tick(at=at)
        with self.assertRaises(Locked):
            self.tick(at=6 * 3600)      # long-term unknown -> hard lock, still no resend
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.journal.meta("lock_info")["code"], "unknown_outcome")

    def test_accepted_but_response_lost_is_recovered_once(self):
        self.broker.lose.add("order")
        with self.assertRaises(UnknownOutcome):
            self.tick()
        self.assertEqual(self.statuses(), [UNKNOWN])
        self.broker.lose.clear()
        self.restart()
        self.assertEqual(self.tick(at=5), "recovered: order")
        self.broker.fill(1)
        self.assertEqual(self.tick(at=10), "confirmed: filled")
        self.assertEqual(self.tick(at=15), "idle: insufficient USD for one share")
        self.assertEqual(len(self.sent("buy")), 1)
        self.assertEqual(self.statuses(), [DONE])

    def test_crash_after_acceptance_before_receipt_saved_is_recovered(self):
        original = self.journal.update
        def failing(action, status, *a, **k):
            if status == ACCEPTED:
                raise sqlite3.OperationalError("disk full")
            return original(action, status, *a, **k)
        with patch.object(self.journal, "update", side_effect=failing):
            with self.assertRaises(sqlite3.OperationalError):
                self.tick()
        self.assertEqual(self.statuses(), [SUBMITTING])
        self.restart()
        self.assertEqual(self.tick(at=5), "recovered: order")
        self.assertEqual(len(self.sent("buy")), 1)


# 2 --------------------------------------------------------------------------
class LedgerLookupTests(Base):
    def lose_one(self):
        self.broker.lose.add("order")
        with self.assertRaises(UnknownOutcome):
            self.tick()
        self.broker.lose.clear()

    def test_empty_then_failed_lookup_hold_then_recover(self):
        self.lose_one()
        self.broker.candidates_script = [[], Stop("Missing/repeated ledger cursor")]
        with self.assertRaises(UnknownOutcome):
            self.tick(at=5)             # empty answer is not proof of non-acceptance
        with self.assertRaises(UnknownOutcome):
            self.tick(at=10)            # lookup failure keeps the hold
        self.assertEqual(self.tick(at=15), "recovered: order")
        self.assertEqual(len(self.sent("buy")), 1)

    def test_several_candidates_lock_and_attribute_none(self):
        self.lose_one()
        self.broker.candidates_script = ["dup"]
        with self.assertRaises(Locked):
            self.tick(at=5)
        self.assertEqual(self.statuses(), [UNKNOWN])
        with self.assertRaises(Locked):
            self.tick(at=10)
        self.assertEqual(len(self.sent("buy")), 1)
        self.assertEqual(self.journal.meta("lock_info")["code"], "ambiguous_attribution")


class AdapterCandidateTests(unittest.TestCase):
    """Meritz.find_candidates filtering and query range, on a stub client."""

    class StubClient:
        def __init__(self, history, today, fx=()):
            self.history, self.today_rows, self.fx, self.queries = history, today, list(fx), []

        def pages(self, path, *, query):
            self.queries.append((path, dict(query)))
            if isinstance(self.history, Exception):
                raise self.history
            return list(self.fx) if "forex" in path else list(self.history)

        def call(self, method, path, *, query=None, **kw):
            return {"rsp_cd": "5766", "data": list(self.today_rows)}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")

    def tearDown(self):
        self.journal.db.close()
        self.tmp.cleanup()

    @staticmethod
    def row(no, at, **kw):
        base = dict(oder_date=at.astimezone(KST).strftime("%Y%m%d"), oder_no=no, oder_rctf_cncl_cls_code="0",
                    iscd="TQQQ.OQ", seln_byov_clcd="2", oder_dqty="3.000000", ovrs_stck_oder_prc="70.0800",
                    oder_exec_ymdh=at.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"))
        base.update(kw)
        return base

    def test_candidates_need_matching_fields_time_and_no_prior_attribution(self):
        created = datetime(2026, 10, 15, 14, 59, tzinfo=timezone.utc)      # 23:59 KST
        rows = [self.row(1, created + timedelta(seconds=1)),               # the one
                self.row(2, created - timedelta(minutes=30)),              # before the intent
                self.row(3, created + timedelta(seconds=2)),               # already tracked
                self.row(4, created + timedelta(seconds=3), ovrs_stck_oder_prc="70.07"),
                self.row(5, created + timedelta(seconds=4), oder_rctf_cncl_cls_code="2", orgl_oder_no=1)]
        client = self.StubClient(rows, [])
        broker = Meritz(client, self.journal)
        payload = dict(symbol="TQQQ", side="buy", qty="3", price="70.08")
        tracked = {"orders": {(rows[2]["oder_date"], 3)}, "fx": set()}
        found = broker.find_candidates("order", payload, created, tracked)
        self.assertEqual([c["number"] for c in found], [1])
        # History is searched from the intent's KST date, across midnight, to today.
        self.assertEqual(client.queries[0][1]["oder_sta_date"], "20261015")
        cancel = broker.find_candidates("cancel", {"original": {"date": rows[0]["oder_date"], "number": 1}},
                                        created, tracked)
        self.assertEqual([c["number"] for c in cancel], [5])

    def test_page_gap_is_a_failed_lookup_not_an_empty_one(self):
        broker = Meritz(self.StubClient(Stop("Missing/repeated ledger cursor; no trading on partial history"), []),
                        self.journal)
        with self.assertRaises(Stop):
            broker.find_candidates("order", dict(symbol="TQQQ", side="buy", qty="3", price="70.08"),
                                   NOW, {"orders": set(), "fx": set()})

    def test_fx_candidates_only_open_api_buys_of_compatible_amount(self):
        created = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)
        def fx(srno, chan="120", amount="999999", hour="100005"):
            return dict(tr_date="20261015", mnec_clcd="1", crcd="USD", chan_type_cls_code=chan, aplt_date="20261015",
                        aplt_hour=hour, aplt_srno=srno, tr_amt=amount, frcr_tr_amt="714.285", aply_exrt="1400.0000",
                        rspn_cntt="정상")
        client = self.StubClient([], [], fx=[fx(1), fx(2, chan="157"), fx(3, amount="500000"), fx(4, hour="090000")])
        found = Meritz(client, self.journal).find_candidates("fx", {"krw": "1000000"}, created, {"orders": set(), "fx": set()})
        self.assertEqual([c["ref"] for c in found], ["20261015:1"])


# 3 --------------------------------------------------------------------------
class PartialFillCancelTests(Base):
    def test_fill_just_before_cancel_confirmation_counts_only_final_fills(self):
        self.broker.positions = {"QQQ": D(10)}
        self.broker.deferred_cancel = True
        self.assertEqual(self.tick(), "submitted: order")                       # sell 10
        self.broker.fill(1, 3)
        self.assertEqual(self.tick(at=30), "waiting: order fill")
        self.assertEqual(self.statuses("order"), [PARTIAL])
        self.assertEqual(self.tick(at=121), "submitted: cancel")
        self.assertEqual(self.statuses("order"), [CANCELLING])
        self.broker.fill(1, 2)                                                  # extra fill before the cancel lands
        self.assertEqual(self.tick(at=125), "waiting: cancellation confirmation")
        self.broker.confirm_cancel(1)
        self.assertEqual(self.tick(at=130), "confirmed: cancellation")
        self.assertEqual(self.tick(at=135), "confirmed: cancelled")
        order = next(r for r in self.journal.rows() if r["kind"] == "order")
        self.assertEqual((order["status"], order["filled"]), (DONE, "5"))
        self.assertEqual(self.broker.positions, {"QQQ": D(5)})
        self.tick(at=140)
        self.assertEqual(self.sent("sell"), [("sell", "QQQ", D(10)), ("sell", "QQQ", D(5))])
        self.assertEqual(len(self.sent("cancel")), 1)


# 4 --------------------------------------------------------------------------
class FillReportOrderingTests(Base):
    def test_duplicate_and_out_of_order_reports_never_double_count(self):
        self.tick()                                                              # buy 14 TQQQ
        self.broker.status_script[1] = [("open", "3"), ("open", "3"), ("open", "2"), ("open", "5")]
        self.assertEqual(self.tick(at=10), "waiting: order fill")
        self.assertEqual(self.tick(at=20), "waiting: order fill")                # duplicate report
        self.assertEqual(self.tick(at=30), "waiting: re-checking inconsistent fill report")
        order = lambda: next(r for r in self.journal.rows() if r["kind"] == "order")
        self.assertEqual(order()["filled"], "3")                                 # never lowered
        self.assertEqual(self.tick(at=40), "waiting: order fill")
        self.assertEqual(order()["filled"], "5")
        self.broker.fill(1)                                                      # missed reports: poll recovers
        self.assertEqual(self.tick(at=50), "confirmed: filled")
        self.assertEqual(self.broker.positions["TQQQ"], D(14))
        self.assertEqual(order()["filled"], "14")
        self.assertEqual(len(self.sent("buy")), 1)


# 5, 6 -----------------------------------------------------------------------
class ProceedsAndCarryoverTests(Base):
    def setUp(self):
        super().setUp()
        self.broker.usd = D(0)

    def test_proceeds_lag_carries_over_then_next_window_buys_once(self):
        self.broker.positions = {"QQQ": D(10)}
        self.tick()
        self.broker.fill(1)
        self.assertEqual(self.tick(at=10), "confirmed: filled")
        self.broker.power = D(0)                                                 # proceeds not yet orderable
        self.assertEqual(self.tick(at=20), "waiting: sale proceeds not yet orderable")
        self.assertEqual(self.tick(at=30), "waiting: sale proceeds not yet orderable")
        self.assertEqual(self.sent("buy"), [])
        after = dict(may_trade=False, window_over=True)
        self.assertEqual(self.tick(at=900, **after), "carried over: sale proceeds not yet orderable")
        self.assertEqual(self.tick(at=960, **after), "idle: outside execution window")   # recorded once
        self.assertEqual(self.journal.meta("execution_stats")[-1]["carried_over"], "sale proceeds not yet orderable")
        self.broker.power = None
        self.restart()
        self.assertEqual(self.tick(at=86400, session="2026-10-16"), "submitted: order")
        self.assertEqual([c[:2] for c in self.broker.calls], [("sell", "QQQ"), ("buy", "TQQQ")])

    def test_target_change_during_carryover_avoids_round_trip(self):
        self.broker.positions = {"TQQQ": D(10)}
        qqq = dict(SIGNAL, target="QQQ")
        self.engine.tick(**qqq, now=NOW, may_trade=True)                         # sell 10 TQQQ
        self.broker.fill(1, 4)
        self.assertEqual(self.engine.tick(**qqq, now=NOW + timedelta(seconds=121), may_trade=True), "submitted: cancel")
        self.engine.tick(**qqq, now=NOW + timedelta(seconds=125), may_trade=True)
        self.assertEqual(self.engine.tick(**qqq, now=NOW + timedelta(seconds=130), may_trade=True), "confirmed: cancelled")
        self.assertEqual(self.engine.tick(**qqq, now=NOW + timedelta(seconds=900), window_over=True),
                         "carried over: switch sell incomplete")
        # Next session the signal is back to TQQQ: keep the 6 TQQQ, buy more with the cash.
        result = self.tick(at=86400, session="2026-10-16")
        self.assertEqual(result, "submitted: order")
        self.assertEqual([c[:2] for c in self.broker.calls], [("sell", "TQQQ"), ("cancel", 1), ("buy", "TQQQ")])

    def test_open_bot_order_for_old_target_is_cancelled_before_timeout(self):
        self.broker.usd = D(1000)
        self.tick()                                                              # buy TQQQ, unfilled
        qqq = dict(SIGNAL, target="QQQ")
        self.assertEqual(self.engine.tick(**qqq, now=NOW + timedelta(seconds=20), may_trade=True), "submitted: cancel")
        cancel = next(r for r in self.journal.rows() if r["kind"] == "cancel")
        self.assertEqual(json.loads(cancel["payload"])["reason"], "target changed")


# 7 --------------------------------------------------------------------------
class ConcurrencyTests(Base):
    def test_deposit_during_switch_never_double_spends(self):
        self.broker.usd = D(0)
        self.broker.positions = {"QQQ": D(1)}
        self.tick()                                                              # sell 1 QQQ (700)
        self.broker.krw = D(1400000)                                             # deposit lands meanwhile
        self.assertEqual(self.tick(at=5, may_exchange=True), "waiting: order fill")
        self.assertEqual(self.sent("fx"), [])                                    # no FX while the sell is live
        self.broker.fill(1)
        self.assertEqual(self.tick(at=10, may_exchange=True), "confirmed: filled")
        self.assertEqual(self.tick(at=15, may_exchange=True, may_trade=False), "submitted: fx")
        self.tick(at=20, may_exchange=True, may_trade=False)                     # FX reflected
        usd_before = self.broker.usd
        self.assertEqual(self.tick(at=25), "submitted: order")
        order = self.broker.orders[3]
        self.assertLessEqual(order["qty"] * order["price"] * (1 + self.engine.cfg.fee_buffer), usd_before)
        self.assertEqual([c[0] for c in self.broker.calls], ["sell", "fx", "buy"])

    def test_database_admits_one_request_in_flight(self):
        other = Journal(self.path)                                               # a second process
        try:
            other.create("order", {"symbol": "TQQQ"}, NOW, "2026-10-15", status=PREPARED, run_id="other")
            with self.assertRaisesRegex(Stop, "Another process may be about to send"):
                self.tick(at=1)
            with self.assertRaisesRegex(Stop, "already in flight"):
                self.journal.create("order", {"symbol": "QQQ"}, NOW, "2026-10-15", status=PREPARED)
        finally:
            other.db.close()
        self.assertEqual(self.broker.calls, [])

    def test_send_start_refuses_an_intent_changed_by_another_writer(self):
        def hijack():
            self.journal.db.execute("UPDATE actions SET status='not_sent', inflight=NULL")
            self.journal.db.commit()
        original = self.broker._sending
        def sending():
            hijack()
            original()
        self.broker._sending = sending
        with self.assertRaisesRegex(NotSent, "another writer"):
            self.tick()
        self.assertEqual(self.broker.orders, {})


class CliConcurrencyTests(Harness):
    def config(self):
        root = self.state / "cli"
        root.mkdir(exist_ok=True)
        (root / "config.json").write_text(json.dumps({"state_dir": "state_observe", "notifications": False,
                                                       "account_lock_dir": str(self.state / "locks"),
                                                       "execution": {"mode": "observe"}}))
        secrets = root / "secrets.env"
        secrets.write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(secrets, 0o600)
        return root

    def run_cli(self, root, now=US_OPEN):
        fake = FakeMeritz(now)
        out = io.StringIO()
        with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)), \
             patch.object(cli, "datetime", wraps=datetime) as clock, \
             patch("sys.argv", ["autotrader", "run", "--config", str(root / "config.json")]), redirect_stdout(out):
            clock.now.return_value = now
            code = cli.main()
        return code, fake

    def test_overlapping_scheduler_or_second_state_folder_does_nothing(self):
        import fcntl
        root = self.config()
        with patch.dict(os.environ, SECRET_ENV):
            lock_file = cli.account_lock_path({"account_lock_dir": str(self.state / "locks")})
        with open(lock_file, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)        # another process owns the account
            code, fake = self.run_cli(root)
        self.assertEqual((code, fake.calls), (0, []))
        state = root / "state_observe"
        with open(state / "run.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)        # same state folder busy
            code, fake = self.run_cli(root)
        self.assertEqual((code, fake.calls), (0, []))
        code, fake = self.run_cli(root)
        self.assertEqual(code, 0)
        self.assertTrue(fake.calls)


# 8 --------------------------------------------------------------------------
class FxRecoveryTests(Base):
    def setUp(self):
        super().setUp()
        self.broker.usd = D(0)
        self.broker.krw = D(1000000)

    def test_lost_fx_response_with_reflection_lag_never_exchanges_twice(self):
        self.broker.lose.add("fx")
        self.broker.usd_shown, self.broker.krw_shown = D(0), D(1000000)          # balances lag
        with self.assertRaises(UnknownOutcome):
            self.tick(may_exchange=True, may_trade=False)
        self.broker.lose.clear()
        self.restart()
        self.assertEqual(self.tick(at=5, may_exchange=True, may_trade=False), "recovered: fx")
        for at in (10, 20):
            self.assertEqual(self.tick(at=at, may_exchange=True, may_trade=False), "waiting: FX balance reflection")
        self.broker.usd_shown = self.broker.krw_shown = None
        self.assertEqual(self.tick(at=30, may_exchange=True, may_trade=False), "idle: outside execution window")
        self.assertEqual(self.tick(at=40, may_exchange=True, may_trade=False), "idle: outside execution window")
        self.assertEqual(len(self.sent("fx")), 1)
        self.assertEqual(self.statuses("fx"), [DONE])

    def test_explicit_fx_refusal_confirmed_by_history_then_retried_once(self):
        self.broker.refuse = RefusedUnconfirmed("Mutation returned non-success API code 7999", "7999")
        with self.assertRaises(UnknownOutcome):
            self.tick(may_exchange=True, may_trade=False)
        self.broker.refuse = None
        with self.assertRaises(UnknownOutcome):
            self.tick(at=60, may_exchange=True, may_trade=False)                 # history may lag: wait
        self.assertEqual(self.tick(at=301, may_exchange=True, may_trade=False), "resolved: fx not executed")
        self.assertEqual(self.statuses("fx"), [REFUSED])
        self.assertTrue(self.tick(at=302, may_exchange=True, may_trade=False).startswith("waiting: retry back-off"))
        self.assertEqual(self.tick(at=400, may_exchange=True, may_trade=False), "submitted: fx")
        self.assertEqual(len(self.sent("fx")), 1)


# 9 --------------------------------------------------------------------------
class LiveFake(FakeMeritz):
    """Live-mode transport: accepts the order POST but loses its response once,
    can expire the token for the next read, and serves order history."""

    def __init__(self, now, today_date):
        super().__init__(now)
        self.today_date, self.ledger, self.posts, self.expire_next_read = today_date, [], [], False
        self.refuse_token = False
        self.responses["/trading/v1/overseas/orders/today"] = lambda q: dict(rsp_cd="5766" if self.ledger else "5820",
                                                                             data=[dict(r) for r in self.ledger])
        self.responses["/trading/v1/overseas/orders/history"] = lambda q: dict(
            rsp_cd="5766" if self.ledger else "5820", tr_cont="0",
            data=[dict(r, oder_dqty=r["odqt"], cntg_dqty=r["cntg_qty"]) for r in self.ledger])

    def __call__(self, method, path, query=None, body=None, *, token=None, form=False):
        if (method, path) == ("POST", "/oauth2/token"):
            self.calls.append((method, path))
            return {"rsp_cd": "EGW00103"} if self.refuse_token else dict(access_token=TOKEN, expires_in="86400")
        if method == "POST":
            self.calls.append((method, path))
            if APIS:
                fields = {f["element"] for f in APIS[(method, path)]["trs"][0]["request"]["body"]}
                assert set(body) <= fields, f"Undocumented body fields for {path}"
            self.posts.append(path)
            self.ledger.append(dict(oder_date=self.today_date, oder_no=str(len(self.ledger) + 1),
                                    oder_rctf_cncl_cls_code="0", ovrs_stck_symb_iscd=body["ovrs_stck_symb_iscd"],
                                    iscd=body["ovrs_stck_symb_iscd"] + ".OQ", seln_byov_clcd="2", odqt=str(body["odqt"]),
                                    cntg_qty="0", ovrs_stck_oder_prc=body["ovrs_stck_oder_prc"],
                                    ovrs_stck_oder_stat_cls_code="0", cncl_cnfr_qty="0", orgl_oder_no="0",
                                    oder_exec_ymdh=self.now.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")))
            raise UnknownOutcome("No reliable response to mutating request")   # accepted, reply lost
        if self.expire_next_read:
            self.expire_next_read = False
            self.calls.append((method, path))
            return {"rsp_cd": "EGW00121"}
        return super().__call__(method, path, query, body, token=token, form=form)


class AuthAndTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.env = patch.dict(os.environ, SECRET_ENV)
        self.env.start()
        self.net = patch("autotrader.meritz.urlopen", side_effect=AssertionError("network forbidden"))
        self.net.start()

    def tearDown(self):
        self.net.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_lost_order_reply_then_token_expiry_recovers_without_resend(self):
        today = datetime.now(KST).strftime("%Y%m%d")       # adapter's order_status uses the real KST date
        fake = LiveFake(US_OPEN, today)
        journal = Journal(self.state / "journal.sqlite")
        try:
            with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)):
                client = Client(self.state, allow_mutations=True)
                client.sleep = lambda s: None
                engine = Engine(Meritz(client, journal), journal,
                                Settings(mode="live", reservation_lookup_start="20260101"))
                args = dict(SIGNAL, now=US_OPEN, may_trade=True)
                with self.assertRaises(UnknownOutcome):
                    engine.tick(**args)
                self.assertEqual(journal.rows()[0]["status"], UNKNOWN)
                fake.expire_next_read = True                     # token expires before the next read
                engine = Engine(Meritz(Client(self.state, allow_mutations=True), journal), journal,
                                Settings(mode="live", reservation_lookup_start="20260101"))
                self.assertEqual(engine.tick(**args), "recovered: order")
                self.assertEqual(engine.tick(**args), "waiting: order fill")
            self.assertEqual(fake.posts, ["/trading/v1/overseas/orders/buy"])          # never resent
            self.assertEqual(fake.calls.count(("POST", "/oauth2/token")), 2)          # re-issued once
            self.assertEqual(journal.rows()[0]["status"], ACCEPTED)
        finally:
            journal.db.close()

    def test_token_refusal_is_explicit_and_post_token_error_is_not_resent(self):
        client = Client(self.state, allow_mutations=True)
        with patch.object(client, "_http", return_value={"rsp_cd": "EGW00103"}):
            with self.assertRaises(AuthError) as ctx:
                client.token()
        self.assertTrue(ctx.exception.explicit)
        client.token_file.write_text(json.dumps({"token": "t", "expires": 9e12, "key_fingerprint": client.key_fingerprint}))
        with patch.object(client, "_http", return_value={"rsp_cd": "EGW00121"}) as http:
            with self.assertRaises(Refused):
                client.call("POST", "/trading/v1/overseas/orders/buy", body={})
        self.assertEqual(http.call_count, 1)
        self.assertFalse(client.token_file.exists())        # next run issues a fresh token


class CliAuthLockTests(Harness):
    def test_repeated_token_refusal_locks_and_unlock_resumes(self):
        root = self.state / "cli"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"state_dir": "state_observe", "notifications": False,
                                                       "account_lock_dir": str(self.state / "locks"),
                                                       "execution": {"mode": "observe"}}))
        (root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(root / "secrets.env", 0o600)

        def run(refuse, argv=("run",)):
            fake = LiveFake(KRW_MORNING, "20261015")
            fake.refuse_token = refuse
            out = io.StringIO()
            with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)), \
                 patch.object(cli, "datetime", wraps=datetime) as clock, \
                 patch("sys.argv", ["autotrader", *argv, "--config", str(root / "config.json")]), redirect_stdout(out):
                clock.now.return_value = KRW_MORNING
                code = cli.main()
            return code, json.loads(out.getvalue() or "{}"), fake

        for attempt in range(3):
            code, out, fake = run(refuse=True)
            self.assertEqual(code, 1)
            self.assertTrue(all(m == "GET" or p == "/oauth2/token" for m, p in fake.calls))
        journal = Journal(root / "state_observe/journal.sqlite")
        self.assertEqual(journal.meta("lock_info")["code"], "auth_refused")
        journal.db.close()
        code, out, _ = run(refuse=False)
        self.assertEqual(code, 1)                                # hard lock survives a working token
        self.assertIn("Token issue refused", out["error"])
        code, out, _ = run(refuse=False, argv=("unlock", "--note", "rotated app key in portal"))
        self.assertEqual((code, out["previous_lock"]), (0, "auth_refused"))
        code, out, _ = run(refuse=False)
        self.assertEqual((code, out["result"]), (0, "observe: fx"))


# 10 -------------------------------------------------------------------------
class RetryAndLockTests(Base):
    def test_refusals_back_off_then_lock_and_survive_restart(self):
        self.broker.refuse = Refused("Order request not accepted by broker (code 3173)", "3173")
        with self.assertRaises(Refused):
            self.tick()
        self.assertTrue(self.tick(at=1).startswith("waiting: retry back-off"))  # no immediate resend
        at = 0
        for _ in range(3):
            at += 200
            with self.assertRaises(Refused):
                self.tick(at=at)
        self.restart()                                                           # counter is persisted
        self.assertEqual(self.journal.meta("failures")["count"], 4)
        with self.assertRaises(Locked):
            self.tick(at=at + 200)
        self.assertEqual(self.statuses(), [REFUSED] * 5)
        self.assertEqual(self.broker.orders, {})                                 # refused: nothing live
        self.broker.refuse = None
        self.restart()
        with self.assertRaises(Locked):
            self.tick(at=at + 400)
        self.assertEqual(self.journal.meta("lock_info")["code"], "repeated_failures")

    def test_acceptance_resets_the_failure_count(self):
        self.broker.refuse = Refused("not accepted", "3173")
        with self.assertRaises(Refused):
            self.tick()
        self.broker.refuse = None
        self.assertEqual(self.tick(at=200), "submitted: order")
        self.assertEqual(self.journal.meta("failures"), {"count": 0})

    def test_temporary_hold_clears_by_itself(self):
        self.broker.reservations = [{"symbol": "QQQ", "side": "buy", "quantity": "1"}]
        with self.assertRaisesRegex(Stop, "active reservation"):
            self.tick()
        self.broker.reservations = []
        self.assertEqual(self.tick(at=60), "submitted: order")


# 11 -------------------------------------------------------------------------
class GateOrderingTests(Base):
    def test_untracked_order_blocks_new_requests_but_not_own_status_checks(self):
        self.tick()
        self.broker.external = True
        self.assertEqual(self.tick(at=10), "waiting: order fill")
        self.broker.fill(1)
        self.assertEqual(self.tick(at=20), "confirmed: filled")
        with self.assertRaisesRegex(Stop, "untracked open order"):
            self.tick(at=30)
        self.assertEqual(len(self.sent("buy")), 1)

    def test_lock_still_reconciles_own_order_but_sends_nothing(self):
        self.tick()
        set_lock(self.journal, "test", "locked for test", NOW)
        self.broker.fill(1)
        self.assertEqual(self.tick(at=200), "confirmed: filled")               # read-only settling continues
        with self.assertRaises(Locked):
            self.tick(at=210)
        self.assertEqual(len(self.broker.calls), 1)

    def test_lock_prevents_own_order_cancel(self):
        self.tick()
        set_lock(self.journal, "test", "locked for test", NOW)
        self.assertEqual(self.tick(at=300), "waiting: market permits cancellation")
        self.assertEqual(self.sent("cancel"), [])


# 12 -------------------------------------------------------------------------
class StateFailureTests(Base):
    def test_send_start_write_failure_sends_nothing(self):
        with patch.object(self.journal, "transition", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(NotSent):
                self.tick()
        self.assertEqual(self.broker.orders, {})
        self.assertEqual(self.statuses(), [NOT_SENT])

    def test_stale_quote_and_unconfirmed_market_send_nothing(self):
        self.broker.quote = lambda symbol, now: Quote(D(70), D("70.01"), D("0.01"), now - timedelta(minutes=5))
        with self.assertRaisesRegex(Stop, "stale"):
            self.tick()
        with self.assertRaisesRegex(Stop, "US regular session not confirmed"):
            self.tick(at=5, may_trade=False, market_block="US regular session not confirmed: test")
        self.assertEqual(self.broker.calls, [])


class CliStateAndNotificationTests(Harness):
    def setup_cli(self, notifications=True):
        root = self.state / "cli"
        root.mkdir(exist_ok=True)
        (root / "config.json").write_text(json.dumps({"state_dir": "state_observe", "notifications": notifications,
                                                       "account_lock_dir": str(self.state / "locks"),
                                                       "execution": {"mode": "observe"}}))
        (root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(root / "secrets.env", 0o600)
        return root

    def run_cli(self, root, now, channels, argv=("run",), **responses):
        fake = FakeMeritz(now, **responses)
        out = io.StringIO()
        with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)), \
             patch.object(cli, "datetime", wraps=datetime) as clock, \
             patch("autotrader.notify.default_channels", return_value=channels), \
             patch("autotrader.__main__.default_channels", return_value=channels), \
             patch("sys.argv", ["autotrader", *argv, "--config", str(root / "config.json")]), redirect_stdout(out):
            clock.now.return_value = now
            code = cli.main()
        return code, json.loads(out.getvalue() or "{}"), fake

    def test_corrupt_state_database_blocks_every_request(self):
        root = self.setup_cli()
        state = root / "state_observe"
        state.mkdir(mode=0o700)
        (state / "journal.sqlite").write_bytes(b"this is not a sqlite database" * 100)
        sent = []
        code, out, fake = self.run_cli(root, US_OPEN, {"fake": sent.append})
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [])
        self.assertTrue((state / cli.JOURNAL_UNUSABLE).exists())
        self.assertEqual(len(sent), 1)
        code, out, fake = self.run_cli(root, US_OPEN, {"fake": sent.append})   # still refused, not re-notified
        self.assertEqual((code, fake.calls, len(sent)), (1, [], 1))
        self.assertEqual((state / "journal.sqlite").read_bytes()[:7], b"this is")   # never reset automatically

    def test_hold_then_automatic_recovery_is_notified_once_each(self):
        root = self.setup_cli()
        sent = []
        reserved = dict(rsp_cd="5766", tr_cont="0", data=[dict(
            ovrs_stck_symb_iscd="QQQ", iscd="QQQ", seln_byov_clcd="2", odqt="1", rsev_cncl_yn="",
            rsev_acpt_date="20261015", rsev_acpt_srno="9")])
        for _ in range(2):
            code, out, _ = self.run_cli(root, US_OPEN, {"fake": sent.append},
                                        **{"/trading/v1/overseas/reserved-orders": reserved})
            self.assertEqual(code, 1)
        code, out, _ = self.run_cli(root, US_OPEN + timedelta(seconds=30), {"fake": sent.append})
        self.assertEqual(code, 0)
        titles = [t.split("]")[0] + "]" for t in sent]
        self.assertEqual(titles.count("[보류]"), 1)                              # repeat suppressed
        self.assertEqual(titles.count("[복구]"), 1)
        self.assertTrue(all("access-token" not in t and "client-secret" not in t for t in sent))

    def test_notification_failure_changes_nothing(self):
        root = self.setup_cli()
        def broken(text):
            raise OSError("telegram unreachable")
        code, out, _ = self.run_cli(root, KRW_MORNING, {"broken": broken})
        self.assertEqual((code, out["result"]), (0, "observe: fx"))
        journal = Journal(root / "state_observe/journal.sqlite")
        try:
            events = [json.loads(r[0]) for r in journal.db.execute("SELECT body FROM events WHERE category='notification'")]
            self.assertTrue(events and events[0]["failed"][0]["channel"] == "broken")
            self.assertIsNone(journal.meta("halt"))
        finally:
            journal.db.close()

    def test_unlock_refuses_while_a_request_is_unresolved(self):
        root = self.setup_cli(notifications=False)
        self.run_cli(root, KRW_MORNING, {})
        journal = Journal(root / "state_observe/journal.sqlite")
        journal.create("order", {"symbol": "TQQQ"}, KRW_MORNING, "x", status=UNKNOWN)
        set_lock(journal, "unknown_outcome", "unknown for test", KRW_MORNING)
        journal.db.close()
        code, out, _ = self.run_cli(root, KRW_MORNING, {}, argv=("unlock", "--note", "checked"))
        self.assertEqual(code, 1)
        self.assertIn("Unresolved requests remain", out["error"])


# 13 -------------------------------------------------------------------------
class SteadyStateTests(Base):
    def test_target_reached_then_reruns_and_restarts_send_nothing(self):
        self.broker.krw = D(0)
        self.tick()
        self.broker.fill(1)
        self.tick(at=10)
        calls = list(self.broker.calls)
        for at in (20, 30, 3600):
            self.assertEqual(self.tick(at=at, may_exchange=True), "idle: insufficient USD for one share")
        self.restart()
        self.assertEqual(self.tick(at=86400, session="2026-10-16", may_exchange=True),
                         "idle: insufficient USD for one share")
        self.assertEqual(self.broker.calls, calls)
        self.assertEqual(self.journal.meta("position_state")["aligned"], True)


# transport and notifications ------------------------------------------------
class TransportClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MERITZ_CLIENT_ID": "dummy", "MERITZ_CLIENT_SECRET": "dummy"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def client(self, **kw):
        c = Client(self.tmp.name, **kw)
        c.token = lambda: "t"
        self.slept = []
        c.sleep = self.slept.append      # no real waiting
        c.rng.seed(1)
        return c

    def test_reads_retry_with_growing_jittered_backoff(self):
        c = self.client()
        with patch.object(c, "_http", side_effect=[Stop("Broker connection failed: URLError"),
                                                   {"rsp_cd": "EGW00200"}, {"rsp_cd": "5766", "data": []}]) as http:
            self.assertEqual(c.call("GET", "/x")["rsp_cd"], "5766")
        self.assertEqual(http.call_count, 3)
        self.assertEqual(len(self.slept), 2)
        self.assertTrue(0.5 <= self.slept[0] <= 0.625 and 1.0 <= self.slept[1] <= 1.25)
        with patch.object(c, "_http", side_effect=Stop("Broker connection failed: URLError")) as http:
            with self.assertRaises(Stop):
                c.call("GET", "/x")
        self.assertEqual(http.call_count, 3)

    def test_mutations_are_never_retried_and_are_classified(self):
        c = self.client(allow_mutations=True)
        with patch.object(c, "_http", return_value={"rsp_cd": "7777"}) as http:
            with self.assertRaises(Refused):
                c.call("POST", "/trading/v1/overseas/orders/buy", body={}, success=("0001",))
            with self.assertRaises(RefusedUnconfirmed):
                c.call("POST", "/forex/v1/exchanges", body={}, success=("0000",))
        self.assertEqual(http.call_count, 2)
        self.assertEqual(self.slept, [])

    def test_only_refused_connections_count_as_not_sent(self):
        c = Client(self.tmp.name, allow_mutations=True)
        c.token = lambda: "t"
        with patch("autotrader.meritz.urlopen", side_effect=URLError(ConnectionRefusedError())):
            with self.assertRaises(NotSent):
                c.call("POST", "/trading/v1/overseas/orders/buy", body={})
        with patch("autotrader.meritz.urlopen", side_effect=URLError(TimeoutError())):
            with self.assertRaises(UnknownOutcome):
                c.call("POST", "/trading/v1/overseas/orders/buy", body={})

    def test_send_start_hook_runs_after_token_and_before_transport(self):
        c = Client(self.tmp.name, allow_mutations=True)
        order = []
        c.token = lambda: order.append("token") or "t"
        c.on_send = lambda: order.append("send-start")
        with patch.object(c, "_http", side_effect=lambda *a, **k: order.append("http") or {"rsp_cd": "0001", "data": {}}):
            c.call("POST", "/trading/v1/overseas/orders/buy", body={}, success=("0001",))
        self.assertEqual(order, ["token", "send-start", "http"])


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")

    def tearDown(self):
        self.journal.db.close()
        self.tmp.cleanup()

    def test_repeat_suppression_survives_restart(self):
        sent = []
        Notifier(self.journal, channels={"t": sent.append}).send("hold", "x", "reservation present", NOW)
        Notifier(self.journal, channels={"t": sent.append}).send("hold", "x", "reservation present", NOW + timedelta(hours=1))
        Notifier(self.journal, channels={"t": sent.append}).send("hold", "x", "reservation present", NOW + timedelta(hours=7))
        self.assertEqual(len(sent), 2)

    def test_failed_delivery_is_recorded_and_retried_later_not_immediately(self):
        calls = []
        def broken(text):
            calls.append(text)
            raise OSError("down")
        n = Notifier(self.journal, channels={"broken": broken})
        self.assertTrue(n.send("lock", "x", "locked", NOW))
        self.assertFalse(n.send("lock", "x", "locked", NOW + timedelta(minutes=5)))
        self.assertTrue(n.send("lock", "x", "locked", NOW + timedelta(minutes=31)))
        self.assertEqual(len(calls), 2)

    def test_text_never_carries_secrets_numbers_or_amounts(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:SECRET-TOKEN", "MERITZ_CLIENT_SECRET": "very-secret"}):
            sent = []
            Notifier(self.journal, channels={"t": sent.append}).send(
                "hold", "y", "balance 1,500,000 KRW price 70.05 account 12345678 very-secret", NOW)
        self.assertNotIn("very-secret", sent[0])
        for figure in ("1,500,000", "70.05", "12345678"):
            self.assertNotIn(figure, sent[0])
        self.assertEqual(scrub("session 2026-10-15"), "session 2026-10-15")

    def test_telegram_puts_token_only_in_the_request_url(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b'{"ok": true}'

        def opener(request, timeout):
            captured.update(url=request.full_url, data=request.data.decode(), timeout=timeout)
            return Response()
        telegram_channel("123456:SECRET", "42", opener=opener)("[보류] test")
        self.assertIn("123456:SECRET", captured["url"])
        self.assertNotIn("SECRET", captured["data"])
        self.assertEqual(captured["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
