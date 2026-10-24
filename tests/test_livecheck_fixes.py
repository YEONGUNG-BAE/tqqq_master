"""Live-check follow-ups: account-wide ownership across state folders, a stop
that reaches a running executor, tax outage not blocking reconciliation, and
read helpers that never commit the caller's transaction. Fake broker / fake
HTTP, temporary DBs, fixed clocks; no real request is sent."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from autotrader import __main__ as cli
from autotrader import livecheck
from autotrader.core import D, Engine, Journal, Settings, Stop
from autotrader.signal import RISK_SYMBOL
from test_fault_injection import FaultBroker
from test_livecheck import DEADLINE, OPEN, PROTECTED, SESSION, Base


class OwnershipTests(unittest.TestCase):
    """Two state folders on one account share the marker next to the account lock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / "account.livecheck"
        self.settings = Settings(mode="observe", reservation_lookup_start="20200101")
        self.side = {}
        for name in ("A", "B"):
            broker = FaultBroker(usd="1000")
            broker.positions = dict(PROTECTED)
            journal = Journal(Path(self.tmp.name) / f"{name}.sqlite")
            self.addCleanup(journal.db.close)
            livecheck.ensure_schema(journal.db)
            plan, reasons = livecheck.create_plan(journal, broker, self.settings, budget="200", now=OPEN,
                                                  tax_reserved_usd=D(0), state_dir=name)
            self.assertEqual(reasons, [])
            self.side[name] = (journal, broker, plan["plan_id"])

    def tick(self, name, at=0, marker=True):
        journal, broker, pid = self.side[name]
        return livecheck.tick(Engine(broker, journal, self.settings), journal, pid, OPEN + timedelta(seconds=at),
                              may_trade=True, may_manage=True, deadline=DEADLINE, session=SESSION,
                              tax_reserved_usd=D(0), marker=self.marker if marker else None)

    def test_a_second_state_folder_cannot_start_a_check_on_the_same_account(self):
        a, b = self.side["A"], self.side["B"]
        livecheck.start_checked(a[0], a[1], a[2], OPEN, marker=self.marker, state_dir="A")
        with self.assertRaisesRegex(Stop, "owns this account"):
            livecheck.start_checked(b[0], b[1], b[2], OPEN, marker=self.marker, state_dir="B")
        self.assertEqual(livecheck._row(b[0], b[2])["status"], "planned")
        self.assertEqual(json.loads(self.marker.read_text())["plan_id"], a[2])
        self.assertEqual(b[1].calls, [])

    def test_running_without_the_marker_reclaims_it_before_any_order_or_refuses(self):
        a, b = self.side["A"], self.side["B"]
        livecheck.start(a[0], a[2], OPEN)                                     # crash before the marker was written
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.tick("A"), "submitted: order")
        self.assertEqual(json.loads(self.marker.read_text())["plan_id"], a[2])
        livecheck.start(b[0], b[2], OPEN)                                     # B forced to running in its own DB
        with self.assertRaisesRegex(Stop, "owns this account"):
            self.tick("B")
        self.assertEqual(b[1].calls, [])

    def test_a_marker_left_by_a_finished_or_unstarted_plan_is_released(self):
        a, b = self.side["A"], self.side["B"]
        livecheck.claim_marker(self.marker, a[2], "A", a[0])                  # crash right after claiming
        self.assertIsNone(livecheck.blocking(a[0], self.marker))             # nothing was sent: no pause
        self.assertFalse(self.marker.exists())
        livecheck.start_checked(b[0], b[1], b[2], OPEN, marker=self.marker, state_dir="B")
        self.assertEqual(json.loads(self.marker.read_text())["plan_id"], b[2])


class ReadHelperTests(Base):
    def test_reading_a_plan_inside_a_transaction_never_commits_it(self):
        plan, _ = self.plan()
        try:
            with self.journal.atomic():
                self.journal.put_meta("probe", 1)
                livecheck._row(self.journal, plan["plan_id"])
                livecheck.blocking(self.journal)
                livecheck.ledger(self.journal, plan)
                raise RuntimeError("roll back")
        except RuntimeError:
            pass
        self.assertIsNone(self.journal.meta("probe"))

    def test_prepared_hook_failure_rolls_back_the_intent_and_evidence(self):
        self.broker.usd = D("160")
        self.started("150")
        for at in (0, 5, 10, 15):
            self.tick(at)
            for number, order in list(self.broker.orders.items()):
                if order["state"] == "open":
                    self.broker.fill(number)
        rows = len(self.journal.rows())
        with patch.object(livecheck, "_store_evidence", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                self.tick(20)                                                 # SGOV buy prepared, then fails
        self.assertEqual(len(self.journal.rows()), rows)                     # no orphan intent row
        self.assertEqual(livecheck._row(self.journal, self.pid)["evidence"], {})
        self.assertEqual(len([c for c in self.broker.calls if c[0] == "buy"]), 1)


class TaxOutageTests(Base):
    def test_unknown_tax_reserve_still_settles_own_orders_and_holds_only_buys(self):
        self.started()
        self.tick(0)                                                          # TQQQ buy open
        self.broker.fill(max(self.broker.orders))
        result = livecheck.tick(self.engine, self.journal, self.pid, OPEN + timedelta(seconds=5), may_trade=True,
                                may_manage=True, deadline=DEADLINE, session=SESSION, tax_reserved_usd=None)
        self.assertEqual(result, "confirmed: filled")                        # reconciled despite the outage
        result = livecheck.tick(self.engine, self.journal, self.pid, OPEN + timedelta(seconds=10), may_trade=True,
                                may_manage=True, deadline=DEADLINE, session=SESSION, tax_reserved_usd=None)
        self.assertEqual(result, "submitted: order")                         # the check's own sell spends nothing
        self.broker.fill(max(self.broker.orders))
        for at in (15,):
            livecheck.tick(self.engine, self.journal, self.pid, OPEN + timedelta(seconds=at), may_trade=True,
                           may_manage=True, deadline=DEADLINE, session=SESSION, tax_reserved_usd=None)
        with self.assertRaisesRegex(Stop, "tax reserves cannot be checked"):
            livecheck.tick(self.engine, self.journal, self.pid, OPEN + timedelta(seconds=20), may_trade=True,
                           may_manage=True, deadline=DEADLINE, session=SESSION, tax_reserved_usd=None)
        self.assertEqual([c[0] for c in self.broker.calls if c[0] in ("buy", "sell")], ["buy", "sell"])


class CliStopTests(unittest.TestCase):
    def setUp(self):
        from test_doctor import SECRET_ENV
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config.json").write_text(json.dumps({
            "state_dir": "state_observe", "notifications": False, "account_lock_dir": str(self.root / "locks"),
            "execution": {"mode": "observe", "reservation_lookup_start": "20200101",
                          "instrument_markets": {"SGOV": "AX"}}}))
        (self.root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(self.root / "secrets.env", 0o600)
        from autotrader.meritz import KST
        from test_doctor import US_OPEN, holding
        from test_sgov import OrderFake
        self.now = US_OPEN
        self.fake = OrderFake(US_OPEN, datetime.now(KST).strftime("%Y%m%d"))
        self.fake.responses["/accounts/v1/me/overseas/holdings"] = dict(rsp_cd="5766", data=[
            holding("NDQX", 7), holding("BRK.B", 3)])
        original = self.fake.__call__

        def call(method, path, query=None, body=None, **kw):
            if method == "POST" and path == "/trading/v1/overseas/orders/cancel":
                self.fake.calls.append((method, path))
                self.fake.bodies.append((path, dict(body)))
                row = next(r for r in self.fake.ledger if r["oder_no"] == str(body["orgl_oder_no"]))
                row.update(ovrs_stck_oder_stat_cls_code="3", cncl_cnfr_qty=row["odqt"])
                return dict(rsp_cd="0001", data=dict(warn_cls_code="0", oder_no=str(900 + len(self.fake.bodies))))
            return original(method, path, query, body, **kw)
        self.call = call

    def cli(self, *argv, ticks=1, during_sleep=None, tax_error=False):
        from autotrader.meritz import Client
        out = io.StringIO()
        real_plan = cli.make_plan

        def make_plan(journal, cfg, now):
            if tax_error:
                raise RuntimeError("tax tables unusable")
            return real_plan(journal, cfg, now)
        with patch.object(Client, "_http", lambda _s, *a, **k: self.call(*a, **k)), \
             patch("autotrader.meritz.urlopen", side_effect=AssertionError("network forbidden")), \
             patch.object(cli, "datetime", wraps=datetime) as clock, \
             patch.object(cli, "live_check_sleep", side_effect=during_sleep or (lambda s: None)), \
             patch.object(cli, "LIVE_CHECK_MAX_TICKS", ticks), patch.object(cli, "make_plan", make_plan), \
             patch("sys.argv", ["autotrader", "live-check", *argv, "--config", str(self.root / "config.json")]), \
             redirect_stdout(out):
            clock.now.return_value = self.now
            code = cli.main()
        text, objects, decoder = out.getvalue().strip(), [], json.JSONDecoder()
        while text:
            obj, end = decoder.raw_decode(text)
            objects.append(obj)
            text = text[end:].strip()
        return code, objects[-1]

    def cancels(self):
        return [b for p, b in self.fake.bodies if p.endswith("/cancel")]

    def test_stop_reaches_a_running_executor_which_cancels_its_open_order(self):
        _, out = self.cli("plan", "--budget-usd", "200")
        pid = out["plan"]["plan_id"]
        stopped = []

        def stop_from_another_terminal(seconds):
            if not stopped:
                code, report = self.cli("stop", "--plan", pid)               # executor still holds the locks
                stopped.append((code, report["stop"]["state"]))
        code, report = self.cli("execute", "--plan", pid, "--confirm-live", ticks=6,
                                during_sleep=stop_from_another_terminal)
        self.assertEqual(stopped, [(0, "requested: own open order not yet cancelled")])
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["stop"]["state"], "done: no open order, no test shares")

    def test_stop_without_an_executor_records_the_request_and_says_cancel_is_not_sent(self):
        _, out = self.cli("plan", "--budget-usd", "200")
        pid = out["plan"]["plan_id"]
        self.cli("execute", "--plan", pid, "--confirm-live")                 # one buy left open
        code, report = self.cli("stop", "--plan", pid)
        self.assertEqual((code, report["status"]), (0, "stopping"))
        self.assertEqual(report["stop"]["state"], "requested: own open order not yet cancelled")
        self.assertEqual(self.cancels(), [])
        code, report = self.cli("stop", "--plan", pid, "--confirm-live", ticks=4)   # no executor: stop cancels
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual(report["status"], "stopped")

    def test_resume_during_a_tax_outage_still_reconciles_the_open_order(self):
        _, out = self.cli("plan", "--budget-usd", "200")
        pid = out["plan"]["plan_id"]
        self.cli("execute", "--plan", pid, "--confirm-live")
        reads = len(self.fake.calls)
        code, report = self.cli("resume", "--plan", pid, "--confirm-live", tax_error=True)
        self.assertIn("/trading/v1/overseas/orders/today", [p for _, p in self.fake.calls[reads:]])
        self.assertEqual(report["run"][0]["result"], "waiting: order fill")
        self.assertEqual(len([b for p, b in self.fake.bodies if not p.endswith("/cancel")]), 1)


if __name__ == "__main__":
    unittest.main()


START_CHILD = r'''
import json, os, sys
sys.path[:0] = [{root!r}, {tests!r}]
from datetime import datetime
from autotrader import livecheck
from autotrader.core import Journal
spec = json.loads(sys.argv[1])
journal = Journal(spec["db"])
original = journal.event
def killer(category, *a, **k):
    result = original(category, *a, **k)
    if category == "live_check_started":
        os._exit(3)                      # marker claimed, running status not committed
    return result
journal.event = killer
livecheck.start(journal, spec["plan"], datetime.fromisoformat(spec["now"]), marker=spec["marker"], state_dir="A")
'''


class StartCrashTests(unittest.TestCase):
    def test_kill_between_claiming_the_account_and_marking_running_recovers(self):
        import subprocess
        import sys
        from test_livecheck import ROOT
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "account.livecheck"
            journal = Journal(Path(tmp) / "j.sqlite")
            broker = FaultBroker(usd="1000")
            broker.positions = dict(PROTECTED)
            settings = Settings(mode="observe", reservation_lookup_start="20200101")
            livecheck.ensure_schema(journal.db)
            plan, _ = livecheck.create_plan(journal, broker, settings, budget="200", now=OPEN, tax_reserved_usd=D(0))
            journal.db.close()
            result = subprocess.run([sys.executable, "-W", "ignore", "-c",
                                     START_CHILD.format(root=str(ROOT), tests=str(ROOT / "tests")),
                                     json.dumps({"db": str(Path(tmp) / "j.sqlite"), "plan": plan["plan_id"],
                                                 "marker": str(marker), "now": OPEN.isoformat()})],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 3, result.stderr[-2000:])
            self.assertTrue(marker.exists())
            journal = Journal(Path(tmp) / "j.sqlite")
            try:
                self.assertEqual(livecheck._row(journal, plan["plan_id"])["status"], "planned")
                self.assertIsNone(livecheck.blocking(journal, marker))         # nothing was sent: released
                livecheck.start_checked(journal, broker, plan["plan_id"], OPEN, marker=marker, state_dir="A")
                self.assertEqual(livecheck._row(journal, plan["plan_id"])["status"], "running")
                self.assertEqual(json.loads(marker.read_text())["plan_id"], plan["plan_id"])
                self.assertEqual(broker.calls, [])
            finally:
                journal.db.close()
