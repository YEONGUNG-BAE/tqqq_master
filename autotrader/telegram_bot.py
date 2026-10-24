"""Telegram command intake (/tax and tax-plan inputs). Never trades.

- Only messages from the configured private chat (TELEGRAM_CHAT_ID, which in a
  private chat is also the sender id) are acted on; others get no reply.
- Each update is processed once: the handler's database changes and the
  processed update id are committed in the same transaction, so a re-delivered
  message or a restart cannot apply a plan change twice.
- One short getUpdates call (timeout 0, 5 s HTTP limit) per invocation, so the
  trading path and the account lock are never held by a long poll.
- Commands only change tax-plan values; the engine applies them on its next run.
  Replies never echo raw API data, exceptions, user free text, keys or order ids.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import tax
from .notify import remove_secrets, render_amounts, secret_values

D = Decimal
API = "https://api.telegram.org/bot{token}/{method}"
REASON_KINDS = (("fee missing", "수수료 누락"), ("settlement FX rate missing", "결제일 환율 누락"),
                ("FX rate out of range", "환율 단위 이상"), ("FX rate from an earlier posting", "이전 고시 환율 사용"),
                ("settlement date unknown", "결제일 미확인"), ("acquisition missing", "취득 내역 누락"),
                ("duplicate_fill_conflict", "중복 체결 불일치"), ("fill_time_missing", "체결 시각 누락"),
                ("holdings not explained", "원장과 보유 수량 불일치"),
                ("other-broker figures marked incomplete", "다른 증권사 자료 미완료"),
                ("broker realized P/L differs", "증권사 실현손익과 큰 차이"))


def http_transport(token, opener=urlopen, timeout=5):
    def call(method, params):
        request = Request(API.format(token=token, method=method), data=urlencode(params).encode(), method="POST")
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read(1_000_000))
    return call


def summarize_reasons(reasons):
    """Kinds and counts only: reason strings can contain order identifiers."""
    counts = {}
    for reason in reasons:
        label = next((ko for en, ko in REASON_KINDS if en in reason), None)
        if label:
            counts[label] = counts.get(label, 0) + 1
    return [f"{label} {n}건" for label, n in counts.items()]


def tax_report_lines(db, settings, now, extra=None):
    """Body of /tax: estimates and verification status, plus plan lines from
    later stages (reserve, external funds, schedule) when available."""
    years = [now.year] if now.month > 5 else [now.year - 1, now.year]
    lines = []
    for year in years:
        est = tax.estimate(db, settings, year)
        status = "확정 가능 자료" if est["complete"] else "추정"
        lines.append(f"[{year}년 귀속] {status}" + (" · 이 계좌만의 추정" if est["this_account_only"] else ""))
        lines += render_amounts([("estimated_tax", est["estimated_tax_krw"]), ("account_net", est["account_net_krw"]),
                                 ("external_net", est["external_net_krw"]), ("deduction", est["deduction_krw"]),
                                 ("taxable", est["taxable_krw"])])
        if est["dividends"]["count"]:
            lines += render_amounts([("dividends_usd", est["dividends"]["amount_usd"])])
            lines.append("배당은 양도손익과 별도 집계(종합과세 확인용, 세액 계산 안 함)")
        missing = summarize_reasons(est["reasons"])
        if missing:
            lines.append("자료 확인 필요: " + ", ".join(missing))
        held = summarize_reasons(est["open_lot_problems"])
        if held and year == years[-1]:
            lines.append("보유분 자료 확인 필요(세금 목적 매도 보류): " + ", ".join(held))
        if extra:
            lines += extra(year)
    lines.append("주문·환전은 이 명령으로 실행되지 않습니다.")
    return lines


HELP = ("명령\n/tax 세금 예상·준비 상황\n/help 이 안내\n"
        "주문·환전·잔고 조회 명령은 없습니다.")


class Context:
    def __init__(self, db, settings, now, extra_lines=None, handlers=None):
        self.db, self.settings, self.now = db, settings, now
        self.extra_lines = extra_lines
        self.handlers = handlers or {}


def handle(ctx, text):
    """Returns the reply. Database writes made here are committed by `poll`
    together with the processed update id; handlers must not commit."""
    parts = text.strip().split()
    command = parts[0].split("@")[0].lower() if parts else ""
    if command in ("/tax", "/세금"):
        return "\n".join(tax_report_lines(ctx.db, ctx.settings, ctx.now, ctx.extra_lines))
    if command in ctx.handlers:
        return ctx.handlers[command](ctx, parts[1:])
    return HELP


def parse_amount(text):
    try:
        value = D(text.replace(",", "").replace("₩", "").replace("원", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def poll(journal, settings, now, *, transport=None, chat_id=None, extra_lines=None, handlers=None):
    """Process pending updates once. Returns the number of updates consumed."""
    chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID", ""))
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if transport is None:
        if not token or not chat_id:
            return 0
        transport = http_transport(token)
    tax.ensure_schema(journal.db)
    last = int(journal.meta("tg_last_update", 0) or 0)
    response = transport("getUpdates", {"offset": last + 1, "timeout": 0, "allowed_updates": '["message"]'})
    if not response.get("ok"):
        return 0
    consumed = 0
    ctx = Context(journal.db, settings, now, extra_lines, handlers)
    for update in sorted(response.get("result", []), key=lambda u: u.get("update_id", 0)):
        uid = int(update.get("update_id", 0))
        if uid <= last:
            continue
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        allowed = (chat_id and str(chat.get("id")) == chat_id and str(sender.get("id")) == chat_id
                   and chat.get("type") == "private")
        reply = None
        db = journal.db
        try:
            if allowed and isinstance(message.get("text"), str):
                reply = handle(ctx, message["text"])
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('tg_last_update', ?)", (json.dumps(uid),))
            db.commit()
        except Exception as exc:
            db.rollback()
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('tg_last_update', ?)", (json.dumps(uid),))
            db.commit()
            journal.event("telegram_command_failed", {"error_type": type(exc).__name__}, now)
            reply = "명령을 처리하지 못했습니다. 계획은 바뀌지 않았습니다." if allowed else None
        last = uid
        consumed += 1
        if reply:
            try:
                transport("sendMessage", {"chat_id": chat_id, "text": remove_secrets(reply, secret_values())[:3500],
                                          "disable_web_page_preview": "true"})
            except Exception as exc:
                journal.event("telegram_reply_failed", {"error_type": type(exc).__name__}, now)
    return consumed
