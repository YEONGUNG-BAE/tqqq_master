"""Tax reserve installments, budget guards, reverse FX, withdrawals (stage 3).

Fake broker, fixed clocks, fixed estimates. Invariants: reserved money is never
converted or reinvested; one amount lives in one state; tax sells never exceed
the installment gap (plus at most one share) or the separately stored yearly
cap; without a cap only plans are recorded; strategy switches are never blocked
or delayed by tax work; nothing is counted twice across restarts or re-deliveries.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autotrader import telegram_bot
from autotrader.core import D, DONE, Engine, Journal, Settings, UnknownOutcome
from autotrader.tax_reserve import TaxPlan
from test_fault_injection import FaultBroker

JAN = datetime(2027, 1, 20, 14, 31, tzinfo=timezone.utc)          # round 1 of 5 elapsed (KST Jan 20/21)
BASE = dict(signal_date="2027-01-19", expected_signal_date="2027-01-19", session="2027-01-20")
TAX = D(1_000_000)                                                # estimated tax for 2026
PER_USD = D(1400) * (1 - D("0.03"))                               # conservative KRW per USD


class ReserveBase(unittest.TestCase):
    estimate = TAX

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "journal.sqlite"
        self.broker = FaultBroker(usd="0")
        self.broker.prices["TQQQ"] = D(70)
        self.blockers = []
        self.journal = None
        self.open()

    def tearDown(self):
        self.journal.db.close()

    def open(self, mode="paper", **cfg):
        if self.journal is not None:
            self.journal.db.close()
        self.journal = Journal(self.path)
        self.plan = TaxPlan(self.journal.db, cfg.pop("tax", {}),
                            lambda year: {"estimated_tax_krw": self.estimate, "complete": True},
                            lambda: D(1400))
        self.engine = Engine(self.broker, self.journal, Settings(mode=mode, **cfg))
        self.engine.tax_plan = self.plan
        self.engine.tax_blockers_fn = lambda year: list(self.blockers)
        self.journal.db.commit()

    restart = open

    def tick(self, at=0, **kw):
        args = dict(BASE, target="TQQQ", now=JAN + timedelta(seconds=at), may_trade=True)
        args.update(kw)
        return self.engine.tick(**args)

    def st(self, now=JAN):
        return self.plan.status(2026, now)

    def cap(self, amount):
        self.plan.set_cap(2026, amount, "user setting for test", "user", JAN)
        self.journal.db.commit()


class InstallmentTests(ReserveBase):
    def test_cumulative_target_and_no_pull_forward_from_a_large_deposit(self):
        self.broker.krw = D(50_000_000)                            # a big deposit during the prep period
        self.tick(may_trade=False, may_exchange=True)
        st = self.st()
        self.assertEqual(st["installment_target"], TAX / 5)
        self.assertEqual(st["reserve_krw"], TAX / 5)               # only this installment, not the whole tax
        fx = [c for c in self.broker.calls if c[0] == "fx"][0]
        self.assertEqual(fx[1], D(20_000_000))                     # the daily cap still applies to the rest
        self.assertLessEqual(fx[1], self.broker.krw + fx[1] - st["reserve_krw"])

    def test_external_funds_and_overdue_plans_recalculate_remaining_installments(self):
        self.plan.set_external(2026, D(600_000), "2027-02-28", JAN)
        self.assertEqual(self.st()["account_need"], D(400_000))
        self.assertEqual(self.st()["installment_target"], D(80_000))
        march = datetime(2027, 3, 20, 3, tzinfo=timezone.utc)
        self.assertTrue(self.st(march)["external"]["overdue"])      # planned date passed, nothing secured
        self.assertEqual(self.st(march)["account_need"], TAX)
        self.plan.set_external_secured(2026, D(300_000), march)
        self.assertEqual(self.st(march)["account_need"], D(700_000))
        self.assertEqual(self.st(march)["installment_target"], D(420_000))   # 3 of 5 rounds elapsed

    def test_missing_external_input_counts_as_zero(self):
        self.assertEqual(self.st()["external"]["valid"], 0)
        self.assertEqual(self.st()["account_need"], TAX)

    def test_early_securing_only_when_chosen(self):
        self.broker.krw = D(5_000_000)
        self.plan.set_early(2026, True)
        self.journal.db.commit()
        self.tick(may_trade=False)
        self.assertEqual(self.st()["reserve_krw"], TAX)


class ReinvestmentTests(ReserveBase):
    def test_reserves_are_neither_converted_nor_bought_with(self):
        self.broker.usd = D(1000)
        self.broker.krw = D(150_000)
        self.tick(may_trade=False, may_exchange=True)              # 150k KRW < 200k due: all reserved
        self.assertEqual([c for c in self.broker.calls if c[0] == "fx"], [])
        st = self.st()
        self.assertEqual(st["reserve_krw"], D(150_000))
        usd_reserved = st["reserve_usd"]
        self.assertGreater(usd_reserved, 0)                        # rest of the gap from idle USD
        self.tick(at=10)
        buy = self.broker.orders[max(self.broker.orders)]
        self.assertLessEqual(buy["qty"] * buy["price"] * (1 + D("0.0015")), D(1000) - usd_reserved)

    def test_usd_reserve_is_not_krw_ready(self):
        self.broker.usd = D(1000)
        self.tick(may_trade=False)
        st = self.st()
        self.assertEqual(st["reserve_krw"], 0)
        self.assertGreater(st["reserve_usd_krw"], 0)
        self.assertLess(st["reserve_usd_krw"], st["reserve_usd"] * 1400)   # conservative haircut


class TaxSellGuardTests(ReserveBase):
    def setUp(self):
        super().setUp()
        self.broker.positions = {"TQQQ": D(100)}

    def test_without_a_cap_only_a_plan_is_recorded_and_strategy_keeps_working(self):
        self.assertEqual(self.tick(), "idle: insufficient USD for one share")
        self.assertEqual(self.broker.calls, [])
        held = self.journal.meta(f"tax_plan_only:2026:{JAN.date()}")
        self.assertIn("no yearly tax-sell cap", held["reasons"][0])

    def test_sell_is_limited_to_the_installment_gap_plus_at_most_one_share(self):
        self.cap(TAX)
        self.assertEqual(self.tick(), "submitted: order")
        order = self.broker.orders[1]
        per_share = order["price"] * (1 - D("0.0015")) * PER_USD
        self.assertEqual(order["side"], "sell")
        self.assertGreaterEqual(order["qty"] * per_share, TAX / 5)
        self.assertLess(order["qty"] * per_share - TAX / 5, per_share)
        payload = json.loads(self.journal.rows()[0]["payload"])
        self.assertEqual(payload["tax_year"], 2026)
        self.assertIn("installment gap", payload["tax_basis"])

    def test_in_progress_sell_is_reserved_and_never_repeated_across_restarts(self):
        self.cap(TAX)
        self.tick()
        self.assertGreater(self.st()["in_progress_krw"], 0)
        self.restart()
        self.assertEqual(self.tick(at=30), "waiting: order fill")
        self.broker.fill(1)
        self.tick(at=40)
        st = self.st()
        self.assertEqual(st["in_progress_krw"], 0)
        self.assertGreater(st["reserve_usd"], 0)                   # proceeds became USD reserve once
        reserve = st["reserve_usd"]
        self.restart()
        self.tick(at=60)
        self.assertEqual(len([c for c in self.broker.calls if c[0] == "sell"]), 1)
        self.assertEqual(self.st()["reserve_usd"], reserve)

    def test_yearly_cap_is_enforced_and_survives_restart(self):
        self.cap(D(150_000))                                       # below the 200k installment
        self.tick()
        self.assertEqual(self.broker.calls, [])
        self.assertIn("cap would be exceeded",
                      self.journal.meta(f"tax_plan_only:2026:{JAN.date()}")["reasons"][0])
        self.restart()
        self.assertEqual(self.plan.cap(2026)["amount"], D(150_000))   # not raised by any estimate
        self.estimate = D(10_000_000)
        self.assertEqual(self.plan.cap(2026)["amount"], D(150_000))

    def test_round_limit_blocks_when_one_share_is_worth_more_than_the_gap_allows(self):
        self.cap(TAX * 10)
        self.broker.prices["TQQQ"] = D(5000)                       # one share >> the 200k gap
        self.tick()
        self.assertEqual(self.broker.calls, [])
        self.assertIn("more than one share",
                      self.journal.meta(f"tax_plan_only:2026:{JAN.date()}")["reasons"][0])

    def test_ledger_problems_block_tax_sells_only(self):
        self.cap(TAX)
        self.blockers = ["fee missing for x"]
        self.tick()
        self.assertEqual(self.broker.calls, [])
        # The same ledger problems never stop a strategy switch.
        self.assertEqual(self.tick(at=10, target="QQQ"), "submitted: order")
        self.assertEqual(self.broker.calls[-1][:2], ("sell", "TQQQ"))

    def test_switch_proceeds_are_earmarked_instead_of_a_separate_tax_sell(self):
        self.cap(TAX)
        self.broker.krw = D(0)
        self.tick(target="QQQ")                                    # risk-reduction switch: sell first
        self.broker.fill(1)
        self.tick(at=10, target="QQQ")
        st = self.st()
        self.assertGreater(st["reserve_usd"], 0)                   # gap taken from the sale proceeds
        self.tick(at=20, target="QQQ")
        sells = [c for c in self.broker.calls if c[0] == "sell"]
        self.assertEqual(len(sells), 1)                            # no extra tax sell
        buy = self.broker.orders[max(self.broker.orders)]
        self.assertEqual(buy["symbol"], "QQQ")
        proceeds = D(100) * D("69.93") * (1 - D("0.0015"))         # conservative sale proceeds
        self.assertLessEqual(buy["qty"] * buy["price"] * (1 + D("0.0015")), proceeds - st["reserve_usd"])

    def test_no_tax_sell_while_a_switch_is_still_open(self):
        self.cap(D(100_000_000))
        self.estimate = D(10_000_000)                              # gap far above the switch proceeds
        self.broker.positions = {"TQQQ": D(50), "QQQ": D(1)}       # return switch: QQQ -> TQQQ
        self.assertEqual(self.tick(), "submitted: order")
        self.assertEqual(self.broker.calls[-1][:2], ("sell", "QQQ"))
        self.broker.fill(1)
        self.tick(at=10)                                           # sale confirmed, proceeds earmarked
        result = self.tick(at=20)                                  # switch still open in this run
        self.assertNotEqual(self.broker.calls[-1][:2], ("sell", "TQQQ"))
        self.assertEqual(result, "idle: insufficient USD for one share")
        self.assertEqual(self.journal.meta("switch_history")[-1]["status"], "done")

    def test_deposit_is_secured_first_and_nothing_is_sold_in_the_same_run(self):
        self.cap(TAX)
        self.broker.krw = D(250_000)
        self.tick()
        self.assertEqual(self.st()["reserve_krw"], TAX / 5)
        self.assertEqual([c for c in self.broker.calls if c[0] == "sell"], [])


class ReverseFxTests(ReserveBase):
    def setUp(self):
        super().setUp()
        self.broker.usd = D(500)
        self.tick(may_trade=False)                                 # earmark USD reserve
        self.usd = self.st()["reserve_usd"]

    def test_unverified_reverse_fx_keeps_dollars_reserved_and_flags_the_need(self):
        self.tick(at=10, may_trade=False, may_exchange=True)
        self.assertEqual([c for c in self.broker.calls if c[0] == "fx_out"], [])
        self.assertEqual(self.journal.meta("reverse_fx_needed")["year"], 2026)

    def test_verified_reverse_fx_lost_reply_is_recovered_and_applied_once(self):
        self.engine.reverse_fx_verified = True
        original = self.broker.exchange_to_krw

        def lost(usd, quote):
            original(usd, quote)
            raise TimeoutError()
        self.broker.exchange_to_krw = lost
        with self.assertRaises(UnknownOutcome):
            self.tick(at=10, may_trade=False, may_exchange=True)
        self.broker.exchange_to_krw = original
        self.restart()
        self.engine.reverse_fx_verified = True
        self.assertEqual(self.tick(at=20, may_trade=False, may_exchange=True), "recovered: fx_out")
        self.tick(at=30, may_trade=False, may_exchange=True)
        st = self.st()
        self.assertEqual(st["reserve_usd"], 0)
        self.assertEqual(st["reserve_krw"], (self.usd * D(1400)).quantize(D(1)))
        self.tick(at=40, may_trade=False, may_exchange=True)
        self.assertEqual(len([c for c in self.broker.calls if c[0] == "fx_out"]), 1)
        self.assertEqual([c for c in self.broker.calls if c[0] == "fx"], [])    # KRW reserve not re-converted


class WithdrawalTests(ReserveBase):
    def setUp(self):
        super().setUp()
        self.plan._move(2026, JAN, "free_cash", "reserve_krw", D(400_000), "KRW", "test")
        self.journal.db.commit()

    def test_registered_withdrawal_moves_once_and_unrelated_ones_are_not_tax(self):
        rid = self.plan.request_withdrawal(2026, D(300_000), "2027-02-01", JAN)
        rows = [dict(tr_type_code="02", tr_amt="300000", tr_date="20270203", tran_srno="77", cncl_yn="N"),
                dict(tr_type_code="02", tr_amt="300000", tr_date="20270203", tran_srno="77", cncl_yn="N"),
                dict(tr_type_code="02", tr_amt="50000", tr_date="20270205", tran_srno="78", cncl_yn="N")]
        moved, unlinked = self.plan.match_withdrawals(rows, JAN)
        moved2, _ = self.plan.match_withdrawals(rows, JAN)          # re-read of the same ledger
        self.journal.db.commit()
        self.assertEqual((moved, moved2, unlinked), ([rid], [], ["78"]))
        st = self.st()
        self.assertEqual((st["reserve_krw"], st["withdrawn"]), (D(100_000), D(300_000)))
        # Withdrawn money still counts as secured: nothing is re-secured for it.
        self.assertEqual(st["shortfall"], TAX - D(400_000))

    def test_partial_payment_redeposit_and_correction(self):
        rid = self.plan.request_withdrawal(2026, D(300_000), "2027-02-01", JAN)
        self.plan.match_withdrawals([dict(tr_type_code="02", tr_amt="300000", tr_date="20270202",
                                          tran_srno="90", cncl_yn="N")], JAN)
        self.plan.record_payment(2026, D(200_000), JAN)
        st = self.st()
        self.assertEqual((st["withdrawn"], st["paid"]), (D(100_000), D(200_000)))
        self.plan.cancel_withdrawal(rid, JAN)                      # remainder re-deposited
        st = self.st()
        self.assertEqual((st["withdrawn"], st["reserve_krw"]), (D(0), D(200_000)))
        self.plan.correct_payment(2026, D(150_000), JAN)
        self.assertEqual(self.st()["paid"], D(150_000))
        moves = self.journal.db.execute("SELECT count(*) FROM tax_moves").fetchone()[0]
        self.assertGreaterEqual(moves, 5)                          # every move audited

    def test_release_only_by_explicit_user_command(self):
        self.plan.record_payment(2026, TAX, JAN, source="other")
        self.assertGreater(self.st()["reserve_krw"], 0)            # paid, but reserve still held
        self.plan.release(2026, JAN)
        self.assertEqual(self.st()["reserve_krw"], 0)
        self.assertEqual(self.plan.reserved(JAN), (D(0), D(0)))


class TelegramPlanCommandTests(ReserveBase):
    def setUp(self):
        super().setUp()
        self.sent, self.updates = [], []
        self.env = patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "7", "TELEGRAM_BOT_TOKEN": "123456:SECRET-T"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def transport(self, method, params):
        if method == "getUpdates":
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= params["offset"]]}
        self.sent.append(params["text"])
        return {"ok": True}

    def say(self, uid, text):
        self.updates.append({"update_id": uid, "message": {"chat": {"id": 7, "type": "private"},
                                                           "from": {"id": 7}, "text": text}})

    def poll(self):
        from autotrader import tax
        lines, now = telegram_bot.plan_lines(self.plan)
        now[0] = JAN
        return telegram_bot.poll(self.journal, tax.TaxSettings(), JAN, transport=self.transport,
                                 extra_lines=lines, handlers=telegram_bot.plan_handlers(self.plan))

    def test_plan_commands_apply_once_even_if_redelivered(self):
        self.say(1, "/tax_external 600,000 2027-03-31")
        self.say(2, "/tax_secured 250000")
        self.say(3, "/tax_paid 50000 기타")
        self.poll()
        self.poll()                                                # same updates offered again
        restarted = Journal(self.path)
        restarted.db.close()
        st = self.st()
        self.assertEqual((st["external"]["planned"], st["external"]["secured"], st["paid"]),
                         (D(600_000), D(250_000), D(50_000)))
        self.assertTrue(all("접수했습니다" in t for t in self.sent))
        self.assertEqual(len(self.sent), 3)

    def test_bad_input_changes_nothing_and_is_not_echoed(self):
        self.say(1, "/tax_external lots-of-money tomorrow")
        self.poll()
        self.assertIsNone(self.st()["external"]["date"])
        self.assertNotIn("lots-of-money", self.sent[0])

    def test_cap_command_keeps_history_and_tax_reply_shows_plan(self):
        self.say(1, "/tax_cap 300000")
        self.say(2, "/tax_cap 400000")
        self.say(3, "/tax")
        self.poll()
        rows = self.journal.db.execute("SELECT amount_krw FROM tax_caps ORDER BY seq").fetchall()
        self.assertEqual([r[0] for r in rows], ["300000", "400000"])
        self.assertIn("이번 회차 확보 필요액: ₩", self.sent[-1])
        self.assertIn("세금 매도 연간 상한: 설정됨", self.sent[-1])
        self.assertNotIn("SECRET-T", self.sent[-1])


class ReverseFxContractTests(unittest.TestCase):
    """USD->KRW request fields against the official portal export (when present)."""

    def test_reverse_fx_request_matches_the_spec(self):
        from test_schema_contract import SPEC, SchemaClient
        if not SPEC.exists():
            self.skipTest("official portal export not present; see README")
        from autotrader.meritz import Meritz
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "j.sqlite")
            try:
                client = SchemaClient({
                    "/forex/v1/rates/applied": dict(rsp_cd="0000", data=dict(
                        exrt_stnd_time="20270120100000", tltr_byrt="1400", tltr_slrt="1400",
                        tltr_aply_byrt="1400.7", tltr_aply_slrt="1399.3")),
                    "/forex/v1/exchanges": dict(rsp_cd="0000", data=dict(
                        tr_srno=1, tr_amt=139930, frcr_tr_amt="100.000", tfam_nwrm=139930))})
                broker = Meritz(client, journal)
                quote = broker.fx_quote_sell(JAN)
                self.assertEqual(quote.ask, D("1399.3"))                    # applied SELL rate
                receipt = broker.exchange_to_krw(D("100"), quote)
                body = client.requests[-1]["params"]
                self.assertEqual((body["mnec_clcd"], body["wncr_amt"], body["frcr_amt"], body["iqry_exrt"]),
                                 ("2", 0, "100.000", "1399.3"))
                self.assertEqual(receipt["krw"], "139930")
            finally:
                journal.db.close()


if __name__ == "__main__":
    unittest.main()
