"""Result notifications: macOS, optional SMTP, optional Telegram.

Notifications report outcomes; they never ask for a decision. Delivery failure
is recorded and otherwise ignored: it can never reset trading state or cause a
request to be resent. Repeats of the same cause are suppressed across restarts
(the record lives in the state database). Texts carry no keys, tokens, account
or order numbers, balances or amounts.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Repeat window per category: same (category, code) is not sent again inside it.
WINDOWS = {"hold": timedelta(hours=6), "lock": timedelta(hours=6), "carryover": timedelta(days=7),
           "recovered": timedelta(minutes=30), "done": timedelta(hours=12), "ok": timedelta(hours=20)}
RETRY_AFTER_FAILURE = timedelta(minutes=30)
RETRY_GIVE_UP = timedelta(hours=24)     # a failed message is retried for at most a day
TITLES = {"ok": "정상", "done": "완료", "hold": "보류", "recovered": "복구", "lock": "잠금", "carryover": "이월"}
FOOTERS = {"hold": "조건이 해소되면 자동으로 재개합니다.",
           "carryover": "다음 실행창에서 최신 신호와 잔고로 다시 계산합니다.",
           "lock": "신규 주문·환전을 멈췄습니다. 조회와 상태 대조는 계속합니다."}
_LONG_NUMBER = re.compile(r"\d[\d,]{5,}|\d+\.\d+")


def scrub(text, secrets=()):
    """Mask configured secrets, long digit runs (account/order numbers, amounts)
    and decimal figures (prices, balances)."""
    text = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    return _LONG_NUMBER.sub("#", text)[:400]


def macos_channel(text):
    if sys.platform != "darwin":
        return
    # Text passed as an argument, never interpolated into AppleScript source.
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    'display notification (item 1 of argv) with title "TQQQ 자동화"', "-e", "end run", text],
                   timeout=5, check=False, capture_output=True)


def smtp_channel(text):
    mail = EmailMessage()
    mail["From"] = os.environ["ALERT_FROM"]
    mail["To"] = os.environ["ALERT_TO"]
    mail["Subject"] = "TQQQ 자동화 알림"
    mail.set_content(text)
    with smtplib.SMTP_SSL(os.environ["ALERT_SMTP_HOST"], int(os.environ.get("ALERT_SMTP_PORT", "465")), timeout=10) as smtp:
        if os.environ.get("ALERT_SMTP_USER"):
            smtp.login(os.environ["ALERT_SMTP_USER"], os.environ["ALERT_SMTP_PASSWORD"])
        smtp.send_message(mail)


def telegram_channel(token, chat_id, opener=urlopen):
    def send(text):
        data = urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
        request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
        with opener(request, timeout=10) as response:
            body = json.loads(response.read(100_000))
        if not body.get("ok"):
            raise RuntimeError("Telegram did not accept the message")
    return send


def default_channels():
    channels = {"macos": macos_channel}
    if os.environ.get("ALERT_SMTP_HOST") and os.environ.get("ALERT_TO") and os.environ.get("ALERT_FROM"):
        channels["smtp"] = smtp_channel
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        channels["telegram"] = telegram_channel(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    return channels


def secret_values():
    return [os.environ.get(k, "") for k in ("MERITZ_CLIENT_ID", "MERITZ_CLIENT_SECRET", "MERITZ_MAC_ADDRESS",
                                            "ALERT_SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")]


class Notifier:
    def __init__(self, journal, *, enabled=True, channels=None):
        self.j = journal
        self.enabled = enabled
        self.channels = channels

    def send(self, category, code, message, now, *, required=None):
        """Returns True if a notification was attempted. Never raises."""
        try:
            return self._send(category, code, message, now, required)
        except Exception as exc:
            try:
                self.j.event("notification_failed", {"category": category, "error_type": type(exc).__name__}, now)
            except Exception:
                pass
            return False

    def _send(self, category, code, message, now, required):
        """Per-channel bookkeeping: a channel that delivered is quiet for the
        category window; a channel that failed is retried after 30 minutes, even
        when another channel succeeded."""
        key = f"{category}:{code}"
        sent = self.j.meta("notified", {})
        record = sent.get(key) or {}
        if "channels" not in record and record.get("at"):
            record = {"channels": {}, "legacy": record}   # older single-status records
        channels = (default_channels() if self.channels is None else self.channels) if self.enabled else {}
        due = {}
        for name, channel in channels.items():
            last = record["channels"].get(name) if record else None
            if last is None and record.get("legacy"):
                last = record["legacy"]
            if last and now - datetime.fromisoformat(last["at"]) < (WINDOWS[category] if last.get("ok") else RETRY_AFTER_FAILURE):
                continue
            due[name] = channel
        if not due and (self.enabled or record):
            return False
        lines = [f"[{TITLES[category]}] {message}"]
        if required:
            lines.append("필요한 조치: " + required)
        if FOOTERS.get(category):
            lines.append(FOOTERS[category])
        text = scrub("\n".join(lines), secret_values())
        delivered, failed = [], []
        status = dict(record.get("channels", {})) if record else {}
        for name, channel in due.items():
            try:
                channel(text)
                delivered.append(name)
                status[name] = {"at": now.isoformat(), "ok": True}
            except Exception as exc:   # delivery problems never touch trading state
                failed.append({"channel": name, "error_type": type(exc).__name__})
                status[name] = {"at": now.isoformat(), "ok": False}
        sent[key] = {"at": now.isoformat(), "channels": status}
        self._remember_failures(key, category, text, delivered, failed, now)
        cutoff = now - timedelta(days=8)
        sent = {k: v for k, v in sent.items() if datetime.fromisoformat(v["at"]) >= cutoff}
        self.j.put_meta("notified", sent)
        self.j.event("notification", {"category": category, "code": code, "text": text,
                                      "delivered": delivered, "failed": failed}, now)
        return True

    def _remember_failures(self, key, category, text, delivered, failed, now):
        """Persist the exact text for channels that failed, so the next run can
        retry them even if the caller never raises the same event again."""
        pending = self.j.meta("notify_pending", {})
        entry = pending.get(key) or {"category": category, "text": text, "channels": [],
                                     "first": now.isoformat(), "attempts": 0}
        entry["channels"] = sorted((set(entry["channels"]) - set(delivered)) | {f["channel"] for f in failed})
        if failed:
            entry.update(text=text, attempts=entry["attempts"] + 1,
                         retry_at=(now + RETRY_AFTER_FAILURE).isoformat())
        if entry["channels"]:
            pending[key] = entry
        else:
            pending.pop(key, None)
        self.j.put_meta("notify_pending", pending)

    def retry_pending(self, now):
        """Called at the start of every run. Re-sends stored texts to channels whose
        retry time has passed. Never raises; gives up after RETRY_GIVE_UP."""
        try:
            pending = self.j.meta("notify_pending", {})
            if not pending or not self.enabled:
                return 0
            channels = default_channels() if self.channels is None else self.channels
            sent = self.j.meta("notified", {})
            attempted = 0
            for key, entry in list(pending.items()):
                if now - datetime.fromisoformat(entry["first"]) >= RETRY_GIVE_UP:
                    self.j.event("notification_dropped", {"key": key, "channels": entry["channels"]}, now)
                    pending.pop(key)
                    continue
                if now < datetime.fromisoformat(entry["retry_at"]):
                    continue
                delivered, failed = [], []
                for name in entry["channels"]:
                    channel = channels.get(name)
                    if channel is None:          # channel no longer configured
                        delivered.append(name)
                        continue
                    attempted += 1
                    try:
                        channel(entry["text"])
                        delivered.append(name)
                    except Exception as exc:
                        failed.append({"channel": name, "error_type": type(exc).__name__})
                record = sent.setdefault(key, {"at": now.isoformat(), "channels": {}})
                record.setdefault("channels", {})
                for name in delivered:
                    record["channels"][name] = {"at": now.isoformat(), "ok": True}
                for f in failed:
                    record["channels"][f["channel"]] = {"at": now.isoformat(), "ok": False}
                entry["channels"] = [f["channel"] for f in failed]
                entry["attempts"] += 1
                entry["retry_at"] = (now + RETRY_AFTER_FAILURE).isoformat()
                if not entry["channels"]:
                    pending.pop(key)
                self.j.event("notification_retry", {"key": key, "delivered": delivered, "failed": failed}, now)
            self.j.put_meta("notified", sent)
            self.j.put_meta("notify_pending", pending)
            return attempted
        except Exception as exc:
            try:
                self.j.event("notification_failed", {"stage": "retry", "error_type": type(exc).__name__}, now)
            except Exception:
                pass
            return 0
