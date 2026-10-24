"""SGOV with the tax reserve, distributions, deposits and alerts. SGOV holdings
are never cash reserves; tax sells of SGOV pass the same independent checks;
distributions are booked once and never become switch money. Fake broker,
fixed clocks; no network. Distribution figures are invented test values."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrader import heartbeat, tax
from autotrader.core import D, Engine, Journal, Settings
from autotrader.schedule import Schedule
from autotrader.signal import DEFENSIVE_SYMBOL, RISK_SYMBOL
from test_intraday_recovery import AFTER_WINDOW, RecoveryBase
from test_tax import LedgerBase, StubBroker
from test_tax_reserve import JAN, PER_USD, TAX, ReserveBase


class SgovTaxSellTests(ReserveBase):
    def setUp(self):
        super().setUp()
        self.broker.positions = {DEFENSIVE_SYMBOL: D(100)}
        self.broker.prices[DEFENSIVE_SYMBOL] = D("100.50")

    def tick(self, at=0, **kw):
        return super().tick(at=at, target=DEFENSIVE_SYMBOL, **kw)

    def test_sgov_tax_sell_stays_within_the_installment_gap_plus_one_share(self):
        self.cap(TAX)
        self.assertEqual(self.tick(), "submitted: order")
        order = self.broker.orders[max(self.broker.orders)]
        per_share = order["price"] * (1 - D("0.0015")) * PER_USD
        self.assertEqual((order["symbol"], order["side"]), (DEFENSIVE_SYMBOL, "sell"))
        self.assertGreaterEqual(order["qty"] * per_share, TAX / 5)
        self.assertLess(order["qty"] * per_share - TAX / 5, per_share)
        self.assertEqual(json.loads(self.journal.rows()[0]["payload"])["target"], DEFENSIVE_SYMBOL)

    def test_without_a_cap_or_with_ledger_problems_only_a_plan_is_recorded(self):
        self.assertNotEqual(self.tick(), "submitted: order")
        self.assertEqual(self.broker.calls, [])
        self.cap(TAX)
        self.blockers = ["fee missing for x"]
        self.tick(at=10)
        self.assertEqual(self.broker.calls, [])

    def test_sgov_holdings_never_count_as_a_cash_reserve(self):
        self.tick(may_trade=False)
        st = self.st()
        self.assertEqual((st["reserve_krw"], st["reserve_usd"]), (0, 0))      # 100 SGOV shares held
        self.assertEqual(st["shortfall"], TAX)


class DistributionLedgerTests(LedgerBase):
    def test_distribution_is_booked_once_apart_from_gains_with_reported_figures(self):
        sessions = [s[0] for s in Schedule(datetime(2026, 11, 10, tzinfo=timezone.utc)).sessions]
        ledger = [dict(tr_type_code="99", smcd_name="해외배당입금", iscd="SGOV.AX", frcr_tr_amt="4.12",
                       tr_date="20261105", tran_srno="301", intx="0.62", cncl_yn="N"),
                  dict(tr_type_code="99", smcd_name="해외ETF분배금입금", iscd="SGOV.AX", frcr_tr_amt="3.98",
                       tr_date="20261203", tran_srno="302", intx="0", cncl_yn="N"),
                  dict(tr_type_code="99", smcd_name="해외배당입금", iscd="SGOV.AX", frcr_tr_amt="1.00",
                       tr_date="20261204", tran_srno="303", intx="0", cncl_yn="Y")]       # cancelled row
        broker = StubBroker([], ledger, {"20261105": D("1390"), "20261203": D("1400")})
        now = datetime(2026, 12, 5, 3, 0, tzinfo=timezone.utc)
        tax.sync(self.db, broker, sessions, now)
        tax.sync(self.db, broker, sessions, now)                               # re-read: nothing twice
        rows = list(self.db.execute("SELECT row_key, symbol, amount_usd, withheld FROM tax_dividends ORDER BY row_key"))
        self.assertEqual([tuple(r) for r in rows], [("div:301", "SGOV", "4.12", "0.62"), ("div:302", "SGOV", "3.98", "0")])
        self.assertEqual(tax.dividends(self.db, 2026)["amount_usd"], D("8.10"))
        result = tax.compute(self.db, self.settings)
        self.assertNotIn(2026, result["years"])                                 # no capital gain from cash income


class DistributionCashTests(RecoveryBase):
    def test_distribution_cash_never_becomes_intraday_switch_money(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW), "submitted: order")
        self.broker.fill(max(self.broker.orders))
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=30))
        self.broker.usd += D(300)                     # a distribution credited by the broker (one balance)
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=60)), "submitted: order")
        buy = self.last_order()
        proceeds = D(10) * D("69.93") * (1 - D("0.0015"))
        self.assertEqual(buy["symbol"], DEFENSIVE_SYMBOL)
        self.assertLessEqual(buy["qty"] * buy["price"] * (1 + D("0.0015")), proceeds)
        self.broker.fill(max(self.broker.orders))
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=90))
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=120)), "idle: switch complete")
        self.broker.usd += D(300)
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=600))
        self.assertEqual(len(self.orders("buy")), 1)                            # later cash waits for the open window


class AlertTests(unittest.TestCase):
    def test_monitor_status_carries_the_sgov_target_without_amounts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        journal = Journal(Path(tmp.name) / "j.sqlite")
        self.addCleanup(journal.db.close)
        journal.put_meta("signal", {"signal_date": "2026-10-14", "target": DEFENSIVE_SYMBOL, "high_252": 759.66})
        journal.put_meta("switch_intent", {"status": "buying", "to": DEFENSIVE_SYMBOL, "signal_date": "2026-10-14",
                                           "proceeds": "698.25"})
        now = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)
        message = heartbeat.build(journal, now=now, run_at=now, outcome="ok", work_done=True)
        self.assertEqual((message["target"], message["switch_to"]), (DEFENSIVE_SYMBOL, DEFENSIVE_SYMBOL))
        self.assertNotIn("698.25", json.dumps(message))
        self.assertNotIn("759.66", json.dumps(message))


if __name__ == "__main__":
    unittest.main()
