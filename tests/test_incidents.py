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
