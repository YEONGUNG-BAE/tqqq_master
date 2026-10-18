"""Generate a per-user launchd file; does not load it or start trading."""
import plistlib
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
python = root / ".venv/bin/python"
if not python.exists():
    raise SystemExit("Create .venv and install requirements first")
log = root / "logs"
log.mkdir(exist_ok=True)
destination = root / "local.tqqq.observer.plist"
with open(destination, "wb") as f:
    plistlib.dump({
        "Label": "local.tqqq.observer",
        "ProgramArguments": [str(python), "-m", "autotrader", "run", "--config", str(root / "config.json")],
        "WorkingDirectory": str(root),
        "StartInterval": 60,
        "RunAtLoad": True,
        "StandardOutPath": str(log / "out.log"),
        "StandardErrorPath": str(log / "error.log"),
        "ProcessType": "Background"
    }, f)
print(destination)
