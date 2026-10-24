"""Operations: switch progress apart from liveness, delay alerts, read-only
Telegram status, late notifications, monitor fallback and service status.
Fake brokers / channels, temporary DBs, real exchange calendars, fixed clocks.
No real request, reboot, network cut or service install happens here."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader import heartbeat, ops, progress, telegram_bot, tax
from autotrader.core import D, Engine, Journal, Settings, Stop, UnknownOutcome
from autotrader.notify import Notifier, mark_resolved
from autotrader.schedule import Schedule
from autotrader.signal import DEFENSIVE_SYMBOL, RISK_SYMBOL

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
OPEN = datetime(2026, 10, 15, 13, 30, tzinfo=UTC)              # EDT: open 13:30 UTC, window ends 13:45
SCHEDULES = {}


def schedule(now):
    key = now.date()
    if key not in SCHEDULES:
        SCHEDULES[key] = Schedule(now)
    return SCHEDULES[key]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "j.sqlite"
        self.sent, self.down = [], False
        self.open()

    def channel(self, text):
        if self.down:
            raise OSError("telegram down")
        self.sent.append(text)

    def open(self):
        if getattr(self, "journal", None) is not None:
            self.journal.db.close()
        self.journal = Journal(self.path)
        self.addCleanup(lambda: self.journal.db.close())
        self.notifier = Notifier(self.journal, channels={"telegram": self.channel})

    def intent(self, to=DEFENSIVE_SYMBOL, status="selling", session="2026-10-15", at=OPEN + timedelta(minutes=1)):
        self.journal.put_meta("switch_intent", {"id": "i1", "to": to, "status": status, "from": {RISK_SYMBOL: "10"},
                                                "created_session": session, "created_at": at.isoformat(),
                                                "signal_date": "2026-10-14", "proceeds": "698.25", "carried": "0",
                                                "spent": "0", "counted": [], "events": [], "recovery_orders": 0})

    def observe(self, now, message="waiting: order fill", mode="paper"):
        return progress.observe(self.journal, self.notifier, schedule(now), now, message, mode=mode,
                                recovery_buffer_minutes=10)

    def alerts(self):
        return [t for t in self.sent if t.startswith("[전환]")]


class DelayAlertTests(Base):
    def test_waiting_with_healthy_runs_raises_the_early_then_the_stall_alert(self):
        self.intent()
        for minutes in (5, 14, 24):                                    # runs and reads succeed, nothing moves
            self.observe(OPEN + timedelta(minutes=minutes))
        self.assertEqual(self.alerts(), [])
        self.observe(OPEN + timedelta(minutes=25))                     # window end + 10 min
        self.assertEqual(len(self.alerts()), 1)
        self.assertIn("TQQQ 이탈 매도 지연", self.alerts()[0])
        self.assertIn("긴급도 높음", self.alerts()[0])
        self.observe(OPEN + timedelta(minutes=30))
        self.assertEqual(len(self.alerts()), 1)                        # same incident: no repeat
        self.observe(OPEN + timedelta(minutes=35))
        self.assertEqual(len(self.alerts()), 2)
        self.assertIn("실제 진척", self.alerts()[1])
        self.assertNotIn("698", "".join(self.sent))

    def test_real_progress_resets_the_stall_clock_and_reads_alone_do_not(self):
        self.intent()
        self.journal.create("order", {"symbol": RISK_SYMBOL, "side": "sell", "qty": "10", "price": "69",
                                      "switch": "i1"}, OPEN + timedelta(minutes=2), "2026-10-15", status="accepted")
        self.observe(OPEN + timedelta(minutes=25))
        row = self.journal.rows()[0]["id"]
        self.journal.update(row, "partial", filled="4")                # a fill is progress
        self.observe(OPEN + timedelta(minutes=33))
        self.observe(OPEN + timedelta(minutes=40))
        self.assertEqual(len(self.alerts()), 1)                        # only the early one so far
        self.observe(OPEN + timedelta(minutes=43))
        self.assertEqual(len(self.alerts()), 2)
        state = self.journal.meta("switch_progress")
        self.assertEqual(state["last_progress_at"], (OPEN + timedelta(minutes=33)).isoformat())

    def test_final_notice_at_the_recovery_cutoff_and_after_a_sleeping_close(self):
        self.intent()
        self.observe(datetime(2026, 10, 15, 19, 51, tzinfo=UTC))       # cutoff 19:50 (close 20:00 - 10)
        self.assertTrue(any("다음 정규장으로 이월" in t for t in self.alerts()))
        self.sent.clear()
        self.journal.put_meta("switch_progress", None)
        intent = self.journal.meta("switch_intent")
        intent["id"] = "i2"                                            # another switch, same session
        self.journal.put_meta("switch_intent", intent)
        self.observe(datetime(2026, 10, 16, 2, 0, tzinfo=UTC))         # Mac slept through the close
        self.assertTrue(any("다음 정규장으로 이월" in t for t in self.alerts()))

    def test_observe_never_raises_switch_alerts(self):
        self.intent()
        for minutes in (25, 40, 60):
            self.observe(OPEN + timedelta(minutes=minutes), mode="observe")
        self.observe(datetime(2026, 10, 15, 19, 51, tzinfo=UTC), mode="observe")
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.journal.meta("switch_progress")["mode"], "observe")

    def test_holiday_half_day_and_overnight_never_count_as_stall(self):
        self.intent(session="2026-11-25", at=datetime(2026, 11, 25, 14, 31, tzinfo=UTC))
        self.observe(datetime(2026, 11, 26, 15, 0, tzinfo=UTC))        # Thanksgiving: no session
        self.assertEqual(self.alerts(), [])
        day = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)               # half day: 14:30-18:00 UTC
        self.observe(day + timedelta(minutes=24))
        self.assertEqual(self.alerts(), [])                            # yesterday's hours are not counted
        self.observe(day + timedelta(minutes=25))
        self.assertEqual(len(self.alerts()), 1)
        self.observe(day + timedelta(minutes=34))
        self.assertEqual(len(self.alerts()), 1)                        # stall counts from the early notice
        self.observe(datetime(2026, 11, 27, 17, 51, tzinfo=UTC))       # early close 18:00 - 10 min
        self.assertTrue(any("이월" in t for t in self.alerts()))

    def test_daylight_saving_shift_moves_the_alert_with_the_open(self):
        self.intent(session="2026-11-02", at=datetime(2026, 11, 2, 14, 31, tzinfo=UTC))   # EST: open 14:30 UTC
        self.observe(datetime(2026, 11, 2, 13, 55, tzinfo=UTC))
        self.observe(datetime(2026, 11, 2, 14, 54, tzinfo=UTC))
        self.assertEqual(self.alerts(), [])
        self.observe(datetime(2026, 11, 2, 14, 55, tzinfo=UTC))
        self.assertEqual(len(self.alerts()), 1)

    def test_three_delay_kinds_are_labelled_apart(self):
        labels = {}
        for to, status in ((DEFENSIVE_SYMBOL, "selling"), (DEFENSIVE_SYMBOL, "buying"),
                           (RISK_SYMBOL, "selling"), (RISK_SYMBOL, "buying")):
            labels[(to, status)] = progress.priority({"to": to, "status": status})
        self.assertEqual(labels[(DEFENSIVE_SYMBOL, "selling")][::2], ("A", "높음"))
        self.assertEqual(labels[(DEFENSIVE_SYMBOL, "buying")][::2], ("C", "보통"))
        self.assertIn("달러 현금", labels[(DEFENSIVE_SYMBOL, "buying")][1])
        self.assertEqual({labels[(RISK_SYMBOL, s)][0] for s in ("selling", "buying")}, {"B"})
        self.assertIn("SGOV 매도 지연", labels[(RISK_SYMBOL, "selling")][1])

    def test_restart_keeps_alert_history_and_completion_is_announced_once(self):
        self.intent(to=RISK_SYMBOL, status="buying")
        self.observe(OPEN + timedelta(minutes=25))
        self.open()                                                    # restart
        self.observe(OPEN + timedelta(minutes=26))
        self.assertEqual(len(self.alerts()), 1)
        intent = self.journal.meta("switch_intent")
        intent["status"] = "done"
        self.journal.put_meta("switch_intent", intent)
        self.observe(OPEN + timedelta(minutes=40))
        self.observe(OPEN + timedelta(minutes=41))
        resolved = [t for t in self.alerts() if "전환 완료" in t]
        self.assertEqual(len(resolved), 1)

    def test_a_completed_switch_with_leftover_cash_raises_nothing(self):
        self.intent(status="buying")
        intent = self.journal.meta("switch_intent")
        intent["status"] = "done"                                      # done by the engine's own rule
        self.journal.put_meta("switch_intent", intent)
        for minutes in (25, 40):
            self.observe(OPEN + timedelta(minutes=minutes))
        self.assertEqual(self.alerts(), [])

    def test_query_failure_and_a_zero_result_are_different_reasons(self):
        self.assertEqual(progress.classify("waiting: sale proceeds not yet orderable")[0], "proceeds_pending")
        self.assertEqual(progress.classify("Internal error: URLError")[0], "query_failed")
        self.assertEqual(progress.classify("New orders blocked: reservation lookup failed (x)")[0],
                         "reservation_query_failed")
        self.assertEqual(progress.classify("anything", locked=True)[2], True)

    def test_undelivered_local_alert_reaches_the_monitor_and_a_delivered_final_is_not_repeated(self):
        from autotrader.monitor import Monitor
        self.intent()
        self.down = True
        self.observe(OPEN + timedelta(minutes=25))
        now = OPEN + timedelta(minutes=26)
        message = heartbeat.build(self.journal, now=now, run_at=now, outcome="ok", work_done=True)
        self.assertEqual((message["switch_alert"], message["switch_alert_delivered"]), ("early", False))
        self.assertNotIn("698", json.dumps(message))
        secret = "s" * 40
        monitor = Monitor(str(Path(self.tmp.name) / "m.sqlite"), {"instances": [message["instance"]]}, secret, [],
                          sleep=lambda s: None)
        body = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(monitor.receive(body, heartbeat.sign(body, secret), now)[0], 200)
        self.assertIn("switch_alert_undelivered", [k for _, k in monitor.evaluate(now)])


class TelegramStatusTests(Base):
    def setUp(self):
        super().setUp()
        self.replies, self.updates = [], []
        env = patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "7", "TELEGRAM_BOT_TOKEN": "1:x"})
        env.start()
        self.addCleanup(env.stop)

    def transport(self, method, params):
        if method == "getUpdates":
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= params["offset"]]}
        self.replies.append(params["text"])
        return {"ok": True}

    def say(self, text, chat=7, now=OPEN + timedelta(minutes=30), readonly_only=False):
        self.updates.append({"update_id": len(self.updates) + 1, "message": {
            "chat": {"id": chat, "type": "private"}, "from": {"id": chat}, "text": text}})
        return telegram_bot.process(self.journal, telegram_bot.fetch(self.journal, self.transport), tax.TaxSettings(),
                                    now, self.transport, readonly_only=readonly_only)

    def seed(self, at=OPEN + timedelta(minutes=29)):
        self.intent()
        self.journal.put_meta("health", {"at": at.isoformat(), "result": "waiting: order fill", "mode": "live"})
        self.journal.put_meta("signal", {"signal_date": "2026-10-14", "target": DEFENSIVE_SYMBOL, "high_252": 759.66})
        self.journal.put_meta("active_hold", {"message": "New orders blocked: 2 active reservation order(s) (QQQ buy 3)",
                                              "since": at.isoformat()})
        self.observe(OPEN + timedelta(minutes=29))

    def test_status_and_issues_answer_from_local_state_without_sensitive_figures(self):
        self.seed()
        self.say("/status")
        self.say("왜 멈췄어")
        status, found = self.replies
        self.assertIn("목표 SGOV", status)
        self.assertIn("TQQQ 이탈 매도 지연", status)
        self.assertIn("활성 예약주문", found)
        self.assertIn("사용자:", found)
        joined = status + found
        for secret in ("698", "759", "QQQ buy 3", "2 active"):
            self.assertNotIn(secret, joined)

    def test_stale_state_is_never_presented_as_current(self):
        self.seed(at=OPEN - timedelta(hours=3))
        self.say("상태")
        self.assertIn("오래됐습니다", self.replies[-1])

    def test_other_chats_get_nothing_and_acknowledgements_change_nothing(self):
        self.seed()
        self.journal.put_meta("halt", "locked for test")
        self.say("/status", chat=99)
        self.assertEqual(self.replies, [])
        self.say("확인했어")
        self.assertIn("하지 않습니다", self.replies[-1])
        self.assertEqual(self.journal.meta("halt"), "locked for test")

    def test_read_only_pass_stops_at_the_first_plan_change(self):
        self.seed()
        self.updates = [{"update_id": i + 1, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                                                        "text": t}} for i, t in enumerate(("/status", "/tax_cap 100",
                                                                                           "/issues"))]
        done = telegram_bot.process(self.journal, telegram_bot.fetch(self.journal, self.transport), tax.TaxSettings(),
                                    OPEN, self.transport, readonly_only=True)
        self.assertEqual(done, 1)
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(telegram_bot._offset(self.journal.db), 1)     # the plan change is not skipped

    def test_run_answers_status_while_another_process_holds_the_state_lock(self):
        import fcntl
        from autotrader import __main__ as cli
        from test_doctor import SECRET_ENV
        root = Path(self.tmp.name) / "cli"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"state_dir": "state", "notifications": False,
                                                       "account_lock_dir": str(root / "locks")}))
        (root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items())
                                          + "TELEGRAM_CHAT_ID=7\nTELEGRAM_BOT_TOKEN=1:x\n")
        os.chmod(root / "secrets.env", 0o600)
        (root / "state").mkdir()
        self.updates = [{"update_id": 1, "message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7},
                                                     "text": "/status"}}]
        lock = open(root / "state" / "run.lock", "a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)               # e.g. a live check is running
        try:
            with patch.object(telegram_bot, "http_transport", lambda token: self.transport), \
                 patch("autotrader.meritz.urlopen", side_effect=AssertionError("network forbidden")), \
                 patch("sys.argv", ["autotrader", "run", "--config", str(root / "config.json")]), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(), 0)
        finally:
            lock.close()
        self.assertEqual(len(self.replies), 1)
        self.assertTrue(self.replies[0].startswith("[상태]"))


class LateNoticeTests(Base):
    def test_a_resent_hold_says_when_it_happened_and_that_it_is_over(self):
        self.down = True
        self.notifier.send("hold", "reservation", "보류 사유", OPEN)
        self.down = False
        mark_resolved(self.journal, "hold:reservation", OPEN + timedelta(minutes=10))
        self.notifier.retry_pending(OPEN + timedelta(minutes=31))
        self.assertTrue(self.sent[-1].startswith("[재전송 · 발생 10-15 22:30 KST · 이미 해소됨 10-15 22:40 KST]"))

    def test_hold_cleared_notice_carries_both_times_and_no_raw_figures(self):
        from autotrader.__main__ import after_success
        self.journal.put_meta("active_hold", {"message": "New orders blocked: 2 active reservation order(s) (QQQ buy 3)",
                                              "since": OPEN.isoformat()})
        after_success(self.journal, self.notifier, "idle: nothing to do", {"target": RISK_SYMBOL}, "2026-10-15",
                      OPEN + timedelta(minutes=20))
        text = [t for t in self.sent if t.startswith("[복구]")][0]
        self.assertIn("발생 10-15 22:30", text)
        self.assertIn("해소 10-15 22:50", text)
        self.assertNotIn("QQQ buy", text)


class NetworkRecoveryTests(Base):
    def test_lost_reply_then_recovery_converges_without_resend_and_records_progress(self):
        from test_fault_injection import FaultBroker
        broker = FaultBroker(usd="0")
        broker.positions = {RISK_SYMBOL: D(10)}
        engine = Engine(broker, self.journal, Settings(mode="paper"))
        args = dict(target=DEFENSIVE_SYMBOL, signal_date="2026-10-14", expected_signal_date="2026-10-14",
                    session="2026-10-15", may_trade=True)
        broker.lose.add("order")
        with self.assertRaises(UnknownOutcome) as ctx:
            engine.tick(now=OPEN + timedelta(minutes=2), **args)
        self.observe(OPEN + timedelta(minutes=2), str(ctx.exception))
        self.assertEqual(self.journal.meta("switch_progress")["wait_code"], "unknown_outcome")
        broker.lose.clear()
        engine = Engine(broker, self.journal, Settings(mode="paper"))   # restart after the network returns
        self.assertEqual(engine.tick(now=OPEN + timedelta(minutes=3), **args), "recovered: order")
        broker.fill(max(broker.orders))
        engine.tick(now=OPEN + timedelta(minutes=4), **args)
        self.observe(OPEN + timedelta(minutes=4), "confirmed: filled")
        self.assertEqual(len([c for c in broker.calls if c[0] == "sell"]), 1)
        self.assertEqual(self.journal.meta("switch_progress")["last_progress_at"],
                         (OPEN + timedelta(minutes=4)).isoformat())
        result = engine.tick(now=OPEN + timedelta(minutes=20), **dict(args, may_trade=False))
        self.observe(OPEN + timedelta(minutes=20), result)              # window closed during the outage
        self.assertEqual(len([c for c in broker.calls if c[0] == "buy"]), 0)


class ServiceTests(unittest.TestCase):
    def test_status_reads_installation_and_flags_two_loaded_services(self):
        calls = []

        def runner(argv):
            calls.append(argv[0])
            if argv[:2] == ["fdesetup", "status"]:
                return 0, "FileVault is On.\n"
            return 0, ""
        report = ops.service_status(ROOT, runner=runner)
        self.assertTrue(report["both_active"])
        self.assertIn("warning", report)
        self.assertEqual(report["filevault"], "FileVault is On.")
        self.assertTrue(set(calls) <= {"launchctl", "fdesetup"})
        self.assertEqual(report["external_monitor"]["deployed_and_verified"][:7], "unknown")

    def test_generator_writes_files_only_and_the_daemon_runs_as_the_user(self):
        import getpass
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".venv/bin").mkdir(parents=True)
            (Path(tmp) / ".venv/bin/python").write_text("")
            script = Path(tmp) / "install_launchd.py"
            script.write_text((ROOT / "install_launchd.py").read_text())
            out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
            if os.getuid() == 0:
                # Intended refusal: the service files must be made by the Mac user.
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("not root", out.stderr)
                return
            self.assertEqual(out.returncode, 0, out.stderr)
            daemon = plistlib.loads((Path(tmp) / "local.tqqq.runner.plist").read_bytes())
            agent = plistlib.loads((Path(tmp) / "local.tqqq.observer.plist").read_bytes())
        self.assertEqual(daemon["UserName"], getpass.getuser())
        self.assertNotEqual(daemon["UserName"], "root")
        self.assertEqual(daemon["ProgramArguments"], agent["ProgramArguments"])
        self.assertEqual(daemon["StartInterval"], 60)


if __name__ == "__main__":
    unittest.main()
