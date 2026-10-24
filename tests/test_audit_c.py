"""Audit fixes 5-7: ledger completeness, settlement dates outside the calendar,
tax years and deadlines across the May/June boundary. No network."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autotrader import tax, telegram_bot
from autotrader.core import D, Engine, Journal, Settings
from autotrader.schedule import Schedule
from autotrader.tax_reserve import TaxPlan
from test_fault_injection import FaultBroker
from test_tax import fill

NOW = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")
        self.addCleanup(self.journal.db.close)
        self.db = self.journal.db
        tax.ensure_schema(self.db)
        self.settings = tax.TaxSettings()

    def buy_ten(self):
        tax.record_fill(self.db, fill("1", "TQQQ", "buy", 10, 100, 1, "2026-03-02T23:00:00+09:00", "2026-03-03", 1300), NOW)
        self.db.commit()


class LedgerCompletenessTests(Base):
    def test_symbol_only_in_the_ledger_is_compared_as_zero_held(self):
        self.buy_ten()
        est = tax.estimate(self.db, self.settings, 2026, holdings={})      # account really empty
        self.assertFalse(est["complete"])
        self.assertTrue(any("holdings not explained" in r for r in est["reasons"]))
        self.assertTrue(tax.tax_sell_blockers(est))

    def test_symbol_only_in_the_account_and_a_normal_empty_account(self):
        self.assertTrue(tax.estimate(self.db, self.settings, 2026, holdings={})["complete"])   # empty both sides
        est = tax.estimate(self.db, self.settings, 2026, holdings={"QQQ": "2"})
        self.assertTrue(any("holdings not explained" in r for r in est["reasons"]))

    def test_holdings_not_checked_is_not_complete(self):
        self.buy_ten()
        est = tax.estimate(self.db, self.settings, 2026, holdings=None)
        self.assertFalse(est["complete"])
        self.assertTrue(any("holdings not checked" in r for r in est["reasons"]))

    def test_failed_or_stale_sync_keeps_tax_sells_on_hold_until_fixed(self):
        self.buy_ten()
        held = {"TQQQ": "10"}
        failed = {"day": "2026-10-24", "ok": False, "error": "API read failed: 9999", "at": NOW.isoformat()}
        est = tax.estimate(self.db, self.settings, 2026, holdings=held, sync=failed, now=NOW)
        self.assertTrue(any("sync failed" in r for r in est["reasons"]))
        stale = {"day": "2026-10-10", "ok": True, "synced_at": (NOW - timedelta(days=14)).isoformat()}
        est = tax.estimate(self.db, self.settings, 2026, holdings=held, sync=stale, now=NOW)
        self.assertTrue(any("sync is stale" in r for r in est["reasons"]))
        fresh = {"day": "2026-10-24", "ok": True, "synced_at": NOW.isoformat()}
        est = tax.estimate(self.db, self.settings, 2026, holdings=held, sync=fresh, now=NOW)
        self.assertEqual(tax.tax_sell_blockers(est), [])                  # corrected data clears the hold

    def test_tax_reply_uses_the_engine_holdings_and_completeness(self):
        self.buy_ten()
        self.journal.put_meta("last_positions", {"at": NOW.isoformat(), "positions": {}})
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "7"}):
            plan = TaxPlan(self.db, {}, lambda y: tax.estimate(self.db, self.settings, y, holdings={}, now=NOW),
                           lambda: D(1400))
            text = telegram_bot.tax_report_lines(self.db, self.settings, NOW, estimate_fn=plan.estimate_fn)
        joined = "\n".join(text)
        self.assertIn("추정", joined)
        self.assertNotIn("확정 가능", joined)

    def test_strategy_switch_still_runs_while_tax_data_is_incomplete(self):
        self.buy_ten()
        broker = FaultBroker(usd="0", positions={"TQQQ": 10})
        engine = Engine(broker, self.journal, Settings(mode="paper"))
        result = engine.tick(target="QQQ", signal_date="2026-10-23", expected_signal_date="2026-10-23",
                             session="2026-10-26", now=datetime(2026, 10, 26, 13, 31, tzinfo=timezone.utc),
                             may_trade=True)
        self.assertEqual(result, "submitted: order")


class SettlementTests(Base):
    def test_trade_before_the_calendar_gets_no_made_up_settlement_date(self):
        sessions = [s[0] for s in Schedule(NOW).sessions]                 # starts in 2024
        self.assertGreater(sessions[0], "2023-12-20")
        settle, source = tax.settlement_date("2023-12-20", sessions)
        self.assertIsNone(settle)
        self.assertIn("calendar", source)

    def test_settlement_cycle_follows_the_era_around_the_t1_switch(self):
        sessions = tax.settlement_sessions("2023-12-01", "2024-06-30")
        self.assertEqual(tax.settlement_date("2023-12-20", sessions)[0], "2023-12-22")   # T+2
        self.assertEqual(tax.settlement_date("2024-05-24", sessions)[0], "2024-05-29")   # T+2 over Memorial Day
        self.assertEqual(tax.settlement_date("2024-05-28", sessions)[0], "2024-05-29")   # T+1 from 2024-05-28
        self.assertEqual(tax.settlement_date("2023-12-29", sessions)[0], "2024-01-03")   # year-end, holiday
        self.assertIsNone(tax.settlement_date("2023-12-30", sessions)[0])                # not a session

    def test_wrong_stored_settlement_dates_are_repaired_once_with_an_audit(self):
        tax.record_fill(self.db, fill("1", "QQQ", "buy", 1, 400, 1, "2023-12-20T23:00:00+09:00", "2024-01-02", 1290), NOW)
        tax.record_fill(self.db, fill("2", "QQQ", "buy", 1, 400, 1, "1994-01-03T23:00:00+09:00", "1994-01-05", 800), NOW)
        self.db.commit()
        first = tax.repair_settlements(self.db, NOW)
        second = tax.repair_settlements(self.db, NOW)
        rows = {r[0]: r[1:] for r in self.db.execute("SELECT fill_key, settle_date, fx, settle_source FROM tax_fills")}
        self.assertEqual(rows["1"][:2], ("2023-12-22", None))             # recomputed; FX refetched later
        self.assertIsNone(rows["2"][0])                                   # no verified rule: quarantined
        self.assertIn("unverified", rows["2"][2])
        self.assertEqual((first, second), (2, 0))                         # idempotent
        audit = self.db.execute("SELECT count(*) FROM tax_corrections").fetchone()[0]
        self.assertEqual(audit, 2)


class TaxYearTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")
        self.addCleanup(self.journal.db.close)
        self.tax_by_year = {2026: D(1_000_000), 2027: D(500_000)}
        self.plan = TaxPlan(self.journal.db, {"payment_deadline": "2027-06-01"},
                            lambda y: {"estimated_tax_krw": self.tax_by_year.get(y, D(0)), "complete": True},
                            lambda: D(1400))

    def test_june_deadline_keeps_the_previous_year_due(self):
        june1 = datetime(2027, 6, 1, 3, tzinfo=timezone.utc)
        self.assertEqual(self.plan.active_years(june1), [2026])
        self.assertEqual(telegram_bot.plan_year(self.plan, june1), 2026)

    def test_unpaid_previous_year_stays_due_after_may_and_late_payment_lands_there(self):
        july = datetime(2027, 7, 10, 3, tzinfo=timezone.utc)
        self.assertEqual(self.plan.active_years(july), [2026])
        self.plan.record_payment(2026, D(1_000_000), july, source="other")
        self.assertEqual(self.plan.active_years(july), [])
        self.assertEqual(self.plan.status(2027, july)["paid"], 0)

    def test_two_unpaid_years_require_an_explicit_year(self):
        jan = datetime(2028, 1, 20, 3, tzinfo=timezone.utc)                # 2026 still unpaid, 2027 now due
        self.assertEqual(self.plan.active_years(jan), [2026, 2027])
        self.assertIsNone(telegram_bot.plan_year(self.plan, jan))

    def test_next_year_schedule_never_reuses_another_years_deadline(self):
        plan = TaxPlan(self.journal.db, {"payment_deadline": "2027-05-31"},
                       lambda y: {"estimated_tax_krw": D(1), "complete": True}, lambda: D(1400))
        self.assertEqual(len(plan.rounds(2027)), 5)
        self.assertEqual(plan.rounds(2027)[-1], date(2028, 5, 15))
        self.assertIsNone(plan.deadline_for(2027))
        self.assertEqual(plan.deadline_for(2026)["date"], "2027-05-31")

    def test_per_year_deadlines_and_separate_reserves(self):
        plan = TaxPlan(self.journal.db, {"payment_deadlines": {"2026": {"date": "2027-05-31", "verified": True},
                                                               "2027": {"date": "2028-05-31"}}},
                       lambda y: {"estimated_tax_krw": D(100), "complete": True}, lambda: D(1400))
        self.assertTrue(plan.deadline_for(2026)["verified"])
        self.assertFalse(plan.deadline_for(2027)["verified"])
        plan._move(2026, datetime(2027, 1, 20, tzinfo=timezone.utc), "free_cash", "reserve_krw", D(50), "KRW", "t")
        self.assertEqual(plan.status(2027, datetime(2028, 1, 20, tzinfo=timezone.utc))["reserve_krw"], 0)
        plan.release(2026, datetime(2028, 1, 20, tzinfo=timezone.utc))      # an older year can still be released
        self.assertEqual(plan.status(2026, datetime(2028, 1, 20, tzinfo=timezone.utc))["reserve_krw"], 0)

    def test_settlement_buffer_counts_calendar_days(self):
        plan = TaxPlan(self.journal.db, {"payment_deadlines": {"2026": "2027-05-10"}, "settlement_buffer_days": 5},
                       lambda y: {"estimated_tax_krw": D(1), "complete": True}, lambda: D(1400))
        self.assertEqual(plan.rounds(2026)[-1], date(2027, 5, 5))

    def test_engine_works_on_the_oldest_year_that_still_needs_money(self):
        jan = datetime(2028, 1, 20, 3, tzinfo=timezone.utc)
        self.plan.record_payment(2026, D(400_000), jan, source="other")
        self.plan._move(2026, jan, "free_cash", "reserve_krw", D(600_000), "KRW", "t")   # 2026 fully covered
        self.assertEqual(self.plan.active_years(jan), [2026, 2027])
        self.assertEqual(self.plan.status(2026, jan)["installment_due"], 0)
        engine = SimpleNamespace(tax_plan=self.plan)
        engine._tax_years = lambda now: Engine._tax_years(engine, now)
        self.assertEqual(Engine._tax_year(engine, jan), 2027)

    def test_notices_cover_every_open_year(self):
        from autotrader.__main__ import plan_notices
        jan = datetime(2028, 1, 20, 3, tzinfo=timezone.utc)
        self.plan._move(2026, jan, "free_cash", "reserve_krw", D(1_000_000), "KRW", "t")
        self.plan._move(2027, jan, "free_cash", "reserve_krw", D(500_000), "KRW", "t")
        sent = []
        notifier = SimpleNamespace(send=lambda kind, key, text, now, amounts=None: sent.append(key))
        plan_notices(self.journal, notifier, self.plan, jan)
        self.assertEqual(sorted(sent), ["transfer:2026", "transfer:2027"])


class TaxYearCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")
        self.addCleanup(self.journal.db.close)
        self.plan = TaxPlan(self.journal.db, {}, lambda y: {"estimated_tax_krw": D(1_000_000), "complete": True},
                            lambda: D(1400))
        self.sent, self.updates = [], []
        env = patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "7", "TELEGRAM_BOT_TOKEN": "1:x"})
        env.start()
        self.addCleanup(env.stop)

    def transport(self, method, params):
        if method == "getUpdates":
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= params["offset"]]}
        self.sent.append(params["text"])
        return {"ok": True}

    def say(self, uid, text, now):
        self.updates.append({"update_id": uid, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                                                           "text": text}})
        telegram_bot.poll(self.journal, tax.TaxSettings(), now, transport=self.transport,
                          handlers=telegram_bot.plan_handlers(self.plan))

    def test_ambiguous_year_is_refused_and_explicit_year_is_applied(self):
        jan = datetime(2028, 1, 20, 3, tzinfo=timezone.utc)
        self.plan.set_deadline(2026, date(2027, 6, 1), jan)                 # 2026 overdue and unpaid, 2027 in prep
        self.assertEqual(self.plan.active_years(jan), [2026, 2027])
        self.say(1, "/tax_paid 100000 기타", jan)
        self.assertIn("연도", self.sent[-1])
        self.assertEqual(self.plan.status(2026, jan)["paid"], 0)
        self.say(2, "/tax_paid 100000 기타 2026", jan)
        self.assertEqual(self.plan.status(2026, jan)["paid"], D(100_000))
        self.assertEqual(self.plan.status(2027, jan)["paid"], 0)

    def test_money_command_with_no_open_year_asks_for_the_year(self):
        july = datetime(2027, 7, 10, 3, tzinfo=timezone.utc)               # nothing open: no prep, no activity
        self.assertEqual(self.plan.active_years(july), [])
        self.say(1, "/tax_paid 100000 기타", july)
        self.assertIn("연도", self.sent[-1])
        self.assertEqual(self.plan.status(2027, july)["paid"], 0)
        self.say(2, "/tax_cap 500000", july)                               # planning commands default to this year
        self.assertIn("2027년", self.sent[-1])


if __name__ == "__main__":
    unittest.main()
