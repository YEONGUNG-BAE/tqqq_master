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


def icacls(code=0, sddl=None, raises=None):
    """Fake `icacls <path> /save <file>`: writes `sddl` (UTF-16) to the file."""
    def run(argv):
        if raises:
            raise raises
        if sddl is not None:
            Path(argv[-1]).write_bytes(("secrets.env\r\n" + sddl + "\r\n").encode("utf-16"))
        return type("Done", (), {"returncode": code})()
    return run


class PermissionReportTests(unittest.TestCase):
    def test_posix_mode_bits(self):
        if os.name == "nt":
            self.skipTest("POSIX mode bits")
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "secrets.env"
            f.write_text("x", encoding="utf-8")
            os.chmod(f, 0o644)
            self.assertFalse(oslock.acl_report(f)["private"])
            os.chmod(f, 0o600)
            self.assertTrue(oslock.acl_report(f)["private"])

    def test_windows_private_returns_false_because_chmod_cannot_set_an_acl(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "secrets.env"
            f.write_text("x", encoding="utf-8")
            if os.name == "nt":
                self.assertFalse(oslock.private(f))
            with patch.object(oslock, "WINDOWS", True):
                self.assertFalse(oslock.private(f))

    def test_windows_acl_is_judged_by_sid_and_a_failed_query_is_never_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "secrets.env"
            f.write_text("x", encoding="utf-8")
            me = "S-1-5-21-111-222-333-1001"
            cases = {
                "owner only": (icacls(sddl=f"D:PAI(A;;FA;;;{me})(A;;FA;;;SY)(A;;FA;;;BA)"), True, []),
                "inherited Users": (icacls(sddl=f"D:AI(A;ID;0x1200a9;;;BU)(A;ID;FA;;;{me})"), False, ["Users"]),
                "Everyone by full SID": (icacls(sddl=f"D:P(A;;FR;;;S-1-1-0)(A;;FA;;;{me})"), False, ["Everyone"]),
                "Domain Users": (icacls(sddl="D:P(A;;FR;;;S-1-5-21-9-8-7-513)"), False, ["Domain Users"]),
                "null DACL": (icacls(sddl="D:NO_ACCESS_CONTROL"), False, ["Everyone"]),
                "deny only": (icacls(sddl=f"D:P(D;;FA;;;WD)(A;;FA;;;{me})"), True, []),
            }
            with patch.object(oslock, "WINDOWS", True):
                for label, (runner, private, broad) in cases.items():
                    report = oslock.acl_report(f, runner=runner)
                    self.assertEqual((report["checked"], report["private"], report["broad_access"]),
                                     (True, private, broad), label)
                for label, runner in {"exit code": icacls(code=5, sddl=""), "empty output": icacls(sddl=""),
                                      "no file": icacls(), "not runnable": icacls(raises=FileNotFoundError()),
                                      "exit code with text": icacls(code=1332, sddl=f"D:P(A;;FA;;;{me})")}.items():
                    report = oslock.acl_report(f, runner=runner)
                    self.assertEqual((report["checked"], report["private"]), (False, None), label)
                    self.assertNotIn("broad_access", report, label)


ENC_PROBE_SKIP = "the child's default encoding is UTF-8; no non-UTF-8 locale to test with"


class TextEncodingTests(unittest.TestCase):
    def test_every_text_file_access_in_the_program_names_its_encoding(self):
        import ast
        sources = sorted((ROOT / "autotrader").glob("*.py")) + sorted(ROOT.glob("install_*.py"))
        missing = []
        for path in sources:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name not in ("open", "read_text", "write_text", "fdopen"):
                    continue
                if isinstance(func, ast.Attribute) and name == "open" and getattr(func.value, "id", "") == "os":
                    continue                                              # os.open: a descriptor, no text
                mode = next((a.value for a in node.args[1:2] if isinstance(a, ast.Constant)), None)
                mode = next((k.value.value for k in node.keywords if k.arg == "mode"), mode)
                if isinstance(mode, str) and "b" in mode:
                    continue
                if not any(k.arg == "encoding" for k in node.keywords):
                    missing.append(f"{path.name}:{node.lineno}")
        self.assertEqual(missing, [])

    def test_utf8_config_and_secrets_with_korean_text_load_under_a_cp949_default(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MERITZ_", "PYTHON"))}
        env.update(LC_ALL="ko_KR.CP949", LANG="ko_KR.CP949", PYTHONUTF8="0")
        probe = subprocess.run([sys.executable, "-c", "import locale; print(locale.getencoding())"],
                               capture_output=True, text=True, env=env, timeout=30)
        if probe.stdout.strip().lower().replace("-", "") in ("utf8", ""):
            self.skipTest(ENC_PROBE_SKIP)
        from test_doctor import SECRET_ENV
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "아버지 설치"
            root.mkdir()
            config = {"state_dir": "상태 관찰", "notifications": False, "account_lock_dir": str(root / "잠금"),
                      "account": {"alias": "아버지 계좌"}}
            # Notepad may add a BOM; both forms must load.
            (root / "config.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(config, ensure_ascii=False).encode())
            (root / "secrets.env").write_bytes(("# 아버지 계좌 키\n" + "".join(
                f"{k}={v}\n" for k, v in SECRET_ENV.items())).encode("utf-8"))
            if os.name != "nt":
                os.chmod(root / "secrets.env", 0o600)
            for command in (["status"], ["account", "show"]):
                out = subprocess.run([sys.executable, "-W", "ignore", "-m", "autotrader", *command,
                                      "--config", str(root / "config.json")],
                                     capture_output=True, cwd=ROOT, env=env, timeout=120)
                self.assertEqual(out.returncode, 0, (command, out.stdout[-500:], out.stderr[-1500:]))
            self.assertTrue((root / "상태 관찰" / "journal.sqlite").exists())
            report = json.loads(out.stdout.decode(probe.stdout.strip()))
            self.assertEqual(report["alias"], "아버지 계좌")

if __name__ == "__main__":
    unittest.main()
