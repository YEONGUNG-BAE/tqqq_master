"""Tax plan unavailable: the engine still settles its own requests and may send
a confirmed TQQQ exit sell (it spends no money); new buys, FX, return-switch
sells and tax work are held because the reserves cannot be checked. Locks and
unknown-outcome rules are unchanged. Fake broker, fixed clocks; no network."""
import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

from autotrader.core import D, Engine, Journal, Locked, Stop
from autotrader.signal import DEFENSIVE_SYMBOL, RISK_SYMBOL
from test_doctor import Harness, KRW_MORNING, SECRET_ENV, FakeMeritz
from test_intraday_recovery import AFTER_WINDOW, OPEN, RecoveryBase
from test_tax_reserve import JAN, TAX, ReserveBase

DOWN = "tax plan unavailable (OperationalError)"


class EngineTests(RecoveryBase):
    def setUp(self):
        super().setUp()
        self.engine.tax_unavailable = DOWN

    def test_confirmed_tqqq_exit_sell_goes_out_and_the_buy_is_held(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.assertEqual(self.window(DEFENSIVE_SYMBOL), "submitted: order")
        self.assertEqual(self.orders(), [("sell", RISK_SYMBOL, D(10))])
        self.broker.fill(max(self.broker.orders))
        self.assertEqual(self.window(DEFENSIVE_SYMBOL, at=OPEN + timedelta(minutes=2)), "confirmed: filled")
        with self.assertRaisesRegex(Stop, "New buys and FX held"):
            self.window(DEFENSIVE_SYMBOL, at=OPEN + timedelta(minutes=3))
        self.assertEqual(self.orders("buy"), [])

    def test_intraday_recovery_sells_the_risk_symbol_but_holds_the_buy(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW), "submitted: order")
        self.broker.fill(max(self.broker.orders))
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=30))
        with self.assertRaisesRegex(Stop, "New buys and FX held"):
            self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=60))
        self.assertEqual(self.orders("buy"), [])

    def test_return_switch_sell_and_new_deposit_fx_are_held(self):
        self.broker.positions = {DEFENSIVE_SYMBOL: D(10)}
        self.broker.krw = D(1_000_000)
        with self.assertRaisesRegex(Stop, "New buys and FX held"):
            self.window(RISK_SYMBOL, may_exchange=True)
        self.assertEqual(self.broker.calls, [])
        with self.assertRaisesRegex(Stop, "New buys and FX held"):
            self.recover(RISK_SYMBOL, AFTER_WINDOW)
        self.assertEqual(self.broker.calls, [])

    def test_order_sent_before_the_outage_is_still_settled(self):
        self.engine.tax_unavailable = None
        self.broker.usd = D(1000)
        self.assertEqual(self.window(RISK_SYMBOL), "submitted: order")      # plain buy, plan healthy
        self.engine.tax_unavailable = DOWN
        self.broker.fill(max(self.broker.orders))
        self.assertEqual(self.window(RISK_SYMBOL, at=OPEN + timedelta(minutes=2)), "confirmed: filled")
        self.assertEqual([r["status"] for r in self.journal.rows()], ["done"])

    def test_locks_still_stop_everything(self):
        self.journal.put_meta("halt", "locked for test")
        self.broker.positions = {RISK_SYMBOL: D(10)}
        with self.assertRaises(Locked):
            self.window(DEFENSIVE_SYMBOL)
        self.assertEqual(self.broker.calls, [])


class TaxOrderDuringOutageTests(ReserveBase):
    def test_tax_sell_in_flight_is_settled_and_booked_once_the_plan_returns(self):
        self.broker.positions = {RISK_SYMBOL: D(100)}
        self.cap(TAX)
        self.assertEqual(self.tick(), "submitted: order")                     # tax sell, plan healthy
        plan = self.engine.tax_plan
        self.engine.tax_plan, self.engine.tax_unavailable = None, DOWN         # tax tables become unusable
        self.broker.fill(max(self.broker.orders))
        self.assertEqual(self.tick(at=10), "confirmed: filled")                # own order settled
        self.assertEqual([r["status"] for r in self.journal.rows()], ["done"])
        with self.assertRaisesRegex(Stop, "New buys and FX held"):
            self.tick(at=15)                                                    # nothing new while down
        self.assertGreater(self.st()["in_progress_krw"], 0)                     # booking waits for the plan
        self.engine.tax_plan, self.engine.tax_unavailable = plan, None
        self.tick(at=20, may_trade=False)
        st = self.st()
        self.assertEqual(st["in_progress_krw"], 0)
        self.assertGreater(st["reserve_usd"], 0)
        self.assertEqual(len([c for c in self.broker.calls if c[0] == "sell"]), 1)


class ReserveFromProceedsAfterOutageTests(ReserveBase):
    """Outage -> exit sell -> plan back -> reserve taken from the proceeds ->
    switch finishes -> a later intraday deposit buys nothing."""

    def recover(self, at, **kw):
        return self.tick(at=at, target=DEFENSIVE_SYMBOL, may_trade=False, may_manage_orders=True, may_recover=True,
                         recovery_deadline=JAN + timedelta(hours=5), **kw)

    def buys(self):
        return [c for c in self.broker.calls if c[0] == "buy"]

    def test_reserve_taken_from_switch_proceeds_comes_out_of_the_switch_budget_once(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.broker.prices[DEFENSIVE_SYMBOL] = D(100)
        plan = self.engine.tax_plan
        self.engine.tax_plan, self.engine.tax_unavailable = None, DOWN
        self.assertEqual(self.tick(target=DEFENSIVE_SYMBOL), "submitted: order")          # exit sell
        self.broker.fill(max(self.broker.orders))
        self.assertEqual(self.tick(at=10, target=DEFENSIVE_SYMBOL), "confirmed: filled")
        self.engine.tax_plan, self.engine.tax_unavailable = plan, None                     # plan back
        self.assertEqual(self.recover(1200), "submitted: order")
        reserve = self.st()["reserve_usd"]
        self.assertGreater(reserve, 0)                                                     # installment from proceeds
        intent = self.journal.meta("switch_intent")
        proceeds = D(10) * D("69.93") * (1 - D("0.0015"))                                  # conservative estimate
        to_tax = D(intent["to_tax"])
        # Cash beyond the conservative estimate (outside the budget) is used first,
        # the rest of the installment comes out of the proceeds and the budget.
        self.assertLess(reserve - to_tax, D(1))
        self.assertGreater(to_tax, 0)
        self.assertEqual(Engine._budget(self.engine, intent), proceeds - to_tax)
        buy = self.broker.orders[max(self.broker.orders)]
        self.assertLessEqual(buy["qty"] * buy["price"] * (1 + D("0.0015")), proceeds - to_tax)
        self.broker.fill(max(self.broker.orders))
        self.recover(1230)
        self.assertEqual(self.recover(1260), "idle: switch complete")
        done = self.st()["reserve_usd"]
        self.restart()                                                                     # counted once
        self.recover(1290)
        self.assertEqual(self.st()["reserve_usd"], done)
        self.assertEqual(self.journal.meta("switch_history")[-1]["to_tax"], str(to_tax))
        from_proceeds = self.journal.db.execute(
            "SELECT amount FROM tax_moves WHERE from_state='switch_proceeds'").fetchall()
        self.assertEqual(sum(D(a) for (a,) in from_proceeds), to_tax)
        self.broker.usd += D(100)                                                          # later deposit
        self.recover(1500)
        self.assertEqual(len(self.buys()), 1)                                              # no recovery buy
        self.assertEqual(self.journal.meta("switch_history")[-1]["to_tax"], str(to_tax))   # switch books unchanged
        self.assertLess(self.st()["reserve_usd"] - done, D("0.05"))                        # at most a rounding cent

    def test_failure_between_reserve_move_and_switch_books_rolls_both_back(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.broker.prices[DEFENSIVE_SYMBOL] = D(100)
        plan = self.engine.tax_plan
        self.engine.tax_plan, self.engine.tax_unavailable = None, DOWN
        self.tick(target=DEFENSIVE_SYMBOL)
        self.broker.fill(max(self.broker.orders))
        self.tick(at=10, target=DEFENSIVE_SYMBOL)
        self.engine.tax_plan, self.engine.tax_unavailable = plan, None
        original, failed = Engine._save_intent, []

        def crash_once(engine, intent):
            if "to_tax" in intent and not failed:
                failed.append(1)
                raise RuntimeError("crash between the two writes")
            return original(engine, intent)
        with patch.object(Engine, "_save_intent", crash_once), self.assertRaises(RuntimeError):
            self.recover(1200)
        self.journal.event("error", {"message": "Internal error: RuntimeError"}, JAN)    # as the CLI records it
        self.restart()
        self.assertEqual(self.journal.db.execute(
            "SELECT count(*) FROM tax_moves WHERE from_state='switch_proceeds'").fetchone()[0], 0)
        self.assertNotIn("to_tax", self.journal.meta("switch_intent"))
        self.assertEqual(self.recover(1230), "submitted: order")                          # applied once now
        intent = self.journal.meta("switch_intent")
        moved = self.journal.db.execute("SELECT amount FROM tax_moves WHERE from_state='switch_proceeds'").fetchall()
        self.assertEqual([D(a) for (a,) in moved], [D(intent["to_tax"])])


class CliTests(Harness):
    def run_cli(self, plan_error=None, halt=None):
        from autotrader import __main__ as cli
        from autotrader.meritz import Client
        root = self.state / "cli"
        root.mkdir(exist_ok=True)
        (root / "config.json").write_text(json.dumps({"state_dir": "state_observe", "notifications": False,
                                                       "account_lock_dir": str(self.state / "locks"),
                                                       "execution": {"mode": "observe"}}))
        (root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(root / "secrets.env", 0o600)
        if halt:
            (root / "state_observe").mkdir(exist_ok=True)
            j = Journal(root / "state_observe/journal.sqlite")
            j.put_meta("halt", halt)
            j.db.close()
        fake = FakeMeritz(KRW_MORNING)
        calls = []
        original = Engine.tick

        def counted(engine, **kw):
            calls.append(engine.tax_unavailable if hasattr(engine, "tax_unavailable") else None)
            return original(engine, **kw)
        real_plan = cli.make_plan

        def failing(journal, cfg, now):
            if plan_error is not None and not calls:                     # the trading path's call only
                raise plan_error
            return real_plan(journal, cfg, now)
        out = io.StringIO()
        with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)), \
             patch.object(Engine, "tick", counted), patch.object(cli, "make_plan", failing), \
             patch.object(cli, "datetime", wraps=datetime) as clock, \
             patch("sys.argv", ["autotrader", "run", "--config", str(root / "config.json")]), redirect_stdout(out):
            clock.now.return_value = KRW_MORNING
            code = cli.main()
        text, objects, decoder = out.getvalue().strip(), [], json.JSONDecoder()
        while text:
            obj, end = decoder.raw_decode(text)
            objects.append(obj)
            text = text[end:].strip()
        return code, objects[-1], calls, Journal(root / "state_observe/journal.sqlite")

    def test_healthy_plan_baseline(self):
        code, last, calls, j = self.run_cli()
        j.db.close()
        self.assertEqual((code, last.get("result"), len(calls)), (0, "observe: fx", 1))

    def test_tax_plan_failure_still_runs_the_engine_and_ends_as_a_hold(self):
        code, last, calls, j = self.run_cli(plan_error=sqlite3.OperationalError("no such column: x"))
        try:
            self.assertEqual(len(calls), 1)                                    # engine ran
            self.assertEqual(code, 1)
            self.assertIn("New buys and FX held", last["error"])               # no FX in this run
            self.assertNotIn("no such column", last["error"])                  # raw error text not published
            self.assertIsNone(j.meta("halt"))                                  # a hold, not a lock
            self.assertIn("New buys and FX held", j.meta("active_hold")["message"])
            kinds = [r[0] for r in j.db.execute("SELECT category FROM events")]
            self.assertIn("tax_plan_failed", kinds)
        finally:
            j.db.close()

    def test_tax_plan_failure_under_a_lock_stays_a_lock(self):
        code, last, calls, j = self.run_cli(plan_error=sqlite3.OperationalError("boom"), halt="locked for test")
        try:
            self.assertEqual(code, 1)
            self.assertEqual(j.meta("halt"), "locked for test")
        finally:
            j.db.close()


if __name__ == "__main__":
    unittest.main()
