"""Audit fix 8: reverse FX (USD -> KRW) passes the same reservation gate as
every other new request. Item 9: books an older version left inconsistent are
found read-only, repaired once with an audit record, or left for a person.
Fake broker, temporary DB, fixed clock; no network."""
import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import patch

from autotrader import books, tax
from autotrader.core import NOT_SENT, D, Engine, Journal, ReservationLookup, Stop, UnknownOutcome
from autotrader.tax_reserve import TaxPlan
from test_tax_reserve import JAN, ReserveBase

RESERVATION = {"symbol": "QQQ", "side": "buy", "quantity": "1"}


class ReverseFxReservationTests(ReserveBase):
    def setUp(self):
        super().setUp()
        self.broker.usd = D(500)
        self.tick(may_trade=False)                                 # earmark a USD reserve
        self.assertGreater(self.st()["reserve_usd"], 0)
        self.engine.reverse_fx_verified = True

    def fx_out(self):
        return [c for c in self.broker.calls if c[0] == "fx_out"]

    def reverse(self, at=10):
        return self.tick(at=at, may_trade=False, may_exchange=True)

    def lookup(self, result):
        def active(now, since=None):
            if isinstance(result, Exception):
                raise result
            return result
        self.broker.active_reservations = active

    def test_active_reservation_blocks_reverse_fx(self):
        self.broker.reservations = [RESERVATION]
        with self.assertRaisesRegex(Stop, "active reservation"):
            self.reverse()
        self.assertEqual(self.fx_out(), [])
        self.assertEqual(self.plan.in_progress(2026)["rows"], [])

    def test_failed_lookup_blocks_reverse_fx(self):
        self.lookup(OSError("lookup down"))
        with self.assertRaisesRegex(Stop, "reservation lookup failed"):
            self.reverse()
        self.assertEqual(self.fx_out(), [])

    def test_incomplete_lookup_blocks_reverse_fx_in_live(self):
        self.path = self.path.with_name("live.sqlite")             # each mode keeps its own state
        self.open(mode="live")
        self.tick(may_trade=False)
        self.assertGreater(self.st()["reserve_usd"], 0)
        self.engine.reverse_fx_verified = True
        self.lookup(ReservationLookup([], complete=False, reason="start date not configured"))
        with self.assertRaisesRegex(Stop, "coverage unverified"):
            self.reverse()
        self.assertEqual(self.fx_out(), [])

    def test_clean_complete_lookup_lets_reverse_fx_proceed(self):
        self.assertEqual(self.reverse(), "submitted: fx_out")
        self.assertEqual(len(self.fx_out()), 1)

    def test_lost_reply_is_settled_even_after_a_reservation_appears_and_never_resent(self):
        original = self.broker.exchange_to_krw

        def lost(usd, quote):
            original(usd, quote)
            raise TimeoutError()
        self.broker.exchange_to_krw = lost
        with self.assertRaises(UnknownOutcome):
            self.reverse()
        self.broker.exchange_to_krw = original
        self.broker.reservations = [RESERVATION]                   # added in the broker app afterwards
        self.restart()
        self.engine.reverse_fx_verified = True
        self.assertEqual(self.reverse(at=20), "recovered: fx_out")
        self.assertEqual(self.reverse(at=30), "idle: outside execution window")
        self.assertEqual(len(self.fx_out()), 1)                    # settled once, never resent
        self.assertEqual(self.st()["reserve_usd"], 0)
        self.assertGreater(self.st()["reserve_krw"], 0)



class ExistingDbTests(ReserveBase):
    """Each defect is injected the way the older version left it: the request's
    status committed while its derived entries did not."""

    def diagnose(self):
        return books.diagnose(self.journal, JAN)

    def audit(self):
        return self.diagnose()["repairs_recorded"]

    def test_fresh_db_is_clean_and_repairs_record_nothing(self):
        self.tick(may_trade=False)
        report = self.diagnose()
        self.assertTrue(report["clean"])
        self.assertEqual(report["repairs_recorded"],
                         {"effects_replayed": 0, "switch_reopened": 0, "reservations_released": 0, "corrections": 0})

    def test_done_switch_order_without_its_switch_booking(self):
        self.broker.positions = {"TQQQ": D(100)}
        self.assertEqual(self.tick(target="QQQ"), "submitted: order")
        self.broker.fill(1)
        with patch.object(Engine, "_order_effects", lambda *a, **k: None):      # older version: DONE only
            self.tick(at=10, target="QQQ", may_trade=False)
        sell = [r["id"] for r in self.journal.rows() if r["kind"] == "order"]
        self.assertEqual(self.diagnose()["auto_repair_pending"]["done_without_switch_effects"], sell)
        self.tick(at=20, target="QQQ", may_trade=False)
        self.assertEqual(self.diagnose()["auto_repair_pending"]["done_without_switch_effects"], [])
        self.assertIn(sell[0], self.journal.meta("switch_intent")["counted"])
        before = self.audit()
        self.assertEqual(before["effects_replayed"], 1)
        self.tick(at=30, target="QQQ", may_trade=False)
        self.assertEqual(self.audit(), before)                                  # idempotent

    def test_done_reverse_fx_without_its_tax_booking(self):
        self.broker.usd = D(500)
        self.tick(may_trade=False)
        self.engine.reverse_fx_verified = True
        with patch.object(self.plan, "finish_fx_out", lambda *a, **k: None):
            self.assertEqual(self.tick(at=10, may_trade=False, may_exchange=True), "submitted: fx_out")
            self.tick(at=15, may_trade=False)                                   # settles to DONE
        fx = [r["id"] for r in self.journal.rows() if r["kind"] == "fx_out"]
        self.assertEqual(self.diagnose()["auto_repair_pending"]["done_without_tax_effects"], fx)
        self.tick(at=20, may_trade=False)
        st = self.st()
        self.assertEqual((st["reserve_usd"], st["in_progress_krw"]), (0, 0))
        self.assertGreater(st["reserve_krw"], 0)
        before = self.audit()
        self.tick(at=30, may_trade=False)
        self.assertEqual(self.audit(), before)
        self.assertEqual(len(self.broker.calls), 1)                            # nothing resent

    def test_orphan_reservations_of_unsent_or_missing_requests(self):
        self.broker.usd = D(500)
        self.tick(may_trade=False)
        unsent = self.journal.create("fx_out", {"usd": "10"}, JAN, "2027-01-20")
        self.journal.update(unsent, NOT_SENT, result={"reason": "older version"})
        self.plan.start(2026, unsent, "fx_out", D(14_000), D(10), JAN)
        self.plan.start(2026, "no-such-request", "sell", D(7_000), D(5), JAN)
        self.journal.db.commit()
        self.assertEqual(sorted(self.diagnose()["auto_repair_pending"]["orphan_reservations"]),
                         sorted([unsent, "no-such-request"]))
        self.tick(at=10, may_trade=False)
        self.assertEqual(self.diagnose()["auto_repair_pending"]["orphan_reservations"], [])
        self.assertEqual(self.audit()["reservations_released"], 2)
        self.tick(at=20, may_trade=False)
        self.assertEqual(self.audit()["reservations_released"], 2)

    def test_unresolved_request_is_reported_and_never_settled_automatically(self):
        self.broker.usd = D(500)
        self.tick(may_trade=False)
        self.engine.reverse_fx_verified = True
        original = self.broker.exchange_to_krw

        def lost(usd, quote):
            original(usd, quote)
            raise TimeoutError()
        self.broker.exchange_to_krw = lost
        with self.assertRaises(UnknownOutcome):
            self.tick(at=10, may_trade=False, may_exchange=True)
        report = self.diagnose()
        self.assertFalse(report["clean"])
        self.assertEqual([(r["kind"], r["status"], r["unresolved"], r["tax_reservation"])
                          for r in report["open_requests"]], [("fx_out", "unknown", True, True)])
        self.assertFalse(any(report["auto_repair_pending"].values()))
        self.assertGreater(self.st()["in_progress_krw"], 0)                     # reservation kept until matched


class ExistingTaxTablesTests(ReserveBase):
    def test_old_external_funds_table_is_migrated_once_with_an_audit(self):
        path = self.path.with_name("old.sqlite")
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE tax_external_funds(year INTEGER PRIMARY KEY, planned_krw TEXT NOT NULL,
              planned_date TEXT NOT NULL, secured_krw TEXT NOT NULL DEFAULT '0', updated TEXT NOT NULL);
            CREATE TABLE tax_moves(seq INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, at TEXT NOT NULL,
              from_state TEXT NOT NULL, to_state TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL, ref TEXT);
            INSERT INTO tax_external_funds VALUES (2026, '500000', '2027-04-30', '300000', '2027-01-01');
            INSERT INTO tax_moves(year, at, from_state, to_state, amount, currency, ref) VALUES
              (2026, '2027-02-01', 'external_secured', 'reserve_krw', '100000', 'KRW', 'old'),
              (2026, '2027-02-02', 'external_secured', 'reserve_krw', '0.5', 'KRW', 'old'),
              (2026, '2027-03-01', 'outside', 'paid_krw', '50000', 'KRW', 'old');""")
        db.commit()
        db.close()
        journal = Journal(path)
        self.addCleanup(journal.db.close)
        self.assertEqual(books.diagnose(journal, JAN)["auto_repair_pending"]["external_funds_migration"], [2026])
        estimate = lambda y: {"estimated_tax_krw": D(1_000_000), "complete": True}
        TaxPlan(journal.db, {}, estimate, lambda: D(1400))
        TaxPlan(journal.db, {}, estimate, lambda: D(1400))                      # reopened: no second pass
        row = journal.db.execute("SELECT deposited_krw FROM tax_external_funds WHERE year=2026").fetchone()
        self.assertEqual(D(row[0]), D("100000.5"))
        report = books.diagnose(journal, JAN)
        self.assertEqual(report["auto_repair_pending"]["external_funds_migration"], [])
        self.assertEqual(report["repairs_recorded"]["corrections"], 1)
        self.assertEqual([i["kind"] for i in report["needs_user"]], ["payment_source_unknown"])
        self.assertFalse(report["clean"])

    def test_wrong_settlement_dates_are_found_then_repaired_once(self):
        tax.ensure_schema(self.journal.db)
        self.journal.db.execute(
            "INSERT INTO tax_fills VALUES ('k1','QQQ','sell','1','400','400',NULL,NULL,'2024-05-24T15:00:00+00:00',"
            "'2024-05-24','2024-05-28','computed T+1','1370','old','old','2024-05-24')")
        self.journal.db.commit()
        self.assertEqual(self.diagnose_settlement(), 1)
        self.assertEqual(tax.repair_settlements(self.journal.db, JAN), 1)
        self.assertEqual(self.diagnose_settlement(), 0)
        self.assertEqual(tax.repair_settlements(self.journal.db, JAN + timedelta(days=1)), 0)
        row = self.journal.db.execute("SELECT settle_date, fx FROM tax_fills").fetchone()
        self.assertEqual(tuple(row), ("2024-05-29", None))                             # T+2 era, Memorial Day skipped

    def diagnose_settlement(self):
        return books.diagnose(self.journal, JAN)["auto_repair_pending"]["settlement_dates"]


if __name__ == "__main__":
    unittest.main()
