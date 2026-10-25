"""Live check: one-share TQQQ/SGOV round trips with a named USD budget while
every other holding stays untouched. Fake broker / fake HTTP, temporary DBs,
fixed clocks, child processes killed with os._exit; no real request is sent.
The protected tickers here ("NDQX", "BRK.B") are placeholders, not the user's."""
from autotrader import oslock
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader import __main__ as cli
from autotrader import livecheck
from autotrader.core import (ACCEPTED, DONE, NOT_SENT, D, Engine, Journal, Locked, Settings, Stop,
                             UnknownOutcome)
from autotrader.signal import DEFENSIVE_SYMBOL, RISK_SYMBOL
from test_fault_injection import FaultBroker

ROOT = Path(__file__).resolve().parent.parent
OPEN = datetime(2026, 10, 15, 13, 31, tzinfo=timezone.utc)            # minute 1 of the open window
DEADLINE = OPEN + timedelta(minutes=14)
SESSION = "2026-10-15"
PROTECTED = {"NDQX": D(7), "BRK.B": D(3)}
EXPECTED = [("buy", RISK_SYMBOL, D(1)), ("sell", RISK_SYMBOL, D(1)), ("buy", DEFENSIVE_SYMBOL, D(1)),
            ("sell", DEFENSIVE_SYMBOL, D(1)), ("buy", RISK_SYMBOL, D(1)), ("sell", RISK_SYMBOL, D(1))]


class Base(unittest.TestCase):
    budget = "200"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "journal.sqlite"
        self.marker = Path(self.tmp.name) / "account.livecheck"
        self.broker = FaultBroker(usd="1000")
        self.broker.positions = dict(PROTECTED)
        self.settings = Settings(mode="observe", reservation_lookup_start="20200101")
        self.journal = None
        self.open()
        self.journal.put_meta("signal", {"target": RISK_SYMBOL, "signal_date": "2026-10-14"})

    def open(self):
        if self.journal is not None:
            self.journal.db.close()
        self.journal = Journal(self.path)
        self.addCleanup(lambda: self.journal.db.close())
        self.engine = Engine(self.broker, self.journal, self.settings)

    def plan(self, budget=None, tax=D(0)):
        return livecheck.create_plan(self.journal, self.broker, self.settings, budget=budget or self.budget,
                                     now=OPEN, tax_reserved_usd=tax)

    def started(self, budget=None):
        plan, reasons = self.plan(budget)
        self.assertEqual(reasons, [])
        livecheck.start_checked(self.journal, self.broker, plan["plan_id"], OPEN, marker=self.marker)
        self.pid = plan["plan_id"]
        return plan

    def tick(self, at=0, may_trade=True, unwind=False, tax=D(0)):
        return livecheck.tick(self.engine, self.journal, self.pid, OPEN + timedelta(seconds=at), may_trade=may_trade,
                              may_manage=True, deadline=DEADLINE, session=SESSION, tax_reserved_usd=tax,
                              marker=self.marker, unwind=unwind)

    def run_all(self, start=0, limit=40):
        at, result = start, None
        for _ in range(limit):
            result = self.tick(at)
            for number, order in list(self.broker.orders.items()):
                if order["state"] == "open":
                    self.broker.fill(number)
            at += 5
            if result == "complete":
                return result
        return result

    def trades(self):
        return [c for c in self.broker.calls if c[0] in ("buy", "sell")]

    def status(self):
        return livecheck._row(self.journal, self.pid)["status"]


class PlanTests(Base):
    def test_plan_is_read_only_and_binds_account_baseline_budget_and_steps(self):
        plan, reasons = self.plan()
        self.assertEqual(reasons, [])
        self.assertEqual(self.broker.calls, [])                                # no financial request
        self.assertEqual(plan["baseline"], [{"symbol": "BRK.B", "qty": "3"}, {"symbol": "NDQX", "qty": "7"}])
        self.assertEqual([(s["side"], s["symbol"], s["qty"]) for s in plan["steps"]],
                         [(a, b, "1") for a, b, _ in EXPECTED])
        self.assertEqual((plan["budget_usd"], plan["account"]), ("200", "paper"))
        self.assertTrue(plan["unverified"])
        self.assertIsNone(livecheck.blocking(self.journal))                     # a plan alone pauses nothing

    def test_refusals_store_nothing_and_send_nothing(self):
        for budget in (None, "0", "-1", "1.234", "abc"):
            plan, reasons = livecheck.create_plan(self.journal, self.broker, self.settings, budget=budget, now=OPEN,
                                                  tax_reserved_usd=D(0))
            self.assertIsNone(plan, budget)
        self.assertIn("one share", " ".join(self.plan("50")[1]))
        self.assertIn("exceeds own USD", " ".join(self.plan("1500")[1]))
        self.assertIn("exceeds own USD", " ".join(self.plan("900", tax=D(200))[1]))   # tax reserve excluded
        self.broker.positions[RISK_SYMBOL] = D(2)
        self.assertIn("already held", " ".join(self.plan()[1]))
        del self.broker.positions[RISK_SYMBOL]
        self.broker.reservations = [{"symbol": "NDQX", "side": "sell", "quantity": "1"}]
        self.assertIn("reservation", " ".join(self.plan()[1]))
        self.broker.reservations = []
        self.journal.put_meta("switch_intent", {"status": "buying", "to": DEFENSIVE_SYMBOL})
        self.assertIn("switch", " ".join(self.plan()[1]))
        self.journal.put_meta("switch_intent", None)
        self.journal.create("order", {"symbol": "TQQQ"}, OPEN, SESSION)          # unresolved request
        self.assertIn("unresolved", " ".join(self.plan()[1]))
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM live_checks").fetchone()[0], 0)
        self.assertEqual(self.broker.calls, [])

    def test_a_new_plan_supersedes_an_unstarted_one_and_only_one_can_run(self):
        first, _ = self.plan()
        second, _ = self.plan()
        self.assertEqual(livecheck._row(self.journal, first["plan_id"])["status"], "superseded")
        with self.assertRaisesRegex(Stop, "superseded"):
            livecheck.start(self.journal, first["plan_id"], OPEN)
        livecheck.start(self.journal, second["plan_id"], OPEN)
        self.assertIn("another live check", " ".join(self.plan()[1]))


class AccountBindingTests(Base):
    def test_plan_of_another_account_or_with_changed_holdings_never_starts(self):
        plan, _ = self.plan()
        self.journal.put_meta("broker_key_fingerprint", "f" * 64)                # different API key now
        with self.assertRaisesRegex(Stop, "different account"):
            livecheck.start(self.journal, plan["plan_id"], OPEN)
        plan, _ = self.plan()
        self.broker.positions["NDQX"] = D(5)
        with self.assertRaisesRegex(Stop, "Protected holdings differ"):
            livecheck.start_checked(self.journal, self.broker, plan["plan_id"], OPEN)
        self.assertEqual(livecheck._row(self.journal, plan["plan_id"])["status"], "planned")
        self.assertEqual(self.broker.calls, [])


class RoundTripTests(Base):
    def test_tax_reserves_and_switch_state_are_untouched_by_check_trades(self):
        from autotrader.tax_reserve import TaxPlan
        plan = TaxPlan(self.journal.db, {}, lambda y: {"estimated_tax_krw": D(1_000_000), "complete": True},
                       lambda: D(1400))
        plan._move(2026, OPEN, "free_cash", "reserve_usd", D(50), "USD", "test")
        self.journal.db.commit()
        before = list(self.journal.db.execute("SELECT * FROM tax_moves"))
        self.assertIn("exceeds own USD", " ".join(self.plan("990", tax=D(50))[1]))   # reserve stays out
        self.started()
        self.run_all()
        self.assertEqual(list(self.journal.db.execute("SELECT * FROM tax_moves")), before)
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM tax_inprogress").fetchone()[0], 0)

    def test_six_one_share_steps_end_in_usd_and_never_touch_other_holdings(self):
        self.started()
        self.assertEqual(self.run_all(), "complete")
        self.assertEqual(self.trades(), EXPECTED)
        self.assertEqual({s: q for s, q in self.broker.positions.items() if q}, PROTECTED)
        self.assertEqual(self.status(), "completed")
        report = livecheck.report(self.journal, self.pid)
        self.assertEqual(report["test_shares_remaining"], {RISK_SYMBOL: "0", DEFENSIVE_SYMBOL: "0"})
        self.assertFalse(report["general_run_paused"])
        self.assertEqual(self.journal.meta("signal"), {"target": RISK_SYMBOL, "signal_date": "2026-10-14"})
        self.assertIsNone(self.journal.meta("switch_intent"))
        payloads = [json.loads(r["payload"]) for r in self.journal.rows()]
        self.assertTrue(all(p["owner"] == livecheck.tag(self.pid) for p in payloads))
        self.assertFalse(self.marker.exists())

    def test_a_finished_plan_never_trades_again(self):
        self.started()
        self.run_all()
        calls = list(self.broker.calls)
        with self.assertRaises(Stop):
            livecheck.start(self.journal, self.pid, OPEN)
        with self.assertRaises(Stop):
            self.tick(500)
        with self.assertRaises(Stop):
            livecheck.Authorization(self.journal, self.pid).permit("order", {
                "owner": livecheck.tag(self.pid), "side": "buy", "symbol": RISK_SYMBOL, "qty": "1",
                "price": "70", "step": 1})
        self.assertEqual(self.broker.calls, calls)

    def test_each_step_waits_for_a_terminal_fill_reflected_in_holdings(self):
        self.started()
        self.assertEqual(self.tick(0), "submitted: order")
        self.assertEqual(self.tick(5), "waiting: order fill")                  # accepted is not filled
        self.broker.fill(max(self.broker.orders))
        original = self.broker.snapshot

        def lagging():
            s = original()
            s.positions = {k: v for k, v in s.positions.items() if k != RISK_SYMBOL}
            return s
        self.broker.snapshot = lagging
        self.assertTrue(self.tick(10).startswith("waiting: holdings"))           # not reflected: no next step
        self.assertEqual(len(self.trades()), 1)
        self.broker.snapshot = original
        self.assertEqual(self.tick(20), "confirmed: filled")                   # settled once reflected
        self.assertEqual(self.tick(25), "submitted: order")
        self.assertEqual(self.trades()[-1], ("sell", RISK_SYMBOL, D(1)))


class BudgetTests(Base):
    def test_budget_never_grows_with_account_cash_deposits_or_price_moves(self):
        self.started("101")
        self.broker.usd += D(5000)                                              # deposit / distribution
        for _ in range(2):
            self.tick(len(self.broker.calls) * 10)
            for number, order in list(self.broker.orders.items()):
                if order["state"] == "open":
                    self.broker.fill(number)
            self.tick(len(self.broker.calls) * 10 + 5)
        with self.assertRaisesRegex(Stop, "budget cannot cover one share"):
            self.tick(200)                                                      # SGOV 1 share > what is left
        self.assertEqual([c[1] for c in self.trades()], [RISK_SYMBOL, RISK_SYMBOL])
        led = livecheck.ledger(self.journal, livecheck._row(self.journal, self.pid)["plan"])
        self.assertLess(led["available"], D(101))

    def test_open_buy_is_reserved_and_counted_once(self):
        plan = self.started()
        self.tick(0)
        led = livecheck.ledger(self.journal, plan)
        order = self.broker.orders[max(self.broker.orders)]
        self.assertEqual(led["reserved"], order["price"] * (1 + self.settings.fee_buffer))
        self.assertEqual(led["available"], D(self.budget) - led["reserved"])
        self.broker.fill(max(self.broker.orders))
        for at in (5, 10):
            self.tick(at)
        led = livecheck.ledger(self.journal, plan)
        self.assertEqual((led["reserved"], led["spent"]), (D(0) if led["open_steps"] == [2] else D(0),
                                                           order["price"] * (1 + self.settings.fee_buffer)))
        self.assertEqual(led["done_steps"], [1])

    def test_authorization_refuses_anything_outside_the_plan(self):
        plan = self.started()
        auth = livecheck.Authorization(self.journal, self.pid)
        owner = livecheck.tag(self.pid)
        base = {"owner": owner, "side": "buy", "symbol": RISK_SYMBOL, "qty": "1", "price": "70", "step": 1}
        auth.permit("order", base)
        for bad in ({"symbol": "NDQX"}, {"symbol": "BRK.B", "side": "sell"}, {"qty": "2"}, {"step": 2},
                    {"side": "sell"}, {"owner": "live_check:other"}, {"price": "400"}):
            with self.assertRaises(Stop, msg=bad):
                auth.permit("order", dict(base, **bad))
        for kind in ("fx", "fx_out"):
            with self.assertRaises(Stop):
                auth.permit(kind, {"owner": owner})
        with self.assertRaises(Stop):
            auth.permit("cancel", {"owner": owner, "original": {"number": 99, "date": "20261015"}})

    def test_a_request_outside_the_plan_leaves_no_intent_row_and_reaches_no_broker(self):
        self.started()
        self.engine.owner = livecheck.tag(self.pid)
        self.engine.live_check = livecheck.Authorization(self.journal, self.pid)
        quote = self.broker.quote(RISK_SYMBOL, OPEN)
        for symbol, side in (("NDQX", "sell"), (RISK_SYMBOL, "sell")):
            with self.assertRaises(Stop):
                self.engine._send_order(symbol=symbol, side=side, qty=D(1), quote=quote, price=D("69.90"),
                                        snap=self.broker.snapshot(), target=None, signal_date=None, session=SESSION,
                                        now=OPEN, deadline=DEADLINE, extra={"owner": livecheck.tag(self.pid), "step": 1})
        self.assertEqual((self.journal.rows(), self.broker.calls), ([], []))

    def test_stop_arriving_between_intent_and_send_means_nothing_is_sent(self):
        self.started()
        original = self.broker._sending

        def sending():
            livecheck.request_stop(self.journal, self.pid, OPEN)              # another process: `live-check stop`
            original()
        self.broker._sending = sending
        self.assertTrue(self.tick(0).startswith("idle: outside the live-check plan at send time"))
        self.assertEqual(self.broker.orders, {})
        self.assertEqual([r["status"] for r in self.journal.rows()], [NOT_SENT])

    def test_sale_proceeds_reuse_is_confirmed_only_when_existing_cash_was_not_enough(self):
        self.broker.usd = D("160")
        self.started("150")
        self.assertEqual(self.run_all(), "complete")
        evidence = livecheck.report(self.journal, self.pid)["proceeds_reuse"]
        self.assertTrue(evidence["3"]["result"].startswith("confirmed"))

    def test_reuse_with_plenty_of_existing_cash_is_inconclusive(self):
        self.started("150")                                                     # account holds 1000 USD
        self.run_all()
        evidence = livecheck.report(self.journal, self.pid)["proceeds_reuse"]
        self.assertTrue(all(e["result"].startswith("inconclusive") for e in evidence.values()))

    def test_proceeds_not_yet_orderable_waits_instead_of_using_other_money(self):
        self.broker.usd = D("160")
        self.started("150")
        for at in (0, 5, 10, 15):
            self.tick(at)
            for number, order in list(self.broker.orders.items()):
                if order["state"] == "open":
                    self.broker.fill(number)
        self.broker.power = D("80")                                             # broker has not released proceeds
        self.assertEqual(self.tick(20), "waiting: sale proceeds not yet orderable")
        self.assertEqual(len(self.trades()), 2)
        self.broker.power = None
        self.assertEqual(self.tick(25), "submitted: order")


class GateTests(Base):
    def setUp(self):
        super().setUp()
        self.started()

    def test_outside_window_reservations_and_bad_quotes_send_nothing(self):
        self.assertTrue(self.tick(0, may_trade=False).startswith("idle:"))
        self.broker.reservations = [{"symbol": "NDQX", "side": "buy", "quantity": "1"}]
        with self.assertRaisesRegex(Stop, "reservation"):
            self.tick(5)
        self.broker.reservations = RuntimeError("lookup down")
        with self.assertRaisesRegex(Stop, "reservation lookup failed"):
            self.tick(10)
        self.broker.reservations = []
        original = self.broker.quote
        self.broker.quote = lambda symbol, now: (lambda q: setattr(q, "at", now - timedelta(minutes=5)) or q)(
            original(symbol, now))
        with self.assertRaisesRegex(Stop, "stale"):
            self.tick(15)
        self.broker.quote = lambda symbol, now: (lambda q: setattr(q, "bid", q.ask * D("0.9")) or q)(original(symbol, now))
        with self.assertRaisesRegex(Stop, "spread"):
            self.tick(20)
        self.broker.quote = lambda symbol, now: (_ for _ in ()).throw(Stop("Security/market trading restriction"))
        with self.assertRaisesRegex(Stop, "restriction"):
            self.tick(25)
        self.assertEqual(self.trades(), [])

    def test_sell_only_blocks_the_buy_but_not_the_check_sell(self):
        original = self.broker.quote
        sell_only = set()

        def quote(symbol, now):
            q = original(symbol, now)
            if symbol in sell_only:
                q.blocked = (("buy", "sell-only at the broker"),)
            return q
        self.broker.quote = quote
        self.tick(0)
        self.broker.fill(max(self.broker.orders))
        self.tick(5)
        sell_only.update(livecheck.SYMBOLS)
        self.assertEqual(self.tick(10), "submitted: order")                    # step 2 sell allowed
        self.broker.fill(max(self.broker.orders))
        self.tick(15)
        with self.assertRaisesRegex(Stop, "buy blocked: sell-only"):
            self.tick(20)
        self.assertEqual([c[0] for c in self.trades()], ["buy", "sell"])

    def test_window_closing_during_the_send_wait_sends_nothing(self):
        late = [False]
        self.engine.clock = lambda: OPEN + (timedelta(minutes=16) if late[0] else timedelta(seconds=1))
        original = self.broker._sending

        def sending():
            late[0] = True                                                      # token issue / pacing took long
            original()
        self.broker._sending = sending
        self.assertTrue(self.tick(0).startswith("idle:"))
        self.assertEqual(self.broker.orders, {})
        self.assertEqual([r["status"] for r in self.journal.rows()], [NOT_SENT])


class RecoveryTests(Base):
    def setUp(self):
        super().setUp()
        self.started()

    def test_lost_reply_is_matched_once_and_never_resent(self):
        self.broker.lose.add("order")
        with self.assertRaises(UnknownOutcome):
            self.tick(0)
        self.broker.lose.clear()
        self.open()
        self.assertEqual(self.tick(5), "recovered: order")
        self.broker.fill(max(self.broker.orders))
        self.tick(10)
        self.assertEqual(len(self.trades()), 1)
        self.assertEqual(livecheck.ledger(self.journal, livecheck._row(self.journal, self.pid)["plan"])["done_steps"], [1])

    def test_timeout_cancel_then_late_fill_counts_the_real_fill_once(self):
        self.tick(0)
        self.broker.deferred_cancel = True
        self.assertEqual(self.tick(130), "submitted: cancel")                  # timed out
        cancel = [json.loads(r["payload"]) for r in self.journal.rows() if r["kind"] == "cancel"][0]
        self.assertEqual(cancel["owner"], livecheck.tag(self.pid))
        number = max(n for n, o in self.broker.orders.items())
        self.broker.fill(number)                                                # filled before the cancel landed
        for at in (135, 140, 145):
            self.tick(at)
        led = livecheck.ledger(self.journal, livecheck._row(self.journal, self.pid)["plan"])
        self.assertEqual((led["done_steps"], led["owned"][RISK_SYMBOL]), ([1], D(1)))
        self.assertEqual([c[0] for c in self.trades()], ["buy", "sell"][:len(self.trades())])

    def test_cancelled_without_fill_retries_the_same_step_with_a_fresh_quote(self):
        self.tick(0)
        self.assertEqual(self.tick(130), "submitted: cancel")
        self.assertEqual([self.tick(135), self.tick(140)], ["confirmed: cancellation", "confirmed: cancelled"])
        self.assertEqual(self.tick(145), "submitted: order")
        self.assertEqual(self.trades(), [EXPECTED[0], EXPECTED[0]])
        led = livecheck.ledger(self.journal, livecheck._row(self.journal, self.pid)["plan"])
        self.assertEqual(led["next_step"], 1)

    def test_protected_holdings_change_holds_new_requests(self):
        self.broker.positions["NDQX"] = D(6)
        with self.assertRaisesRegex(Stop, "Protected holdings differ"):
            self.tick(0)
        self.broker.positions["NDQX"] = D(14)
        changes = livecheck.baseline_changes(livecheck._row(self.journal, self.pid)["plan"], self.broker.snapshot())
        self.assertIn("possible split", changes[0]["note"])
        self.assertEqual(self.trades(), [])

    def test_stop_cancels_own_open_order_and_never_sells_without_unwind(self):
        self.tick(0)
        livecheck.request_stop(self.journal, self.pid, OPEN)
        self.assertEqual(self.tick(5), "submitted: cancel")
        self.assertEqual([self.tick(10), self.tick(12)], ["confirmed: cancellation", "confirmed: cancelled"])
        self.assertEqual(self.tick(15), "stopped: no test shares remain")
        self.assertEqual(self.status(), "stopped")
        self.assertFalse(self.marker.exists())

    def test_stop_with_a_test_share_left_keeps_the_pause_until_an_explicit_unwind(self):
        self.tick(0)
        self.broker.fill(max(self.broker.orders))
        self.tick(5)
        livecheck.request_stop(self.journal, self.pid, OPEN)
        self.assertTrue(self.tick(10).startswith("stopping: test shares remain"))
        self.assertIsNotNone(livecheck.blocking(self.journal))
        self.assertEqual(self.tick(15, unwind=True), "submitted: order")
        self.assertEqual(self.trades()[-1], ("sell", RISK_SYMBOL, D(1)))
        self.broker.fill(max(self.broker.orders))
        self.tick(20, unwind=True)
        self.assertEqual(self.tick(25, unwind=True), "stopped: no test shares remain")
        self.assertEqual([c[0] for c in self.trades()], ["buy", "sell"])


class IsolationTests(Base):
    def test_general_engine_ignores_owned_rows_and_owned_rows_feed_no_switch_or_tax_book(self):
        self.started()
        self.tick(0)
        general = Engine(self.broker, self.journal, self.settings)
        self.assertIsNone(general._reconcile(self.broker.snapshot(), OPEN + timedelta(minutes=10),
                                             target=DEFENSIVE_SYMBOL, session=SESSION))   # no conflict cancel
        self.assertFalse([c for c in self.broker.calls if c[0] == "cancel"])
        self.journal.put_meta("switch_intent", {"id": "x", "to": RISK_SYMBOL, "from": {}, "status": "buying",
                                                "created_at": (OPEN - timedelta(hours=1)).isoformat(),
                                                "proceeds": "0", "carried": "0", "spent": "0", "counted": [],
                                                "events": [], "recovery_orders": 0})
        self.broker.fill(max(self.broker.orders))
        self.tick(5)
        intent = self.journal.meta("switch_intent")
        self.assertEqual((intent["spent"], intent["counted"]), ("0", []))
        from autotrader.core import missing_switch_effects
        self.assertEqual(missing_switch_effects(self.journal), [])
        row = self.journal.rows()[0]
        general._switch_fill(row, json.loads(row["payload"]), D(1), "filled", OPEN)   # even if reached directly
        self.assertEqual(self.journal.meta("switch_intent")["counted"], [])

    def test_check_engine_refuses_while_a_general_request_is_unresolved(self):
        self.started()
        self.journal.create("order", {"symbol": RISK_SYMBOL, "side": "buy", "qty": "1", "price": "70"}, OPEN, SESSION)
        with self.assertRaisesRegex(Stop, "general engine is unresolved"):
            self.tick(0)
        self.assertEqual(self.trades(), [])


CHILD = r'''
import json, os, sys
sys.path[:0] = [{root!r}, {tests!r}]
from datetime import datetime, timedelta
from autotrader import livecheck
from autotrader.core import Engine, Journal, Settings, D
from autotrader.replay import PersistentPaper
spec = json.loads(sys.argv[1])
journal = Journal(spec["db"])
broker = PersistentPaper(journal)
engine = Engine(broker, journal, Settings(mode="paper", reservation_lookup_start="20200101"))
original = getattr(journal, spec["method"])
seen = [0]
def killer(*a, **k):
    result = original(*a, **k)
    if spec["when"] in json.dumps([str(x) for x in a] + [str(v) for v in k.values()]):
        seen[0] += 1
        if seen[0] > spec.get("skip", 0):
            os._exit(3)
    return result
setattr(journal, spec["method"], killer)
now = datetime.fromisoformat(spec["now"])
for i in range(spec["ticks"]):
    t = now + timedelta(seconds=5 * i)
    livecheck.tick(engine, journal, spec["plan"], t, may_trade=True, may_manage=True,
                   deadline=now + timedelta(minutes=14), session="2026-10-15", tax_reserved_usd=D(0))
    for number, order in list(broker.orders.items()):
        if order["state"] == "open":
            broker.fill(number)
'''


class CrashTests(unittest.TestCase):
    """Kills at real commit boundaries, then restarts with a new process state."""

    def prepare(self, tmp):
        from autotrader.replay import PersistentPaper
        db = str(Path(tmp) / "j.sqlite")
        j = Journal(db)
        broker = PersistentPaper(j, initial={"usd": "1000", "positions": {k: str(v) for k, v in PROTECTED.items()}})
        settings = Settings(mode="paper", reservation_lookup_start="20200101")
        plan, reasons = livecheck.create_plan(j, broker, settings, budget="200", now=OPEN, tax_reserved_usd=D(0))
        livecheck.start(j, plan["plan_id"], OPEN)
        j.db.close()
        return db, plan["plan_id"]

    def finish(self, db, pid):
        from autotrader.replay import PersistentPaper
        j = Journal(db)
        try:
            broker = PersistentPaper(j)
            engine = Engine(broker, j, Settings(mode="paper", reservation_lookup_start="20200101"))
            result = None
            for i in range(40):
                result = livecheck.tick(engine, j, pid, OPEN + timedelta(minutes=5, seconds=5 * i), may_trade=True,
                                        may_manage=True, deadline=DEADLINE, session=SESSION, tax_reserved_usd=D(0))
                for number, order in list(broker.orders.items()):
                    if order["state"] == "open":
                        broker.fill(number)
                if result == "complete":
                    break
            # Filled orders only: an order cancelled unfilled on timeout is retried
            # with a fresh quote, which is not a duplicate.
            filled = [(o["side"], o["symbol"], D(str(o["filled"]))) for _, o in sorted(broker.orders.items())
                      if D(str(o["filled"])) > 0]
            return result, filled, livecheck._row(j, pid)["status"]
        finally:
            j.db.close()

    def test_kill_right_after_the_send_start_record_stays_unknown_and_is_never_resent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, pid = self.prepare(tmp)
            result = subprocess.run([sys.executable, "-W", "ignore", "-c",
                                     CHILD.format(root=str(ROOT), tests=str(ROOT / "tests")),
                                     json.dumps({"db": db, "plan": pid, "method": "transition", "when": "submitting",
                                                 "skip": 0, "ticks": 1, "now": OPEN.isoformat()})],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 3, result.stderr[-2000:])
            with self.assertRaises(UnknownOutcome):
                self.finish(db, pid)                                            # zero matches proves nothing
            from autotrader.replay import PersistentPaper
            j = Journal(db)
            try:
                self.assertEqual(PersistentPaper(j).orders, {})
                self.assertEqual(livecheck._row(j, pid)["status"], "running")    # still pauses the general run
            finally:
                j.db.close()

    def test_kill_at_send_done_and_completion_boundaries_never_duplicates_or_loses(self):
        cases = [("update", "accepted", 0, 2), ("update", "done", 1, 6),
                 ("event", "live_check_completed", 0, 30)]
        for method, when, skip, ticks in cases:
            with self.subTest(kill=(method, when)), tempfile.TemporaryDirectory() as tmp:
                db, pid = self.prepare(tmp)
                result = subprocess.run([sys.executable, "-W", "ignore", "-c",
                                         CHILD.format(root=str(ROOT), tests=str(ROOT / "tests")),
                                         json.dumps({"db": db, "plan": pid, "method": method, "when": when,
                                                     "skip": skip, "ticks": ticks, "now": OPEN.isoformat()})],
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 3, result.stderr[-2000:])
                final, trades, status = self.finish(db, pid)
                self.assertEqual((final, status), ("complete", "completed"))
                self.assertEqual(trades, EXPECTED)                              # never duplicated or skipped


class CliTests(unittest.TestCase):
    """Real CLI with fake HTTP. Mutations are counted at the transport."""

    def setUp(self):
        from test_doctor import SECRET_ENV
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config.json").write_text(json.dumps({
            "state_dir": "state_observe", "notifications": False, "account_lock_dir": str(self.root / "locks"),
            "execution": {"mode": "observe", "reservation_lookup_start": "20200101",
                          "instrument_markets": {"SGOV": "AX"}}}))
        (self.root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(self.root / "secrets.env", 0o600)

    def cli(self, *argv, now=None, fake=None):
        from autotrader.meritz import Client
        from test_doctor import US_OPEN, holding
        from test_sgov import OrderFake
        now = now or US_OPEN
        fake = fake or OrderFake(now, now.strftime("%Y%m%d"))
        fake.responses.setdefault("/accounts/v1/me/overseas/holdings", None)
        fake.responses["/accounts/v1/me/overseas/holdings"] = dict(rsp_cd="5766", data=[holding("NDQX", 7),
                                                                                      holding("BRK.B", 3)])
        out = io.StringIO()
        with patch.object(Client, "_http", lambda _s, *a, **k: fake(*a, **k)), \
             patch("autotrader.meritz.urlopen", side_effect=AssertionError("network forbidden")), \
             patch.object(cli, "datetime", wraps=datetime) as clock, patch.object(cli, "live_check_sleep"), \
             patch.object(cli, "LIVE_CHECK_MAX_TICKS", 1), \
             patch("sys.argv", ["autotrader", "live-check", *argv, "--config", str(self.root / "config.json")]), \
             redirect_stdout(out):
            clock.now.return_value = now
            code = cli.main()
        text, objects, decoder = out.getvalue().strip(), [], json.JSONDecoder()
        while text:
            obj, end = decoder.raw_decode(text)
            objects.append(obj)
            text = text[end:].strip()
        return code, objects[-1], fake

    def posts(self, fake):
        return [b for b in fake.bodies]

    def test_plan_status_and_missing_options_send_no_financial_request(self):
        code, out, fake = self.cli("plan")
        self.assertEqual(code, 1)
        self.assertIn("--budget-usd", out["error"])
        code, out, fake = self.cli("plan", "--budget-usd", "<검증예산USD>")
        self.assertIsNone(out["plan"])
        code, out, fake = self.cli("plan", "--budget-usd", "200")
        self.assertEqual((code, self.posts(fake)), (0, []))
        pid = out["plan"]["plan_id"]
        self.assertEqual(out["plan"]["routes"]["SGOV"]["order_exchange"], "0066")
        for argv in (("execute", "--plan", pid), ("execute", "--confirm-live"), ("resume", "--plan", pid),
                     ("status",), ("status", "--plan", pid)):
            code, out, fake = self.cli(*argv)
            self.assertEqual(self.posts(fake), [], argv)
        report = json.loads((self.root / "state_observe/live_check" / f"{pid}.json").read_text())
        self.assertEqual(report["status"], "planned")
        if os.name != "nt":                      # Windows: ACLs, checked by oslock.acl_report
            self.assertEqual(oct((self.root / "state_observe/live_check" / f"{pid}.json").stat().st_mode & 0o777),
                             "0o600")

    def test_execute_sends_only_the_plan_step_and_pauses_the_general_run(self):
        code, out, _ = self.cli("plan", "--budget-usd", "200")
        pid = out["plan"]["plan_id"]
        code, out, fake = self.cli("execute", "--plan", pid, "--confirm-live")
        self.assertEqual([(p, b["ovrs_stck_symb_iscd"], b["odqt"], b["ovrs_exch_vndr_idno"]) for p, b in fake.bodies],
                         [("/trading/v1/overseas/orders/buy", "TQQQ", 1, "0537")])
        self.assertEqual(out["status"], "running")
        code, out, fake = self.cli("execute", "--plan", pid, "--confirm-live")   # already running
        self.assertEqual((code, self.posts(fake)), (1, []))
        from autotrader.meritz import Client
        from test_doctor import US_OPEN
        from test_sgov import OrderFake
        run_fake = OrderFake(US_OPEN, "x")
        buf = io.StringIO()
        with patch.object(Client, "_http", lambda _s, *a, **k: run_fake(*a, **k)), \
             patch.object(cli, "datetime", wraps=datetime) as clock, \
             patch("sys.argv", ["autotrader", "run", "--config", str(self.root / "config.json")]), redirect_stdout(buf):
            clock.now.return_value = US_OPEN
            self.assertEqual(cli.main(), 1)
        self.assertIn("Live check in progress", buf.getvalue())
        self.assertEqual(run_fake.bodies, [])

    def test_marker_pauses_a_run_from_another_state_folder(self):
        code, out, _ = self.cli("plan", "--budget-usd", "200")
        pid = out["plan"]["plan_id"]
        self.cli("execute", "--plan", pid, "--confirm-live")
        cfg = json.loads((self.root / "config.json").read_text())
        other = self.root / "other.json"
        other.write_text(json.dumps(dict(cfg, state_dir="state_elsewhere")))
        from autotrader.meritz import Client
        from test_doctor import US_OPEN
        from test_sgov import OrderFake
        run_fake = OrderFake(US_OPEN, "x")
        buf = io.StringIO()
        with patch.object(Client, "_http", lambda _s, *a, **k: run_fake(*a, **k)), \
             patch.object(cli, "datetime", wraps=datetime) as clock, \
             patch("sys.argv", ["autotrader", "run", "--config", str(other)]), redirect_stdout(buf):
            clock.now.return_value = US_OPEN
            cli.main()
        self.assertIn("account marker", buf.getvalue())
        self.assertEqual(run_fake.bodies, [])

    def test_a_held_account_lock_means_nothing_is_sent(self):
        code, out, _ = self.cli("plan", "--budget-usd", "200")
        pid = out["plan"]["plan_id"]
        cfg = json.loads((self.root / "config.json").read_text())
        with patch.dict(os.environ, {"MERITZ_CLIENT_ID": __import__("test_doctor").SECRET_ENV["MERITZ_CLIENT_ID"]}):
            lock = open(cli.account_lock_path(cfg), "a")
        oslock.acquire(lock)                      # another process holds the account
        try:
            code, out, fake = self.cli("execute", "--plan", pid, "--confirm-live")
        finally:
            lock.close()
        self.assertEqual((code, self.posts(fake)), (1, []))
        self.assertIn("Another process", out["error"])

    def test_transport_filter_refuses_everything_else(self):
        code, out, _ = self.cli("plan", "--budget-usd", "200")
        pid = out["plan"]["plan_id"]
        j = Journal(self.root / "state_observe/journal.sqlite")
        try:
            check = livecheck.transport_filter(j, pid, out["plan"]["routes"])
            check("/trading/v1/overseas/orders/buy", {"ovrs_stck_symb_iscd": "SGOV", "odqt": 1,
                                                      "ovrs_exch_vndr_idno": "0066"})
            for path, body in (("/forex/v1/exchanges", {}),
                               ("/trading/v1/overseas/orders/sell", {"ovrs_stck_symb_iscd": "NDQX", "odqt": 1}),
                               ("/trading/v1/overseas/orders/buy", {"ovrs_stck_symb_iscd": "TQQQ", "odqt": 2,
                                                                    "ovrs_exch_vndr_idno": "0537"}),
                               ("/trading/v1/overseas/orders/buy", {"ovrs_stck_symb_iscd": "SGOV", "odqt": 1,
                                                                    "ovrs_exch_vndr_idno": "0537"}),
                               ("/trading/v1/overseas/orders/cancel", {"oder_date": "20261015", "orgl_oder_no": "1"})):
                with self.assertRaises(Stop, msg=path):
                    check(path, body)
        finally:
            j.db.close()


class GeneralRunUnchangedTests(unittest.TestCase):
    def test_non_strategy_holdings_still_stop_the_general_engine_and_migrate_keeps_its_meaning(self):
        from autotrader.core import unsupported_holdings
        self.assertEqual(unsupported_holdings({"NDQX": D(1), RISK_SYMBOL: D(1)}, Settings()), ["NDQX"])
        with self.assertRaises(ValueError):
            Settings(migrate_symbols=("NDQX",))                                  # not an ignore list

    def test_schema_is_added_to_an_existing_journal_without_touching_other_tables(self):
        # Built here as older versions left it (records, no live_checks table);
        # never copied from a user's state folder.
        with tempfile.TemporaryDirectory() as tmp:
            j = Journal(Path(tmp) / "older.sqlite")
            try:
                j.create("order", {"symbol": RISK_SYMBOL}, OPEN, "2026-10-15", status="done")
                j.db.execute("DROP TABLE IF EXISTS live_checks")
                j.db.commit()
                before = j.db.execute("SELECT * FROM actions").fetchall()
                livecheck.ensure_schema(j.db)
                livecheck.ensure_schema(j.db)
                self.assertEqual(j.db.execute("SELECT * FROM actions").fetchall(), before)
                self.assertIsNone(livecheck.blocking(j))
            finally:
                j.db.close()

if __name__ == "__main__":
    unittest.main()
