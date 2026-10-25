"""Operations follow-ups: remote-channel delivery decides the monitor fallback,
read-only chat answers survive a broken tax plan, and a late notice carries
only the resolution of its own occurrence. Temporary DBs, fake channels and
fake Telegram transport; no real request is sent."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader import __main__ as cli
from autotrader import heartbeat, progress, tax, telegram_bot
from autotrader.core import Journal
from autotrader.notify import Notifier, mark_resolved
from autotrader.schedule import Schedule
from autotrader.signal import DEFENSIVE_SYMBOL, RISK_SYMBOL

UTC = timezone.utc
OPEN = datetime(2026, 10, 15, 13, 30, tzinfo=UTC)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")
        self.addCleanup(self.journal.db.close)
        self.mac, self.telegram, self.down = [], [], False

    def tg(self, text):
        if self.down:
            raise OSError("telegram down")
        self.telegram.append(text)

    def notifier(self, with_telegram=True):
        channels = {"macos": self.mac.append}
        if with_telegram:
            channels["telegram"] = self.tg
        return Notifier(self.journal, channels=channels)


class RemoteDeliveryTests(Base):
    def setUp(self):
        super().setUp()
        self.journal.put_meta("switch_intent", {"id": "i1", "to": DEFENSIVE_SYMBOL, "status": "selling",
                                                "from": {RISK_SYMBOL: "10"}, "created_session": "2026-10-15",
                                                "created_at": (OPEN + timedelta(minutes=1)).isoformat(),
                                                "proceeds": "0", "carried": "0", "spent": "0", "counted": [],
                                                "events": [], "recovery_orders": 0})

    def observe(self, notifier, at):
        return progress.observe(self.journal, notifier, Schedule(at), at, "waiting: order fill", mode="live",
                                recovery_buffer_minutes=10)

    def test_mac_banner_alone_does_not_count_as_delivered(self):
        self.down = True
        state = self.observe(self.notifier(), OPEN + timedelta(minutes=25))
        self.assertEqual(len(self.mac), 1)                                # the screen got it
        self.assertFalse(state["alert_delivered"])                        # nobody away from the Mac did
        now = OPEN + timedelta(minutes=26)
        message = heartbeat.build(self.journal, now=now, run_at=now, outcome="ok", work_done=True)
        self.assertIs(message["switch_alert_delivered"], False)

    def test_without_any_remote_channel_the_monitor_is_not_silenced(self):
        state = self.observe(self.notifier(with_telegram=False), OPEN + timedelta(minutes=25))
        self.assertFalse(state["alert_delivered"])

    def test_telegram_success_counts_as_delivered(self):
        state = self.observe(self.notifier(), OPEN + timedelta(minutes=25))
        self.assertTrue(state["alert_delivered"])


class ChatWithBrokenTaxPlanTests(Base):
    def setUp(self):
        super().setUp()
        self.replies, self.updates = [], []
        env = patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "7", "TELEGRAM_BOT_TOKEN": "1:x"})
        env.start()
        self.addCleanup(env.stop)
        self.journal.put_meta("health", {"at": OPEN.isoformat(), "error": "x", "mode": "live"})

    def transport(self, method, params):
        if method == "getUpdates":
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= params["offset"]]}
        self.replies.append(params["text"])
        return {"ok": True}

    def queue(self, *texts):
        for t in texts:
            self.updates.append({"update_id": len(self.updates) + 1, "message": {
                "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": t}})

    def test_housekeeping_still_answers_status_when_the_tax_plan_cannot_be_built(self):
        self.queue("/status", "/tax_cap 100", "왜 멈췄어")
        with patch.object(cli, "make_plan", side_effect=RuntimeError("tax tables unusable")), \
             patch.object(telegram_bot, "http_transport", lambda token: self.transport):
            cli.housekeeping(self.journal, self.notifier(), {"notifications": True}, None, None, OPEN)
        self.assertEqual(len(self.replies), 3)
        self.assertTrue(self.replies[0].startswith("[상태]"))
        self.assertIn("반영하지 않았습니다", self.replies[1])                # plan change refused, not applied
        self.assertTrue(self.replies[2].startswith("[문제]"))
        self.assertEqual(telegram_bot._offset(self.journal.db), 3)        # order kept, nothing redone

    def test_status_survives_a_broken_tax_schema(self):
        self.queue("/status")
        with patch.object(tax, "ensure_schema", side_effect=RuntimeError("tax schema broken")):
            done = telegram_bot.process(self.journal, telegram_bot.fetch(self.journal, self.transport),
                                        tax.TaxSettings(), OPEN, self.transport)
        self.assertEqual(done, 1)
        self.assertTrue(self.replies[0].startswith("[상태]"))

    def test_telegram_command_answers_when_make_plan_fails(self):
        from test_doctor import SECRET_ENV
        root = Path(self.tmp.name) / "cli"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"state_dir": "state", "notifications": True}))
        (root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items())
                                          + "TELEGRAM_CHAT_ID=7\nTELEGRAM_BOT_TOKEN=1:x\n")
        os.chmod(root / "secrets.env", 0o600)
        self.queue("상태")
        out = io.StringIO()
        with patch.object(cli, "make_plan", side_effect=RuntimeError("tax tables unusable")), \
             patch.object(telegram_bot, "http_transport", lambda token: self.transport), \
             patch("sys.argv", ["autotrader", "telegram", "--config", str(root / "config.json")]), \
             redirect_stdout(out):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(len(self.replies), 1)
        self.assertTrue(self.replies[0].startswith("[상태]"))


class OccurrenceTests(Base):
    def test_a_recurrence_never_borrows_the_previous_resolution(self):
        notifier = self.notifier()
        self.down = True
        notifier.send("hold", "reservation", "보류 사유", OPEN)            # occurrence 1, Telegram failed
        self.down = False
        notifier.retry_pending(OPEN + timedelta(minutes=31))              # delivered late
        mark_resolved(self.journal, "hold:reservation", OPEN + timedelta(minutes=40))
        self.down = True
        later = OPEN + timedelta(hours=7)                                  # occurrence 2 (after the 6 h window)
        notifier.send("hold", "reservation", "보류 사유", later)
        self.down = False
        notifier.retry_pending(later + timedelta(minutes=31))
        self.assertIn("발생 10-16 05:30 KST", self.telegram[-1])
        self.assertNotIn("이미 해소됨", self.telegram[-1])
        mark_resolved(self.journal, "hold:reservation", later + timedelta(minutes=50))
        self.down = True
        notifier.send("hold", "other", "다른 보류", later + timedelta(minutes=51))
        self.down = False

    def test_a_recurrence_while_the_old_copy_is_still_pending_is_dated_by_the_new_occurrence(self):
        notifier = self.notifier()
        self.down = True
        notifier.send("lock", "x", "잠금 1", OPEN)
        mark_resolved(self.journal, "lock:x", OPEN + timedelta(minutes=10))
        notifier.send("lock", "x", "잠금 2", OPEN + timedelta(hours=7))  # recurs before the copy got out
        self.down = False
        notifier.retry_pending(OPEN + timedelta(hours=8))
        # Two incidents, two late copies: the open one first, undated by the old resolution.
        current = [t for t in self.telegram if "잠금 2" in t]
        self.assertEqual(len(current), 1)
        self.assertEqual(self.telegram[0], current[0])
        self.assertIn("발생 10-16 05:30 KST", current[0])
        self.assertNotIn("이미 해소됨", current[0])
        old = [t for t in self.telegram if "잠금 1" in t]
        self.assertIn("이미 해소됨 10-15 22:40 KST", old[0])

    def test_a_copy_of_an_occurrence_resolved_later_still_says_so(self):
        notifier = self.notifier()
        self.down = True
        notifier.send("hold", "reservation", "보류 사유", OPEN)
        mark_resolved(self.journal, "hold:reservation", OPEN + timedelta(minutes=10))
        self.down = False
        notifier.retry_pending(OPEN + timedelta(minutes=31))
        self.assertIn("이미 해소됨 10-15 22:40 KST", self.telegram[-1])


if __name__ == "__main__":
    unittest.main()
