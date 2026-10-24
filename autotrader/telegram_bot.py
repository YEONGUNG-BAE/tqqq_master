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


HELP = ("명령\n/tax 세금 예상·준비 상황\n"
        "/tax_external 금액 YYYY-MM-DD  외부 납부 재원 예정\n"
        "/tax_secured 금액  외부 재원 실제 확보액\n"
        "/tax_deposited 금액  외부 재원을 거래 계좌에 입금함\n"
        "/tax_withdraw 금액 YYYY-MM-DD  세금용 출금 등록(내역과 대조)\n"
        "/tax_withdraw_cancel 요청번호  세금용 출금 취소·재입금\n"
        "/tax_paid 금액  납부 완료\n/tax_paid_fix 총액  납부액 정정\n"
        "/tax_release 확인  남은 준비금을 일반 현금으로\n"
        "/tax_cap 금액  연간 세금 매도 상한\n/tax_early on|off  미래 회차 조기 확보\n"
        "/help 이 안내\n주문·환전·잔고 조회 명령은 없습니다. 바뀐 계획은 다음 실행 주기에 반영됩니다.")


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


# ---- tax plan commands (stage 3) ----

def plan_year(plan, now):
    """Jan-May: the year being paid; otherwise the current year (paid next May)."""
    return plan.due_year(now) or now.astimezone(_kst()).year


def _kst():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Asia/Seoul")


def _date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except (ValueError, TypeError):
        return None


def plan_lines(plan):
    def lines(year):
        st = plan.status(year, plan_now[0])
        out = render_amounts([("paid", st["paid"]), ("reserve_krw", st["reserve_krw"]),
                              ("withdrawn_pending", st["withdrawn"]),
                              ("external_planned", st["external"]["planned"]),
                              ("external_secured", st["external"]["secured"]), ("shortfall", st["shortfall"]),
                              ("installment_target", st["installment_target"]),
                              ("installment_due", st["installment_due"])])
        if st["reserve_usd"] > 0:
            out += (render_amounts([("reserve_usd_krw", st["reserve_usd_krw"])]) if st["reserve_usd_krw"] is not None
                    else ["달러 준비금: 환율 확인 전이라 원화 준비 완료로 보지 않음"])
        if st["external"]["overdue"]:
            out.append("외부 재원 예정일 경과: 확보 여부를 입력하세요 (미입력분은 계좌 부족분으로 계산)")
        out.append(f"다음 확보일: {st['next_round'] or '없음'} · 최종 확보일: {st['final_round']}")
        out.append("납부 기한: " + (st["deadline"] or "미설정") + ("" if st["deadline_verified"] else " (미검증)"))
        out.append("세금 매도 연간 상한: " + ("설정됨" if st["cap"] else "미설정 → 세금 매도는 관찰 계획만"))
        return out
    plan_now = [None]
    return lines, plan_now


def plan_handlers(plan):
    def year_of(ctx):
        return plan_year(plan, ctx.now)

    def ack(ctx, what, year):
        st = plan.status(year, ctx.now)
        return "\n".join([f"접수했습니다: {what} ({year}년 귀속)"] + render_amounts(
            [("shortfall", st["shortfall"]), ("installment_due", st["installment_due"])]) +
            ["다음 실행 주기에 반영합니다. 주문·환전은 이 명령으로 실행되지 않습니다."])

    def usage(name):
        return f"형식이 맞지 않습니다. /help 에서 {name} 형식을 확인하세요."

    def external(ctx, args):
        amount, day = (parse_amount(args[0]) if args else None), (_date(args[1]) if len(args) > 1 else None)
        if amount is None or day is None:
            return usage("/tax_external")
        plan.set_external(year_of(ctx), amount, day, ctx.now)
        return ack(ctx, "외부 재원 예정", year_of(ctx))

    def secured(ctx, args):
        amount = parse_amount(args[0]) if args else None
        if amount is None:
            return usage("/tax_secured")
        plan.set_external_secured(year_of(ctx), amount, ctx.now)
        return ack(ctx, "외부 재원 확보액", year_of(ctx))

    def deposited(ctx, args):
        amount = parse_amount(args[0]) if args else None
        if amount is None:
            return usage("/tax_deposited")
        plan.external_deposited(year_of(ctx), amount, ctx.now)
        return ack(ctx, "외부 재원 계좌 입금(준비금으로 이동)", year_of(ctx))

    def withdraw(ctx, args):
        amount, day = (parse_amount(args[0]) if args else None), (_date(args[1]) if len(args) > 1 else None)
        if amount is None or day is None:
            return usage("/tax_withdraw")
        rid = plan.request_withdrawal(year_of(ctx), amount, day, ctx.now)
        return ack(ctx, f"세금용 출금 등록(요청번호 {rid}, 출금 내역과 대조 후 반영)", year_of(ctx))

    def withdraw_cancel(ctx, args):
        rid = args[0] if args and args[0].isalnum() and len(args[0]) <= 16 else None
        if rid is None or not plan.cancel_withdrawal(rid, ctx.now):
            return "해당 출금 요청을 찾지 못했습니다."
        return ack(ctx, "세금용 출금 취소·재입금", year_of(ctx))

    def paid(ctx, args):
        amount = parse_amount(args[0]) if args else None
        if amount is None:
            return usage("/tax_paid")
        plan.record_payment(year_of(ctx), amount, ctx.now)
        return ack(ctx, "납부 완료", year_of(ctx)) + "\n남은 준비금은 /tax_release 확인 으로만 일반 현금으로 돌립니다."

    def paid_fix(ctx, args):
        amount = parse_amount(args[0]) if args else None
        if amount is None:
            return usage("/tax_paid_fix")
        plan.correct_payment(year_of(ctx), amount, ctx.now)
        return ack(ctx, "납부액 정정(감소분을 자동 재투자하지 않음)", year_of(ctx))

    def release(ctx, args):
        if args != ["확인"]:
            return "남은 준비금을 일반 현금으로 돌리려면 /tax_release 확인 을 보내세요."
        plan.release(year_of(ctx), ctx.now)
        return ack(ctx, "준비금 해제", year_of(ctx))

    def cap(ctx, args):
        amount = parse_amount(args[0]) if args else None
        if amount is None:
            return usage("/tax_cap")
        plan.set_cap(year_of(ctx), amount, "telegram user setting", "user", ctx.now)
        return ack(ctx, "연간 세금 매도 상한(변경 이력 보관)", year_of(ctx))

    def early(ctx, args):
        if args not in (["on"], ["off"]):
            return usage("/tax_early")
        plan.set_early(year_of(ctx), args[0] == "on")
        return ack(ctx, "조기 확보 " + ("켬" if args[0] == "on" else "끔"), year_of(ctx))

    return {"/tax_external": external, "/tax_secured": secured, "/tax_deposited": deposited,
            "/tax_withdraw": withdraw, "/tax_withdraw_cancel": withdraw_cancel, "/tax_paid": paid,
            "/tax_paid_fix": paid_fix, "/tax_release": release, "/tax_cap": cap, "/tax_early": early}
