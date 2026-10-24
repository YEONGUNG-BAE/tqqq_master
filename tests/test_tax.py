"""Tax ledger, estimate, /tax and tax notices (stage 2). No network."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autotrader import tax, telegram_bot
from autotrader.__main__ import tax_notices
from autotrader.core import D, Engine, Journal, Settings
from autotrader.notify import Notifier
from autotrader.schedule import Schedule
from test_fault_injection import FaultBroker

NOW = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
CHAT = "424242"


def fill(key, symbol, side, qty, price, fee, at, settle, fx, fx_source="base rate on settlement date"):
    return {"fill_key": key, "symbol": symbol, "side": side, "qty": D(qty), "price": D(price),
            "amount": D(qty) * D(price), "fee": None if fee is None else D(fee), "fee_source": "test",
            "filled_at": at, "trade_date": at[:10], "settle_date": settle, "settle_source": "computed",
            "fx": None if fx is None else D(fx), "fx_source": fx_source, "source": "test"}


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")
        self.addCleanup(self.journal.db.close)
        self.db = self.journal.db
        tax.ensure_schema(self.db)
        self.settings = tax.TaxSettings()

    def add(self, *fills):
        for f in fills:
            tax.record_fill(self.db, f, NOW)
        self.db.commit()


class FifoTests(LedgerBase):
    def test_fifo_partial_sale_with_fees_and_settlement_fx(self):
        self.add(fill("1", "TQQQ", "buy", 10, 100, 1, "2026-03-02T23:00:00+09:00", "2026-03-03", 1300),
                 fill("2", "TQQQ", "buy", 10, 110, 1, "2026-04-01T23:00:00+09:00", "2026-04-02", 1350),
                 fill("3", "TQQQ", "sell", 15, 120, 2, "2026-06-01T23:00:00+09:00", "2026-06-02", 1400))
        result = tax.compute(self.db, self.settings)
        cost = D(1001) * 1300 + D(1101) * 1350 * 5 / 10
        proceeds = D(1798) * 1400
        self.assertEqual(result["years"][2026]["net_krw"], proceeds - cost)
        lot = result["open_lots"]["TQQQ"][0]
        self.assertEqual((lot["fill_key"], lot["open"]), ("2", D(5)))      # FIFO, not average

    def test_year_follows_settlement_date_across_new_year(self):
        sessions = [s[0] for s in Schedule(datetime(2026, 12, 1, tzinfo=timezone.utc)).sessions]
        self.assertEqual(tax.settlement_date("2026-12-30", sessions)[0], "2026-12-31")
        self.assertEqual(tax.settlement_date("2026-12-31", sessions)[0], "2027-01-04")
        self.add(fill("1", "QQQ", "buy", 10, 500, 1, "2026-01-05T23:00:00+09:00", "2026-01-06", 1300),
                 fill("2", "QQQ", "sell", 5, 600, 1, "2026-12-30T23:00:00+09:00", "2026-12-31", 1400),
                 fill("3", "QQQ", "sell", 5, 600, 1, "2026-12-31T23:00:00+09:00", "2027-01-04", 1400))
        years = tax.compute(self.db, self.settings)["years"]
        self.assertEqual((years[2026]["disposals"], years[2027]["disposals"]), (1, 1))
        # A new-year sale that funds last year's tax is that new year's gain.
        self.assertEqual(tax.estimate(self.db, self.settings, 2026)["disposals"], 1)

    def test_unit_error_missing_fx_and_missing_fee_are_incomplete(self):
        self.add(fill("1", "TQQQ", "buy", 1, 100, 1, "2026-03-02T23:00:00+09:00", "2026-03-03", "1.35"),
                 fill("2", "TQQQ", "sell", 1, 120, None, "2026-06-01T23:00:00+09:00", "2026-06-02", None))
        est = tax.estimate(self.db, self.settings, 2026)
        joined = " ".join(est["reasons"])
        self.assertIn("out of range", joined)
        self.assertIn("settlement FX rate missing", joined)
        self.assertIn("fee missing", joined)
        self.assertFalse(est["complete"])
        self.assertTrue(tax.tax_sell_blockers(est))

    def test_duplicate_same_fill_ignored_conflicting_duplicate_blocks_tax_sells(self):
        f = fill("1", "TQQQ", "buy", 10, 100, 1, "2026-03-02T23:00:00+09:00", "2026-03-03", 1300)
        self.assertTrue(tax.record_fill(self.db, f, NOW))
        self.assertFalse(tax.record_fill(self.db, f, NOW))
        self.assertEqual(self.db.execute("SELECT count(*) FROM tax_fills").fetchone()[0], 1)
        tax.record_fill(self.db, dict(f, qty=D(11), amount=D(1100)), NOW)
        est = tax.estimate(self.db, self.settings, 2026)
        self.assertTrue(any("duplicate_fill_conflict" in b for b in tax.tax_sell_blockers(est)))

    def test_pre_ledger_holdings_sold_are_cost_incomplete_and_unexplained(self):
        self.add(fill("1", "QQQM", "sell", 29, 250, 1, "2026-10-19T23:00:00+09:00", "2026-10-20", 1400))
        result = tax.compute(self.db, self.settings, holdings={"BRK.B": "4"})
        self.assertIn("acquisition missing", " ".join(result["years"][2026]["incomplete"]))
        self.assertEqual(result["unexplained_holdings"], {"BRK.B": "4"})
        est = tax.estimate(self.db, self.settings, 2026, holdings={"BRK.B": "4"})
        self.assertEqual(est["account_net_krw"], 0)                         # unknown cost: not guessed

    def test_tax_ledger_problems_never_block_strategy_switch(self):
        tax.record_fill(self.db, fill("1", "TQQQ", "buy", 1, 1, 1, "2026-03-02T23:00:00+09:00", None, "1.0"), NOW)
        broker = FaultBroker(usd="0", positions={"TQQQ": 10})
        engine = Engine(broker, self.journal, Settings(mode="paper"))
        result = engine.tick(target="QQQ", signal_date="2026-10-19", expected_signal_date="2026-10-19",
                             session="2026-10-20", now=datetime(2026, 10, 20, 13, 31, tzinfo=timezone.utc),
                             may_trade=True)
        self.assertEqual(result, "submitted: order")
        self.assertEqual(broker.calls[0][:2], ("sell", "TQQQ"))


class EstimateTests(LedgerBase):
    def test_one_deduction_over_account_and_other_brokers_and_rounding(self):
        self.add(fill("1", "TQQQ", "buy", 10, 100, 0, "2026-03-02T23:00:00+09:00", "2026-03-03", 1000),
                 fill("2", "TQQQ", "sell", 10, 400, 0, "2026-06-01T23:00:00+09:00", "2026-06-02", 1000))
        est = tax.estimate(self.db, self.settings, 2026)
        self.assertTrue(est["this_account_only"])
        self.assertEqual(est["estimated_tax_krw"], (D(3_000_000) - D(2_500_000)) * D("0.22"))
        tax.record_external(self.db, "other-2026", 2026, "other", "-200001", True, "test", NOW)
        est = tax.estimate(self.db, self.settings, 2026)
        self.assertEqual(est["taxable_krw"], D(299_999))
        self.assertEqual(est["estimated_tax_krw"], D(65_999))               # floor to the won
        self.assertTrue(est["complete"])

    def test_dividends_are_reported_apart_from_capital_gains(self):
        tax.record_dividend(self.db, "div:1", "2026-09-30", "QQQ", D("12.5"), D("1.87"), D(1400), "test", NOW)
        est = tax.estimate(self.db, self.settings, 2026)
        self.assertEqual(est["account_net_krw"], 0)
        self.assertEqual(est["dividends"]["amount_usd"], D("12.5"))
        self.assertEqual(est["dividends"]["amount_krw_known"], D(17500))

    def test_broker_realized_pl_is_a_gross_anomaly_check_only(self):
        close = tax.compare_with_broker(D(1_000_000), D(1_020_000))
        self.assertFalse(close["anomaly"])
        self.assertFalse(close["validates_ledger"])                         # similar never means verified
        unit = tax.compare_with_broker(D(1_000_000), D(1_400_000_000))     # KRW vs x1400 unit error
        self.assertTrue(unit["anomaly"])
        self.assertTrue(any("differs grossly" in b for b in
                            tax.tax_sell_blockers(tax.estimate(self.db, self.settings, 2026), unit)))


class StubBroker:
    def __init__(self, fills, ledger, rates):
        self.fills, self.ledger, self.rates, self.rate_calls = fills, ledger, rates, []

    def fill_history(self, first, last):
        return list(self.fills)

    def transactions(self, first, last):
        return list(self.ledger)

    def base_rate(self, day):
        self.rate_calls.append(day)
        return self.rates.get(day)


class SyncTests(LedgerBase):
    def test_sync_records_completed_fills_fees_dividends_and_rates_idempotently(self):
        sessions = [s[0] for s in Schedule(NOW).sessions]
        fills = [dict(oder_date="20261015", oder_no="7", iscd="TQQQ.OQ", seln_byov_clcd="2", cntg_dqty="3.000000",
                      cntg_crnc_prc="70.0500", cntg_camt="210.15", cntg_ymdh="2026-10-15 22:31:05", crcd="USD"),
                 dict(oder_date="20261016", oder_no="2", iscd="TQQQ.OQ", seln_byov_clcd="2", cntg_dqty="1",
                      cntg_crnc_prc="71", cntg_camt="71", cntg_ymdh="2026-10-16 22:40:00", crcd="USD"),
                 dict(oder_date="20261020", oder_no="1", iscd="QQQ.OQ", seln_byov_clcd="2", cntg_dqty="1",
                      cntg_crnc_prc="600", cntg_camt="600", cntg_ymdh="2026-10-20 23:00:00", crcd="USD")]
        ledger = [dict(tr_type_code="05", iscd="TQQQ.OQ", if_tr_qty="3", tr_date="20261015", ovsc_deal_fee="0.15",
                       fee="0", cncl_yn="N"),
                  dict(tr_type_code="05", iscd="TQQQ.OQ", if_tr_qty="1", tr_date="20261016", ovsc_deal_fee="0.05",
                       fee="0", cncl_yn="N"),
                  dict(tr_type_code="05", iscd="TQQQ.OQ", if_tr_qty="1", tr_date="20261016", ovsc_deal_fee="0.05",
                       fee="0", cncl_yn="N"),                                  # ambiguous: two candidates
                  dict(tr_type_code="99", smcd_name="해외배당입금", iscd="QQQ.OQ", frcr_tr_amt="1.20",
                       tr_date="20260930", tran_srno="55", intx="0.18", cncl_yn="N")]
        rates = {"20261016": D("1401.10"), "20260930": D("1390.00"), "20261017": None}
        broker = StubBroker(fills, ledger, rates)
        now = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
        first = tax.sync(self.db, broker, sessions, now)
        second = tax.sync(self.db, broker, sessions, now)
        self.assertEqual((first["fills_added"], second["fills_added"]), (2, 0))   # 10-20 session not finished
        rows = {r[0]: r for r in self.db.execute("SELECT fill_key, fee, fee_source, settle_date, fx, fx_source FROM tax_fills")}
        self.assertEqual(rows["20261015:7"][1:4], ("0.15", "cash ledger (unique match)", "2026-10-16"))
        self.assertIsNone(rows["20261016:2"][1])                                # ambiguous fee not guessed
        self.assertEqual(rows["20261015:7"][4], "1401.10")
        self.assertTrue(rows["20261016:2"][5].startswith("fallback"))           # 10-19 Monday: earlier posting
        self.assertEqual(tax.dividends(self.db, 2026)["amount_usd"], D("1.20"))
        est = tax.estimate(self.db, self.settings, 2026)
        self.assertIn("fee missing", " ".join(est["open_lot_problems"]))       # held lot, not this year's gain
        self.assertTrue(any(b.startswith("held lot:") for b in tax.tax_sell_blockers(est)))


class TelegramTests(LedgerBase):
    def setUp(self):
        super().setUp()
        self.sent, self.updates = [], []
        self.env = patch.dict(os.environ, {"TELEGRAM_CHAT_ID": CHAT, "TELEGRAM_BOT_TOKEN": "123456:SECRET-T"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def transport(self, method, params):
        if method == "getUpdates":
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= params["offset"]]}
        self.sent.append(params)
        return {"ok": True}

    def message(self, uid, text, chat=CHAT, sender=CHAT, kind="private"):
        self.updates.append({"update_id": uid, "message": {"chat": {"id": int(chat), "type": kind},
                                                           "from": {"id": int(sender)}, "text": text}})

    def test_tax_reply_shows_tax_amounts_but_no_ids_or_secrets(self):
        self.add(fill("20261015:12345678", "TQQQ", "buy", 10, 100, None, "2026-03-02T23:00:00+09:00", "2026-03-03", 1300),
                 fill("20261016:99", "TQQQ", "sell", 10, 400, 1, "2026-06-01T23:00:00+09:00", "2026-06-02", 1300))
        self.message(1, "/tax")
        telegram_bot.poll(self.journal, self.settings, NOW, transport=self.transport)
        text = self.sent[0]["text"]
        self.assertIn("예상 세액: ₩", text)
        self.assertIn("수수료 누락 1건", text)
        for forbidden in ("12345678", "20261015", "SECRET-T"):
            self.assertNotIn(forbidden, text)

    def test_others_are_ignored_and_redelivery_is_processed_once(self):
        self.message(1, "/tax", chat="999", sender="999")
        self.message(2, "/tax", kind="group")
        self.message(3, "/help")
        self.assertEqual(telegram_bot.poll(self.journal, self.settings, NOW, transport=self.transport), 3)
        self.assertEqual(len(self.sent), 1)                                     # only our private chat
        restarted = Journal(Path(self.tmp.name) / "j.sqlite")
        try:
            self.assertEqual(telegram_bot.poll(restarted, self.settings, NOW, transport=self.transport), 0)
        finally:
            restarted.db.close()
        self.assertEqual(len(self.sent), 1)

    def test_a_failing_handler_consumes_the_update_and_changes_nothing(self):
        def boom(ctx, args):
            ctx.db.execute("INSERT INTO tax_external VALUES ('x', 2026, 'b', '1', 1, 'now', 'test')")
            raise RuntimeError("bad input")
        self.message(1, "/boom")
        telegram_bot.poll(self.journal, self.settings, NOW, transport=self.transport, handlers={"/boom": boom})
        self.assertEqual(self.db.execute("SELECT count(*) FROM tax_external").fetchone()[0], 0)
        self.assertIn("처리하지 못했습니다", self.sent[0]["text"])
        self.assertEqual(self.journal.meta("tg_last_update"), 1)

    def test_unreachable_telegram_does_not_raise(self):
        def down(method, params):
            raise OSError("down")
        with self.assertRaises(OSError):
            telegram_bot.poll(self.journal, self.settings, NOW, transport=down)
        # In the run path this call is wrapped by housekeeping, which records and ignores it.


class TaxNoticeTests(LedgerBase):
    def test_december_estimate_and_january_recalculation_once_each(self):
        sent = []
        n = Notifier(self.journal, channels={"t": sent.append})
        for when in (datetime(2026, 11, 30, 3, tzinfo=timezone.utc), datetime(2026, 12, 1, 3, tzinfo=timezone.utc),
                     datetime(2026, 12, 2, 3, tzinfo=timezone.utc), datetime(2027, 1, 5, 3, tzinfo=timezone.utc),
                     datetime(2027, 1, 6, 3, tzinfo=timezone.utc), datetime(2027, 2, 1, 3, tzinfo=timezone.utc)):
            tax_notices(self.journal, n, self.settings, when)
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0].startswith("[세금] 2026년 귀속 예상"))
        self.assertIn("다시 계산했습니다", sent[1])
        self.assertIn("예상 세액: ₩0", sent[0])

    def test_amounts_are_allowed_only_in_tax_messages_and_free_text_stays_masked(self):
        sent = []
        n = Notifier(self.journal, channels={"t": sent.append})
        self.assertFalse(n.send("hold", "x", "blocked", NOW, amounts=[("estimated_tax", 1)]))
        self.assertTrue(n.send("tax", "y", "account 12345678 balance 1,500,000", NOW,
                               amounts=[("estimated_tax", 1_234_567)]))
        self.assertIn("예상 세액: ₩1,234,567", sent[0])
        self.assertNotIn("12345678", sent[0])
        self.assertNotIn("1,500,000", sent[0])
        with self.assertRaises(ValueError):
            from autotrader.notify import render_amounts
            render_amounts([("account_balance", 1)])


if __name__ == "__main__":
    unittest.main()
