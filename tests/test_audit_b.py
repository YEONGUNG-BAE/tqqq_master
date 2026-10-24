"""Audit fixes 3-4: external funds counted once; Telegram updates applied once
across processes. Fake transports, temp databases, fixed clocks."""
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader import tax, telegram_bot
from autotrader.core import D, Journal
from autotrader.tax_reserve import TaxPlan

JAN = datetime(2027, 1, 20, 3, 0, tzinfo=timezone.utc)
TAX = D(1_000_000)


def plan_for(journal):
    return TaxPlan(journal.db, {}, lambda y: {"estimated_tax_krw": TAX, "complete": True}, lambda: D(1400))


class ExternalFundsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")
        self.addCleanup(self.journal.db.close)
        self.plan = plan_for(self.journal)

    def shortfall(self, now=JAN):
        return self.plan.status(2026, now)["shortfall"]

    def test_a_payment_from_secured_external_money_consumes_it(self):
        self.plan.set_external_secured(2026, D(600_000), JAN)
        self.plan.record_payment(2026, D(300_000), JAN, source="external")
        st = self.plan.status(2026, JAN)
        self.assertEqual((st["paid"], st["external"]["secured"]), (D(300_000), D(300_000)))
        self.assertEqual(self.shortfall(), D(400_000))

    def test_b_deposit_before_the_planned_date_is_counted_once(self):
        self.plan.set_external(2026, D(600_000), "2027-03-31", JAN)
        self.plan.set_external_secured(2026, D(600_000), JAN)
        rid = self.plan.expect_external_deposit(2026, D(300_000), "2027-01-20", JAN)
        self.assertEqual(self.shortfall(), D(400_000))             # not yet seen in the account: still outside
        self.assertEqual(self.plan.status(2026, JAN)["reserve_krw"], 0)
        moved = self.plan.match_deposits([dict(tr_type_code="01", tr_amt="300000", tr_date="20270120",
                                               tran_srno="5", cncl_yn="N")], JAN)
        self.assertEqual(moved, [rid])
        st = self.plan.status(2026, JAN)
        self.assertEqual((st["reserve_krw"], st["external"]["secured"]), (D(300_000), D(300_000)))
        self.assertEqual(self.shortfall(), D(400_000))
        self.assertEqual(self.shortfall(datetime(2027, 4, 2, tzinfo=timezone.utc)), D(400_000))   # after the date

    def test_unconfirmed_deposit_is_held_back_but_not_krw_ready(self):
        self.plan.set_external_secured(2026, D(600_000), JAN)
        self.plan.expect_external_deposit(2026, D(300_000), "2027-01-20", JAN)
        krw, usd = self.plan.reserved(JAN)
        self.assertEqual(krw, D(300_000))                          # kept from FX/auto-allocation meanwhile
        self.assertEqual(self.plan.status(2026, JAN)["reserve_krw"], 0)

    def test_deposit_matched_twice_or_redelivered_moves_once(self):
        self.plan.set_external_secured(2026, D(600_000), JAN)
        self.plan.expect_external_deposit(2026, D(300_000), "2027-01-20", JAN)
        row = dict(tr_type_code="01", tr_amt="300000", tr_date="20270121", tran_srno="9", cncl_yn="N")
        self.plan.match_deposits([row, dict(row)], JAN)
        self.plan.match_deposits([row], JAN)
        st = self.plan.status(2026, JAN)
        self.assertEqual((st["reserve_krw"], st["external"]["secured"]), (D(300_000), D(300_000)))

    def test_smaller_bonus_is_reflected_in_remaining_installments_without_resecuring(self):
        self.plan.set_external(2026, D(600_000), "2027-03-31", JAN)
        march = datetime(2027, 3, 20, 3, tzinfo=timezone.utc)
        self.plan._move(2026, march, "free_cash", "reserve_krw", D(200_000), "KRW", "test")
        self.plan.set_external(2026, D(300_000), "2027-03-31", march)        # bonus smaller than planned
        st = self.plan.status(2026, march)
        self.assertEqual(st["account_need"], D(700_000))
        self.assertEqual(st["installment_target"], D(420_000))
        self.assertEqual(st["installment_due"], D(220_000))                   # 200k already secured is kept

    def test_payment_source_must_be_named_when_ambiguous_and_never_goes_negative(self):
        self.plan.set_external_secured(2026, D(100_000), JAN)
        self.plan._move(2026, JAN, "free_cash", "reserve_krw", D(100_000), "KRW", "test")
        rid = self.plan.request_withdrawal(2026, D(100_000), "2027-01-20", JAN)
        self.plan.match_withdrawals([dict(tr_type_code="02", tr_amt="100000", tr_date="20270120",
                                          tran_srno="1", cncl_yn="N")], JAN)
        with self.assertRaises(ValueError):
            self.plan.record_payment(2026, D(50_000), JAN)                    # withdrawn or external? ask
        with self.assertRaises(ValueError):
            self.plan.record_payment(2026, D(150_000), JAN, source="external")   # more than secured
        self.plan.record_payment(2026, D(50_000), JAN, source="withdrawn")
        st = self.plan.status(2026, JAN)
        self.assertEqual((st["withdrawn"], st["external"]["secured"], st["paid"]), (D(50_000), D(100_000), D(50_000)))


class TelegramConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "j.sqlite"
        Journal(self.path).db.close()
        self.updates = [{"update_id": 1, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                                                     "text": "/tax_paid 100000 기타 2026"}},
                        {"update_id": 2, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                                                     "text": "/tax_paid 5000 기타 2026"}}]
        self.sent = []
        self.env = patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "7", "TELEGRAM_BOT_TOKEN": "123456:SECRET-T"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def transport(self, method, params):
        if method == "getUpdates":
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= params["offset"]]}
        self.sent.append(params["text"])
        return {"ok": True}

    def receiver(self):
        journal = Journal(self.path)
        plan = plan_for(journal)
        return journal, plan

    def paid(self):
        journal, plan = self.receiver()
        try:
            return plan.status(2026, JAN)["paid"]
        finally:
            journal.db.close()

    def test_two_receivers_that_read_the_same_offset_apply_once(self):
        a, plan_a = self.receiver()
        b, plan_b = self.receiver()
        try:
            got_a = telegram_bot.fetch(a, self.transport)              # both see last=0 and update 1
            got_b = telegram_bot.fetch(b, self.transport)
            self.assertEqual([u["update_id"] for u in got_a], [u["update_id"] for u in got_b])
            telegram_bot.process(a, got_a, tax.TaxSettings(), JAN, self.transport, handlers=telegram_bot.plan_handlers(plan_a))
            telegram_bot.process(b, got_b, tax.TaxSettings(), JAN, self.transport, handlers=telegram_bot.plan_handlers(plan_b))
            failures = a.db.execute("SELECT count(*) FROM events WHERE category='telegram_command_failed'").fetchone()[0]
        finally:
            a.db.close()
            b.db.close()
        self.assertEqual(self.paid(), D(105_000))
        # The second receiver simply skips what the first took: no failure, one reply each.
        self.assertEqual(failures, 0)
        self.assertEqual(len(self.sent), 2)
        self.assertFalse(any("처리하지 못했습니다" in t for t in self.sent))

    def test_concurrent_threads_apply_each_message_once_and_offset_never_moves_back(self):
        barrier = threading.Barrier(2)
        errors = []

        def run():
            journal, plan = self.receiver()
            try:
                got = telegram_bot.fetch(journal, self.transport)
                barrier.wait()
                telegram_bot.process(journal, got, tax.TaxSettings(), JAN, self.transport,
                                     handlers=telegram_bot.plan_handlers(plan))
            except Exception as exc:
                errors.append(exc)
            finally:
                journal.db.close()
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.paid(), D(105_000))
        journal = Journal(self.path)
        try:
            self.assertEqual(journal.meta("tg_last_update"), 2)
            journal.put_meta("tg_last_update", 2)
            telegram_bot.process(journal, [self.updates[0]], tax.TaxSettings(), JAN, self.transport,
                                 handlers=telegram_bot.plan_handlers(plan_for(journal)))   # a stale receiver
            self.assertEqual(journal.meta("tg_last_update"), 2)                            # never moves back
        finally:
            journal.db.close()
        self.assertEqual(self.paid(), D(105_000))

    def test_lost_reply_does_not_reapply_the_change(self):
        journal, plan = self.receiver()
        try:
            def flaky(method, params):
                if method == "sendMessage":
                    raise OSError("reply lost")
                return self.transport(method, params)
            got = telegram_bot.fetch(journal, flaky)
            telegram_bot.process(journal, got, tax.TaxSettings(), JAN, flaky, handlers=telegram_bot.plan_handlers(plan))
            got = telegram_bot.fetch(journal, flaky)
            telegram_bot.process(journal, got, tax.TaxSettings(), JAN, flaky, handlers=telegram_bot.plan_handlers(plan))
        finally:
            journal.db.close()
        self.assertEqual(self.paid(), D(105_000))


if __name__ == "__main__":
    unittest.main()
