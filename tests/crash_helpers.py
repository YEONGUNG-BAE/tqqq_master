"""Run engine steps in a separate process that is killed at a real commit boundary.

The child opens the journal, performs the requested ticks against a persisted
paper broker, and calls os._exit(0) the moment the chosen hook fires: no
Python cleanup, no rollback, no close. The parent then reopens the database,
exactly as a restart after a power cut or kill -9 would.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHILD = r'''
import json, os, sys
sys.path[:0] = [{root!r}, {tests!r}]
from datetime import datetime, timezone, timedelta
from autotrader.core import Engine, Journal, Settings, D
from autotrader.replay import PersistentPaper
spec = json.loads(sys.argv[1])
journal = Journal(spec["db"])
broker = PersistentPaper(journal, initial=spec.get("initial"))
for symbol, price in spec.get("prices", {{}}).items():
    broker.prices[symbol] = D(price)
engine = Engine(broker, journal, Settings(mode="paper"))
engine.reverse_fx_verified = bool(spec.get("reverse_fx"))
if spec.get("tax"):
    from autotrader.tax_reserve import TaxPlan
    engine.tax_plan = TaxPlan(journal.db, {{}}, lambda y: {{"estimated_tax_krw": D(spec["tax"]), "complete": True}},
                              lambda: D(1400))
    if spec.get("cap"):
        if engine.tax_plan.cap(2026) is None:
            engine.tax_plan.set_cap(2026, D(spec["cap"]), "test", "user", datetime(2027, 1, 20, tzinfo=timezone.utc))
            journal.db.commit()
kill = spec.get("kill")
if kill:
    owner, name = kill.split(".")
    target = {{"Journal": journal, "Engine": engine, "Plan": engine.tax_plan}}[owner]
    original = getattr(target, name)
    seen = [0]
    def killer(*a, **k):
        hit = spec.get("kill_when") is None or spec["kill_when"] in json.dumps([str(x) for x in a] + [str(v) for v in k.values()])
        if hit:
            seen[0] += 1
            hit = seen[0] > spec.get("kill_skip", 0)
        if hit and spec.get("kill_before"):
            os._exit(0)
        result = original(*a, **k)
        if hit:
            os._exit(0)
        return result
    setattr(target, name, killer)
for step in spec["steps"]:
    if step.get("fill"):
        for number, order in list(broker.orders.items()):
            if order["state"] == "open":
                broker.fill(number)
        continue
    now = datetime.fromisoformat(step["now"])
    args = {{k: (datetime.fromisoformat(v) if k.endswith("deadline") and v else v) for k, v in step.items() if k != "now"}}
    try:
        engine.tick(now=now, **args)
    except Exception as exc:
        print(type(exc).__name__, exc, file=sys.stderr)
journal.db.close()
'''


def run_child(spec):
    code = CHILD.format(root=str(ROOT), tests=str(ROOT / "tests"))
    result = subprocess.run([sys.executable, "-W", "ignore", "-c", code, json.dumps(spec)],
                            capture_output=True, text=True, timeout=120)
    return result
