"""Operator view: what the bot is doing now and what needs a person.

Built only from the local journal (no broker call), so it answers instantly
and also while a run or a live check holds the account. Stale data is said to
be stale. No amount, balance, quantity, account or order number, key, token or
personal path is ever included.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from .core import SUBMITTING, UNKNOWN
from .progress import classify
from .schedule import KST

STALE_AFTER = timedelta(minutes=5)       # runs every 60 s; five missed runs means "check the Mac"
AGENT_LABEL = "local.tqqq.observer"
DAEMON_LABEL = "local.tqqq.runner"


def _meta(db, key, default=None):
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _alias(db):
    alias = _meta(db, "account_alias")
    return " · " + alias if alias else ""


def _kst(text_or_dt):
    if not text_or_dt:
        return "기록 없음"
    at = datetime.fromisoformat(text_or_dt) if isinstance(text_or_dt, str) else text_or_dt
    return at.astimezone(KST).strftime("%m-%d %H:%M")


def _ago(at, now):
    minutes = int((now - datetime.fromisoformat(at)).total_seconds() // 60)
    return f"{minutes}분 전" if minutes < 120 else f"{minutes // 60}시간 전"


def _pending(db):
    rows = db.execute("SELECT kind, status, payload FROM actions WHERE status IN "
                      "('submitting','unknown','accepted','partial','cancelling')").fetchall()
    unknown = sum(1 for r in rows if r[1] in (SUBMITTING, UNKNOWN))
    owned = sum(1 for r in rows if json.loads(r[2] or "{}").get("owner"))
    return len(rows), unknown, owned


def _last_signal_at(db):
    row = db.execute("SELECT at FROM events WHERE category='signal' ORDER BY rowid DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _next_open(now):
    try:
        from .schedule import Schedule
        nxt = Schedule(now).next_session(now)
        return _kst(nxt[1]) if nxt else "달력 범위 밖"
    except Exception:
        return "확인 불가"


def status_lines(db, now):
    health = _meta(db, "health") or {}
    signal = _meta(db, "signal") or {}
    halt = _meta(db, "halt")
    lock = _meta(db, "lock_info") or {}
    hold = _meta(db, "active_hold")
    prog = _meta(db, "switch_progress") or {}
    total, unknown, owned = _pending(db)
    alias = _meta(db, "account_alias")
    lines = [f"[상태{' · ' + alias if alias else ''}] 응답 기준 {_kst(now)} KST (이 컴퓨터의 로컬 기록, 증권사 추가 조회 없음)"]
    if health.get("at"):
        stale = now - datetime.fromisoformat(health["at"]) > STALE_AFTER
        outcome = "잠금" if health.get("locked") else ("오류·보류" if health.get("error") else "정상 종료")
        lines.append(f"마지막 실행: {_kst(health['at'])} KST ({_ago(health['at'], now)}), {outcome}")
        if stale:
            lines.append("주의: 마지막 실행 기록이 오래됐습니다. 아래는 그 시점 기준이며 현재 정상 여부는 확인되지 "
                         "않았습니다. Mac 전원·네트워크·실행 예약을 확인하세요.")
    else:
        lines.append("마지막 실행: 기록 없음 (실행 예약이 아직 동작하지 않았을 수 있음)")
    if signal.get("signal_date"):
        lines.append(f"신호: {signal['signal_date']} QQQ 종가 기준 목표 {signal.get('target')}"
                     f" (신호 갱신 {_kst(_last_signal_at(db))} KST)")
    else:
        lines.append("신호: 아직 없음")
    if prog.get("intent_id") and not prog.get("closed_at"):
        lines.append(f"전환: {prog.get('label')} (단계 {'매도' if prog.get('phase') == 'selling' else '매수'}, "
                     f"마지막 실제 진척 {_kst(prog.get('last_progress_at'))} KST)")
        lines.append(f"  대기 사유: {prog.get('wait_reason')} / 자동 재시도: {'예' if prog.get('auto_retry') else '아니오'}"
                     f" / 사람 조치 필요: {'예' if prog.get('human_needed') else '아니오'}")
        if prog.get("mode") == "observe":
            lines.append("  observe 모드: 실제 주문이 없으므로 전환 지연 경고는 보내지 않습니다.")
    else:
        lines.append("전환: 진행 중인 전환 없음")
    if halt:
        lines.append(f"자동 처리: 잠금 ({lock.get('code', 'lock')}) — 사람 조치 필요, /issues 참고")
    elif hold:
        code, reason, human, auto = classify(hold.get("message"))
        lines.append(f"자동 처리: 보류 ({reason}, {_kst(hold.get('since'))} KST부터) — "
                     + ("사람 조치 필요" if human else "조건이 풀리면 자동 재개"))
    else:
        lines.append("자동 처리: 보류·잠금 없음")
    lines.append("진행 중 요청: " + ("없음" if not total else
                                f"{total}건 (접수 불명 {unknown}건{', 실계좌 검증 소유 ' + str(owned) + '건' if owned else ''})"))
    lines.append(f"다음 확인: 실행 예약이 켜져 있으면 약 1분마다 / 다음 미국 정규장 개장 {_next_open(now)} KST")
    lines.append(f"모드: {health.get('mode', '확인 불가')}" + (" (실제 주문 없음)" if health.get("mode") == "observe" else ""))
    return lines


def issues(db, now, *, monitor_configured=None):
    """[(what, not confirmed, automatic, user action)]"""
    out = []
    health = _meta(db, "health") or {}
    if health.get("at") and now - datetime.fromisoformat(health["at"]) > STALE_AFTER:
        out.append(("마지막 실행 기록이 오래됨", "Mac·네트워크·실행 프로세스의 현재 상태",
                    "없음 (실행이 멈춰 있으면 자동 처리도 멈춤)", "Mac 전원·네트워크·실행 예약(service-status) 확인"))
    if _meta(db, "halt"):
        lock = _meta(db, "lock_info") or {}
        out.append((f"해소 불가 잠금 ({lock.get('code', 'lock')}, {_kst(lock.get('since'))} KST)",
                    "잠금 원인의 실제 상태", "조회·자기 요청 대조는 계속, 신규 요청은 보내지 않음",
                    (lock.get("required") or "앱에서 확인") + " — 이 채팅으로는 잠금을 풀지 않습니다"))
    total, unknown, _ = _pending(db)
    if unknown:
        out.append((f"접수 여부 불명 요청 {unknown}건", "증권사가 실제로 접수했는지",
                    "재전송하지 않고 증권사 기록과 계속 대조", "오래 해소되지 않으면 Mac에서 status 확인 후 resolve"))
    hold = _meta(db, "active_hold")
    if hold and not _meta(db, "halt"):
        code, reason, human, auto = classify(hold.get("message"))
        out.append((f"보류: {reason}", "보류 조건이 지금도 남아 있는지(다음 실행에서 재확인)",
                    "조건이 풀리면 자동 재개" if auto else "없음", "필요 없음" if not human else "원인 확인 후 조치"))
    prog = _meta(db, "switch_progress") or {}
    if prog.get("alerts") and not prog.get("closed_at"):
        out.append((f"{prog.get('label')} (지연 경고 {len(prog['alerts'])}회, 첫 경고 {prog.get('first_alert_kst')} KST)",
                    "전환 완료 여부", "기존 주문 대조 후 허용된 시간·예산 안에서 자동 재시도",
                    "필요 없음" if not prog.get("human_needed") else prog.get("wait_reason")))
    check = None
    try:
        row = db.execute("SELECT plan_id, status FROM live_checks WHERE status IN ('running','stopping')").fetchone()
        check = row
    except Exception:
        pass
    if check:
        out.append((f"실계좌 검증 {check[1]} (일반 운용 정지)", "검증 완료 여부", "실행 중인 명령이 단계별로 진행",
                    "Mac에서 live-check status 확인"))
    pending_notes = _meta(db, "notify_pending", {}) or {}
    if pending_notes:
        out.append((f"전달 실패 알림 {len(pending_notes)}건", "해당 채널 복구 여부", "30분 간격으로 최대 24시간 재전송",
                    "필요 없음 (반복되면 채널 설정 확인)"))
    try:
        count = db.execute("SELECT count(*) FROM tax_issues WHERE issue_key NOT IN "
                           "(SELECT issue_key FROM tax_issue_resolutions)").fetchone()[0]
    except Exception:
        count = 0
    if count:
        out.append((f"세무 자료 확인 필요 {count}건", "세무 원장의 해당 항목", "세금 목적 매도만 보류(전략 매매는 계속)",
                    "Mac에서 status의 books 확인"))
    send = _meta(db, "monitor_last_send")
    if monitor_configured is False:
        out.append(("외부 감시 미설정", "Mac이 꺼졌을 때 알림 경로", "없음", "외부 호스트 배포(README) 후 MONITOR_URL 설정"))
    elif send and not send.get("ok"):
        out.append(("외부 감시로 상태 전송 실패", "외부 감시 호스트 상태", "다음 실행에서 새 상태로 재시도",
                    "외부 감시 호스트 확인"))
    return out


def issues_lines(db, now, *, monitor_configured=None):
    found = issues(db, now, monitor_configured=monitor_configured)
    if not found:
        return [f"[문제{_alias(db)}] {_kst(now)} KST 기준 확인된 미해결 문제가 없습니다. (이 컴퓨터의 로컬 기록 기준)"]
    lines = [f"[문제{_alias(db)}] {_kst(now)} KST 기준 {len(found)}건 (이 컴퓨터의 로컬 기록 기준)"]
    for what, unknown, auto, user in found:
        lines += [f"- {what}", f"  확인 안 된 것: {unknown}", f"  자동: {auto}", f"  사용자: {user}"]
    return lines


# ---- service installation (read-only) ----

def _run(argv):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        return done.returncode, done.stdout
    except Exception as exc:
        return None, type(exc).__name__


def service_status(root, journal_db=None, now=None, runner=_run):
    """What is actually installed; never installs, loads or changes anything."""
    uid = os.getuid()
    agent_file = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"
    daemon_file = Path("/Library/LaunchDaemons") / f"{DAEMON_LABEL}.plist"
    agent_code, _ = runner(["launchctl", "print", f"gui/{uid}/{AGENT_LABEL}"])
    daemon_code, _ = runner(["launchctl", "print", f"system/{DAEMON_LABEL}"])
    vault_code, vault_text = runner(["fdesetup", "status"])
    report = {
        "launch_agent": {"file_installed": agent_file.exists(), "loaded": agent_code == 0,
                         "starts": "after this user logs in (not before login)"},
        "launch_daemon": {"file_installed": daemon_file.exists(), "loaded": daemon_code == 0,
                          "starts": "at boot as the configured user, once the disk is unlocked"},
        "both_active": agent_code == 0 and daemon_code == 0,
        "filevault": (vault_text.strip() if vault_code == 0 else "unknown"),
        "generated_files": {name: (Path(root) / name).exists()
                            for name in (f"{AGENT_LABEL}.plist", f"{DAEMON_LABEL}.plist")},
        "external_monitor": {"code": "implemented (autotrader/monitor.py)",
                             "configured_on_this_mac": bool(os.environ.get("MONITOR_URL")),
                             "deployed_and_verified": "unknown from this Mac: check the monitor host"},
    }
    if journal_db is not None and now is not None:
        health = _meta(journal_db, "health") or {}
        report["last_run"] = {"at": health.get("at"), "stale": bool(health.get("at")) and
                              now - datetime.fromisoformat(health["at"]) > STALE_AFTER}
        report["monitor_last_send"] = _meta(journal_db, "monitor_last_send")
    if report["both_active"]:
        report["warning"] = "agent and daemon are both loaded: boot out one of them (README rollback steps)"
    return report


def plist(path):
    with open(path, "rb") as f:
        return plistlib.load(f)
