"""Existing databases from the QQQ-defensive version (strategy v1): signal cache,
switch intents and open/unknown/DONE requests that name QQQ. The logical target
moves to SGOV; real QQQ orders, fills, receipts and tax records stay QQQ.
Temporary DBs, fake brokers, child processes killed with os._exit; no network."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrader import __main__ as cli
from autotrader import tax
from autotrader.core import ACCEPTED, DONE, UNKNOWN, D, Engine, Journal, Settings, Stop
from autotrader.replay import PersistentPaper
from autotrader.signal import DEFENSIVE_SYMBOL, RISK_SYMBOL, STRATEGY_VERSION, decisions
from test_fault_injection import FaultBroker

ROOT = Path(__file__).resolve().parent.parent
OPEN = datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc)
AFTER = OPEN + timedelta(minutes=16)
CUTOFF = datetime(2026, 10, 15, 19, 50, tzinfo=timezone.utc)
BASE = dict(signal_date="2026-10-14", expected_signal_date="2026-10-14", session="2026-10-15")
PROCEEDS = "698.25"


def legacy_intent(**extra):
    intent = {"id": "legacy-intent", "to": "QQQ", "from": {"TQQQ": "10"}, "signal_date": "2026-10-14",
              "created_session": "2026-10-15", "created_at": (OPEN - timedelta(minutes=5)).isoformat(),
              "status": "buying", "proceeds": PROCEEDS, "carried": "0", "spent": "0", "counted": ["sold-before"],
              "events": [], "recovery_orders": 0}
    intent.update(extra)
    return intent


def legacy_payload(symbol="QQQ", side="buy", qty="1", price="600.60"):
    # Written by the v1 engine: no strategy field, logical target "QQQ".
    return {"symbol": symbol, "side": side, "qty": qty, "price": price, "held_before": "0", "target": "QQQ",
            "signal_date": "2026-10-14", "quoted_at": OPEN.isoformat(), "switch": "legacy-intent"}


def write_history(path, closes, last="2026-10-14"):
    days = []
    day = datetime.fromisoformat(last).date()
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    path.write_text("date,close\n" + "".join(f"{d},{c}\n" for d, c in zip(reversed(days), closes)))


class SignalCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.journal = Journal(self.dir / "j.sqlite")
        self.addCleanup(lambda: self.journal.db.close())

    def old_cache(self, target="QQQ"):
        old = {"target": target, "signal_date": "2026-10-14", "rule": "DD15_12_252_v1", "drawdown": -0.13}
        self.journal.put_meta("signal", old)
        return old

    def test_same_day_cache_of_the_old_version_is_rebuilt_from_the_same_closes_once(self):
        path = self.dir / "qqq_close.csv"
        closes = ["100"] * 300 + ["80"] + ["86.5"]                          # -20% then back to -13.5%
        write_history(path, closes)
        old = self.old_cache()
        new = cli.migrate_signal(self.journal, path, old, OPEN)
        self.assertEqual(new["target"], DEFENSIVE_SYMBOL)                    # mid band keeps the defensive state
        self.assertEqual(new["target"], decisions(closes)[-1].target)
        self.assertEqual((new["strategy"], new["signal_date"]), (STRATEGY_VERSION, "2026-10-14"))
        self.assertEqual(self.journal.meta("signal"), new)
        cli.migrate_signal(self.journal, path, old, OPEN)                    # repeated: still one event
        events = [json.loads(b) for (b,) in self.journal.db.execute(
            "SELECT body FROM events WHERE category='strategy_changed'")]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["old_target"], events[0]["new_target"]), ("QQQ", DEFENSIVE_SYMBOL))

    def test_mid_band_is_never_reset_to_tqqq(self):
        path = self.dir / "qqq_close.csv"
        closes = ["100"] * 300 + ["84"] + ["87"] * 5                         # in the band after an exit
        write_history(path, closes)
        new = cli.migrate_signal(self.journal, path, self.old_cache(), OPEN)
        self.assertEqual(new["target"], DEFENSIVE_SYMBOL)
        # Inside the band: a state reset to TQQQ at this row would stay TQQQ.
        self.assertTrue(-0.15 < new["drawdown"] < -0.12)

    def test_short_or_stale_history_gives_no_target(self):
        path = self.dir / "qqq_close.csv"
        write_history(path, ["100"] * 100)
        self.assertIsNone(cli.migrate_signal(self.journal, path, self.old_cache(), OPEN))
        self.assertIsNone(self.journal.meta("signal"))
        write_history(path, ["100"] * 300, last="2026-10-09")
        self.journal.put_meta("signal", {"target": "QQQ", "signal_date": "2026-10-14", "rule": "DD15_12_252_v1"})
        self.assertIsNone(cli.migrate_signal(self.journal, path, self.journal.meta("signal"), OPEN))


MIGRATE_CHILD = r'''
import json, os, sys
sys.path.insert(0, {root!r})
from datetime import datetime
from autotrader import __main__ as cli
from autotrader.core import Journal
spec = json.loads(sys.argv[1])
journal = Journal(spec["db"])
original = getattr(journal, spec["method"])
def killer(*a, **k):
    result = original(*a, **k)
    if spec["when"] in json.dumps([str(x) for x in a]):
        os._exit(3)
    return result
setattr(journal, spec["method"], killer)
cli.migrate_signal(journal, spec["history"], journal.meta("signal"), datetime.fromisoformat(spec["now"]))
'''


class SignalMigrationCrashTests(unittest.TestCase):
    def test_kill_inside_the_migration_leaves_the_old_cache_and_a_restart_applies_it_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db, history = Path(tmp.name) / "j.sqlite", Path(tmp.name) / "qqq_close.csv"
        write_history(history, ["100"] * 300 + ["80"])
        for method, when in (("put_meta", "signal"), ("event", "strategy_changed"), ("put_meta", "strategy_version")):
            j = Journal(db)
            j.db.execute("DELETE FROM metadata")
            j.db.execute("DELETE FROM events")
            j.db.commit()
            j.put_meta("signal", {"target": "QQQ", "signal_date": "2026-10-14", "rule": "DD15_12_252_v1"})
            j.db.close()
            result = subprocess.run([sys.executable, "-W", "ignore", "-c", MIGRATE_CHILD.format(root=str(ROOT)),
                                     json.dumps({"db": str(db), "history": str(history), "method": method,
                                                 "when": when, "now": OPEN.isoformat()})],
                                    capture_output=True, text=True, timeout=60)
            with self.subTest(kill=(method, when)):
                self.assertEqual(result.returncode, 3, result.stderr)
                j = Journal(db)
                try:
                    self.assertEqual(j.meta("signal")["target"], "QQQ")             # nothing half-applied
                    self.assertIsNone(j.meta("strategy_version"))
                    cli.migrate_signal(j, history, j.meta("signal"), OPEN)
                    cli.migrate_signal(j, history, j.meta("signal"), OPEN)
                    self.assertEqual(j.meta("signal")["target"], DEFENSIVE_SYMBOL)
                    self.assertEqual(j.db.execute("SELECT count(*) FROM events WHERE category='strategy_changed'")
                                     .fetchone()[0], 1)
                finally:
                    j.db.close()


class LegacyRequestTests(unittest.TestCase):
    """Open, unknown and unbooked requests of the old version, settled with
    their original QQQ identity before the old intent is superseded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "j.sqlite"
        self.broker = FaultBroker(usd=PROCEEDS)
        self.broker.prices.update({"QQQ": D("600"), DEFENSIVE_SYMBOL: D("100.50")})
        self.journal = None
        self.open()
        self.journal.put_meta("switch_intent", legacy_intent())

    def open(self, **cfg):
        if self.journal is not None:
            self.journal.db.close()
        self.journal = Journal(self.path)
        self.addCleanup(lambda: self.journal.db.close())
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper", **cfg))

    def recover(self, at):
        return self.engine.tick(**dict(BASE, target=DEFENSIVE_SYMBOL, now=AFTER + timedelta(seconds=at),
                                       may_trade=False, may_manage_orders=True, may_recover=True,
                                       recovery_deadline=CUTOFF))

    def legacy_order(self, status, receipt=True, at=OPEN, **payload):
        p = legacy_payload(**payload)
        r = self.broker.order(p["symbol"], p["side"], D(p["qty"]), D(p["price"]), at)
        row = self.journal.create("order", p, at, "2026-10-15", status=status,
                                  **({"receipt": json.dumps(r)} if receipt else {}))
        return row, p, r

    def new_intent(self):
        intent = self.journal.meta("switch_intent")
        self.assertNotEqual(intent["id"], "legacy-intent")
        return intent

    def test_open_qqq_buy_is_cancelled_with_its_receipt_and_the_budget_carries_once(self):
        row, payload, receipt = self.legacy_order(ACCEPTED)
        tax.ensure_schema(self.journal.db)
        tax.record_fill(self.journal.db, {"fill_key": "20261014:7", "symbol": "QQQ", "side": "buy", "qty": D(1),
                                          "price": D(600), "amount": D(600), "filled_at": OPEN.isoformat(),
                                          "trade_date": "2026-10-14", "settle_date": None, "settle_source": None,
                                          "source": "broker_history"}, OPEN)
        self.journal.db.commit()
        self.assertEqual(self.recover(0), "submitted: cancel")
        cancel = [r for r in self.journal.rows() if r["kind"] == "cancel"][0]
        self.assertEqual(json.loads(cancel["payload"])["original"], receipt)
        self.assertEqual([self.recover(30), self.recover(60)], ["confirmed: cancellation", "confirmed: cancelled"])
        self.assertEqual(self.recover(90), "submitted: order")             # SGOV from the carried money
        intent = self.new_intent()
        self.assertEqual((intent["to"], D(intent["carried"])), (DEFENSIVE_SYMBOL, D(PROCEEDS)))
        buy = self.broker.orders[max(self.broker.orders)]
        self.assertEqual(buy["symbol"], DEFENSIVE_SYMBOL)
        self.assertLessEqual(buy["qty"] * buy["price"] * (1 + D("0.0015")), D(PROCEEDS))
        old = [r for r in self.journal.rows() if r["id"] == row][0]
        self.assertEqual((json.loads(old["payload"]), json.loads(old["receipt"])), (payload, receipt))
        self.assertEqual([tuple(r) for r in self.journal.db.execute("SELECT symbol FROM tax_fills")], [("QQQ",)])
        history = self.journal.meta("switch_history")
        self.assertEqual([(h["id"], h["to"], h["status"]) for h in history], [("legacy-intent", "QQQ", "superseded")])

    def test_open_tqqq_exit_sell_of_the_old_version_is_not_cancelled_as_a_conflict(self):
        self.journal.put_meta("switch_intent", legacy_intent(status="selling", proceeds="0", counted=[]))
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.legacy_order(ACCEPTED, at=AFTER, symbol=RISK_SYMBOL, side="sell", qty="10", price="69.93")
        self.assertEqual(self.recover(0), "waiting: order fill")
        self.assertFalse([c for c in self.broker.calls if c[0] == "cancel"])

    def test_unknown_qqq_buy_is_matched_by_its_original_identity_and_never_resent(self):
        row, payload, _ = self.legacy_order(UNKNOWN, receipt=False)
        self.assertEqual(self.recover(0), "recovered: order")
        self.assertEqual(self.journal.status(row), ACCEPTED)
        self.assertEqual(json.loads([r for r in self.journal.rows() if r["id"] == row][0]["payload"]), payload)
        self.assertEqual([c for c in self.broker.calls if c[0] == "buy"], [("buy", "QQQ", D(1))])   # no resend

    def test_done_qqq_buy_missing_from_the_old_intent_is_booked_there_before_the_carry(self):
        row, _, receipt = self.legacy_order(ACCEPTED)
        self.broker.fill(receipt["number"])
        self.journal.update(row, DONE, filled="1", result={"filled": "1", "state": "filled"})   # v1 crash: no booking
        self.journal.db.commit()
        with self.assertRaisesRegex(Stop, "Existing holdings left untouched: QQQ"):
            self.recover(0)                                                 # real QQQ is held: user decides
        history = self.journal.meta("switch_history")
        self.assertIsNone(history)                                          # not superseded before the stop
        intent = self.journal.meta("switch_intent")
        self.assertIn(row, intent["counted"])                               # booked into the old intent once
        spent = D(intent["spent"])
        self.assertEqual(spent, D("600.60") * (1 + D("0.0015")))
        self.open(migrate_symbols=("QQQ",))
        self.assertEqual(self.recover(30), "idle: switch complete")        # carry is too small for one SGOV share
        intent = self.new_intent()
        self.assertEqual(D(intent["carried"]), D(PROCEEDS) - spent)          # carried once, minus the QQQ spend
        self.assertFalse([c for c in self.broker.calls if c[:2] == ("sell", "QQQ")])   # recovery never migrates
        window = self.engine.tick(**dict(BASE, target=DEFENSIVE_SYMBOL, now=OPEN + timedelta(minutes=1),
                                         may_trade=True))
        self.assertEqual(window, "submitted: order")                       # open window, listed by the user
        self.assertEqual(self.broker.calls[-1][:2], ("sell", "QQQ"))


CRASH_CHILD = r'''
import json, os, sys
sys.path[:0] = [{root!r}, {tests!r}]
from datetime import datetime, timedelta
from autotrader.core import Engine, Journal, Settings, D
from autotrader.replay import PersistentPaper
spec = json.loads(sys.argv[1])
journal = Journal(spec["db"])
broker = PersistentPaper(journal)
engine = Engine(broker, journal, Settings(mode="paper"))
original = journal.event
def killer(category, *a, **k):
    result = original(category, *a, **k)
    if category == spec["when"]:
        os._exit(3)
    return result
journal.event = killer
now = datetime.fromisoformat(spec["now"])
engine.tick(target="SGOV", signal_date="2026-10-14", expected_signal_date="2026-10-14", session="2026-10-15",
            now=now, may_trade=False, may_manage_orders=True, may_recover=True,
            recovery_deadline=datetime.fromisoformat(spec["cutoff"]))
'''


class SupersedeCrashTests(unittest.TestCase):
    def test_kill_between_superseding_and_the_new_intent_never_loses_the_carried_money(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for when in ("switch_superseded", "switch_started"):
            db = Path(tmp.name) / f"{when}.sqlite"
            j = Journal(db)
            PersistentPaper(j, initial={"usd": PROCEEDS})
            j.put_meta("switch_intent", legacy_intent())
            j.db.close()
            result = subprocess.run([sys.executable, "-W", "ignore", "-c",
                                     CRASH_CHILD.format(root=str(ROOT), tests=str(ROOT / "tests")),
                                     json.dumps({"db": str(db), "when": when, "now": AFTER.isoformat(),
                                                 "cutoff": CUTOFF.isoformat()})],
                                    capture_output=True, text=True, timeout=60)
            with self.subTest(kill=when):
                self.assertEqual(result.returncode, 3, result.stderr)
                j = Journal(db)
                try:
                    self.assertEqual(j.meta("switch_intent")["id"], "legacy-intent")   # all or nothing
                    engine = Engine(PersistentPaper(j), j, Settings(mode="paper"))
                    for i in range(2):
                        engine.tick(**dict(BASE, target=DEFENSIVE_SYMBOL, now=AFTER + timedelta(minutes=1 + i),
                                           may_trade=False, may_manage_orders=True, may_recover=True,
                                           recovery_deadline=CUTOFF))
                    intent = j.meta("switch_intent")
                    self.assertEqual((intent["to"], D(intent["carried"])), (DEFENSIVE_SYMBOL, D(PROCEEDS)))
                    count = j.db.execute("SELECT count(*) FROM events WHERE category='switch_superseded'").fetchone()
                    self.assertEqual(count[0], 1)
                finally:
                    j.db.close()


if __name__ == "__main__":
    unittest.main()
