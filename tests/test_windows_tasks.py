"""Windows Task Scheduler files and service status. The XML is generated and
parsed here (any OS); registering and running it is a manual step on Windows
and is NOT verified by these tests."""
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from autotrader import ops
from test_doctor import SECRET_ENV

ROOT = Path(__file__).resolve().parent.parent
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

# Content models of the Task Scheduler schema elements the generator writes,
# taken from the Microsoft Learn schema reference (taskschd/taskschedulerschema-*):
# "seq" = xs:sequence (order matters; triggers extend triggerBaseType, whose
# elements come first), "all" = xs:all (any order, each at most once).
TASK_SCHEMA = {
    "Task": ("all", ["RegistrationInfo", "Triggers", "Settings", "Data", "Principals", "Actions"], ["Actions"]),
    "LogonTrigger": ("seq", ["Enabled", "StartBoundary", "EndBoundary", "Repetition", "ExecutionTimeLimit",
                             "UserId", "Delay"], []),
    "BootTrigger": ("seq", ["Enabled", "StartBoundary", "EndBoundary", "Repetition", "ExecutionTimeLimit",
                            "Delay"], []),
    "Repetition": ("all", ["Interval", "Duration", "StopAtDurationEnd"], ["Interval"]),
    "Principal": ("all", ["UserId", "LogonType", "GroupId", "DisplayName", "RunLevel", "ProcessTokenSidType",
                          "RequiredPrivileges"], []),
    "Settings": ("all", ["AllowStartOnDemand", "RestartOnFailure", "MultipleInstancesPolicy",
                         "DisallowStartIfOnBatteries", "StopIfGoingOnBatteries", "AllowHardTerminate",
                         "StartWhenAvailable", "NetworkProfileName", "RunOnlyIfNetworkAvailable", "WakeToRun",
                         "Enabled", "Hidden", "DeleteExpiredTaskAfter", "IdleSettings", "NetworkSettings",
                         "ExecutionTimeLimit", "Priority", "RunOnlyIfIdle", "UseUnifiedSchedulingEngine",
                         "DisallowStartOnRemoteAppSession"], []),
    "Exec": ("all", ["Command", "Arguments", "WorkingDirectory"], ["Command"]),
}


def schema_errors(element):
    """Order/membership errors against TASK_SCHEMA, for this element and below."""
    errors = []
    name = element.tag.split("}")[-1]
    children = [c.tag.split("}")[-1] for c in element]
    if name in TASK_SCHEMA:
        model, allowed, required = TASK_SCHEMA[name]
        for child in children:
            if child not in allowed:
                errors.append(f"{name}: unexpected {child}")
        if len(set(children)) != len(children):
            errors.append(f"{name}: repeated child")
        if model == "seq":
            positions = [allowed.index(c) for c in children if c in allowed]
            if positions != sorted(positions):
                errors.append(f"{name}: order {children}")
        errors += [f"{name}: missing {r}" for r in required if r not in children]
    for child in element:
        errors += schema_errors(child)
    return errors


class GeneratorTests(unittest.TestCase):
    def test_generated_files_follow_the_schema_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.generate(Path(tmp))
            for kind in ("logon", "startup"):
                task = ET.fromstring((Path(tmp) / f"tqqq-task-{kind}.xml").read_text(encoding="utf-16"))
                self.assertEqual(schema_errors(task), [], kind)
                self.assertEqual(task.get("version"), "1.3")             # the documented schema's fixed value

    def test_the_schema_check_rejects_the_previous_order(self):
        bad = ET.fromstring("<LogonTrigger><Enabled>true</Enabled><UserId>u</UserId><Delay>PT1M</Delay>"
                            "<Repetition><Interval>PT1M</Interval></Repetition></LogonTrigger>")
        self.assertTrue(schema_errors(bad))
        self.assertTrue(schema_errors(ET.fromstring("<Exec><Arguments>x</Arguments></Exec>")))

    def generate(self, root, env=None):
        python = r"C:\Users\아버지\tqqq auto\.venv\Scripts\python.exe"
        config = r"C:\Users\아버지\tqqq auto\config.json"
        out = subprocess.run([sys.executable, str(ROOT / "install_windows_task.py"), "--root", str(root),
                              "--python", python, "--config", config, "--user", r"DESKTOP\아버지"],
                             capture_output=True, text=True, timeout=30, env=dict(os.environ, **(env or {})))
        self.assertEqual(out.returncode, 0, out.stderr)
        return python, config

    def test_both_tasks_are_least_privilege_absolute_single_instance_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "공백 있는 폴더"
            root.mkdir()
            python, config = self.generate(root, env=SECRET_ENV)
            parsed = {}
            for kind in ("logon", "startup"):
                raw = (root / f"tqqq-task-{kind}.xml").read_bytes()
                self.assertTrue(raw.startswith(b"\xff\xfe"))                       # UTF-16 as Task Scheduler expects
                text = raw.decode("utf-16")
                for secret in SECRET_ENV.values():
                    self.assertNotIn(secret, text)
                parsed[kind] = ET.fromstring(text.split("?>", 1)[1])
        for kind, tree in parsed.items():
            find = lambda path: tree.find(path, NS).text
            self.assertEqual(find("t:Actions/t:Exec/t:Command"), python)
            self.assertEqual(find("t:Actions/t:Exec/t:Arguments"), f'-m autotrader run --config "{config}"')
            self.assertEqual(find("t:Principals/t:Principal/t:RunLevel"), "LeastPrivilege")
            self.assertEqual(find("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
            self.assertEqual(find("t:Settings/t:StartWhenAvailable"), "false")       # no catch-up burst
            self.assertEqual(tree.find(".//t:Repetition/t:Interval", NS).text, "PT1M")
        self.assertEqual(parsed["logon"].find("t:Principals/t:Principal/t:LogonType", NS).text, "InteractiveToken")
        self.assertEqual(parsed["startup"].find("t:Principals/t:Principal/t:LogonType", NS).text, "Password")
        self.assertIsNotNone(parsed["startup"].find("t:Triggers/t:BootTrigger", NS))

    def test_relative_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run([sys.executable, str(ROOT / "install_windows_task.py"), "--root", tmp,
                                  "--python", "python.exe", "--config", "config.json"],
                                 capture_output=True, text=True, timeout=30)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("absolute", out.stderr)


class WindowsStatusTests(unittest.TestCase):
    def test_registration_is_read_from_schtasks_not_from_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "tqqq-task-logon.xml").write_text("x")
            calls = []

            def runner(argv):
                calls.append(argv)
                if argv[3] == ops.WINDOWS_TASKS["logon"]:
                    return 0, "Status:                               Ready\nScheduled Task State:  Enabled\n"
                return 1, "ERROR: The system cannot find the file specified."
            state = Path(tmp) / "OneDrive" / "state"
            report = ops.service_status(tmp, runner=runner, platform="win32", state=state)
        self.assertTrue(report["windows_tasks"]["logon"]["registered"])
        self.assertFalse(report["windows_tasks"]["startup"]["registered"])
        self.assertEqual(report["windows_tasks"]["logon"]["status"], "Ready; Enabled")
        self.assertTrue(report["generated_files"]["tqqq-task-logon.xml"])
        self.assertFalse(report["both_active"])
        self.assertTrue(report["synced_folder_warning"])
        self.assertTrue(all(c[0] == "schtasks" and "/Query" in c for c in calls))   # read-only queries only

    def test_two_registered_tasks_are_flagged(self):
        report = ops.service_status(".", runner=lambda argv: (0, "Status: Ready"), platform="win32")
        self.assertIn("warning", report)


if __name__ == "__main__":
    unittest.main()
