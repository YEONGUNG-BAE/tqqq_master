"""External monitor and status heartbeat (stage 4). No network, fixed clocks."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader import heartbeat
from autotrader.core import Journal
from autotrader.monitor import Monitor
from autotrader.schedule import Schedule
from test_doctor import Harness, KRW_MORNING, SECRET_ENV, FakeMeritz

SECRET = "s" * 40
MID_SESSION = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)     # Thursday, US session open


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")
        self.addCleanup(self.journal.db.close)
        self.alerts, self.backup = [], []
        self.primary_down = False

        def primary(text):
            if self.primary_down:
                raise OSError("telegram down")
            self.alerts.append(text)
        self.monitor = Monitor(str(Path(self.tmp.name) / "monitor.sqlite"),
                               {"instances": [heartbeat.instance_id(self.journal)]}, SECRET,
                               [("telegram", primary), ("backup_webhook", self.backup.append)],
                               schedule_factory=Schedule, sleep=lambda s: None)
        self.addCleanup(self.monitor.db.close)

    def status(self, now, **meta):
        for key, value in meta.items():
            self.journal.put_meta(key, value)
        locked = bool(self.journal.meta("halt"))
        return heartbeat.build(self.journal, now=now, run_at=now, outcome="lock" if locked else "ok",
                               work_done=not locked)

    def deliver(self, message, now):
        body = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
        return self.monitor.receive(body, heartbeat.sign(body, SECRET), now)

    def beat(self, now, **meta):
        return self.deliver(self.status(now, **meta), now)

    def kinds(self, now):
        return sorted(kind for _, kind in self.monitor.evaluate(now))


class PayloadTests(Fixture):
    def test_payload_is_a_whitelist_without_amounts_ids_or_errors(self):
        self.journal.put_meta("signal", {"signal_date": "2026-10-14", "target": "TQQQ", "high_252": 759.66})
        self.journal.put_meta("active_hold", {"message": "Order 20261015:12345 balance 1,500,000 USD 124.96"})
        self.journal.put_meta("last_positions", {"TQQQ": "12"})
        message = heartbeat.build(self.journal, now=MID_SESSION, run_at=MID_SESSION, outcome="hold", work_done=False)
        self.assertEqual(set(message), set(heartbeat.FIELDS))
        text = json.dumps(message)
        for forbidden in ("12345", "1,500,000", "124.96", "759.66", "TQQQ\": \"12"):
            self.assertNotIn(forbidden, text)
        self.assertTrue(message["instance"].startswith("inst-"))
        self.assertFalse(any(c.isdigit() for c in message["hold_code"]))

    def test_sequence_is_persisted_across_restarts(self):
        first = heartbeat.build(self.journal, now=MID_SESSION, run_at=MID_SESSION, outcome="ok", work_done=True)
        reopened = Journal(Path(self.tmp.name) / "j.sqlite")
        try:
            second = heartbeat.build(reopened, now=MID_SESSION, run_at=MID_SESSION, outcome="ok", work_done=True)
        finally:
            reopened.db.close()
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        self.assertEqual(first["instance"], second["instance"])


class ReceiveTests(Fixture):
    def test_signature_replay_and_duplicates(self):
        message = self.status(MID_SESSION)
        body = json.dumps(message).encode()
        self.assertEqual(self.monitor.receive(body, "0" * 64, MID_SESSION)[0], 401)
        self.assertEqual(self.deliver(message, MID_SESSION), (200, "ok"))
        self.assertEqual(self.deliver(message, MID_SESSION + timedelta(seconds=5)), (200, "duplicate"))
        newer = self.status(MID_SESSION + timedelta(seconds=60))
        self.assertEqual(self.deliver(newer, MID_SESSION + timedelta(seconds=60)), (200, "ok"))
        # An old, validly signed message replayed later cannot revive the state.
        self.assertEqual(self.deliver(message, MID_SESSION + timedelta(seconds=90))[0], 409)
        later = MID_SESSION + timedelta(minutes=30)
        self.assertEqual(self.deliver(newer, later)[0], 409)          # stale send time
        self.assertIn("no_heartbeat", self.kinds(later))

    def test_unknown_instance_is_refused(self):
        message = dict(self.status(MID_SESSION), instance="inst-someone-else")
        self.assertEqual(self.deliver(message, MID_SESSION)[0], 403)


class EvaluateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.journal.put_meta("signal", {"signal_date": "2026-10-14", "target": "TQQQ"})
        self.journal.put_meta("position_state", {"target": "TQQQ", "aligned": True})

    def test_healthy_then_mac_down_is_detected_and_recovery_is_reported(self):
        self.beat(MID_SESSION)
        self.assertEqual(self.kinds(MID_SESSION + timedelta(minutes=2)), [])
        down = MID_SESSION + timedelta(minutes=6)                      # shutdown / network cut / process gone
        self.assertEqual(self.kinds(down), ["no_heartbeat"])
        self.monitor.check(down)
        self.assertIn("상태 신호가 오지 않습니다", self.alerts[-1])
        self.monitor.check(down + timedelta(minutes=30))
        self.assertEqual(len(self.alerts), 1)                          # repeat suppressed
        back = down + timedelta(hours=1)
        self.beat(back)
        self.monitor.check(back)
        self.assertIn("해소됨", self.alerts[-1])

    def test_alive_but_locked_is_distinct_from_down(self):
        self.journal.put_meta("halt", "locked for test")
        self.journal.put_meta("lock_info", {"code": "unknown_outcome"})
        self.beat(MID_SESSION)
        self.assertIn("locked", self.kinds(MID_SESSION + timedelta(minutes=1)))
        self.assertNotIn("no_heartbeat", self.kinds(MID_SESSION + timedelta(minutes=1)))

    def test_only_the_signal_is_stale(self):
        friday = datetime(2026, 10, 16, 23, 0, tzinfo=timezone.utc)   # 3h after Friday's close
        self.journal.put_meta("signal", {"signal_date": "2026-10-15", "target": "TQQQ"})
        self.beat(friday)
        self.assertEqual(self.kinds(friday), ["signal_stale"])
        # Thanksgiving: no session, so yesterday's signal is current, not stale.
        holiday = datetime(2026, 11, 26, 23, 0, tzinfo=timezone.utc)
        self.journal.put_meta("signal", {"signal_date": "2026-11-25", "target": "TQQQ"})
        self.beat(holiday)
        self.assertEqual(self.kinds(holiday), [])

    def test_switch_overdue_uses_the_calendar_close_including_early_closes(self):
        self.journal.put_meta("switch_intent", {"status": "buying", "to": "QQQ", "signal_date": "2026-11-25"})
        self.journal.put_meta("signal", {"signal_date": "2026-11-25", "target": "QQQ"})
        before = datetime(2026, 11, 27, 17, 50, tzinfo=timezone.utc)   # half day: close 18:00 UTC
        self.beat(before)
        self.assertNotIn("switch_overdue", self.kinds(before))
        after = datetime(2026, 11, 27, 18, 10, tzinfo=timezone.utc)    # cutoff 17:50 + 15 min grace
        self.beat(after)
        self.assertIn("switch_overdue", self.kinds(after))
        self.monitor.check(after)
        self.assertIn("주문하지 않습니다", self.alerts[-1])

    def test_failed_primary_channel_falls_back_and_retries_later(self):
        self.primary_down = True
        down = MID_SESSION + timedelta(minutes=6)
        self.monitor.check(down)
        self.assertEqual(len(self.backup), 1)
        self.assertEqual(self.alerts, [])

    def test_alert_texts_carry_no_secret_or_identifiers(self):
        self.monitor.check(MID_SESSION + timedelta(minutes=6))
        for text in self.alerts + self.backup:
            self.assertNotIn(SECRET, text)
            self.assertNotIn("20261015", text)


class TradingPathTests(Harness):
    def test_monitor_outage_never_changes_the_run_result(self):
        from autotrader import __main__ as cli
        from autotrader.meritz import Client
        root = self.state / "cli"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"state_dir": "state_observe", "notifications": False,
                                                       "account_lock_dir": str(self.state / "locks"),
                                                       "execution": {"mode": "observe"}}))
        (root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items())
                                          + "MONITOR_URL=https://monitor.invalid/heartbeat\nMONITOR_SECRET=" + SECRET + "\n")
        os.chmod(root / "secrets.env", 0o600)
        fake = FakeMeritz(KRW_MORNING)
        out = io.StringIO()
        with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)), \
             patch("autotrader.heartbeat.urlopen", side_effect=TimeoutError("monitor unreachable")) as net, \
             patch.object(cli, "datetime", wraps=datetime) as clock, \
             patch("sys.argv", ["autotrader", "run", "--config", str(root / "config.json")]), redirect_stdout(out):
            clock.now.return_value = KRW_MORNING
            code = cli.main()
        self.assertEqual((code, json.loads(out.getvalue())["result"]), (0, "observe: fx"))
        self.assertEqual(net.call_args.kwargs["timeout"], 3)
        j = Journal(root / "state_observe/journal.sqlite")
        try:
            self.assertEqual(j.meta("monitor_last_send")["ok"], False)
            self.assertEqual(j.meta("monitor_outbox")["outcome"], "ok")
            self.assertIsNone(j.meta("halt"))
        finally:
            j.db.close()


if __name__ == "__main__":
    unittest.main()
