"""Switch progress, kept apart from process liveness and API success.

Three different facts:
  liveness   a run finished (health.at, heartbeat)          - says nothing about the switch
  API reads  the run's broker reads worked (result/hold)    - says nothing about the switch
  progress   the switch itself moved, defined as ANY of:
               - more shares filled on one of the switch's orders (partial or full)
               - sale proceeds counted or a buy's spend counted into the intent
               - the intent entered its next phase (selling -> buying) or completed
             Nothing else moves it: a repeated "waiting", a successful read, a
             heartbeat, or a new/resubmitted order that has not filled.

Per switch intent this stores target, phase, priority, first session, last
progress, the current wait reason, whether the engine retries by itself and
whether a person is needed, plus the alerts already sent. It survives restarts
(journal metadata) and only ever sends notifications; it never changes trading.

Alerts (live and paper runs only; observe sends no switch alerts because it
never trades):
  early   the open window ended `early_minutes_after_window` ago (default 10,
          i.e. 25 minutes after the open) and the switch is not complete
  stall   inside the intraday recovery window, after the early notice, no
          progress for `stall_minutes` (default 10) of session time; nights and
          closed days never count
  final   the recovery window closed with the switch unfinished (carried over)
  resolved  a switch that had an alert completed
Locks and broker refusals are alerted immediately by the run itself.
The 10-minute defaults are operating choices, not tuned to returns.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .core import DONE, PENDING
from .schedule import KST
from .signal import DEFENSIVE_SYMBOL, RISK_SYMBOL

DEFAULTS = {"early_minutes_after_window": 10, "stall_minutes": 10}
FINAL_TEXT = ("오늘 장중 복구 마감까지 끝나지 않아 다음 정규장으로 이월합니다. 다음 세션에 최신 확정 신호와 잔고로 "
              "다시 계산합니다.")
OPEN_WINDOW_MINUTES = 15

# (substring of the run result or hold message, code, Korean reason, person needed, engine retries itself)
WAITS = (
    ("sale proceeds not yet orderable", "proceeds_pending",
     "매도대금이 증권사 주문가능금액에 아직 반영되지 않음 (조회는 성공)", False, True),
    ("waiting: order fill", "order_open", "주문 체결 대기", False, True),
    ("cancellation", "cancel_pending", "취소 확인 대기", False, True),
    ("holdings reflection", "holdings_pending", "체결 후 잔고 반영 대기", False, True),
    ("shares become sellable", "sellable_pending", "매도 가능 수량 반영 대기", False, True),
    ("retry back-off", "retry_backoff", "접수되지 않은 요청 뒤 재시도 대기", False, True),
    ("recovery pacing", "pacing", "장중 복구 주문 간격 대기", False, True),
    ("outside execution window", "outside_window", "주문 가능 시간 밖 (다음 허용 구간에 자동 진행)", False, True),
    ("window closed", "outside_window", "주문 가능 시간 밖 (다음 허용 구간에 자동 진행)", False, True),
    ("active reservation", "reservation_active", "활성 예약주문이 있어 신규 주문 차단 (앱에서 정리 필요)", True, False),
    ("reservation lookup failed", "reservation_query_failed", "예약주문 조회 실패 (자동 재조회)", False, True),
    ("coverage unverified", "reservation_coverage", "예약주문 조회 범위 미확인", True, False),
    ("regular session not confirmed", "market_unconfirmed", "정규장 확인 실패 (자동 재확인)", False, True),
    ("Invalid or stale real-time", "quote_invalid", "호가가 오래됐거나 비정상 (자동 재조회)", False, True),
    ("spread exceeds", "spread", "호가 스프레드가 한도를 넘음 (자동 재조회)", False, True),
    ("trading restriction", "trading_restricted", "거래정지·VCM 등 거래 제한", False, True),
    ("buy blocked", "side_blocked", "증권사 매수 제한(매도 전용 등)", True, False),
    ("completed-close signal", "signal_stale", "확정 종가 신호를 갱신하지 못함 (자동 재시도)", False, True),
    ("tax reserves cannot be checked", "tax_unavailable", "세금 준비금 확인 불가: 신규 매수 보류, 기존 주문 대조는 계속", False, True),
    ("Existing holdings left untouched", "non_strategy", "전략 외 보유가 있어 자동 처리 보류 (사용자 결정 필요)", True, False),
    ("Live check in progress", "live_check", "실계좌 검증 진행 중이라 일반 운용 정지", False, True),
    ("Order count limit", "order_cap", "세션 주문 상한 도달 (다음 세션에 재개)", False, True),
    ("No confirmed Meritz market", "route_unverified", "종목 시장 코드 미확인 (doctor 확인 후 설정 필요)", True, False),
    ("Unresolved action", "unknown_outcome", "접수 여부가 불명확한 요청을 증권사 기록과 대조 중 (재전송 안 함)", False, True),
    ("Unknown outcome for action", "unknown_outcome", "접수 여부가 불명확한 요청을 증권사 기록과 대조 중 (재전송 안 함)",
     False, True),
    ("Insufficient", "funds_insufficient", "주문 가능 금액 부족 (조회 성공, 결과값 부족)", False, True),
)
QUERY_FAILED = ("query_failed", "증권사 조회 실패 (조회 결과 0과 다름, 자동 재조회)", False, True)


def classify(message, locked=False):
    """(code, reason, person needed, engine retries). Never echoes the raw text."""
    if locked:
        return "lock", "해소 불가 잠금 (사람 조치 필요, 조회와 대조는 계속)", True, False
    text = str(message or "")
    for needle, code, reason, human, auto in WAITS:
        if needle in text:
            return code, reason, human, auto
    if text.startswith(("idle:", "submitted:", "confirmed:", "recovered:", "resolved:")) or not text:
        return "progressing", "진행 중", False, True
    if text.startswith("Internal error") or "API" in text or "HTTP" in text or "response" in text:
        return QUERY_FAILED
    return "other_hold", "기타 보류 (상세는 Mac의 status 명령)", False, True


def priority(intent):
    """Alert priority only; never a reason to bypass any safety check."""
    if intent["to"] == DEFENSIVE_SYMBOL and intent["status"] == "selling":
        return "A", f"{RISK_SYMBOL} 이탈 매도 지연", "높음"
    if intent["to"] == RISK_SYMBOL:
        if intent["status"] == "selling":
            return "B", f"{DEFENSIVE_SYMBOL} 매도 지연으로 {RISK_SYMBOL} 복귀 지연", "높음"
        return "B", f"{RISK_SYMBOL} 복귀 매수 지연", "높음"
    return "C", f"{DEFENSIVE_SYMBOL} 매수 대기 (달러 현금으로 대기 중)", "보통"


def fingerprint(journal, intent):
    """What counts as progress (see the module text). Order statuses and new
    unfilled orders are deliberately left out."""
    filled = {}
    for r in journal.rows():
        if r["kind"] != "order" or (r["status"] not in PENDING and r["status"] != DONE):
            continue
        payload = json.loads(r["payload"] or "{}")
        if payload.get("owner") or payload.get("switch") not in (intent["id"], None):
            continue
        if r["created"] < intent["created_at"]:
            continue
        if str(r["filled"] or "0") not in ("0", "0.0", ""):
            filled[r["id"]] = str(r["filled"])
    return {"status": intent["status"], "counted": len(intent.get("counted", [])),
            "proceeds": str(intent.get("proceeds")), "spent": str(intent.get("spent")),
            "to_tax": str(intent.get("to_tax", "0")), "filled": filled}


def delivered(journal, key):
    record = (journal.meta("notified", {}) or {}).get(key) or {}
    return any(c.get("ok") for c in (record.get("channels") or {}).values())


def _kst(at):
    return at.astimezone(KST).strftime("%m-%d %H:%M")


def observe(journal, notifier, schedule, now, message, *, mode, recovery_buffer_minutes, alerts_cfg=None,
            locked=False):
    """Update the stored progress and send at most the alerts that are due."""
    cfg = dict(DEFAULTS, **(alerts_cfg or {}))
    intent = journal.meta("switch_intent")
    state = journal.meta("switch_progress") or {}
    if not intent or intent.get("status") in ("done", "superseded") or "created_at" not in intent:
        if state.get("intent_id") and not state.get("closed_at"):
            finished = intent and intent.get("id") == state["intent_id"] and intent.get("status") == "done"
            if finished and state.get("alerts") and mode != "observe":
                key = f"{state['intent_id']}:resolved"
                notifier.send("switch", key, f"전환 완료: {state.get('label', '전환')}. 앞서 알린 지연이 해소되었습니다 "
                              f"(완료 {_kst(now)} KST, 첫 지연 경고 {state['first_alert_kst']} KST).", now)
            state.update(closed_at=now.isoformat(), outcome=intent.get("status") if intent else "none")
            from .notify import mark_resolved
            for key in state.get("alerts", {}):
                mark_resolved(journal, f"switch:{state['intent_id']}:{key}", now)
            journal.put_meta("switch_progress", state)
        return state
    if state.get("intent_id") != intent["id"]:
        state = {"intent_id": intent["id"], "target": intent["to"], "first_session": intent.get("created_session"),
                 "started_at": intent["created_at"], "last_progress_at": intent["created_at"],
                 "fingerprint": fingerprint(journal, intent), "alerts": {}}
    current = fingerprint(journal, intent)
    if current != state["fingerprint"]:
        state.update(fingerprint=current, last_progress_at=now.isoformat())
    level, label, urgency = priority(intent)
    code, reason, human, auto = classify(message, locked)
    state.update(phase=intent["status"], priority=level, label=label, urgency=urgency, wait_code=code,
                 wait_reason=reason, human_needed=human, auto_retry=auto, checked_at=now.isoformat(), mode=mode)
    due = []
    session = schedule.trading_session(now)
    if mode != "observe" and session and session[0] >= (state.get("first_session") or session[0]):
        window_end = session[1] + timedelta(minutes=OPEN_WINDOW_MINUTES)
        recovery = schedule.recovery_window(now, recovery_buffer_minutes)
        recovery_end = recovery[1] if recovery else session[2]
        if now >= window_end + timedelta(minutes=cfg["early_minutes_after_window"]):
            due.append(("early", f"early:{session[0]}",
                        f"개장 실행창이 끝나고 {cfg['early_minutes_after_window']}분이 지났지만 전환이 끝나지 않았습니다."))
        early_at = window_end + timedelta(minutes=cfg["early_minutes_after_window"])
        if early_at <= now < recovery_end:
            # Counted from the later of the last progress and the early notice, so
            # the two never fire for the same minutes; nights and closed days
            # never count because only today's session time is measured.
            since = max(datetime.fromisoformat(state["last_progress_at"]), early_at)
            if now - since >= timedelta(minutes=cfg["stall_minutes"]):
                due.append(("stall", f"stall:{session[0]}:{since.isoformat()}",
                            f"장중 복구 구간에서 {cfg['stall_minutes']}분 동안 실제 진척(체결·매도대금 반영·단계 진입)이 없습니다."))
        if now >= recovery_end:
            due.append(("final", f"final:{session[0]}", FINAL_TEXT))
    elif mode != "observe" and session is None:
        # After the close (e.g. the Mac slept through it): the session that just
        # ended still gets its final notice; overnight time never counts as stall.
        ended = [s for s in schedule.sessions if s[2] <= now and now - s[2] < timedelta(hours=18)
                 and s[0] >= (state.get("first_session") or s[0])]
        if ended:
            due.append(("final", f"final:{ended[-1][0]}", FINAL_TEXT))
    for kind, key, text in due:
        if key in state["alerts"]:
            continue
        action = ("사람 조치가 필요합니다: " + reason) if human else ("자동으로 다시 시도합니다. 대기 사유: " + reason)
        notifier.send("switch", f"{intent['id']}:{key}",
                      f"[확인 필요/긴급도 {urgency}] {label}. {text} {action}. /issues로 상세 상태를 확인할 수 있습니다.", now)
        state["alerts"][key] = now.isoformat()
        state.setdefault("first_alert_kst", _kst(now))
        state["alert_level"], state["alert_key"] = kind, f"switch:{intent['id']}:{key}"
    if state.get("alert_key"):
        state["alert_delivered"] = delivered(journal, state["alert_key"])
    journal.put_meta("switch_progress", state)
    return state


def summary(journal):
    """Fields for /status, /issues and the heartbeat. No amounts."""
    return journal.meta("switch_progress") or {}
