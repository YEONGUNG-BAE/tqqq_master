"""Generate launchd files; never loads, installs or starts anything.

  local.tqqq.observer.plist  LaunchAgent: runs after THIS user logs in.
  local.tqqq.runner.plist    LaunchDaemon: runs at boot as THIS user (UserName),
                             never as root; still needs the disk unlocked, so
                             with FileVault on it starts only after the first
                             unlock. Same program, paths, config and account lock.

Only one of the two may be loaded at a time (README: switch and rollback).
`python -m autotrader service-status` shows what is actually installed.
"""
import getpass
import os
import plistlib
from pathlib import Path

root = Path(__file__).resolve().parent
python = root / ".venv/bin/python"
if not python.exists():
    raise SystemExit("Create .venv and install requirements first")
log = root / "logs"
log.mkdir(exist_ok=True, mode=0o700)
program = [str(python), "-m", "autotrader", "run", "--config", str(root / "config.json")]
common = {
    "ProgramArguments": program,
    "WorkingDirectory": str(root),
    "StartInterval": 60,                  # each run is short; a late network just makes early runs hold
    "RunAtLoad": True,
    "ThrottleInterval": 30,
    "StandardOutPath": str(log / "out.log"),
    "StandardErrorPath": str(log / "error.log"),
    "ProcessType": "Background",
    "Umask": 0o077,
}
agent = dict(common, Label="local.tqqq.observer")
user = getpass.getuser()
if user == "root":
    raise SystemExit("Run this as the Mac user who owns the project, not root")
daemon = dict(common, Label="local.tqqq.runner", UserName=user, GroupName="staff",
              EnvironmentVariables={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
for name, data in (("local.tqqq.observer.plist", agent), ("local.tqqq.runner.plist", daemon)):
    path = root / name
    with open(path, "wb") as f:
        plistlib.dump(data, f)
    os.chmod(path, 0o644)
    print(path)
