"""Audit fixes 1-2: kill at real commit boundaries, restart, check the books.

Each scenario runs in a child process that dies with os._exit(0) right after
(or right before) a chosen database write. The parent reopens the database
and keeps running. Invariants: derived bookkeeping (switch proceeds, switch
spend, tax reserve, in-progress reservations, reverse FX) is applied exactly
once; no false "switch complete"; unknown requests are never resent; money
not actually secured is never counted as being secured.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrader.core import D, DONE, NOT_SENT, Engine, Journal, Settings
from autotrader.replay import PersistentPaper
from autotrader.tax_reserve import TaxPlan
from autotrader.signal import DEFENSIVE_SYMBOL
from crash_helpers import run_child

OPEN = datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc)
AFTER = OPEN + timedelta(minutes=16)
CUTOFF = datetime(2026, 10, 15, 19, 50, tzinfo=timezone.utc)
JAN = datetime(2027, 1, 20, 14, 31, tzinfo=timezone.utc)


def recover_step(now, target=DEFENSIVE_SYMBOL):
    return dict(now=now.isoformat(), target=target, signal_date="2026-10-14", expected_signal_date="2026-10-14",
                session="2026-10-15", may_trade=False, may_manage_orders=True, may_recover=True,
                recovery_deadline=CUTOFF.isoformat())


def window_step(now, target="TQQQ", **extra):
    step = dict(now=now.isoformat(), target=target, signal_date="2027-01-19", expected_signal_date="2027-01-19",
                session="2027-01-20", may_trade=True)
    step.update(extra)
    return step


class Restart:
    """Reopen journal, persisted paper broker and engine as a restart would."""

    def __init__(self, db, tax=None, cap=None, reverse_fx=False):
        self.journal = Journal(db)
        self.broker = PersistentPaper(self.journal)
        self.broker.prices.update({DEFENSIVE_SYMBOL: D(100), "TQQQ": D(70)})
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper"))
        self.engine.reverse_fx_verified = reverse_fx
        self.plan = None
        if tax:
            self.plan = TaxPlan(self.journal.db, {}, lambda y: {"estimated_tax_krw": D(tax), "complete": True},
                                lambda: D(1400))
            self.engine.tax_plan = self.plan

    def close(self):
        self.journal.db.close()


class SwitchCrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "journal.sqlite")

    def child(self, steps, **spec):
        result = run_child(dict(db=self.db, initial={"usd": "0", "positions": {"TQQQ": 10}},
                                prices={DEFENSIVE_SYMBOL: "100", "TQQQ": "70"}, steps=steps, **spec))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_kill_after_sell_done_commit_still_buys_the_target(self):
        self.child([recover_step(AFTER), {"fill": True}, recover_step(AFTER + timedelta(seconds=30))],
                   kill="Journal.update", kill_when="done")
        for attempt in range(3):                                   # several restarts in a row
            r = Restart(self.db)
            try:
                result = r.engine.tick(**{k: (datetime.fromisoformat(v) if k in ("now", "recovery_deadline") else v)
                                          for k, v in recover_step(AFTER + timedelta(minutes=30 + attempt)).items()})
                intent = r.journal.meta("switch_intent")
                self.assertNotEqual(result, "idle: switch complete")
                self.assertGreater(D(intent["proceeds"]), 0)        # sale proceeds counted once
                self.assertEqual(len(intent["counted"]), len(set(intent["counted"])))
            finally:
                r.close()
        r = Restart(self.db)
        try:
            buys = [o for o in r.broker.orders.values() if o["side"] == "buy"]
            self.assertEqual([o["symbol"] for o in buys], [DEFENSIVE_SYMBOL])  # exactly one target buy
        finally:
            r.close()

    def test_kill_after_buy_done_commit_counts_the_spend_once(self):
        steps = [recover_step(AFTER), {"fill": True}, recover_step(AFTER + timedelta(seconds=30)),
                 recover_step(AFTER + timedelta(seconds=60)), {"fill": True}, recover_step(AFTER + timedelta(seconds=90))]
        self.child(steps, kill="Journal.update", kill_when="done", kill_skip=1)
        r = Restart(self.db)
        try:
            for i in range(3):
                r.engine.tick(**{k: (datetime.fromisoformat(v) if k in ("now", "recovery_deadline") else v)
                                 for k, v in recover_step(AFTER + timedelta(minutes=40 + i)).items()})
            buys = [o for o in r.broker.orders.values() if o["side"] == "buy"]
            self.assertEqual(len(buys), 1)
            intent = r.journal.meta("switch_history")[-1]
            self.assertGreater(D(intent["spent"]), 0)
        finally:
            r.close()


class AtomicityTests(unittest.TestCase):
    """The kill lands inside the completion transaction: on disk there must be
    either the old state or the complete new state, never DONE without books."""

    def test_no_done_order_without_its_switch_entry_on_disk(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "journal.sqlite")
        result = run_child(dict(db=db, initial={"usd": "0", "positions": {"TQQQ": 10}},
                                prices={DEFENSIVE_SYMBOL: "100", "TQQQ": "70"},
                                steps=[recover_step(AFTER), {"fill": True}, recover_step(AFTER + timedelta(seconds=30))],
                                kill="Journal.update", kill_when="done"))
        self.assertEqual(result.returncode, 0, result.stderr)
        journal = Journal(db)                                        # no tick: inspect the disk as left
        try:
            intent = journal.meta("switch_intent")
            for row in journal.rows():
                if row["status"] == DONE:
                    self.assertIn(row["id"], intent["counted"])
            self.assertTrue(any(r["status"] == "accepted" for r in journal.rows()))   # rolled back as a whole
        finally:
            journal.db.close()


class LegacyDatabaseTests(unittest.TestCase):
    """Databases written by the older version may already hold DONE rows whose
    books were never applied, a falsely completed intent, or orphan
    reservations. They are repaired from journal evidence, once."""

    def test_falsely_completed_intent_is_reopened_and_finished_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "journal.sqlite")
        r = Restart(db)
        try:
            r.broker.positions = {"TQQQ": D(10)}
            r.broker._save()
            step = lambda at: {k: (datetime.fromisoformat(v) if k in ("now", "recovery_deadline") else v)
                               for k, v in recover_step(at).items()}
            r.engine.tick(**step(AFTER))
            r.broker.fill(1)
            # Simulate the old bug: DONE committed, books never applied, then the
            # old code completed the intent with zero proceeds.
            order = r.journal.rows()[0]
            r.journal.update(order["id"], DONE, filled="10", result={"filled": "10", "state": "filled"})
            intent = r.journal.meta("switch_intent")
            intent.update(status="done", completed_by="intraday recovery")
            r.journal.put_meta("switch_intent", intent)
            for i in range(3):
                r.engine.tick(**step(AFTER + timedelta(minutes=5 + i)))
            buys = [o for o in r.broker.orders.values() if o["side"] == "buy"]
            self.assertEqual([o["symbol"] for o in buys], [DEFENSIVE_SYMBOL])
            events = [json.loads(e[0]) for e in r.journal.db.execute(
                "SELECT body FROM events WHERE category IN ('switch_reopened','effects_replayed')")]
            self.assertEqual(sum(1 for e in events if e.get("what") == "switch"), 1)   # replayed once
            self.assertEqual(r.journal.meta("switch_intent")["counted"].count(order["id"]), 1)
        finally:
            r.close()

    def test_done_tax_sell_with_open_reservation_is_finished_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "journal.sqlite")
        r = Restart(db, tax="1000000")
        try:
            action = r.journal.create("order", {"symbol": "TQQQ", "side": "sell", "qty": "3", "price": "69.93",
                                                "tax_year": 2026, "held_before": "100"}, JAN, "2027-01-20",
                                      status=DONE, filled="3")
            r.plan.start(2026, action, "sell", D(280000), D("209.79"), JAN)
            r.journal.db.commit()
            for i in range(3):
                r.engine.tick(**{k: (datetime.fromisoformat(v) if k == "now" else v)
                                 for k, v in window_step(JAN + timedelta(minutes=1 + i), may_trade=False).items()})
            st = r.plan.status(2026, JAN)
            self.assertEqual(st["in_progress_krw"], 0)
            self.assertEqual(st["reserve_usd"], (D(3) * D("69.93") * (1 - D("0.0015"))).quantize(D("0.01"), rounding="ROUND_DOWN"))
        finally:
            r.close()


class TaxCrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "journal.sqlite")

    def child(self, steps, **spec):
        result = run_child(dict(db=self.db, initial={"usd": "0", "positions": {"TQQQ": 100}},
                                prices={"TQQQ": "70"}, steps=steps, tax="1000000", cap="1000000", **spec))
        self.assertEqual(result.returncode, 0, result.stderr)

    def status(self, r):
        return r.plan.status(2026, JAN)

    def test_kill_after_tax_sell_done_moves_proceeds_exactly_once(self):
        self.child([window_step(JAN), {"fill": True}, window_step(JAN + timedelta(seconds=30))],
                   kill="Journal.update", kill_when="done")
        reserves = []
        for i in range(3):
            r = Restart(self.db, tax="1000000")
            try:
                r.engine.tick(**{k: (datetime.fromisoformat(v) if k == "now" else v)
                                 for k, v in window_step(JAN + timedelta(minutes=1 + i)).items()})
                st = self.status(r)
                self.assertEqual(st["in_progress_krw"], 0)
                reserves.append(st["reserve_usd"])
                self.assertEqual(len([o for o in r.broker.orders.values() if o["side"] == "sell"]), 1)
            finally:
                r.close()
        self.assertGreater(reserves[0], 0)
        self.assertEqual(len(set(reserves)), 1)                    # never applied twice

    def test_kill_after_reservation_before_send_releases_it(self):
        # Intent and its in-progress reservation are committed; the process dies
        # before the send-start record. Nothing was sent.
        self.child([window_step(JAN)], kill="Journal.transition", kill_before=True)
        r = Restart(self.db, tax="1000000")
        try:
            r.engine.tick(**{k: (datetime.fromisoformat(v) if k == "now" else v)
                             for k, v in window_step(JAN + timedelta(minutes=6)).items()})
            statuses = [row["status"] for row in r.journal.rows()]
            self.assertEqual(statuses[0], NOT_SENT)
            reasons = [m[0] for m in r.journal.db.execute("SELECT to_state FROM tax_moves WHERE from_state='in_progress'")]
            self.assertEqual(reasons, ["released: send never started"])   # released with the NOT_SENT itself
            st = self.status(r)
            self.assertEqual(st["reserve_usd"], 0)
            # Only the re-planned, actually sent sell may be in progress now.
            live = [row for row in r.journal.rows() if row["status"] not in (NOT_SENT,)]
            progress = r.plan.in_progress(2026)["rows"]
            self.assertEqual({p[0] for p in progress}, {row["id"] for row in live})
        finally:
            r.close()

    def test_kill_between_intent_and_reservation_leaves_nothing_counted(self):
        self.child([window_step(JAN)], kill="Plan.start", kill_before=True)
        r = Restart(self.db, tax="1000000")
        try:
            self.assertEqual(r.plan.in_progress(2026)["rows"], [])
            self.assertEqual([row["status"] for row in r.journal.rows()], [])   # intent and reservation together
        finally:
            r.close()

    def test_orphan_reservation_on_a_terminal_request_is_released(self):
        r = Restart(self.db, tax="1000000")
        try:
            action = r.journal.create("order", {"symbol": "TQQQ", "side": "sell", "qty": "1", "price": "70",
                                                "tax_year": 2026}, JAN, "2027-01-20", status=NOT_SENT)
            r.plan.start(2026, action, "sell", D(95000), D(70), JAN)
            r.plan.start(2026, "no-such-action", "sell", D(50000), D(35), JAN)
            r.journal.db.commit()
            self.assertGreater(self.status(r)["in_progress_krw"], 0)
            r.engine.tick(**{k: (datetime.fromisoformat(v) if k == "now" else v)
                             for k, v in window_step(JAN + timedelta(minutes=1), may_trade=False).items()})
            self.assertEqual(self.status(r)["in_progress_krw"], 0)
            moves = r.journal.db.execute("SELECT ref FROM tax_moves WHERE from_state='in_progress'").fetchall()
            self.assertEqual({m[0] for m in moves}, {action, "no-such-action"})
        finally:
            r.close()

    def test_unknown_request_keeps_its_reservation_and_is_not_resent(self):
        r = Restart(self.db, tax="1000000")
        try:
            action = r.journal.create("order", {"symbol": "TQQQ", "side": "sell", "qty": "3", "price": "69.93",
                                                "tax_year": 2026, "held_before": "100"}, JAN, "2027-01-20",
                                      status="unknown")
            r.plan.start(2026, action, "sell", D(280000), D(209.79), JAN)
            r.journal.db.commit()
            for i in range(2):
                try:
                    r.engine.tick(**{k: (datetime.fromisoformat(v) if k == "now" else v)
                                     for k, v in window_step(JAN + timedelta(minutes=10 + i)).items()})
                except Exception:
                    pass
            self.assertEqual(len(r.plan.in_progress(2026)["rows"]), 1)
            self.assertEqual(r.broker.orders, {})
        finally:
            r.close()


class ReverseFxCrashTests(unittest.TestCase):
    def test_kill_after_fx_out_done_moves_the_reserve_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "journal.sqlite")
        fx = dict(now=JAN.isoformat(), target="TQQQ", signal_date="2027-01-19", expected_signal_date="2027-01-19",
                  session="KRW:2027-01-21", may_trade=False, may_exchange=True)
        result = run_child(dict(db=db, initial={"usd": "500", "krw": "0"}, steps=[
            dict(fx), dict(fx, now=(JAN + timedelta(seconds=10)).isoformat()),
            dict(fx, now=(JAN + timedelta(seconds=20)).isoformat())],
            tax="1000000", reverse_fx=True, kill="Journal.update", kill_when="done"))
        self.assertEqual(result.returncode, 0, result.stderr)
        for i in range(3):
            r = Restart(db, tax="1000000", reverse_fx=True)
            try:
                r.engine.tick(**{k: (datetime.fromisoformat(v) if k == "now" else v)
                                 for k, v in dict(fx, now=(JAN + timedelta(minutes=1 + i)).isoformat()).items()})
                st = r.plan.status(2026, JAN)
                self.assertEqual(st["in_progress_krw"], 0)
                self.assertEqual(st["reserve_usd"], 0)
                self.assertGreater(st["reserve_krw"], 0)
                self.assertEqual(len([c for c in r.broker.calls if c[0] == "fx_out"]), 1)
            finally:
                r.close()


if __name__ == "__main__":
    unittest.main()
