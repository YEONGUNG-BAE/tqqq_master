"""Status heartbeat from the trading Mac to an independent external monitor.

Sent after every run (success, hold, lock or error), with a short timeout, after
the trading tick has finished: a monitor outage can never delay an order or
change trading state. The payload is a fixed whitelist of fields; no quantity,
amount, return, account or order number, raw error, key or token is ever sent.
The instance id is a random opaque value unrelated to the account.

Integrity: HMAC-SHA256 over the exact body with a shared secret
(MONITOR_SECRET in secrets.env), a send time and a strictly increasing sequence
number persisted in the state database, so the monitor can reject replays and
stale messages.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from urllib.request import Request, urlopen
from uuid import uuid4

FIELDS = ("v", "instance", "seq", "sent_at", "run_at", "work_done_at", "outcome", "signal_date", "target",
          "aligned", "switch_status", "switch_to", "switch_signal_date", "hold_code", "lock_code",
          # switch progress (progress.py): last real progress, wait code, local alert level and delivery
          "switch_priority", "switch_last_progress", "switch_wait_code", "switch_alert", "switch_alert_delivered")
_CODE = re.compile(r"[^a-z_]")


def code_only(text):
    """A short lowercase code without digits, so messages that might contain
    numbers or identifiers are never forwarded."""
    if not text:
        return None
    return _CODE.sub("", str(text).lower().replace(" ", "_"))[:60] or None


def instance_id(journal):
    value = journal.meta("monitor_instance")
    if not value:
        value = "inst-" + uuid4().hex[:12]
        journal.put_meta("monitor_instance", value)
    return value


def build(journal, *, now, run_at, outcome, work_done):
    """Next status message (also increments the persisted sequence)."""
    signal = journal.meta("signal") or {}
    position = journal.meta("position_state") or {}
    intent = journal.meta("switch_intent") or {}
    lock = journal.meta("lock_info") or {}
    hold = journal.meta("active_hold") or {}
    seq = int(journal.meta("monitor_seq", 0) or 0) + 1
    journal.put_meta("monitor_seq", seq)
    message = {"v": 1, "instance": instance_id(journal), "seq": seq, "sent_at": now.isoformat(),
               "run_at": run_at.isoformat(), "work_done_at": now.isoformat() if work_done else None,
               "outcome": outcome, "signal_date": signal.get("signal_date"), "target": signal.get("target"),
               "aligned": position.get("aligned") if position.get("target") == signal.get("target") else None,
               "switch_status": intent.get("status"), "switch_to": intent.get("to"),
               "switch_signal_date": intent.get("signal_date"),
               "hold_code": code_only(hold.get("message")) if outcome == "hold" else None,
               "lock_code": code_only(lock.get("code")) if journal.meta("halt") else None}
    prog = journal.meta("switch_progress") or {}
    active = bool(prog.get("intent_id")) and not prog.get("closed_at")
    message.update(switch_priority=code_only(prog.get("priority")) if active else None,
                   switch_last_progress=prog.get("last_progress_at") if active else None,
                   switch_wait_code=code_only(prog.get("wait_code")) if active else None,
                   switch_alert=code_only(prog.get("alert_level")) if active else None,
                   switch_alert_delivered=bool(prog.get("alert_delivered")) if active and prog.get("alert_level") else None)
    if work_done:
        journal.put_meta("monitor_last_work", now.isoformat())
    else:
        message["work_done_at"] = journal.meta("monitor_last_work")
    return {k: message[k] for k in FIELDS}


def sign(body, secret):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send(message, *, url=None, secret=None, opener=None, timeout=3):
    """POST the message. Returns True on HTTP success; never raises."""
    url = url or os.environ.get("MONITOR_URL", "")
    secret = secret or os.environ.get("MONITOR_SECRET", "")
    if not url or not secret:
        return False
    body = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    request = Request(url, data=body, method="POST",
                      headers={"Content-Type": "application/json", "X-Signature": sign(body, secret)})
    try:
        with (opener or urlopen)(request, timeout=timeout) as response:
            return 200 <= getattr(response, "status", 200) < 300
    except Exception:
        return False


def publish(journal, *, now, run_at, outcome, work_done, sender=send):
    """Queue (latest status in the journal) and send once. A failure is
    recorded; the next run sends a newer status, never this one again."""
    try:
        message = build(journal, now=now, run_at=run_at, outcome=outcome, work_done=work_done)
        journal.put_meta("monitor_outbox", message)
        ok = sender(message)
        journal.put_meta("monitor_last_send", {"at": now.isoformat(), "ok": bool(ok), "seq": message["seq"]})
        return ok
    except Exception as exc:
        try:
            journal.event("monitor_send_failed", {"error_type": type(exc).__name__}, now)
        except Exception:
            pass
        return False
