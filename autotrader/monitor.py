"""Independent external monitor. Runs on a host OTHER than the trading Mac.

It has no broker key, no account access and no order permission, and never
takes over trading. It receives signed status heartbeats from the trading Mac
and alerts directly (Telegram, plus an optional backup webhook) when:

  no_heartbeat     nothing arrived in time: the Mac, its network or the process
                   is down (the Mac could not send anything out)
  locked           alive, but trading is hard-locked
  held             alive, but runs keep ending in a hold or an error
  signal_stale     alive, but the completed-close signal did not advance
  switch_overdue   a confirmed switch is not finished by its completion deadline

Session timing (holidays, early closes, daylight saving) comes from the exchange
calendar. A deadline passing only raises an alert: it never unlocks, overrides
a safety check or chases prices.

Run:   python -m autotrader.monitor serve --config monitor.json
       python -m autotrader.monitor check --config monitor.json   (cron style)
Secrets (environment on the monitor host): MONITOR_SECRET, MONITOR_TELEGRAM_BOT_TOKEN,
MONITOR_TELEGRAM_CHAT_ID, optional MONITOR_BACKUP_WEBHOOK.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .heartbeat import FIELDS

UTC = timezone.utc
DEFAULTS = {
    # Operational defaults, not tuned to backtest returns:
    "heartbeat_timeout_seconds": 300,   # 5 missed 60-second runs: tolerates a few slow/failed runs
    "work_freshness_seconds": 900,      # 15 min of consecutive holds/errors before alerting
    "signal_grace_minutes": 120,        # the Mac refreshes ~5 min after the close; allow slow data
    "switch_grace_minutes": 15,         # after the recovery cutoff of the first session after the signal
    "recovery_buffer_minutes": 10,      # matches the engine's recovery_close_buffer_minutes default
    "max_clock_skew_seconds": 300,      # older or future-dated messages are rejected
    "alert_repeat_hours": 6,
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeats(instance TEXT PRIMARY KEY, seq INTEGER NOT NULL, received_at TEXT NOT NULL,
                                      status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts(key TEXT PRIMARY KEY, raised_at TEXT NOT NULL, last_sent TEXT, delivered INTEGER NOT NULL);
"""


def telegram(token, chat_id, opener=urlopen):
    def send(text):
        data = urlencode({"chat_id": chat_id, "text": text}).encode()
        with opener(Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"),
                    timeout=10) as r:
            if not json.loads(r.read(100_000)).get("ok"):
                raise RuntimeError("telegram refused")
    return send


def webhook(url, opener=urlopen):
    def send(text):
        body = json.dumps({"text": text}).encode()
        with opener(Request(url, data=body, method="POST", headers={"Content-Type": "application/json"}),
                    timeout=10) as r:
            if not 200 <= getattr(r, "status", 200) < 300:
                raise RuntimeError("webhook refused")
    return send


def env_channels():
    channels = []
    if os.environ.get("MONITOR_TELEGRAM_BOT_TOKEN") and os.environ.get("MONITOR_TELEGRAM_CHAT_ID"):
        channels.append(("telegram", telegram(os.environ["MONITOR_TELEGRAM_BOT_TOKEN"],
                                              os.environ["MONITOR_TELEGRAM_CHAT_ID"])))
    if os.environ.get("MONITOR_BACKUP_WEBHOOK"):
        channels.append(("backup_webhook", webhook(os.environ["MONITOR_BACKUP_WEBHOOK"])))
    return channels


MESSAGES = {
    "no_heartbeat": "거래 Mac에서 상태 신호가 오지 않습니다. Mac 전원·네트워크·실행 프로세스를 확인하세요.",
    "locked": "거래 Mac은 살아 있지만 거래가 잠겨 있습니다. 잠금 알림의 필요한 조치를 확인하세요.",
    "held": "거래 Mac은 살아 있지만 실행이 계속 보류·오류로 끝납니다.",
    "signal_stale": "거래 Mac은 살아 있지만 종가 신호가 갱신되지 않았습니다.",
    "switch_overdue": "확정 신호에 따른 전환이 완료 기한까지 끝나지 않았습니다. (외부 감시는 주문하지 않습니다)",
}


class Monitor:
    def __init__(self, store, config, secret, channels, schedule_factory=None, sleep=time.sleep):
        self.db = sqlite3.connect(store, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.cfg = dict(DEFAULTS, **(config or {}))
        self.instances = set(self.cfg.get("instances", []))
        self.secret = secret.encode()
        self.channels = channels
        self.lock = threading.Lock()
        self.sleep = sleep
        if schedule_factory is None:
            from .schedule import Schedule
            schedule_factory = Schedule
        self.schedule_factory = schedule_factory

    # ---- receiving ----

    def receive(self, body, signature, now):
        """(HTTP status, short reason). Verifies signature, instance, send time
        and sequence; a replayed or older message can never revive the state."""
        expected = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            return 401, "bad signature"
        try:
            message = json.loads(body)
            sent = datetime.fromisoformat(message["sent_at"])
            seq, instance = int(message["seq"]), str(message["instance"])
        except (ValueError, KeyError, TypeError):
            return 400, "malformed"
        if instance not in self.instances:
            return 403, "unknown instance"
        if abs((now - sent).total_seconds()) > self.cfg["max_clock_skew_seconds"]:
            return 409, "stale or future message"
        status = {k: message.get(k) for k in FIELDS}
        with self.lock:
            row = self.db.execute("SELECT seq FROM heartbeats WHERE instance=?", (instance,)).fetchone()
            if row and seq == row[0]:
                return 200, "duplicate"
            if row and seq < row[0]:
                return 409, "older sequence"
            self.db.execute("INSERT OR REPLACE INTO heartbeats VALUES (?,?,?,?)",
                            (instance, seq, now.isoformat(), json.dumps(status)))
            self.db.commit()
        return 200, "ok"

    # ---- evaluating ----

    def evaluate(self, now):
        conditions = []
        schedule = self.schedule_factory(now)
        for instance in sorted(self.instances):
            row = self.db.execute("SELECT received_at, status FROM heartbeats WHERE instance=?", (instance,)).fetchone()
            if row is None or (now - datetime.fromisoformat(row[0])).total_seconds() > self.cfg["heartbeat_timeout_seconds"]:
                conditions.append((f"{instance}:no_heartbeat", "no_heartbeat"))
                continue
            st = json.loads(row[1])
            if st.get("lock_code"):
                conditions.append((f"{instance}:locked", "locked"))
            work = st.get("work_done_at")
            if work is None or (now - datetime.fromisoformat(work)).total_seconds() > self.cfg["work_freshness_seconds"]:
                conditions.append((f"{instance}:held", "held"))
            expected = schedule.expected_close(now)
            close = next(s[2] for s in schedule.sessions if s[0] == expected)
            if (st.get("signal_date") or "") < expected and now > close + timedelta(minutes=self.cfg["signal_grace_minutes"]):
                conditions.append((f"{instance}:signal_stale", "signal_stale"))
            if st.get("switch_status") in ("selling", "buying") and st.get("switch_signal_date"):
                later = [s for s in schedule.sessions if s[0] > st["switch_signal_date"]]
                if later:
                    deadline = (later[0][2] - timedelta(minutes=self.cfg["recovery_buffer_minutes"])
                                + timedelta(minutes=self.cfg["switch_grace_minutes"]))
                    if now > deadline:
                        conditions.append((f"{instance}:switch_overdue", "switch_overdue"))
        return conditions

    # ---- alerting ----

    def _deliver(self, text):
        """Primary channel first; backup only if it fails. Up to 3 tries per channel."""
        for name, channel in self.channels:
            for attempt in range(3):
                try:
                    channel(text)
                    return name
                except Exception:
                    self.sleep(min(10, 2 ** attempt))
        return None

    def check(self, now):
        active = self.evaluate(now)
        keys = {k for k, _ in active}
        repeat = timedelta(hours=self.cfg["alert_repeat_hours"])
        sent = []
        for key, kind in active:
            row = self.db.execute("SELECT last_sent, delivered FROM alerts WHERE key=?", (key,)).fetchone()
            due = row is None or not row[1] or row[0] is None or now - datetime.fromisoformat(row[0]) >= repeat
            if not due:
                continue
            channel = self._deliver(f"[외부 감시] {MESSAGES[kind]} ({key.split(':')[0]})")
            self.db.execute("INSERT INTO alerts VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET last_sent=excluded.last_sent, "
                            "delivered=excluded.delivered", (key, now.isoformat(), now.isoformat(), 1 if channel else 0))
            sent.append((key, channel))
        for (key,) in self.db.execute("SELECT key FROM alerts").fetchall():
            if key not in keys:
                kind = key.split(":", 1)[1]
                if self._deliver(f"[외부 감시] 해소됨: {MESSAGES.get(kind, kind)} ({key.split(':')[0]})"):
                    self.db.execute("DELETE FROM alerts WHERE key=?", (key,))
                sent.append((key, "resolved"))
        self.db.commit()
        return sent


def serve(monitor, host, port, interval=60):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/heartbeat":
                self.send_response(404)
                self.end_headers()
                return
            body = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 10_000))
            code, reason = monitor.receive(body, self.headers.get("X-Signature", ""), datetime.now(UTC))
            self.send_response(code)
            self.end_headers()
            self.wfile.write(reason.encode())

        def log_message(self, *args):     # no request logging: nothing sensitive is written
            pass

    def loop():
        while True:
            try:
                monitor.check(datetime.now(UTC))
            except Exception:
                pass
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main():
    p = argparse.ArgumentParser(description="External monitor for the TQQQ automation (no broker access)")
    p.add_argument("command", choices=["serve", "check"])
    p.add_argument("--config", default="monitor.json")
    args = p.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    secret = os.environ.get("MONITOR_SECRET", "")
    if len(secret) < 32:
        raise SystemExit("Set MONITOR_SECRET (32+ characters) in the monitor host environment")
    monitor = Monitor(cfg.get("store", "monitor.sqlite"), cfg, secret, env_channels())
    if args.command == "check":
        print(json.dumps(monitor.check(datetime.now(UTC)), ensure_ascii=False))
    else:
        serve(monitor, cfg.get("host", "0.0.0.0"), int(cfg.get("port", 8787)))


if __name__ == "__main__":
    main()
