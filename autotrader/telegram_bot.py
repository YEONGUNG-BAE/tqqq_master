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


def tax_report_lines(db, settings, now, extra=None, estimate_fn=None, years=None):
    """Body of /tax: estimates and verification status, plus plan lines.
    `estimate_fn` must be the engine's own (same holdings and sync checks)."""
    estimate_fn = estimate_fn or (lambda y: tax.estimate(db, settings, y, holdings=None, sync=None, now=now))
    years = years or ([now.year] if now.month > 5 else [now.year - 1, now.year])
    lines = []
    for year in years:
        est = estimate_fn(year)
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


HELP = ("조회: /status (상태), /issues (문제·필요한 조치) — '상태', '왜 멈췄어', '문제'라고 보내도 됩니다\n"
        "명령 (상태 변경 명령 끝에 귀속연도를 붙일 수 있습니다. 준비 중인 연도가 둘 이상이면 꼭 붙이세요. "
        "준비 중인 연도가 없으면 계획 명령은 올해 귀속으로, 출금·납부·정정·해제는 연도를 붙여야 처리합니다)\n"
        "/tax 세금 예상·준비 상황\n"
        "/tax_external 금액 YYYY-MM-DD [연도]  외부 재원 계획 총액·예정일\n"
        "/tax_secured 금액 [연도]  외부 재원 실제 확보액(계좌 밖, 미사용)\n"
        "/tax_deposited 금액 YYYY-MM-DD [연도]  외부 재원 계좌 입금 예정(입출금 내역 확인 후 준비금)\n"
        "/tax_withdraw 금액 YYYY-MM-DD [연도]  세금용 출금 등록(내역과 대조)\n"
        "/tax_withdraw_cancel 요청번호  세금용 출금 취소·재입금\n"
        "/tax_paid 금액 [출금|외부|기타] [연도]  납부 완료(출처가 모호하면 지정 요청)\n"
        "/tax_paid_fix 총액 [연도]  납부액 정정\n"
        "/tax_release 확인 [연도]  남은 준비금을 일반 현금으로\n"
        "/tax_cap 금액 [연도]  연간 세금 매도 상한\n/tax_early on|off [연도]  미래 회차 조기 확보\n"
        "/tax_deadline YYYY-MM-DD [연도]  납부 기한(사용자 입력, 미검증)\n"
        "/help 이 안내\n주문·환전·잔고 조회 명령은 없습니다. 바뀐 계획은 다음 실행 주기에 반영됩니다.")



PLAN_UNAVAILABLE = ("세금 계획을 지금 열 수 없어 이 명령은 반영하지 않았습니다. 복구된 뒤 다시 보내 주세요. "
                    "현재 상태는 /status, 문제는 /issues 로 확인할 수 있습니다.")


class Context:
    def __init__(self, db, settings, now, extra_lines=None, handlers=None, estimate_fn=None, years=None,
                 plan_unavailable=False):
        self.db, self.settings, self.now = db, settings, now
        self.plan_unavailable = plan_unavailable
        self.extra_lines = extra_lines
        self.handlers = handlers or {}
        self.estimate_fn = estimate_fn
        self.years = years


# Read-only views. Fixed Korean phrases are aliases; free text is never read as
# an instruction, and "okay/resume" replies never unlock, resend or sell.
READONLY = {"/status": "status", "/상태": "status", "/issues": "issues", "/문제": "issues", "/help": "help",
            "/start": "help"}
ALIASES = {"상태": "/status", "왜 멈췄어": "/issues", "왜멈췄어": "/issues", "문제": "/issues"}
ACKS = ("확인했어", "재개", "괜찮아", "풀어줘", "해제")
ACK_REPLY = ("이 채팅의 답장으로는 잠금 해제·주문 재전송·verified 변경·보유 매도를 하지 않습니다. 필요한 조치는 "
             "/issues 에 나온 명령을 Mac에서 직접 실행하세요 (예: 앱 확인 후 resolve, unlock).")


def command_of(text):
    text = (text or "").strip()
    text = ALIASES.get(text, text)
    parts = text.split()
    return (parts[0].split("@")[0].lower() if parts else ""), parts


def is_readonly(text):
    command, _ = command_of(text)
    return command in READONLY or (text or "").strip() in ACKS


def handle(ctx, text):
    """Returns the reply. Database writes made here are committed by `poll`
    together with the processed update id; handlers must not commit."""
    command, parts = command_of(text)
    if text.strip() in ACKS:
        return ACK_REPLY
    if READONLY.get(command) == "status":
        from .ops import status_lines
        return "\n".join(status_lines(ctx.db, ctx.now))
    if READONLY.get(command) == "issues":
        from .ops import issues_lines
        return "\n".join(issues_lines(ctx.db, ctx.now, monitor_configured=bool(os.environ.get("MONITOR_URL"))))
    if ctx.plan_unavailable and command.startswith("/"):
        return PLAN_UNAVAILABLE                         # consumed in order, nothing applied
    if command in ("/tax", "/세금"):
        return "\n".join(tax_report_lines(ctx.db, ctx.settings, ctx.now, ctx.extra_lines, ctx.estimate_fn, ctx.years))
    if command in ctx.handlers:
        return ctx.handlers[command](ctx, parts[1:])
    return HELP


def parse_amount(text):
    try:
        value = D(text.replace(",", "").replace("₩", "").replace("원", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


SCHEMA = "CREATE TABLE IF NOT EXISTS tg_updates(update_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL)"


def _transport(transport):
    if transport is not None:
        return transport
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    return http_transport(token) if token and chat_id else None


def _offset(db):
    row = db.execute("SELECT value FROM metadata WHERE key='tg_last_update'").fetchone()
    legacy = int(json.loads(row[0])) if row else 0
    top = db.execute("SELECT MAX(update_id) FROM tg_updates").fetchone()[0] or 0
    return max(legacy, top)


def fetch(journal, transport=None):
    """One short getUpdates call. No lock is held during the network wait; the
    offset read here is only a hint - `process` re-checks inside its lock."""
    transport = _transport(transport)
    if transport is None:
        return []
    journal.db.execute(SCHEMA)
    journal.db.commit()
    response = transport("getUpdates", {"offset": _offset(journal.db) + 1, "timeout": 0,
                                        "allowed_updates": '["message"]'})
    return sorted(response.get("result", []), key=lambda u: u.get("update_id", 0)) if response.get("ok") else []


def process(journal, updates, settings, now, transport=None, *, chat_id=None, extra_lines=None, handlers=None,
            estimate_fn=None, years=None, readonly_only=False, plan_unavailable=False):
    """Apply each update at most once across every process sharing the DB.

    Per update, in one IMMEDIATE transaction: re-check it is unprocessed, run
    the handler's plan changes, record the update id (UNIQUE) and raise the
    offset to max(current, id) - an old receiver can never move it back. The
    reply is sent after the commit; a failed reply never re-runs the change.

    readonly_only (used while another process holds the state lock): answer
    the leading read-only messages and stop at the first command that could
    change the plan, so it is left for the run and the offset never skips it."""
    chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID", ""))
    transport = _transport(transport)
    journal.db.execute(SCHEMA)
    journal.db.commit()
    try:
        tax.ensure_schema(journal.db)                   # /status and /issues never depend on it
    except Exception:
        plan_unavailable = True
    ctx = Context(journal.db, settings, now, extra_lines, None if plan_unavailable else handlers, estimate_fn,
                  years, plan_unavailable=plan_unavailable)
    applied = 0
    for update in updates:
        uid = int(update.get("update_id", 0))
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        allowed = bool(chat_id) and str(chat.get("id")) == chat_id and str(sender.get("id")) == chat_id \
            and chat.get("type") == "private"
        if readonly_only and allowed and not (isinstance(message.get("text"), str) and is_readonly(message["text"])):
            break
        reply = None
        try:
            with journal.atomic(immediate=True):
                if uid <= _offset(journal.db) or journal.db.execute(
                        "SELECT 1 FROM tg_updates WHERE update_id=?", (uid,)).fetchone():
                    continue                                    # another receiver already took it
                if allowed and isinstance(message.get("text"), str):
                    reply = handle(ctx, message["text"])
                journal.db.execute("INSERT INTO tg_updates VALUES (?,?)", (uid, now.isoformat()))
                journal.db.execute("INSERT OR REPLACE INTO metadata VALUES ('tg_last_update', ?)",
                                   (json.dumps(max(uid, _offset(journal.db))),))
        except Exception as exc:
            # The handler's changes were rolled back; mark the update consumed so
            # the same message is not retried forever, and say nothing changed.
            with journal.atomic(immediate=True):
                if not journal.db.execute("SELECT 1 FROM tg_updates WHERE update_id=?", (uid,)).fetchone():
                    journal.db.execute("INSERT INTO tg_updates VALUES (?,?)", (uid, now.isoformat()))
                    journal.db.execute("INSERT OR REPLACE INTO metadata VALUES ('tg_last_update', ?)",
                                       (json.dumps(max(uid, _offset(journal.db))),))
            journal.event("telegram_command_failed", {"error_type": type(exc).__name__}, now)
            reply = "명령을 처리하지 못했습니다. 계획은 바뀌지 않았습니다." if allowed else None
        applied += 1
        if reply and transport is not None:
            try:
                transport("sendMessage", {"chat_id": chat_id, "text": remove_secrets(reply, secret_values())[:3500],
                                          "disable_web_page_preview": "true"})
            except Exception as exc:
                journal.event("telegram_reply_failed", {"error_type": type(exc).__name__}, now)
    return applied


def poll(journal, settings, now, *, transport=None, chat_id=None, extra_lines=None, handlers=None,
         estimate_fn=None, years=None, plan_unavailable=False):
    """fetch + process (used inside `run`, which already holds the state lock)."""
    transport = _transport(transport)
    if transport is None:
        return 0
    return process(journal, fetch(journal, transport), settings, now, transport, chat_id=chat_id,
                   extra_lines=extra_lines, handlers=handlers, estimate_fn=estimate_fn, years=years,
                   plan_unavailable=plan_unavailable)


# ---- tax plan commands (stage 3) ----

def plan_year(plan, now):
    """The single active tax year, or None when there are none or several."""
    active = plan.active_years(now)
    return active[0] if len(active) == 1 else None


class YearNeeded(Exception):
    pass


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
    def year_of(ctx, planning=True):
        """Default tax year. Several open years, or a money command with no open
        year, is ambiguous: the user must name the year."""
        active = plan.active_years(ctx.now)
        if len(active) == 1:
            return active[0]
        if not active and planning:
            return ctx.now.astimezone(_kst()).year       # planning for next May's payment
        raise YearNeeded()

    def take_year(args, ctx, planning=True):
        """An explicit 4-digit tax year anywhere in the arguments wins."""
        years = [a for a in args if a.isdigit() and len(a) == 4 and 2000 <= int(a) <= 2100]
        rest = [a for a in args if a not in years]
        return rest, (int(years[0]) if years else year_of(ctx, planning))

    def ack(ctx, what, year):
        st = plan.status(year, ctx.now)
        return "\n".join([f"접수했습니다: {what} ({year}년 귀속)"] + render_amounts(
            [("shortfall", st["shortfall"]), ("installment_due", st["installment_due"])]) +
            ["다음 실행 주기에 반영합니다. 주문·환전은 이 명령으로 실행되지 않습니다."])

    def usage(name):
        return f"형식이 맞지 않습니다. /help 에서 {name} 형식을 확인하세요."

    def external(ctx, args):
        rest, year = take_year(args, ctx)
        amount, day = (parse_amount(rest[0]) if rest else None), (_date(rest[1]) if len(rest) > 1 else None)
        if amount is None or day is None:
            return usage("/tax_external")
        plan.set_external(year, amount, day, ctx.now)
        return ack(ctx, "외부 재원 계획 총액·예정일", year)

    def secured(ctx, args):
        rest, year = take_year(args, ctx)
        amount = parse_amount(rest[0]) if rest else None
        if amount is None:
            return usage("/tax_secured")
        plan.set_external_secured(year, amount, ctx.now)
        return ack(ctx, "외부 재원 확보액(계좌 밖, 미사용)", year)

    def deposited(ctx, args):
        rest, year = take_year(args, ctx)
        amount, day = (parse_amount(rest[0]) if rest else None), (_date(rest[1]) if len(rest) > 1 else None)
        if amount is None or day is None:
            return usage("/tax_deposited")
        try:
            rid = plan.expect_external_deposit(year, amount, day, ctx.now)
        except ValueError:
            return "먼저 /tax_secured 로 외부 확보액을 입력하세요. 확보액보다 큰 입금은 등록하지 않습니다."
        return ack(ctx, f"외부 재원 입금 예정 등록(요청번호 {rid}). 입출금 내역에서 확인되면 준비금으로 옮깁니다", year)

    def withdraw(ctx, args):
        rest, year = take_year(args, ctx, planning=False)
        amount, day = (parse_amount(rest[0]) if rest else None), (_date(rest[1]) if len(rest) > 1 else None)
        if amount is None or day is None:
            return usage("/tax_withdraw")
        rid = plan.request_withdrawal(year, amount, day, ctx.now)
        return ack(ctx, f"세금용 출금 등록(요청번호 {rid}, 출금 내역과 대조 후 반영)", year)

    def withdraw_cancel(ctx, args):
        rid = args[0] if args and args[0].isalnum() and len(args[0]) <= 16 else None
        row = ctx.db.execute("SELECT year FROM tax_withdrawals WHERE request_id=?", (rid,)).fetchone() if rid else None
        if row is None or not plan.cancel_withdrawal(rid, ctx.now):
            return "해당 출금 요청을 찾지 못했습니다."
        return ack(ctx, "세금용 출금 취소·재입금", row[0])

    def paid(ctx, args):
        rest, year = take_year(args, ctx, planning=False)
        sources = {"출금": "withdrawn", "외부": "external", "기타": "other"}
        source = next((sources[a] for a in rest if a in sources), None)
        rest = [a for a in rest if a not in sources]
        amount = parse_amount(rest[0]) if rest else None
        if amount is None:
            return usage("/tax_paid")
        was_open = plan.payment_source_open(year)
        try:
            used = plan.record_payment(year, amount, ctx.now, source=source)
        except ValueError:
            return ("납부 출처가 모호하거나 금액이 출처 잔액보다 큽니다. 출처를 붙여 다시 보내세요: "
                    "/tax_paid 금액 출금|외부|기타 [연도]")
        label = {"withdrawn": "세금용 출금분", "external": "외부 확보분", "other": "기타 자금"}[used]
        return (ack(ctx, f"납부 완료({label}에서)", year) + "\n남은 준비금은 /tax_release 확인 으로만 일반 현금으로 돌립니다."
                + source_note(year, was_open))

    def source_note(year, was_open):
        """Where the year's payment-source issue stands after a payment change."""
        if plan.payment_source_open(year):
            return (f"\n이전 버전이 출처 없이 기록한 납부가 남아 있을 수 있어 세금 목적 매도 보류를 유지합니다. "
                    f"/tax_paid_fix 0 {year} 후 실제 납부를 출처와 함께 /tax_paid 로 다시 입력하세요.")
        return "\n출처 미확인 납부 문제를 해결 처리했습니다(감사 기록 남김)." if was_open else ""

    def paid_fix(ctx, args):
        rest, year = take_year(args, ctx, planning=False)
        amount = parse_amount(rest[0]) if rest else None
        if amount is None:
            return usage("/tax_paid_fix")
        was_open = plan.payment_source_open(year)
        plan.correct_payment(year, amount, ctx.now)
        return ack(ctx, "납부액 정정(감소분을 자동 재투자하지 않음)", year) + source_note(year, was_open)

    def release(ctx, args):
        rest, year = take_year(args, ctx, planning=False)
        if rest != ["확인"]:
            return "남은 준비금을 일반 현금으로 돌리려면 /tax_release 확인 [연도] 를 보내세요."
        plan.release(year, ctx.now)
        return ack(ctx, "준비금 해제", year)

    def cap(ctx, args):
        rest, year = take_year(args, ctx)
        amount = parse_amount(rest[0]) if rest else None
        if amount is None:
            return usage("/tax_cap")
        plan.set_cap(year, amount, "telegram user setting", "user", ctx.now)
        return ack(ctx, "연간 세금 매도 상한(변경 이력 보관)", year)

    def early(ctx, args):
        rest, year = take_year(args, ctx)
        if rest not in (["on"], ["off"]):
            return usage("/tax_early")
        plan.set_early(year, rest[0] == "on")
        return ack(ctx, "조기 확보 " + ("켬" if rest[0] == "on" else "끔"), year)

    def deadline(ctx, args):
        rest, year = take_year(args, ctx)
        day = _date(rest[0]) if rest else None
        if day is None:
            return usage("/tax_deadline")
        plan.set_deadline(year, day, ctx.now)
        return ack(ctx, f"{year}년 귀속 납부 기한 {day} (사용자 입력, 미검증)", year)

    def guarded(fn):
        def run(ctx, args):
            try:
                return fn(ctx, args)
            except YearNeeded:
                return ("대상 귀속연도가 모호합니다. 명령 끝에 연도를 붙여 다시 보내세요 (예: 2026). "
                        "현재 준비 중인 연도: " + (", ".join(map(str, plan.active_years(ctx.now))) or "없음"))
        return run
    handlers = {"/tax_external": external, "/tax_secured": secured, "/tax_deposited": deposited,
                "/tax_withdraw": withdraw, "/tax_withdraw_cancel": withdraw_cancel, "/tax_paid": paid,
                "/tax_paid_fix": paid_fix, "/tax_release": release, "/tax_cap": cap, "/tax_early": early,
                "/tax_deadline": deadline}
    return {name: guarded(fn) for name, fn in handlers.items()}
