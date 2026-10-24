"""Read-only account diagnosis and order-free observation plan.

Never sends FX, order, modify or cancel requests: the client is always built
with allow_mutations=False, and only read methods are called. Sizing reuses the
engine functions in core so the observation and a future order cannot differ.

The broker adapter supplies read_account, build_snapshot(parts, problems),
account_report, order_report, active_reservations, fx_report, market_status,
quote_report and completed_candles. Reports hold normalized values only: no raw
API rows, keys, tokens, device MAC, account or order numbers.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict
from datetime import timedelta
from decimal import ROUND_DOWN
from pathlib import Path

from .core import (D, Quote, Stop, buy_quantity, check_fx_quote, check_quote, fx_amount, fx_spent,
                   limit_price, money, symbols_to_sell, unsupported_holdings)
from .history import read_history, refresh
from .schedule import KST, NY, confirm_regular_session, regular_session_check
from .signal import decisions

SCHEMA = "doctor-report/v1"
CREDENTIAL_KEYS = ("MERITZ_CLIENT_ID", "MERITZ_CLIENT_SECRET")
VERIFY_KEYS = ("split_only_data", "usd_buying_power_and_fees", "partial_fill_cancel", "open_window_execution")
MARKET_CODES = {"000": "휴장", "001": "주간거래 개시", "002": "주간거래 종료", "003": "프리마켓 개시",
                "004": "정규장 개시", "005": "애프터마켓 개시", "006": "애프터마켓 종료",
                "011": "시장 열림(구분 미상)", "012": "시장 닫힘", "013": "휴일"}
SENSITIVE_KEY = re.compile(r"token|secret|password|authorization|mac_address|client_id|acno|cano|account_number|oder_no|order_number",
                           re.IGNORECASE)


def _error(exc):
    # Third-party exception text could echo request data; publish only our Stop text.
    return str(exc) if isinstance(exc, Stop) else "Internal error: " + type(exc).__name__


def section(fn):
    try:
        return fn()
    except Exception as exc:  # each section fails independently
        return {"status": "error", "error": _error(exc)}


def sanitize(value, secrets=()):
    """Drop credential-like keys and mask any configured secret value."""
    secrets = [s for s in secrets if s and len(s) >= 4]
    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items() if not SENSITIVE_KEY.search(str(k))}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        if isinstance(v, str):
            for s in secrets:
                if s in v:
                    v = v.replace(s, "[REDACTED]")
            return v
        return v
    return walk(value)


class _RecordingFeed:
    """Keeps the closes fetched by refresh for the price-basis overlap report."""

    def __init__(self, broker):
        self.broker, self.fetched = broker, {}

    def completed_candles(self, first, last):
        chunk = self.broker.completed_candles(first, last)
        self.fetched.update(chunk)
        return chunk


def _high_details(history, schedule, last):
    closes = sorted((d, p) for d, p in history.items() if d <= last)[-252:]
    high = max(p for _, p in closes)
    high_date = max(d for d, p in closes if p == high)
    dates = [s[0] for s in schedule.sessions]
    expires = dates[dates.index(high_date) + 252] if high_date in dates and dates.index(high_date) + 252 < len(dates) else None
    return {"high_252": str(high), "high_date": high_date, "high_leaves_window_on": expires,
            "window_sessions": len(closes)}


def explain(decision):
    target, transition = decision["target"], decision["transition"]
    if transition == "TQQQ->QQQ":
        return "하락률이 −15% 이하: TQQQ→QQQ 전환"
    if transition == "QQQ->TQQQ":
        if decision["expiry_driven"]:
            return "하락률이 −12% 이상으로 회복: QQQ→TQQQ 복귀 (252일 고점 만료가 원인)"
        return "하락률이 −12% 이상으로 회복: QQQ→TQQQ 복귀"
    if target == "TQQQ":
        return "TQQQ 유지: 하락률이 −15%보다 높음"
    return "QQQ 유지: 하락률이 −12% 미만 (전환 임계값 사이 또는 아래)"


def signal_section(broker, history_path, schedule, expected, verified):
    report = {"rule": "DD15_12_252_v1", "expected_session": expected,
              "price_basis": {"declared": "QQQ 분할 조정·배당 미조정 종가 (seed 기준)",
                              "broker_option": "일봉 mod_stpr_cls_code=1; 명세상 0/1 응답이 같아 API로 기준 구분 불가",
                              "overlap_check": "not_run",
                              "config_verified_split_only": verified.get("split_only_data") is True},
              "execution": {"research": "NEXT_US_SESSION_CLOSE",
                            "current_code_candidate": "NEXT_SESSION_OPEN_WINDOW_SEQUENTIAL_LIMIT (개장 1~15분 지정가 순차)",
                            "same": False}}
    old = read_history(history_path)
    signal, source, error = None, "local_history_only", None
    if broker is not None:
        feed = _RecordingFeed(broker)
        try:
            signal = refresh(feed, history_path, schedule, expected)
            source = "broker_refresh"
            overlap = sorted(set(old) & set(feed.fetched))
            report["price_basis"].update(
                overlap_check="passed", overlap_sessions=len(overlap),
                max_abs_diff=str(max((abs(old[d] - feed.fetched[d]) for d in overlap), default=D(0))),
                history_rebase=signal.get("history_rebase"))
        except Exception as exc:
            error = _error(exc)
            report["price_basis"]["overlap_check"] = "failed"
    history = read_history(history_path)
    last = max(d for d in history if d <= expected) if any(d <= expected for d in history) else max(history)
    if signal is None:
        series = [p for d, p in sorted(history.items()) if d <= last]
        signal = asdict(decisions(series)[-1])
        signal.update(signal_date=last)
    high = _high_details(history, schedule, last)
    usable = source == "broker_refresh" and signal["signal_date"] == expected
    report.update(
        status="ok" if usable else "unverified", source=source, error=error,
        usable_for_plan=usable, last_close_date=last, last_close=str(history[last]),
        fresh=last == expected, **high,
        drawdown=f"{signal['drawdown']:.6f}",
        switch_to_qqq_at_or_below=str((money(high["high_252"]) * D("0.85")).quantize(D("0.0001"))),
        return_to_tqqq_at_or_above=str((money(high["high_252"]) * D("0.88")).quantize(D("0.0001"))),
        previous_target=signal["previous_target"], target=signal["target"],
        transition=signal["transition"], reason=explain(signal),
        expiry_driven=signal["expiry_driven"], near_threshold=signal["near_threshold"],
        nonexpiring_high_diagnostic=signal["nonexpiring_high"])
    if not usable:
        report["unverified_reason"] = error or ("키 미설정: 로컬 이력만 사용" if broker is None
                                                else "최신 완료 거래일 종가 없음")
    return report, signal if usable else None


def market_section(broker, schedule, now):
    session = schedule.trading_session(now)
    nxt = schedule.next_session(now)
    report = {"now_utc": now.isoformat(), "now_kst": now.astimezone(KST).isoformat(),
              "now_new_york": now.astimezone(NY).isoformat(),
              "calendar": {"us_regular_session_open": bool(session),
                           "session_date": session[0] if session else None,
                           "session_close_new_york": session[2].astimezone(NY).isoformat() if session else None,
                           "next_session_date": nxt[0] if nxt else None,
                           "next_open_kst": nxt[1].astimezone(KST).isoformat() if nxt else None},
              "engine_windows": {"new_order_window_now": schedule.execution_window(now),
                                 "new_order_window_rule": "정규장 개장 후 1~15분",
                                 "krw_fx_window_now": schedule.exchange_window(now),
                                 "krw_fx_window_rule": "평일 09:00~15:20 중 한국거래소(XKRX) 휴장일 제외 (보수적 필터, 메리츠 환전 가능일과 같다고 미확인)"}}
    if broker is None:
        report["broker_market"] = {"status": "not_available"}
        check = regular_session_check(session, None, now, "no broker credentials")
    else:
        # Same function the run command uses; asked even when the calendar is
        # closed so the broker's answer is visible for comparison.
        check = confirm_regular_session(schedule, broker.market_status, now)
        if check["broker_code"] is None:
            report["broker_market"] = {"status": "error", "error": check["reasons"][-1]}
        else:
            report["broker_market"] = {"status": "ok", "code": check["broker_code"],
                                       "begin_kst": check["broker_begin_kst"], "end_kst": check["broker_end_kst"],
                                       "meaning": MARKET_CODES.get(check["broker_code"], "목록 밖 코드")}
    report["regular_session_check"] = check
    regular = check["regular"]
    report["order_possible_now"] = {
        "engine_would_place_new_order": bool(session) and schedule.execution_window(now) and regular,
        "engine_would_manage_open_orders": bool(session) and regular,
        "this_command_sends_orders": False}
    return report


def _indicative(quote_entry, now, cfg):
    """Return (Quote, price_source): the engine quote when it passes the engine's
    check, otherwise the same quote or the last price labelled as indicative."""
    quote, info, _ = quote_entry
    if quote is not None and quote.bid > 0 and quote.ask >= quote.bid:
        try:
            check_quote(quote, now, cfg)
            return quote, "quote"
        except Stop:
            return quote, "quote_failed_engine_check (indicative)"
    try:
        last = money(info.get("last") or 0)
    except Stop:
        last = D(0)
    if last > 0:
        return Quote(last, last, D("0.01"), now), "last_price (indicative)"
    return None, None


def observation_plan(*, snap, target, cfg, fx_quote, fx_error, quotes, now, spent, blockers):
    """What the engine would size right now, using the engine's own functions."""
    plan = {"sizing_functions": "core.fx_amount / check_fx_quote / limit_price / buy_quantity (엔진과 동일)",
            "whole_shares_only": True, "fee_buffer": str(cfg.fee_buffer), "usd_reserve": str(cfg.usd_reserve),
            "krw_reserve": str(cfg.krw_reserve), "engine_would_execute": not blockers,
            "blocked_by": [b["message"] for b in blockers], "not_computable": []}
    if snap is None:
        plan["not_computable"].append("계좌 조회 실패 또는 USD 주문가능 행 누락: 환전·수량 계산 불가")
        return plan
    amount = fx_amount(snap.krw, spent, cfg)
    fx = {"krw_available_engine": str(snap.krw), "fx_sent_this_session": str(spent),
          "daily_cap": str(cfg.max_fx_krw_per_day), "amount_krw": str(amount),
          "minimum_krw": str(cfg.min_fx_krw), "krw_after": str(snap.krw - amount)}
    fx_usd = D(0)
    if amount < cfg.min_fx_krw:
        fx["status"] = "below_minimum"
        fx["amount_krw"], fx["krw_after"] = "0", str(snap.krw)
    elif fx_quote is None:
        fx["status"] = "not_computable"
        plan["not_computable"].append("적용 환율 조회 실패: 예상 USD 계산 불가" + (f" ({fx_error})" if fx_error else ""))
    else:
        try:
            check_fx_quote(fx_quote, now, cfg)
            fx["engine_quote_check"] = "pass"
        except Stop as exc:
            fx["engine_quote_check"] = str(exc)
        fx_usd = (amount / fx_quote.ask).quantize(D("0.01"), rounding=ROUND_DOWN)
        fx.update(status="planned", applied_rate=str(fx_quote.ask), estimated_usd=str(fx_usd),
                  estimate_note="환전 USD는 원화/적용환율 0.01 절사 추정; 실제 체결 금액은 증권사 결정")
    plan["fx"] = fx

    if target is None:
        plan["not_computable"].append("신호 미검증: 목표 종목·매도·매수 수량 계산 생략")
        return plan
    sells, proceeds = [], D(0)
    for symbol in symbols_to_sell(snap.positions, target):
        entry = {"symbol": symbol, "held": str(snap.positions[symbol]),
                 "sellable": str(snap.sellable.get(symbol, D(0)))}
        if symbol in unsupported_holdings({symbol: 1}, cfg):
            entry["status"] = "left_untouched (비전략 보유: 자동 매도·전환하지 않음)"
            sells.append(entry)
            continue
        qty = min(snap.positions[symbol], snap.sellable.get(symbol, D(0)))
        quote, source = _indicative(quotes.get(symbol, (None, {}, "시세 미조회")), now, cfg)
        if quote is None:
            entry["status"] = "not_computable: 시세 없음"
            plan["not_computable"].append(f"{symbol} 매도가 계산 불가: 시세 없음")
        else:
            price = limit_price(quote, "sell", cfg)
            value = (qty * price * (1 - cfg.fee_buffer)).quantize(D("0.01"), rounding=ROUND_DOWN)
            proceeds += value
            entry.update(status="planned_sell", quantity=str(qty), limit_price=str(price), price_source=source,
                         estimated_proceeds_after_fee_buffer=str(value),
                         engine_quote_check=_quote_check(quotes.get(symbol), now, cfg))
        sells.append(entry)
    plan["switch_sells"] = sells
    plan["target"] = target

    quote, source = _indicative(quotes.get(target, (None, {}, "시세 미조회")), now, cfg)
    if quote is None:
        plan["not_computable"].append(f"{target} 매수가 계산 불가: 시세 없음")
        return plan
    price = limit_price(quote, "buy", cfg)
    buy = {"symbol": target, "limit_price": str(price), "price_source": source,
           "engine_quote_check": _quote_check(quotes.get(target), now, cfg)}

    def size(usd, power):
        qty = buy_quantity(usd, power, price, cfg)
        cost = qty * price
        return {"usd": str(usd), "buying_power": str(power), "quantity": str(qty), "cost": str(cost),
                "fee_buffer_amount": str((cost * cfg.fee_buffer).quantize(D("0.01"))),
                "usd_after_with_fee_buffer": str((usd - cost * (1 + cfg.fee_buffer)).quantize(D("0.01")))}
    buy["with_current_usd"] = size(snap.usd, snap.buying_power)
    if fx_usd or proceeds:
        buy["after_planned_fx_and_sales"] = dict(
            size(snap.usd + fx_usd + proceeds, snap.buying_power + fx_usd + proceeds),
            assumption="환전액·매도대금이 주문가능금액에 그대로 더해진다고 가정 (미검증)")
    plan["buy"] = buy
    return plan


def _quote_check(entry, now, cfg):
    if not entry or entry[0] is None:
        return (entry[2] if entry else None) or "시세 없음"
    try:
        check_quote(entry[0], now, cfg)
        return "pass"
    except Stop as exc:
        return str(exc)


def collect(*, cfg, state, settings, now, schedule, journal, client_factory, broker_factory, root):
    """Build the full diagnostic report. Only read requests (and token issue) are made."""
    verified = cfg.get("verified", {})
    report = {"schema": SCHEMA, "generated_at": now.isoformat(), "mode": settings.mode,
              "send_policy": {"fx_orders_modify_cancel_sent": False, "transport_mutations_allowed": False,
                              "allowed_requests": "GET 조회와 POST /oauth2/token (토큰 발급)만"},
              "verified_flags": {k: verified.get(k) is True for k in VERIFY_KEYS}}
    expected = schedule.expected_close(now)
    history_path = Path(state) / "qqq_close.csv"
    if not history_path.exists():
        history_path.write_bytes((Path(root) / "qqq_seed.csv").read_bytes())

    broker = None
    if not all(os.environ.get(k) for k in CREDENTIAL_KEYS):
        report["auth"] = {"status": "not_configured", "credentials": "missing",
                          "detail": "secrets.env에 MERITZ_CLIENT_ID/SECRET 미설정: 계좌 항목 조회 생략"}
    else:
        try:
            client = client_factory()
            if client.allow_mutations:
                raise Stop("Diagnostics client must not allow mutations")
            client.token()
            broker = broker_factory(client)
            report["auth"] = {"status": "ok", "credentials": "configured",
                              "device_id_source": "env" if os.environ.get("MERITZ_MAC_ADDRESS") else "uuid.getnode"}
        except Exception as exc:
            report["auth"] = {"status": "error", "credentials": "configured", "error": _error(exc)}

    signal_report, signal = signal_section(broker, history_path, schedule, expected, verified)
    report["signal"] = signal_report
    if signal is not None:
        journal.put_meta("signal", signal)
    report["market"] = market_section(broker, schedule, now)

    snap, problems, parts, fx_quote, fx_error, quotes = None, [], None, None, None, {}
    if broker is None:
        for name in ("account", "orders", "reserved_orders", "fx", "quotes"):
            report[name] = {"status": "not_available"}
    else:
        try:
            parts = broker.read_account()
            report["account"] = dict(broker.account_report(parts), status="ok")
        except Exception as exc:
            report["account"] = {"status": "error", "error": _error(exc)}
        if parts is not None:
            try:
                snap = broker.build_snapshot(parts, problems)
            except KeyError as exc:
                problems.append(f"Required balance field missing: {exc.args[0]}")
            except Exception as exc:
                problems.append(_error(exc))
            report["orders"] = section(lambda: dict(broker.order_report(parts), status="ok"))
        else:
            report["orders"] = {"status": "error", "error": "계좌 조회 실패"}
        def reservations():
            lookup = broker.active_reservations(now, settings.reservation_lookup_start)
            return {"status": "ok", "engine_checks_reservations": True, "live": lookup.live,
                    "coverage": {"complete": lookup.complete, "searched_from": lookup.searched_from,
                                 "searched_to": lookup.searched_to, "requests": lookup.chunks,
                                 "configured_start": settings.reservation_lookup_start,
                                 "reason": lookup.reason}}
        report["reserved_orders"] = section(reservations)
        try:
            fx_quote, fx_details = broker.fx_report(now)
            try:
                check_fx_quote(fx_quote, now, settings)
                check = "pass"
            except Stop as exc:
                check = str(exc)
            report["fx"] = dict(fx_details, status="ok", engine_spread_limit=str(settings.max_fx_spread),
                                engine_quote_check=check)
        except Exception as exc:
            fx_error = _error(exc)
            report["fx"] = {"status": "error", "error": fx_error}
        # Non-strategy holdings are never traded, so they need no order-grade quote.
        symbols = {"QQQ", "TQQQ"} | (set(snap.positions) & set(settings.migrate_symbols) if snap else set())
        qreport = {}
        for symbol in sorted(symbols):
            try:
                quotes[symbol] = broker.quote_report(symbol, now)
                qreport[symbol] = dict(quotes[symbol][1], engine_quote_check=_quote_check(quotes[symbol], now, settings))
            except Exception as exc:
                qreport[symbol] = {"status": "error", "error": _error(exc)}
        report["quotes"] = dict(qreport, status="ok")

    report["fees"] = fee_section(report, settings)
    report["holdings_state"] = holdings_state(report, signal)
    report["recovery"] = recovery_section(journal, now)
    blockers = collect_blockers(report, journal, snap, problems, settings, signal)
    report["blockers"] = blockers
    session = schedule.session_key(now)
    report["plan"] = observation_plan(snap=snap, target=signal["target"] if signal else None, cfg=settings,
                                      fx_quote=fx_quote, fx_error=fx_error, quotes=quotes, now=now,
                                      spent=fx_spent(journal.rows(), session), blockers=blockers)
    report["plan"]["session"] = session
    return report


def fee_section(report, settings):
    observed = (report.get("orders") or {}).get("fee_observations") or []
    fx = report.get("fx") or {}
    return {"trading_commission": {"status": "not_verified",
                                   "reason": "공식 명세에 수수료율·우대 조회 API 없음; 체결 후 oder_cfee(당일)·cntg_cfee(실현손익)만 제공",
                                   "engine_fee_buffer": str(settings.fee_buffer),
                                   "observed_today_from_fills": observed},
            "fx_spread": {"status": "observed_from_api" if fx.get("status") == "ok" else "not_available",
                          "spread_krw": fx.get("spread_krw"), "discount_pct": fx.get("discount_pct"),
                          "spread_ratio": fx.get("spread_ratio")},
            "other_costs": {"status": "not_verified", "reason": "SEC fee 등 제비용 조회 API 없음"}}


def recovery_section(journal, now):
    """Locks, holds, retry state, live requests and carry-overs. Action ids are
    internal journal ids; broker order numbers are never included."""
    counts = {}
    for row in journal.rows():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    stats = journal.meta("execution_stats", [])
    recent = stats[-10:]
    carried = [r for r in recent if r.get("carried_over")]
    streak = 0
    for record in reversed(stats):
        if not record.get("carried_over"):
            break
        streak += 1
    intent = journal.meta("switch_intent") or {}
    switch = ({k: intent.get(k) for k in ("to", "status", "signal_date", "created_session", "created_at",
                                           "completed_at", "completed_by", "recovery_orders")}
              if intent else None)
    return {"lock": journal.meta("lock_info"), "hold": journal.meta("active_hold"), "switch": switch,
            "failures": journal.meta("failures", {"count": 0}), "auth_failures": journal.meta("auth_failures", 0),
            "action_status_counts": counts,
            "live_requests": [{"id": r["id"], "kind": r["kind"], "status": r["status"], "created": r["created"],
                               "filled": r["filled"], "attempt": r["attempt"]} for r in journal.pending()],
            "carryover": {"recent_sessions": len(recent), "carried_over": len(carried), "current_streak": streak,
                          "reasons": sorted({r["carried_over"] for r in carried}),
                          "differs_from_backtest_fill": streak >= 2,
                          "note": "이월은 실행상의 예외 처리이며 기존 연구의 체결 가정(다음 거래일 종가)과 다름"}}


def holdings_state(report, signal):
    holdings = (report.get("account") or {}).get("holdings")
    if holdings is None:
        return {"status": "not_available"}
    strategy = sorted({h["symbol"] for h in holdings if h["symbol"] in {"QQQ", "TQQQ"}})
    actual = strategy[0] if len(strategy) == 1 else ("mixed" if strategy else "none")
    return {"status": "ok", "signal_target": signal["target"] if signal else None, "actual_strategy_holding": actual,
            "aligned": bool(signal) and actual == signal["target"],
            "non_strategy": sorted(h["symbol"] for h in holdings if h["symbol"] not in {"QQQ", "TQQQ"})}


def collect_blockers(report, journal, snap, problems, settings, signal):
    """Reasons the engine would stop or skip; `engine_checks` False marks a doctor-only risk."""
    out = []
    def add(code, message, engine_checks=True):
        out.append({"code": code, "message": message, "engine_checks": engine_checks})
    if report["auth"]["status"] != "ok":
        add("auth", "인증·연결 미완료: " + report["auth"].get("error", report["auth"].get("detail", "")))
    if journal.meta("halt"):
        info = journal.meta("lock_info") or {}
        add("halt", "해소 불가 잠금: " + str(journal.meta("halt")) + (f" — 필요한 조치: {info['required']}" if info.get("required") else ""))
    if journal.pending():
        add("pending_actions", f"확인 대기 중인 기록 {len(journal.pending())}건")
    failures = journal.meta("failures", {})
    if failures.get("count"):
        add("retry_backoff", f"연속 미접수 {failures['count']}회, 재시도 대기: {failures.get('last')}")
    recovery = report.get("recovery") or {}
    if (recovery.get("carryover") or {}).get("differs_from_backtest_fill"):
        add("repeated_carryover", f"최근 {recovery['carryover']['current_streak']}개 세션 연속 이월: "
            "시가 직후 체결 가정과 다른 운용 조건", engine_checks=False)
    if signal is None:
        add("signal_unverified", report["signal"].get("unverified_reason", "신호 미검증"))
    for message in problems:
        add("account_unsupported", message)
    if snap is not None:
        if snap.external_open_orders:
            add("untracked_open_order", "엔진이 추적하지 않는 미체결 주문 존재")
        unsupported = unsupported_holdings(snap.positions, settings)
        if unsupported:
            add("non_strategy_holdings", "Existing holdings left untouched: " + ", ".join(unsupported))
    reserved = report.get("reserved_orders") or {}
    if reserved.get("live"):
        add("reserved_orders", f"활성 예약주문 {len(reserved['live'])}건: 엔진이 신규 환전·주문을 차단함(예약주문은 취소하지 않음)")
    elif reserved.get("status") == "error":
        add("reserved_orders_lookup", "예약주문 조회 실패·불완전: 엔진이 신규 환전·주문을 차단함 — " + reserved.get("error", ""))
    if reserved.get("status") == "ok" and not reserved["coverage"]["complete"]:
        add("reserved_orders_coverage", f"예약주문 조회 범위 미검증({reserved['coverage']['searched_from']} 이후 접수분만 확인):"
            " live에서는 신규 환전·주문 차단, observe는 미검증 기록. execution.reservation_lookup_start에"
            " 계좌 개설일(또는 그 이전)을 넣으면 그날부터 나눠 조회함")
    market = report["market"]
    if market["engine_windows"]["new_order_window_now"] and not market["regular_session_check"]["regular"]:
        add("market_not_confirmed", "정규장 확인 실패, 엔진이 신규 주문을 차단함: "
            + "; ".join(market["regular_session_check"]["reasons"]))
    fx = report.get("fx") or {}
    if fx.get("status") == "ok" and fx.get("engine_quote_check") != "pass":
        note = "" if report["market"]["engine_windows"]["krw_fx_window_now"] else " (환전 시간 외 조회라 시각 검사 실패는 정상일 수 있음)"
        add("fx_quote_check", "환율 검사 실패, 엔진은 환전하지 않음: " + fx["engine_quote_check"] + note)
    if settings.mode == "observe":
        add("observe_mode", "observe 모드: 환전·주문을 계획만 하고 전송하지 않음")
    missing = [k for k, v in report["verified_flags"].items() if not v]
    if missing:
        add("verification_incomplete", "live 전환 불가, 미검증 항목: " + ", ".join(missing))
    return out


def _short(value):
    try:
        return str(money(value).quantize(D("0.0001")).normalize())
    except Exception:
        return str(value)


def render(report):
    """Short Korean summary for a terminal. Contains no identifiers."""
    lines = [f"[진단] {report['generated_at']}  모드={report['mode']}  환전·주문·정정·취소 전송: 없음"]
    auth = report["auth"]
    lines.append(f"1) 인증·연결: {auth['status']}" + (f" — {auth.get('error') or auth.get('detail')}" if auth["status"] != "ok" else ""))
    acct = report.get("account", {})
    if acct.get("status") == "ok":
        k, u = acct["krw"], acct["usd"]
        lines.append(f"2) 원화: 예수금 {k['deposit_total']} / 출금가능 {k['withdrawable']} / 주문가능현금 {k['order_cash']}")
        lines.append(f"   USD: 예수금 {u['deposit_current']} / 추정예수금 {u['deposit_estimated_with_unsettled']}"
                     f" / 주문가능 {u['order_available']} / 재매매가능 {u['rebuy_available']}")
        if acct["missing_fields"]:
            lines.append("   누락 필드: " + ", ".join(acct["missing_fields"]))
        if acct["debts"]:
            lines.append("   신용·미수: " + ", ".join(f"{k}={v}" for k, v in acct["debts"].items()))
        hold = ", ".join(f"{h['symbol']} {h['quantity']}주({h['balance_kind']})" for h in acct["holdings"]) or "없음"
        lines.append(f"3) 보유: {hold}")
    else:
        lines.append(f"2-3) 잔고·보유: {acct.get('status')} {acct.get('error', '')}".rstrip())
    orders = report.get("orders", {})
    reserved = report.get("reserved_orders", {})
    lines.append(f"   미체결: {len(orders.get('open', [])) if orders.get('status') == 'ok' else orders.get('status')}"
                 f" / 예약주문: {len(reserved.get('live', [])) if reserved.get('status') == 'ok' else reserved.get('status')}"
                 + (f" (조회 범위 {reserved['coverage']['searched_from']}~, {'전체' if reserved['coverage']['complete'] else '미검증'})"
                    if reserved.get("status") == "ok" else ""))
    fx = report.get("fx", {})
    if fx.get("status") == "ok":
        lines.append(f"4) 환율: 적용 매입율 {fx['applied_buy_rate']} (기준 {fx['base_buy_rate']}, 스프레드 {fx['spread_krw']}원"
                     f" = {fx['spread_ratio']}, 할인율 {fx['discount_pct']}%) 엔진 환율 검사: {fx['engine_quote_check']}")
    else:
        lines.append(f"4) 환율: {fx.get('status')} {fx.get('error', '')}".rstrip())
    fees = report["fees"]["trading_commission"]
    lines.append(f"   매매 수수료: {fees['status']} (엔진 여유율 {fees['engine_fee_buffer']})")
    sig = report["signal"]
    lines.append(f"5) 종가: 예상 완료 거래일 {sig['expected_session']}, 보유 이력 최종 {sig['last_close_date']} {_short(sig['last_close'])}"
                 f" — 출처 {sig['source']}, 기준 대조 {sig['price_basis']['overlap_check']}")
    lines.append(f"6) 신호: 252일 고점 {_short(sig['high_252'])}({sig['high_date']}, 만료 {sig['high_leaves_window_on']})"
                 f" 하락률 {sig['drawdown']} → 목표 {sig['target']} [{sig['status']}]")
    lines.append(f"   사유: {sig['reason']} | QQQ 전환 ≤ {sig['switch_to_qqq_at_or_below']}, TQQQ 복귀 ≥ {sig['return_to_tqqq_at_or_above']}")
    hs = report["holdings_state"]
    if hs.get("status") == "ok":
        lines.append(f"   실제 보유 상태: {hs['actual_strategy_holding']} (신호와 일치: {hs['aligned']})"
                     + (f", 비전략 보유: {', '.join(hs['non_strategy'])}" if hs["non_strategy"] else ""))
    mk = report["market"]
    bm = mk.get("broker_market", {})
    lines.append(f"7) 시장: 거래소 달력 정규장 {'열림' if mk['calendar']['us_regular_session_open'] else '닫힘'}"
                 f", 다음 개장 {mk['calendar']['next_open_kst']}, 증권사 상태 {bm.get('code', bm.get('status'))}{' ' + bm['meaning'] if bm.get('meaning') else ''}"
                 f", 신규 주문 시간 {mk['engine_windows']['new_order_window_now']}, 환전 시간 {mk['engine_windows']['krw_fx_window_now']}")
    rc = mk["regular_session_check"]
    lines.append(f"   정규장 확인(달력+증권사 구간+현재 시각): {'확인' if rc['regular'] else '미확인'}"
                 + (f" — {'; '.join(rc['reasons'])}" if rc["reasons"] else "")
                 + (f" [{rc['note']}]" if rc.get("note") else ""))
    lines.append("   실계좌 미확인: " + " / ".join(rc["unverified"]))
    plan = report["plan"]
    lines.append("[관찰 계획] (주문 없음, 정수 주식)")
    fxp = plan.get("fx")
    if fxp:
        lines.append(f"   환전: {fxp['status']} {fxp['amount_krw']}원" + (f" → 약 {fxp['estimated_usd']} USD @ {fxp['applied_rate']}" if fxp.get("estimated_usd") else "")
                     + f", 환전 후 원화 {fxp['krw_after']}")
    for s in plan.get("switch_sells", []):
        if s["status"].startswith("left_untouched"):
            lines.append(f"   유지: {s['symbol']} {s['held']}주 (비전략 보유: 자동 매도·전환 안 함)")
        else:
            lines.append(f"   매도: {s['symbol']} {s.get('quantity', s['held'])}주 {s['status']}"
                         + (f" @ {s['limit_price']} ({s['price_source']})" if s.get("limit_price") else ""))
    buy = plan.get("buy")
    if buy:
        now_ = buy["with_current_usd"]
        lines.append(f"   매수: {buy['symbol']} 지정가 {buy['limit_price']} ({buy['price_source']}, 엔진 호가검사: {buy['engine_quote_check']})")
        lines.append(f"     현재 USD 기준 {now_['quantity']}주, 예상 잔액 {now_['usd_after_with_fee_buffer']} USD")
        after = buy.get("after_planned_fx_and_sales")
        if after:
            lines.append(f"     환전·매도 반영 추정 {after['quantity']}주, 예상 잔액 {after['usd_after_with_fee_buffer']} USD (가정)")
    for reason in plan["not_computable"]:
        lines.append("   계산 불가: " + reason)
    rec = report.get("recovery") or {}
    if rec:
        lock = rec.get("lock") or {}
        co = rec.get("carryover", {})
        lines.append(f"[복구 상태] 잠금: {lock.get('code', '없음')} / 보류: {(rec.get('hold') or {}).get('message', '없음')}"
                     f" / 연속 미접수: {rec.get('failures', {}).get('count', 0)} / 진행 중 요청: {len(rec.get('live_requests', []))}"
                     f" / 최근 이월: {co.get('carried_over', 0)}/{co.get('recent_sessions', 0)}")
    lines.append("[멈춤·제한 사유]")
    for b in report["blockers"]:
        lines.append(f"   - {b['message']}" + ("" if b["engine_checks"] else " (엔진 미검사)"))
    return "\n".join(lines)
