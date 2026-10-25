"""Generate Windows Task Scheduler files; never registers or starts anything.

  tqqq-task-logon.xml    runs `autotrader run` every minute after THIS user logs on
                         (InteractiveToken: only while logged on, no stored password)
  tqqq-task-startup.xml  runs every minute from boot, logged on or not; Windows
                         stores the user's password when you register it (schtasks /RP *)

Both: least privilege (no "highest privileges"), absolute paths, no secrets in
the command line or XML, IgnoreNew when a run is still going (the app's own
account lock is the second guard), no catch-up burst after sleep
(StartWhenAvailable false), a short start delay for the network.
Register only ONE of them (README: install, check, remove).

Usage (on the Windows PC, in the project folder):
  .venv\\Scripts\\python.exe install_windows_task.py
Options --root/--python/--config/--user exist for review on another machine.
"""
import argparse
import getpass
import os
import sys
from pathlib import Path, PureWindowsPath
from xml.sax.saxutils import escape

TASKS = {"logon": r"\tqqq\run-on-logon", "startup": r"\tqqq\run-at-startup"}


def task_xml(kind, python, config, workdir, user):
    # Schema order: the base trigger elements (Enabled, Repetition) come
    # before the trigger's own (UserId, Delay).
    trigger = ("<LogonTrigger><Enabled>true</Enabled>{rep}<UserId>{user}</UserId><Delay>PT1M</Delay></LogonTrigger>"
               if kind == "logon" else
               "<BootTrigger><Enabled>true</Enabled>{rep}<Delay>PT2M</Delay></BootTrigger>")
    repetition = "<Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
    logon_type = "InteractiveToken" if kind == "logon" else "Password"
    arguments = f'-m autotrader run --config "{config}"'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>TQQQ/SGOV automation: one short run per minute (generated; contains no secrets)</Description>
  </RegistrationInfo>
  <Triggers>{trigger.format(user=escape(user), rep=repetition)}</Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>{logon_type}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Priority>7</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def main(argv=None):
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Generate Task Scheduler XML (does not register anything)")
    p.add_argument("--root", default=str(here))
    p.add_argument("--python")
    p.add_argument("--config")
    p.add_argument("--user", help="DOMAIN\\user or COMPUTER\\user (default: the current user)")
    args = p.parse_args(argv)
    root = Path(args.root)
    windows_paths = os.name == "nt" or args.python
    python = args.python or str(root / ".venv" / "Scripts" / "python.exe")
    config = args.config or str(root / "config.json")
    if os.name == "nt" and not Path(python).exists():
        raise SystemExit("Create .venv and install requirements first (.venv\\Scripts\\python.exe not found)")
    for value in (python, config):
        if not (PureWindowsPath(value).is_absolute() or Path(value).is_absolute()):
            raise SystemExit("Python and config paths must be absolute")
    if "onedrive" in str(root).lower():
        print("WARNING: the project is under OneDrive; keep the state DB and token in a local, non-synced folder",
              file=sys.stderr)
    user = args.user or (f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
                         if os.name == "nt" else getpass.getuser())
    if not windows_paths:
        print("note: generated on a non-Windows machine for review only", file=sys.stderr)
    for kind in TASKS:
        path = root / f"tqqq-task-{kind}.xml"
        path.write_text(task_xml(kind, python, config, root, user), encoding="utf-16")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
