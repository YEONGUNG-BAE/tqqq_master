"""Same-session recovery of a confirmed switch after the open window (stage 1).

Fake broker and fixed clocks only. Invariants: recovery uses only the switch's
own holdings and sale proceeds (never new deposits or idle cash), no duplicate
request, no order after the close buffer, pending confirmations survive nights
without being turned into locks, and session caps still hold.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrader.core import (D, Engine, Journal, Locked, PendingConfirmation, Refused, Settings, Stop)
from autotrader.schedule import Schedule
from autotrader.signal import DEFENSIVE_SYMBOL, RISK_SYMBOL
from test_doctor import Harness, holding
from test_fault_injection import FaultBroker

OPEN = datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc)
AFTER_WINDOW = OPEN + timedelta(minutes=16)
CUTOFF = datetime(2026, 10, 15, 19, 50, tzinfo=timezone.utc)      # 20:00 close - 10 min
BASE = dict(signal_date="2026-10-14", expected_signal_date="2026-10-14", session="2026-10-15")


class RecoveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "journal.sqlite"
        self.broker = FaultBroker(usd="0")
        self.broker.prices[DEFENSIVE_SYMBOL] = D(100)
        self.broker.prices[RISK_SYMBOL] = D(70)
        self.journal = None
        self.open()

    def tearDown(self):
        self.journal.db.close()

    def open(self, **cfg):
        if self.journal is not None:
            self.journal.db.close()
        self.journal = Journal(self.path)
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper", **cfg))

    restart = open

    def window(self, target, at=OPEN + timedelta(minutes=1), **kw):
        return self.engine.tick(**dict(BASE, target=target, now=at, may_trade=True,
                                       trade_deadline=OPEN + timedelta(minutes=15)), **kw)

    def recover(self, target, at, **kw):
        args = dict(BASE, target=target, now=at, may_trade=False, may_manage_orders=True,
                    may_recover=True, recovery_deadline=CUTOFF)
        args.update(kw)
        return self.engine.tick(**args)

    def orders(self, side=None):
        return [c for c in self.broker.calls if c[0] in (("buy", "sell") if side is None else (side,))]

    def intent(self):
        return self.journal.meta("switch_intent")

    def last_order(self):
        return self.broker.orders[max(self.broker.orders)]


class DefensiveAndReturnRecoveryTests(RecoveryBase):
    def test_open_window_outage_then_defensive_switch_completes_from_proceeds_only(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.broker.usd = D(500)                                   # idle cash / new deposit
        original = self.broker.quote
        self.broker.quote = lambda symbol, now: (_ for _ in ()).throw(Stop("Invalid or stale real-time bid/ask"))
        with self.assertRaisesRegex(Stop, "stale"):
            self.window(DEFENSIVE_SYMBOL)                           # outage inside the open window
        self.assertEqual(self.intent()["to"], DEFENSIVE_SYMBOL)    # the switch is recorded anyway
        self.broker.quote = original
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW), "submitted: order")
        self.assertEqual(self.orders()[-1], ("sell", RISK_SYMBOL, D(10)))
        self.broker.fill(max(self.broker.orders))
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=30)), "confirmed: filled")
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=60)), "submitted: order")
        buy = self.last_order()
        proceeds = D(10) * D("69.93") * (1 - D("0.0015"))
        self.assertEqual(buy["symbol"], DEFENSIVE_SYMBOL)
        self.assertLessEqual(buy["qty"] * buy["price"] * (1 + D("0.0015")), proceeds)   # not the 500 idle USD
        self.assertEqual(buy["qty"], D(6))
        self.broker.fill(max(self.broker.orders))
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=90))
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=120)), "idle: switch complete")
        self.assertEqual(self.journal.meta("switch_history")[-1]["completed_by"], "intraday recovery")
        self.assertGreater(self.broker.usd, D(500))                # idle cash untouched

    def test_return_switch_after_missing_the_whole_window(self):
        self.broker.positions = {DEFENSIVE_SYMBOL: D(10)}
        # No tick at all during the open window.
        self.assertEqual(self.recover(RISK_SYMBOL, AFTER_WINDOW), "submitted: order")
        self.assertEqual(self.orders()[-1], ("sell", DEFENSIVE_SYMBOL, D(10)))
        self.broker.fill(max(self.broker.orders))
        self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=30))
        self.assertEqual(self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=60)), "submitted: order")
        self.assertEqual(self.last_order()["symbol"], RISK_SYMBOL)
        events = self.intent()["events"]
        self.assertTrue(all(e["recovery"] for e in events))
        self.assertTrue(all(e.get("quoted_at") for e in events))

    def test_partial_fill_then_cancel_then_only_the_rest_is_sold(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW)
        self.broker.fill(1, 4)
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=30)), "waiting: order fill")
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=121)), "submitted: cancel")
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=125))
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=130)), "confirmed: cancelled")
        # Not fully filled: the retry waits for the recovery spacing.
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=140)),
                         "waiting: intraday recovery pacing")
        later = AFTER_WINDOW + timedelta(hours=1)
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, later), "submitted: order")
        self.assertEqual(self.orders("sell"), [("sell", RISK_SYMBOL, D(10)), ("sell", RISK_SYMBOL, D(6))])


class BlockedBuyAndCarryoverTests(RecoveryBase):
    def setUp(self):
        super().setUp()
        self.broker.positions = {DEFENSIVE_SYMBOL: D(10)}
        self.recover(RISK_SYMBOL, AFTER_WINDOW)
        self.broker.fill(1)
        self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=30))     # sell confirmed

    def test_refused_buy_backs_off_then_succeeds_once(self):
        self.broker.refuse = Refused("Order request not accepted by broker (code 3173)", "3173")
        with self.assertRaises(Refused):
            self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=60))
        self.broker.refuse = None
        self.assertTrue(self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=61)).startswith("waiting"))
        self.assertEqual(self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(hours=1)), "submitted: order")
        self.assertEqual(len(self.orders("buy")), 1)

    def test_proceeds_lag_carries_over_then_next_open_window_buys(self):
        self.broker.power = D(0)
        self.assertEqual(self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=60)),
                         "waiting: sale proceeds not yet orderable")
        result = self.engine.tick(**dict(BASE, target=RISK_SYMBOL, now=CUTOFF + timedelta(minutes=1),
                                         may_trade=False, may_manage_orders=True, window_over=True))
        self.assertEqual(result, "carried over: sale proceeds not yet orderable")
        self.broker.power = None
        self.restart()
        nxt = OPEN + timedelta(days=1, minutes=1)
        result = self.engine.tick(**dict(BASE, target=RISK_SYMBOL, now=nxt, session="2026-10-16", may_trade=True))
        self.assertEqual(result, "submitted: order")
        self.assertEqual(self.last_order()["symbol"], RISK_SYMBOL)

    def test_signal_back_to_defensive_supersedes_the_stale_return_buy(self):
        self.broker.power = D(0)
        self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=60))       # buy blocked: lag
        self.broker.power = None
        # Next session the confirmed close says defensive again.
        nxt = dict(BASE, session="2026-10-16", signal_date="2026-10-15", expected_signal_date="2026-10-15")
        self.engine.tick(**dict(nxt, target=DEFENSIVE_SYMBOL, now=OPEN + timedelta(days=1, minutes=16),
                                may_trade=False, may_manage_orders=True, may_recover=True,
                                recovery_deadline=CUTOFF + timedelta(days=1)))
        history = self.journal.meta("switch_history")
        self.assertEqual(history[-1]["status"], "superseded")
        self.assertEqual(self.intent()["to"], DEFENSIVE_SYMBOL)
        self.assertGreater(D(self.intent()["carried"]), 0)                    # switch money carried over
        self.assertEqual(self.orders("buy")[-1][1], DEFENSIVE_SYMBOL)         # never the stale TQQQ buy
        self.assertFalse(any(c[1] == RISK_SYMBOL for c in self.orders("buy")))

    def test_open_stale_buy_is_cancelled_before_following_the_new_target(self):
        self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=60))       # TQQQ buy open
        nxt = dict(BASE, session="2026-10-16", signal_date="2026-10-15", expected_signal_date="2026-10-15")
        result = self.engine.tick(**dict(nxt, target=DEFENSIVE_SYMBOL, now=AFTER_WINDOW + timedelta(seconds=90),
                                         may_trade=False, may_manage_orders=True, may_recover=True,
                                         recovery_deadline=CUTOFF))
        self.assertEqual(result, "submitted: cancel")


class DepositAndTaxBoundaryTests(RecoveryBase):
    def test_new_deposit_is_not_invested_after_the_open_window(self):
        self.broker.positions = {RISK_SYMBOL: D(5)}
        self.broker.usd = D(1000)                                  # aligned, fresh deposit
        self.assertEqual(self.recover(RISK_SYMBOL, AFTER_WINDOW), "idle: outside execution window")
        self.assertEqual(self.broker.calls, [])
        self.assertIsNone(self.intent())

    def test_switch_recovery_with_concurrent_deposit_buys_only_the_proceeds(self):
        self.broker.positions = {DEFENSIVE_SYMBOL: D(3)}
        self.recover(RISK_SYMBOL, AFTER_WINDOW)
        self.broker.fill(1)
        self.broker.usd += D(5000)                                 # deposit lands during recovery
        self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=30))
        self.recover(RISK_SYMBOL, AFTER_WINDOW + timedelta(seconds=60))
        buy = self.last_order()
        self.assertEqual(buy["symbol"], RISK_SYMBOL)
        self.assertLessEqual(buy["qty"] * buy["price"], D(3) * D("99.90"))


class CloseAndCapTests(RecoveryBase):
    def test_no_new_recovery_order_after_the_close_buffer(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.engine.clock = lambda: CUTOFF + timedelta(seconds=5)
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, CUTOFF - timedelta(seconds=5)),
                         "idle: recovery window closed during this run")
        self.assertEqual(self.broker.calls, [])

    def test_early_close_day_uses_the_calendar_close(self):
        s = Schedule(OPEN)
        start, cutoff = s.recovery_window(datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc), 10)
        self.assertEqual(cutoff, datetime(2026, 11, 27, 17, 50, tzinfo=timezone.utc))   # 13:00 ET close
        self.assertIsNone(s.recovery_window(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc), 10))

    def test_unconfirmed_cancel_at_the_close_survives_the_night_then_locks_only_in_a_later_session(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.recover(DEFENSIVE_SYMBOL, CUTOFF - timedelta(minutes=5))
        self.recover(DEFENSIVE_SYMBOL, CUTOFF - timedelta(minutes=2))          # timed out -> cancel
        def pending(receipt):
            raise PendingConfirmation("Historical order is not explicitly terminal; reconcile cancellation in account")
        self.broker.order_status = pending
        for hours, session in ((3, "KRW:2026-10-16"), (12, "KRW:2026-10-16"), (60, "KRW:2026-10-18")):
            with self.assertRaises(PendingConfirmation):
                self.engine.tick(**dict(BASE, target=DEFENSIVE_SYMBOL, now=CUTOFF + timedelta(hours=hours),
                                        session=session))
        self.assertIsNone(self.journal.meta("halt"))                             # nights and a weekend: no lock
        with self.assertRaises(Locked):
            self.engine.tick(**dict(BASE, target=DEFENSIVE_SYMBOL, now=OPEN + timedelta(days=4, minutes=5),
                                    session="2026-10-19"))
        self.assertEqual(self.journal.meta("lock_info")["code"], "order_never_terminal")
        self.assertEqual(len(self.orders("sell")), 1)                            # nothing resent meanwhile

    def test_session_cap_during_recovery_holds_and_reports(self):
        self.open(max_orders_per_session=1)
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW)
        self.broker.fill(1, 3)
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=121))
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=125))
        self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(seconds=130))
        with self.assertRaisesRegex(Stop, "Order count limit reached during intraday switch recovery"):
            self.recover(DEFENSIVE_SYMBOL, AFTER_WINDOW + timedelta(hours=2))

    def test_restart_near_the_close_continues_without_duplicates(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.recover(DEFENSIVE_SYMBOL, CUTOFF - timedelta(minutes=20))
        self.restart()
        self.assertEqual(self.recover(DEFENSIVE_SYMBOL, CUTOFF - timedelta(minutes=19)), "waiting: order fill")
        self.assertEqual(len(self.orders()), 1)
        self.assertEqual(self.intent()["to"], DEFENSIVE_SYMBOL)


class CliRecoveryTests(Harness):
    def test_run_after_the_open_window_plans_switch_recovery(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from autotrader import __main__ as cli
        from autotrader.meritz import Client
        from test_doctor import FakeMeritz, SECRET_ENV
        root = self.state / "cli"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"state_dir": "state_observe", "notifications": False,
                                                       "account_lock_dir": str(self.state / "locks"),
                                                       "execution": {"mode": "observe"}}))
        (root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(root / "secrets.env", 0o600)
        now = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)                # mid-session, after the window
        fake = FakeMeritz(now, **{"/accounts/v1/me/overseas/holdings":
                                  dict(rsp_cd="5766", data=[holding(DEFENSIVE_SYMBOL, 2)])})
        out = io.StringIO()
        with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)), \
             patch.object(cli, "datetime", wraps=datetime) as clock, \
             patch("sys.argv", ["autotrader", "run", "--config", str(root / "config.json")]), redirect_stdout(out):
            clock.now.return_value = now
            code = cli.main()
        self.assertEqual((code, json.loads(out.getvalue())["result"]), (0, "observe: order"))
        j = Journal(root / "state_observe/journal.sqlite")
        try:
            plan = json.loads(j.db.execute("SELECT body FROM events WHERE category='plan'").fetchone()[0])
            self.assertEqual((plan["symbol"], plan["side"], plan["recovery"]), (DEFENSIVE_SYMBOL, "sell", True))
            self.assertEqual(j.rows(), [])
        finally:
            j.db.close()


if __name__ == "__main__":
    unittest.main()
