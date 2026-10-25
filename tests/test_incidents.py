"""Incidents: the same error after its resolution is a new incident; dedup,
retry and resolution are kept per incident; the incident is registered before
the suppression decision; an older incident's late delivery never silences
the current one; one continuing error stays one incident. Temporary DB, fake
channels, fixed clock; restarts reopen the journal and the notifier."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrader.core import Journal
from autotrader.notify import Notifier, current_record, mark_resolved

T0 = datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc)        # 22:30 KST


class Base(unittest.TestCase):
    restart_each_step = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "j.sqlite"
        self.mac, self.telegram, self.down = [], [], False
        self.journal = None
        self.open()

    def tg(self, text):
        if self.down:
            raise OSError("telegram down")
        self.telegram.append(text)

    def open(self):
        if self.journal is not None:
            self.journal.db.close()
        self.journal = Journal(self.path)
        self.addCleanup(lambda: self.journal.db.close())
        self.notifier = Notifier(self.journal, channels={"macos": self.mac.append, "telegram": self.tg})

    def step(self):
        if self.restart_each_step:
            self.open()

    def at(self, minutes):
        return T0 + timedelta(minutes=minutes)

    def run_minute(self, minutes, failing=True):
        """One scheduled run while the error persists: retries first, then the
        hold notice for the error that is present now."""
        self.step()
        self.notifier.retry_pending(self.at(minutes))
        if failing:
            self.notifier.send("hold", "reservation", "예약주문 보류", self.at(minutes))

    def resolve(self, minutes):
        self.step()
        mark_resolved(self.journal, "hold:reservation", self.at(minutes))


class RecurrenceTests(Base):
    def test_resolution_then_recurrence_is_a_new_incident_delivered_accurately(self):
        self.down = True
        self.run_minute(0)                                   # incident 1, Telegram fails
        self.resolve(10)
        self.run_minute(20)                                  # recurrence: incident 2, Telegram still down
        self.down = False
        self.run_minute(31)                                  # old copy is due; the error is still there
        self.assertEqual(len(self.telegram), 2)
        current, old = self.telegram
        self.assertTrue(current.startswith("[재전송 · 발생 10-15 22:50 KST]"))      # the present incident first
        self.assertNotIn("이미 해소됨", current)
        self.assertTrue(old.startswith("[재전송 · 발생 10-15 22:30 KST · 이미 해소됨 10-15 22:40 KST]"))
        for minute in range(32, 120):                        # one continuing error: no new incident, no repeat
            self.run_minute(minute)
        self.assertEqual(len(self.telegram), 2)
        self.assertEqual(self.journal.meta("incidents")["hold:reservation"]["id"], 2)
        self.assertEqual(self.journal.meta("notify_pending"), {})

    def test_an_older_incident_delivered_late_never_silences_the_current_one(self):
        self.down = True
        self.run_minute(0)
        self.resolve(10)
        self.down = False
        self.run_minute(20)                                  # recurrence with Telegram back: sent at once
        self.assertEqual(self.telegram[-1].splitlines()[0], "[보류] 예약주문 보류")
        self.run_minute(31)                                  # incident 1's late copy, then incident 2 persists
        self.assertEqual(len(self.telegram), 2)
        self.assertIn("이미 해소됨", self.telegram[-1])
        self.assertTrue(current_record(self.journal, "hold:reservation")["channels"]["telegram"]["ok"])

    def test_each_late_copy_carries_its_own_incident_resolution(self):
        self.down = True
        self.run_minute(0)                                   # incident 1
        self.resolve(10)
        self.run_minute(20)                                  # incident 2
        self.resolve(25)                                     # incident 2 resolved too
        self.down = False
        self.run_minute(31, failing=False)
        first = [t for t in self.telegram if "발생 10-15 22:30" in t][0]
        second = [t for t in self.telegram if "발생 10-15 22:50" in t][0]
        self.assertIn("이미 해소됨 10-15 22:40 KST", first)   # its own resolution, not the later one
        self.assertIn("이미 해소됨 10-15 22:55 KST", second)

    def test_a_third_occurrence_after_a_second_resolution_is_its_own_incident(self):
        self.run_minute(0)
        self.resolve(5)
        self.run_minute(6)
        self.resolve(7)
        self.run_minute(8)
        self.assertEqual(len(self.telegram), 3)              # each incident announced once
        self.assertEqual(self.journal.meta("incidents")["hold:reservation"]["id"], 3)

    def test_registration_happens_before_suppression(self):
        self.run_minute(0)
        self.resolve(1)
        before = len(self.telegram)
        self.run_minute(2)                                   # inside the 6 h window of incident 1
        self.assertEqual(len(self.telegram), before + 1)

    def test_resolving_twice_or_without_an_open_incident_changes_nothing(self):
        self.resolve(0)
        self.run_minute(1)
        self.resolve(2)
        self.resolve(3)
        self.assertEqual(self.journal.meta("incidents")["hold:reservation"]["resolved"], self.at(2).isoformat())


class RecurrenceAcrossRestartsTests(RecurrenceTests):
    restart_each_step = True


class LegacyStateTests(Base):
    def test_a_record_from_before_incidents_is_adopted_not_resent(self):
        self.journal.put_meta("notified", {"hold:reservation": {"at": self.at(-60).isoformat(), "channels": {
            "telegram": {"at": self.at(-60).isoformat(), "ok": True},
            "macos": {"at": self.at(-60).isoformat(), "ok": True}}}})
        self.run_minute(0)
        self.assertEqual(self.telegram, [])
        self.resolve(5)
        self.run_minute(6)
        self.assertEqual(len(self.telegram), 1)


if __name__ == "__main__":
    unittest.main()


class RetrySpacingTests(Base):
    def setUp(self):
        self.attempts = []
        super().setUp()

    def tg(self, text):
        self.attempts.append(text)                           # every attempt, across restarts
        super().tg(text)

    def test_a_failing_channel_keeps_its_retry_time_even_with_an_overdue_old_entry(self):
        attempts = self.attempts
        self.down = True
        self.run_minute(0)                                   # incident 1 fails (1 attempt)
        self.resolve(10)
        self.run_minute(20)                                  # incident 2 fails (1 attempt)
        self.assertEqual(len(attempts), 2)
        self.run_minute(31)                                  # incident 1 due: one try on the channel, fails
        tried_at_31 = len(attempts)
        for minute in range(32, 61):
            self.run_minute(minute)
        self.assertEqual(len(attempts), tried_at_31)         # nothing until the channel's retry time
        self.down = False
        self.run_minute(61)
        self.assertTrue(self.telegram[0].startswith("[재전송 · 발생 10-15 22:50 KST]"))   # current first
        self.assertEqual(len(self.telegram), 2)


class RetrySpacingAcrossRestartsTests(RetrySpacingTests):
    restart_each_step = True


LEGACY = {
    "notified": {"hold:reservation": {"at": "2026-10-15T13:30:00+00:00", "channels": {
        "macos": {"at": "2026-10-15T13:30:00+00:00", "ok": True},
        "telegram": {"at": "2026-10-15T13:30:00+00:00", "ok": False}}}},
    "notify_pending": {"hold:reservation": {"category": "hold", "text": "[보류] 예약주문 보류", "channels": ["telegram"],
                                            "first": "2026-10-15T13:30:00+00:00", "attempts": 1,
                                            "retry_at": "2026-10-15T14:00:00+00:00"}},
    "resolved_incidents": {"hold:reservation": "2026-10-15T13:40:00+00:00"},
}


class LegacyMigrationTests(Base):
    def seed(self):
        for key, value in LEGACY.items():
            self.journal.put_meta(key, value)

    def test_delivery_pending_and_resolution_move_into_one_incident(self):
        self.seed()
        self.notifier.retry_pending(self.at(31))
        self.assertEqual(len(self.telegram), 1)
        self.assertTrue(self.telegram[0].startswith("[재전송 · 발생 10-15 22:30 KST · 이미 해소됨 10-15 22:40 KST]"))
        record = self.journal.meta("notified")["hold:reservation#1"]
        self.assertTrue(record["channels"]["telegram"]["ok"])
        self.assertEqual(self.journal.meta("notify_pending"), {})
        self.assertNotIn("hold:reservation", self.journal.meta("notified"))
        self.run_minute(32)                                  # the same error now: a new incident, announced
        self.assertEqual(len(self.telegram), 2)
        self.assertEqual(self.telegram[-1].splitlines()[0], "[보류] 예약주문 보류")
        self.assertEqual(self.journal.meta("incidents")["hold:reservation"]["id"], 2)

    def test_migration_applies_once_even_when_run_again(self):
        self.seed()
        from autotrader.notify import migrate_legacy
        migrate_legacy(self.journal)
        snapshot = {k: self.journal.meta(k) for k in ("notified", "notify_pending", "resolved_incidents", "incidents")}
        migrate_legacy(self.journal)
        self.open()
        migrate_legacy(self.journal)
        self.assertEqual({k: self.journal.meta(k) for k in snapshot}, snapshot)

    def test_kill_in_the_middle_of_the_migration_leaves_the_old_records_and_reruns_once(self):
        import json
        import os
        import subprocess
        import sys
        self.seed()
        self.journal.db.close()
        root = Path(__file__).resolve().parent.parent
        child = (f"import os, sys\nsys.path.insert(0, {str(root)!r})\nfrom autotrader.core import Journal\n"
                 "from autotrader import notify\nj = Journal(sys.argv[1])\noriginal = j.put_meta\n"
                 "def killer(key, value):\n    original(key, value)\n    if key == 'notify_pending':\n        os._exit(3)\n"
                 "j.put_meta = killer\nnotify.migrate_legacy(j)\n")
        result = subprocess.run([sys.executable, "-W", "ignore", "-c", child, str(self.path)], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.journal = None
        self.open()
        self.assertEqual(self.journal.meta("notify_pending"), LEGACY["notify_pending"])    # rolled back whole
        self.notifier.retry_pending(self.at(31))
        self.assertTrue(self.telegram[0].startswith("[재전송 · 발생 10-15 22:30 KST · 이미 해소됨 10-15 22:40 KST]"))
        self.assertEqual(sorted(self.journal.meta("notified")), ["hold:reservation#1"])


class ConcurrentIncidentsTests(Base):
    """Two different problems at once: a failed retry holds the channel for
    every path, send() included; only a new incident's first notice is tried."""

    def setUp(self):
        self.attempts = []
        super().setUp()

    def tg(self, text):
        self.attempts.append(text)
        super().tg(text)

    def notice(self, code, minutes):
        self.step()
        self.notifier.send("hold", code, f"보류 {code}", self.at(minutes))

    def test_a_continuing_incident_never_bypasses_the_channel_retry_time(self):
        self.down = True
        self.notice("a", 0)                                  # A fails
        self.notice("b", 10)                                 # B fails
        self.assertEqual(len(self.attempts), 2)
        self.step()
        self.notifier.retry_pending(self.at(31))             # retry fails: channel held until 61
        tried = len(self.attempts)
        self.notice("a", 31)                                 # A still present in the same run
        self.notice("b", 32)
        for minute in range(33, 61):
            self.step()
            self.notifier.retry_pending(self.at(minute))
            self.notice("a", minute)
        self.assertEqual(len(self.attempts), tried)
        self.notice("c", 40)                                 # a NEW incident's first notice is still tried
        self.assertEqual(len(self.attempts), tried + 1)
        self.down = False
        self.step()
        self.notifier.retry_pending(self.at(61))             # the channel's time has come
        self.assertGreater(len(self.telegram), 0)
        self.notice("a", 62)
        delivered_a = [t for t in self.telegram if "보류 a" in t]
        self.assertEqual(len(delivered_a), 1)                # delivered once, not again by send()


class ConcurrentIncidentsAcrossRestartsTests(ConcurrentIncidentsTests):
    restart_each_step = True
