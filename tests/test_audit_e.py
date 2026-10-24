"""Follow-up fixes after 558878e: settlement calendar and confirmed dates,
crash-safe tax schema migration, and resolving the payment-source issue.
Temporary DBs, fixed clocks, child processes killed with os._exit; no network."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader import books, tax, telegram_bot
from autotrader.core import D, Journal
from autotrader.tax_reserve import TaxPlan, ensure_schema

ROOT = Path(__file__).resolve().parent.parent
JAN = datetime(2027, 1, 20, 3, tzinfo=timezone.utc)
ESTIMATE = lambda y: {"estimated_tax_krw": D(1_000_000), "complete": True}       # noqa: E731

OLD_SCHEMA = """
CREATE TABLE tax_external_funds(year INTEGER PRIMARY KEY, planned_krw TEXT NOT NULL,
  planned_date TEXT NOT NULL, secured_krw TEXT NOT NULL DEFAULT '0', updated TEXT NOT NULL);
CREATE TABLE tax_moves(seq INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, at TEXT NOT NULL,
  from_state TEXT NOT NULL, to_state TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL, ref TEXT);
CREATE TABLE tax_state(year INTEGER PRIMARY KEY, reserve_krw TEXT NOT NULL DEFAULT '0',
  reserve_usd TEXT NOT NULL DEFAULT '0', withdrawn_krw TEXT NOT NULL DEFAULT '0', paid_krw TEXT NOT NULL DEFAULT '0',
  sold_for_tax_krw TEXT NOT NULL DEFAULT '0', released INTEGER NOT NULL DEFAULT 0, early INTEGER NOT NULL DEFAULT 0);
INSERT INTO tax_external_funds VALUES (2026, '500000', '2027-04-30', '300000', '2027-01-01');
INSERT INTO tax_state(year, reserve_krw, paid_krw) VALUES (2026, '100000.5', '50000');
INSERT INTO tax_moves(year, at, from_state, to_state, amount, currency, ref) VALUES
  (2026, '2027-02-01', 'external_secured', 'reserve_krw', '100000', 'KRW', 'old'),
  (2026, '2027-02-02', 'external_secured', 'reserve_krw', '0.5', 'KRW', 'old'),
  (2026, '2027-03-01', 'outside', 'paid_krw', '50000', 'KRW', 'payment (outside funds)');
"""


def old_db(path):
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.commit()
    db.close()


class SettlementCalendarTests(unittest.TestCase):
    def setUp(self):
        self.sessions = tax.settlement_sessions("2017-06-01", "2026-12-31")

    def settle(self, day):
        return tax.settlement_date(day, self.sessions)

    def test_bank_holidays_with_the_market_open_are_not_settlement_days(self):
        self.assertEqual(self.settle("2026-10-09")[0], "2026-10-13")            # Columbus Day 10-12
        self.assertEqual(self.settle("2025-11-10")[0], "2025-11-12")            # Veterans Day 11-11
        self.assertEqual(self.settle("2026-06-18")[0], "2026-06-22")            # Juneteenth (market closed too)
        self.assertEqual(self.settle("2023-11-09")[0], "2023-11-13")            # T+2; Sat 11-11: Fed open Fri 11-10
        self.assertIn("Federal Reserve", self.settle("2026-10-09")[1])

    def test_unscheduled_closure_inside_the_window_is_not_guessed(self):
        settle, basis = self.settle("2025-01-08")                              # national day of mourning 01-09
        self.assertIsNone(settle)
        self.assertIn("unscheduled", basis)
        self.assertIsNone(self.settle("2018-12-04")[0])                        # T+2 over 2018-12-05

    def test_confirmed_dates_are_kept_and_only_computed_values_are_corrected(self):
        db = sqlite3.connect(":memory:")
        tax.ensure_schema(db)
        rows = [("confirmed", "2026-10-13", "broker confirmed settlement date"),
                ("old_calc", "2026-10-12", "computed T+1 (exchange calendar)"),
                ("user", "2026-10-14", "user entered from broker statement")]
        for key, settle, source in rows:
            db.execute("INSERT INTO tax_fills VALUES (?,'QQQ','buy','1','600','600',NULL,NULL,"
                       "'2026-10-09T15:00:00+00:00','2026-10-09',?,?,'1400','fx','broker_history','x')",
                       (key, settle, source))
        db.commit()
        self.assertEqual([m[0] for m in tax.settlement_mismatches(db, JAN)], ["old_calc"])
        self.assertEqual(tax.repair_settlements(db, JAN), 1)
        got = {r[0]: r[1:] for r in db.execute("SELECT fill_key, settle_date, fx FROM tax_fills")}
        self.assertEqual(got["confirmed"], ("2026-10-13", "1400"))
        self.assertEqual(got["user"], ("2026-10-14", "1400"))                   # differs, but not ours to change
        self.assertEqual(got["old_calc"], ("2026-10-13", None))
        self.assertEqual(tax.repair_settlements(db, JAN), 0)


CHILD = r'''
import json, os, sqlite3, sys
sys.path.insert(0, {root!r})
from autotrader import tax_reserve
spec = json.loads(sys.argv[1])

class Killing(sqlite3.Connection):
    seen = 0
    def _step(self):
        Killing.seen += 1
        if Killing.seen == spec["kill_at"]:
            os._exit(3)
    def execute(self, sql, *args):
        if sql.lstrip().split()[0].upper() in ("ALTER", "UPDATE", "INSERT", "COMMIT", "BEGIN"):
            self._step()
        return super().execute(sql, *args)
    def commit(self):
        self._step()
        return super().commit()

db = sqlite3.connect(spec["db"], factory=Killing)
tax_reserve.ensure_schema(db)
db.close()
'''


class MigrationCrashTests(unittest.TestCase):
    def check(self, path):
        db = sqlite3.connect(path)
        try:
            ensure_schema(db)
            ensure_schema(db)                                                   # reopened again
            plan = TaxPlan(db, {}, ESTIMATE, lambda: D(1400))
            status = plan.status(2026, JAN)
            self.assertEqual(status["external"]["deposited"], D("100000.5"))
            self.assertEqual(status["external"]["paid_ext"], D(0))
            audit = db.execute("SELECT count(*) FROM tax_corrections WHERE fill_key='external_funds:2026'").fetchone()
            issues = db.execute("SELECT count(*) FROM tax_issues WHERE kind='payment_source_unknown'").fetchone()
            self.assertEqual((audit[0], issues[0]), (1, 1))
        finally:
            db.close()

    def on_disk(self, path):
        """State as the killed process left it, before any restart."""
        db = sqlite3.connect(path)
        try:
            cols = {r[1] for r in db.execute("PRAGMA table_info(tax_external_funds)")} & {"deposited_krw", "paid_ext_krw"}
            marked = db.execute("SELECT count(*) FROM sqlite_master WHERE name='tax_migrations'").fetchone()[0] and \
                db.execute("SELECT count(*) FROM tax_migrations").fetchone()[0]
            audit = db.execute("SELECT count(*) FROM sqlite_master WHERE name='tax_corrections'").fetchone()[0] and \
                db.execute("SELECT count(*) FROM tax_corrections").fetchone()[0]
        finally:
            db.close()
        if not cols and not marked and not audit:
            return "old"
        return "migrated" if len(cols) == 2 and marked and audit else f"half: {sorted(cols)} {marked} {audit}"

    def test_kill_at_every_migration_step_recovers_on_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        killed = 0
        for step in range(1, 40):
            path = str(Path(tmp.name) / f"m{step}.sqlite")
            old_db(path)
            result = subprocess.run([sys.executable, "-W", "ignore", "-c", CHILD.format(root=str(ROOT)),
                                     json.dumps({"db": path, "kill_at": step})], capture_output=True, text=True,
                                    timeout=60)
            with self.subTest(step=step):
                self.assertIn(result.returncode, (0, 3), result.stderr)
                self.assertIn(self.on_disk(path), ("old", "migrated"))          # never half-applied
                self.check(path)
            if result.returncode == 0:
                break
            killed += 1
        self.assertGreaterEqual(killed, 3)                                      # both columns and the data step

    def test_half_applied_database_from_the_previous_version_is_completed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "half.sqlite")
        old_db(path)
        db = sqlite3.connect(path)
        db.execute("ALTER TABLE tax_external_funds ADD COLUMN deposited_krw TEXT NOT NULL DEFAULT '0'")
        db.commit()
        db.close()
        journal = Journal(path)
        try:
            self.assertEqual(books.diagnose(journal, JAN)["auto_repair_pending"]["external_funds_migration"], [2026])
        finally:
            journal.db.close()
        self.check(path)


class PaymentSourceIssueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "j.sqlite"
        old_db(self.path)
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.db.close)
        tax.ensure_schema(self.journal.db)
        self.plan = TaxPlan(self.journal.db, {}, ESTIMATE, lambda: D(1400))
        self.sent, self.updates = [], []
        env = patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "7", "TELEGRAM_BOT_TOKEN": "1:x"})
        env.start()
        self.addCleanup(env.stop)

    def transport(self, method, params):
        if method == "getUpdates":
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= params["offset"]]}
        self.sent.append(params["text"])
        return {"ok": True}

    def say(self, text):
        self.updates.append({"update_id": len(self.updates) + 1, "message": {
            "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": text}})
        telegram_bot.poll(self.journal, tax.TaxSettings(), JAN, transport=self.transport,
                          handlers=telegram_bot.plan_handlers(self.plan))
        return self.sent[-1]

    def blocked(self):
        return any(i["kind"] == "payment_source_unknown" for i in tax.compute(self.journal.db, tax.TaxSettings())["issues"])

    def test_detect_then_correct_then_the_hold_is_lifted_with_an_audit(self):
        self.assertTrue(self.blocked())
        self.say("/tax_paid_fix 0 2026")
        self.say("/tax_paid 100000 기타 2026")
        self.assertFalse(self.blocked())
        self.assertIn("해결", self.sent[-1] + self.sent[-2])
        self.assertEqual(self.plan.status(2026, JAN)["paid"], D(100_000))
        audit = self.journal.db.execute("SELECT issue_key, basis FROM tax_issue_resolutions").fetchall()
        self.assertEqual([a[0] for a in audit], ["payment_source:2026"])
        self.assertEqual(books.diagnose(self.journal, JAN)["needs_user"], [])
        ensure_schema(self.journal.db)                                          # restart: not flagged again
        self.assertFalse(self.blocked())

    def test_a_correction_that_may_still_hold_unknown_money_does_not_lift_it(self):
        self.say("/tax_paid_fix 20000 2026")                                    # 20,000 of unknown origin may remain
        self.assertTrue(self.blocked())
        self.assertIn("/tax_paid_fix 0", self.sent[-1])
        self.say("/tax_paid 30000 기타 2026")                                   # sourced money does not clear it
        self.assertTrue(self.blocked())

    def test_new_other_money_payments_are_never_flagged_as_unknown(self):
        fresh = Journal(Path(self.tmp.name) / "fresh.sqlite")
        self.addCleanup(fresh.db.close)
        tax.ensure_schema(fresh.db)
        plan = TaxPlan(fresh.db, {}, ESTIMATE, lambda: D(1400))
        plan.record_payment(2026, D(100_000), JAN, source="other")
        fresh.db.commit()
        fresh.db.execute("DELETE FROM tax_migrations")                          # force the migration to run again
        fresh.db.commit()
        ensure_schema(fresh.db)
        self.assertEqual(fresh.db.execute("SELECT count(*) FROM tax_issues").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
