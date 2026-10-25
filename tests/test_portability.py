"""Portability (macOS/Linux and Windows): process locks across real processes,
replace retries, platform-only channels, time-zone independence, paths with
spaces and Korean, permission reports. On macOS these run the POSIX branch;
the Windows branch runs only when this suite runs on native Windows. Tests
that patch sys.platform or oslock.WINDOWS check logic only, not Windows."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader import notify, oslock

ROOT = Path(__file__).resolve().parent.parent
HOLDER = r'''
import sys, time
sys.path.insert(0, {root!r})
from autotrader import oslock
with oslock.held(sys.argv[1]):
    open(sys.argv[2], "w").write("held")
    time.sleep(float(sys.argv[3]))
'''


def hold_in_child(lock, seconds):
    flag = Path(str(lock) + ".ready")
    proc = subprocess.Popen([sys.executable, "-c", HOLDER.format(root=str(ROOT)), str(lock), str(flag), str(seconds)])
    deadline = time.time() + 20
    while not flag.exists():
        if time.time() > deadline or proc.poll() is not None:
            proc.kill()
            raise AssertionError("lock holder did not start")
        time.sleep(0.05)
    return proc


class ProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_lock_held_by_another_process_refuses_and_its_death_releases_it(self):
        lock = self.dir / "account-a.lock"
        child = hold_in_child(lock, 60)
        try:
            with self.assertRaises(oslock.Busy):
                with oslock.held(lock):
                    pass
            self.assertTrue(issubclass(oslock.Busy, BlockingIOError))    # existing `except BlockingIOError` paths
        finally:
            child.kill()                                                # crash: no clean unlock
            child.wait(10)
        with oslock.held(lock):                                         # the OS released it
            pass

    def test_different_accounts_never_block_each_other(self):
        child = hold_in_child(self.dir / "account-a.lock", 60)
        try:
            with oslock.held(self.dir / "account-b.lock"):
                pass
        finally:
            child.kill()
            child.wait(10)

    def test_a_blocking_lock_waits_for_the_other_process(self):
        lock = self.dir / "token.lock"
        child = hold_in_child(lock, 1.0)
        started = time.time()
        with oslock.held(lock, blocking=True):
            waited = time.time() - started
        child.wait(10)
        self.assertGreater(waited, 0.3)


class ReplaceTests(unittest.TestCase):
    def test_windows_branch_retries_a_target_held_open_then_raises_without_losing_the_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, temp = Path(tmp) / "a.json", Path(tmp) / "a.tmp"
            target.write_text("old")
            temp.write_text("new")
            calls = []

            def busy(src, dst):
                calls.append(1)
                raise PermissionError("in use")
            with patch.object(oslock, "WINDOWS", True), patch.object(oslock.os, "replace", busy), \
                 patch.object(oslock.time, "sleep", lambda s: None):
                with self.assertRaises(PermissionError):
                    oslock.replace(temp, target, attempts=3)
            self.assertEqual((len(calls), target.read_text()), (3, "old"))
            oslock.replace(temp, target)
            self.assertEqual(target.read_text(), "new")


class ChannelTests(unittest.TestCase):
    def test_the_macos_banner_exists_only_on_macos(self):
        with patch.object(notify.sys, "platform", "win32"), patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("macos", notify.default_channels())
        with patch.object(notify.sys, "platform", "darwin"), patch.dict(os.environ, {}, clear=True):
            self.assertIn("macos", notify.default_channels())


TZ_PROBE = r'''
import json, sys
sys.path.insert(0, {root!r})
from datetime import datetime, timezone
from autotrader.schedule import Schedule
from autotrader.signal import decisions
now = datetime(2026, 11, 2, 14, 40, tzinfo=timezone.utc)
s = Schedule(now)
row = s.trading_session(now)
print(json.dumps({{"expected": s.expected_close(now), "window": s.execution_window(now),
                  "open": row[1].isoformat(), "close": row[2].isoformat(), "key": s.session_key(now),
                  "fx": s.exchange_window(datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)),
                  "signal": decisions(["100"] * 300 + ["84"])[-1].target}}))
'''


class TimeZoneTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "TZ variable semantics differ on Windows; run the probe with another system zone")
    def test_results_do_not_depend_on_the_machine_time_zone(self):
        outputs = set()
        for zone in ("Asia/Seoul", "America/Los_Angeles", "UTC", "Europe/Berlin"):
            env = dict(os.environ, TZ=zone)
            out = subprocess.run([sys.executable, "-W", "ignore", "-c", TZ_PROBE.format(root=str(ROOT))],
                                 capture_output=True, text=True, timeout=120, env=env)
            self.assertEqual(out.returncode, 0, out.stderr[-1500:])
            outputs.add(out.stdout.strip())
        self.assertEqual(len(outputs), 1)


class PathTests(unittest.TestCase):
    def test_config_in_a_korean_path_with_spaces_from_another_working_directory(self):
        from autotrader import __main__ as cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "아버지 계좌 설치" / "tqqq auto"
            root.mkdir(parents=True)
            (root / "config.json").write_text(json.dumps({"state_dir": "state 관찰", "notifications": False,
                                                          "account_lock_dir": str(root / "잠금 폴더")}))
            elsewhere = Path(tmp) / "다른 곳"
            elsewhere.mkdir()
            out, cwd = io.StringIO(), os.getcwd()
            os.chdir(elsewhere)
            try:
                with patch("sys.argv", ["autotrader", "status", "--config", str(root / "config.json")]), \
                     redirect_stdout(out):
                    self.assertEqual(cli.main(), 0)
            finally:
                os.chdir(cwd)
            self.assertTrue((root / "state 관찰" / "journal.sqlite").exists())
            self.assertFalse((elsewhere / "state 관찰").exists())
            self.assertIn("health", json.loads(out.getvalue()))


class PermissionReportTests(unittest.TestCase):
    def test_posix_mode_or_windows_acl_is_reported_not_assumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "secrets.env"
            f.write_text("x")
            if os.name != "nt":
                os.chmod(f, 0o644)
                self.assertFalse(oslock.acl_report(f)["private"])
                os.chmod(f, 0o600)
                self.assertTrue(oslock.acl_report(f)["private"])
            fake = lambda argv: type("R", (), {"stdout": f"{f} BUILTIN\\Users:(RX)\n  DESKTOP\\dad:(F)\n"})()
            with patch.object(oslock, "WINDOWS", True):
                report = oslock.acl_report(f, runner=fake)
            self.assertEqual((report["private"], report["broad_access"]), (False, ["BUILTIN\\Users"]))
            self.assertEqual(oslock.private(f) if os.name == "nt" else None, None)


if __name__ == "__main__":
    unittest.main()
