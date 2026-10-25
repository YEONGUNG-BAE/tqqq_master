import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import (D, SUBMITTING, UNKNOWN, Engine, Journal, JournalError, Settings, Stop, set_lock)
from .history import recompute, refresh
from .signal import STRATEGY_VERSION
from .meritz import AuthError, Client, Meritz
from .notify import Notifier, default_channels, mark_resolved, scrub, secret_values
from . import books, heartbeat, livecheck, ops, progress, tax, telegram_bot
from .tax_reserve import TaxPlan
from .paper import Paper
from .schedule import KST, Schedule, confirm_regular_session

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_UNUSABLE = "journal_unusable.json"
AUTH_LOCK_COUNT = 3      # explicit token refusals in a row -> hard lock (revoked key/permission)
ENV_KEYS = {"MERITZ_CLIENT_ID", "MERITZ_CLIENT_SECRET", "MERITZ_MAC_ADDRESS",
            "ALERT_SMTP_HOST", "ALERT_SMTP_PORT", "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD",
            "ALERT_FROM", "ALERT_TO", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MONITOR_URL", "MONITOR_SECRET"}


def output(value):
    print(json.dumps(value, ensure_ascii=False, default=str, indent=2))


def load_config(path, *, allow_unverified_live=False):
    cfg_path = Path(path).resolve()
    cfg = json.loads(cfg_path.read_text())
    state = Path(cfg.get("state_dir", "state_observe")).expanduser()
    if not state.is_absolute():
        state = cfg_path.parent / state
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    settings = Settings(**cfg.get("execution", {}))
    if settings.mode == "paper":
        raise Stop("Paper mode uses the offline demo command; run supports observe/live")
    if settings.mode == "live" and not allow_unverified_live:
        required = ("split_only_data", "usd_buying_power_and_fees", "partial_fill_cancel", "open_window_execution")
        checks = cfg.get("verified", {})
        if not all(checks.get(k) is True for k in required):
            raise Stop("Live integration checks are incomplete: " + ", ".join(k for k in required if checks.get(k) is not True))
    return cfg, state, settings


def load_env(path):
    # Secrets remain on the user's Mac; this file is never part of a package.
    path = Path(path)
    if not path.exists():
        return
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise Stop("Set credentials file permissions to 600")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in ENV_KEYS:
            raise Stop("Unknown credentials-file entry")
        os.environ[key] = value.strip()


def account_lock_path(cfg):
    """One lock per API key across every state folder, so two configs can never
    trade the same account at once. flock is released by the OS on crash; the
    journal keeps any unresolved request regardless."""
    base = cfg.get("account_lock_dir") or os.environ.get("TQQQ_ACCOUNT_LOCK_DIR")
    if not base:
        base = ("~/Library/Application Support/tqqq_auto/locks" if sys.platform == "darwin"
                else "~/.local/state/tqqq_auto/locks")
    folder = Path(base).expanduser()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = os.environ.get("MERITZ_CLIENT_ID", "")
    name = hashlib.sha256(key.encode()).hexdigest()[:16] if key else "unconfigured"
    return folder / f"account-{name}.lock"


def account_marker_path(cfg):
    """Live-check marker next to the account lock: a run from ANY state folder
    on this account sends nothing while it exists."""
    return account_lock_path(cfg).with_suffix(".livecheck")


def hold_code(message):
    return "".join(c for c in message if not c.isdigit())[:80]


def journal_unusable(marker, cfg, now, reason):
    """State DB cannot be trusted: never send anything. Notify at most every 6h
    using only the marker file (the database itself cannot record it)."""
    try:
        info = json.loads(marker.read_text())
    except (OSError, ValueError):
        info = {}
    info.setdefault("reason", reason or "unknown")
    info.setdefault("since", now.isoformat())
    last = info.get("notified_at")
    if cfg.get("notifications", True) and (not last or now - datetime.fromisoformat(last) >= timedelta(hours=6)):
        text = scrub("[잠금] 상태 데이터베이스를 읽을 수 없어 모든 주문·환전을 멈췄습니다.\n필요한 조치: "
                     "백업에서 journal.sqlite를 복구한 뒤 journal_unusable.json을 직접 지우세요. "
                     "자동으로 초기화하지 않습니다.", secret_values())
        for channel in default_channels().values():
            try:
                channel(text)
            except Exception:
                pass
        info["notified_at"] = now.isoformat()
    marker.write_text(json.dumps(info))
    os.chmod(marker, 0o600)
    output({"error": "State database unusable: " + info["reason"] + "; no requests sent"})
    return 1


def after_success(journal, notifier, result, signal, session, now):
    hold = journal.meta("active_hold")
    if hold:
        _, reason, _, _ = progress.classify(hold["message"])
        notifier.send("recovered", "hold-cleared:" + hold_code(hold["message"]),
                      f"보류가 해소되어 자동 운용을 재개했습니다 (발생 {_kst_short(hold.get('since'))}, 해소 "
                      f"{_kst_short(now.isoformat())} KST): {reason}", now)
        mark_resolved(journal, "hold:" + hold_code(hold["message"]), now)
        journal.put_meta("active_hold", None)
    if result.startswith("recovered:"):
        notifier.send("recovered", result + session, "응답이 불명확했던 요청을 증권사 기록으로 확인했습니다 ("
                      + result.split(":", 1)[1].strip() + "). 재전송하지 않았습니다.", now)
    # Carry-over (final not-finished) notices come from progress.observe, with
    # the switch's priority and wait reason.
    position = journal.meta("position_state") or {}
    if position.get("aligned") and journal.meta("aligned_target") != position.get("target"):
        notifier.send("done", "aligned:" + position["target"], f"목표 {position['target']} 보유로 정렬을 마쳤습니다.", now)
        journal.put_meta("aligned_target", position["target"])
    day = now.astimezone(KST).date().isoformat()
    if journal.meta("heartbeat_day") != day:
        notifier.send("ok", day, f"자동 운용 중입니다. 현재 신호 목표: {signal['target']}", now)
        journal.put_meta("heartbeat_day", day)


def _kst_short(at):
    return datetime.fromisoformat(at).astimezone(KST).strftime("%m-%d %H:%M") if at else "기록 없음"


def tax_notices(journal, notifier, settings, now, estimate_fn):
    """December estimate; January recalculation after year-end trades settle."""
    local = now.astimezone(KST)
    if local.month == 12:
        year, code = local.year, f"dec:{local.year}"
        message = f"{year}년 귀속 예상 양도소득세(추정)입니다. 1월에 연말 거래·결제를 반영해 다시 계산합니다."
    elif local.month == 1:
        year, code = local.year - 1, f"jan:{local.year - 1}"
        message = f"{year}년 귀속 예상 양도소득세를 연말 거래·결제를 반영해 다시 계산했습니다."
    else:
        return
    if journal.meta("tax_notice:" + code):
        return
    est = estimate_fn(year)
    message += " 자료 상태: " + ("확정 가능" if est["complete"] else "추정") + (
        ", 이 계좌만의 추정" if est["this_account_only"] else "")
    notifier.send("tax", code, message, now, amounts=[
        ("estimated_tax", est["estimated_tax_krw"]), ("account_net", est["account_net_krw"]),
        ("external_net", est["external_net_krw"]), ("taxable", est["taxable_krw"])])
    journal.put_meta("tax_notice:" + code, now.isoformat())


def make_plan(journal, cfg, now):
    """Tax plan bound to this journal: estimates from the ledger, a recent base
    rate (at most 10 days old) for conservative USD valuation."""
    settings = tax.TaxSettings.from_config(cfg.get("tax"))
    tax.ensure_schema(journal.db)

    def holdings():
        """Engine's latest account snapshot if at most 2 days old, else None
        (not checked). Older journals stored the bare mapping without a time."""
        snap = journal.meta("last_positions")
        if not isinstance(snap, dict) or "at" not in snap:
            return None
        return snap["positions"] if now - datetime.fromisoformat(snap["at"]) <= timedelta(days=2) else None

    def estimate(year):
        return tax.estimate(journal.db, settings, year, holdings=holdings(), sync=journal.meta("tax_sync"), now=now)

    def rate():
        info = journal.meta("last_usd_rate") or {}
        try:
            fresh = now - datetime.fromisoformat(info["at"]) <= timedelta(days=10)
        except (KeyError, ValueError, TypeError):
            return None
        return info.get("rate") if fresh else None
    plan = TaxPlan(journal.db, cfg.get("tax"), estimate, rate)

    def blockers(year):
        est = estimate(year)
        broker_pl = journal.meta(f"broker_pl:{year}")
        comparison = tax.compare_with_broker(est["account_net_krw"], broker_pl) if broker_pl is not None else None
        return tax.tax_sell_blockers(est, comparison)
    return settings, plan, blockers


def migrate_signal(journal, history_path, old, now):
    """Replace a cached signal of an earlier strategy version. Same stored QQQ
    closes, same full replay; the old record is kept in the event and the
    change is recorded once. Returns the new signal, or None to fall back to a
    normal broker refresh (e.g. the stored history ends before that session)."""
    try:
        new = recompute(history_path, old["signal_date"])
    except Stop:
        new = None
    with journal.atomic():
        journal.put_meta("signal", new)
        if journal.meta("strategy_version") != STRATEGY_VERSION:
            journal.event("strategy_changed", {"from": old.get("strategy") or old.get("rule"),
                                               "to": STRATEGY_VERSION, "old_target": old.get("target"),
                                               "new_target": (new or {}).get("target"),
                                               "signal_date": old.get("signal_date")}, now)
            journal.put_meta("strategy_version", STRATEGY_VERSION)
    return new


def plan_notices(journal, notifier, plan, now):
    for year in plan.active_years(now):
        year_notices(journal, notifier, plan, year, now)


def year_notices(journal, notifier, plan, year, now):
    st = plan.status(year, now)
    deadline = (st["deadline"] or "미설정") + ("" if st["deadline_verified"] else " (미검증)")
    remaining = st["account_need"] - st["paid"] - st["withdrawn"]
    if st["account_need"] > 0 and remaining > 0 and st["reserve_krw"] >= remaining:
        notifier.send("tax", f"transfer:{year}", f"{year}년 귀속 세금 원화 준비가 끝났습니다. 납부 기한: {deadline}. "
                      "은행 이체와 신고·납부는 직접 하세요.", now, amounts=[("transfer", remaining)])
    need = journal.meta("reverse_fx_needed") or {}
    if need.get("year") == year:
        notifier.send("tax", f"reverse_fx:{year}", "달러 준비금은 재투자하지 않고 묶어 두었습니다. 달러→원화 환전은 "
                      "실계좌 검증 전이라 실행하지 않았습니다. 원화 환전이 필요합니다.", now,
                      amounts=[("reserve_usd_krw", st["reserve_usd_krw"] or 0), ("shortfall", st["shortfall"])])
    if st["external"]["overdue"]:
        notifier.send("tax", f"external_overdue:{year}:{st['external']['date']}",
                      "외부 납부 재원 예정일이 지났지만 확보 입력이 없습니다. 미확보분은 계좌에서 마련할 부족분으로 계산합니다.",
                      now, amounts=[("external_planned", st["external"]["planned"]), ("shortfall", st["shortfall"])])
    held = journal.meta(f"tax_plan_only:{year}:{now.astimezone(timezone.utc).date()}")
    if held:
        notifier.send("tax", f"tax_hold:{year}:{now.astimezone(KST).date()}", "세금 목적 매도는 보류하고 관찰 계획만 "
                      "남겼습니다(전략 매매는 영향 없음). 사유: " + "; ".join(held["reasons"]), now,
                      amounts=[("installment_due", held["due"])])


def housekeeping(journal, notifier, cfg, broker, schedule, now):
    """After the trading tick: tax ledger sync (daily, US market closed), tax
    notices and Telegram commands. Every failure here is recorded and ignored;
    none of it can change trading state or send a financial request."""
    tax_cfg = cfg.get("tax", {}) or {}
    try:
        settings, plan, _ = make_plan(journal, cfg, now)
    except Exception as exc:
        journal.event("tax_failed", {"stage": "settings", "error_type": type(exc).__name__}, now)
        if cfg.get("notifications", True):
            try:
                # Questions are answered exactly when something is wrong: read-only
                # views still work; tax commands are consumed in order, not applied.
                telegram_bot.poll(journal, tax.TaxSettings(), now, plan_unavailable=True)
            except Exception as error:
                journal.event("housekeeping_failed", {"error_type": type(error).__name__}, now)
        return
    day = now.astimezone(KST).date().isoformat()
    if broker is not None and schedule is not None and not schedule.trading_session(now):
        info = journal.meta("tax_sync") or {}
        if info.get("day") != day:
            try:
                unlinked = []

                def ledger(rows):
                    unlinked.extend(plan.match_withdrawals(rows, now)[1])
                    plan.match_deposits(rows, now)
                result = tax.sync(journal.db, broker, [s[0] for s in schedule.sessions], now,
                                  str(tax_cfg.get("ledger_start", "00000000")), on_ledger=ledger)
                for year in {now.astimezone(KST).year, now.astimezone(KST).year - 1}:
                    value = broker.realized_pl_krw(f"{year}0101", f"{year}1231")
                    if value is not None:
                        journal.put_meta(f"broker_pl:{year}", str(value))
                rate = broker.base_rate(now.astimezone(KST).strftime("%Y%m%d"))
                if rate is not None:
                    journal.put_meta("last_usd_rate", {"rate": str(rate), "at": now.isoformat()})
                journal.put_meta("tax_sync", dict(result, day=day, ok=True, unlinked_withdrawals=len(unlinked)))
            except Exception as exc:
                journal.put_meta("tax_sync", {"day": day, "ok": False,
                                              "error": str(exc) if isinstance(exc, Stop) else type(exc).__name__})
    lines, line_now = telegram_bot.plan_lines(plan)
    line_now[0] = now
    for step in (lambda: tax_notices(journal, notifier, settings, now, plan.estimate_fn),
                 lambda: plan_notices(journal, notifier, plan, now),
                 lambda: telegram_bot.poll(journal, settings, now, extra_lines=lines,
                                           handlers=telegram_bot.plan_handlers(plan), estimate_fn=plan.estimate_fn)
                 if cfg.get("notifications", True) else 0):
        try:
            step()
        except Exception as exc:
            journal.event("housekeeping_failed", {"error_type": type(exc).__name__}, now)


def run(args):
    cfg, state, settings = load_config(args.config)
    load_env(Path(args.config).resolve().parent / "secrets.env")
    with open(state / "run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            answer_readonly(state)    # a long order or live check holds it: still answer /status, /issues
            return 0
        with open(account_lock_path(cfg), "a") as account:
            try:
                fcntl.flock(account, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                answer_readonly(state)
                return 0   # another process (any state folder) is working on this account
            return run_locked(cfg, state, settings)


def answer_readonly(state):
    """Read-only Telegram answers without the state lock (local journal only).
    Plan-changing commands stay queued for the process that holds the lock.
    Never raises: a failure here cannot affect trading."""
    try:
        journal = Journal(state / "journal.sqlite")
    except Exception:
        return 0
    try:
        now = datetime.now(timezone.utc)
        return telegram_bot.process(journal, telegram_bot.fetch(journal), tax.TaxSettings(), now, readonly_only=True)
    except Exception as exc:
        try:
            journal.event("telegram_readonly_failed", {"error_type": type(exc).__name__}, datetime.now(timezone.utc))
        except Exception:
            pass
        return 0
    finally:
        journal.db.close()


def run_locked(cfg, state, settings):
    now = datetime.now(timezone.utc)
    marker = state / JOURNAL_UNUSABLE
    if marker.exists():
        return journal_unusable(marker, cfg, now, None)
    try:
        journal = Journal(state / "journal.sqlite")
    except JournalError as exc:
        return journal_unusable(marker, cfg, now, str(exc))
    notifier = Notifier(journal, enabled=cfg.get("notifications", True))
    notifier.retry_pending(now)   # failed one-time messages are not lost if the event never repeats
    broker = schedule = None
    outcome = "error"
    last_message = None
    try:
        check = livecheck.blocking(journal, account_marker_path(cfg))
        if check:
            # The live check owns this account until it is complete or stopped with
            # no test shares left; the general engine sends nothing meanwhile.
            raise Stop(f"Live check in progress ({check['source']}); the general run sends nothing until it ends")
        client = Client(state, allow_mutations=settings.mode == "live")
        broker = Meritz(client, journal, settings.instrument_markets)
        schedule = Schedule(now)
        expected = schedule.expected_close(now)
        history_path = state / "qqq_close.csv"
        if not history_path.exists():
            shutil.copyfile(ROOT / "qqq_seed.csv", history_path)
        signal = journal.meta("signal")
        signal_error = None
        if signal is not None and signal.get("strategy") != STRATEGY_VERSION:
            # A cache from an earlier strategy version (defensive QQQ): rebuilt from
            # the same stored QQQ closes even for the same session, never patched.
            signal = migrate_signal(journal, history_path, signal, now)
        if signal is None or signal["signal_date"] != expected:
            try:
                signal = refresh(broker, history_path, schedule, expected)
                journal.put_meta("signal", signal)
                journal.event("signal", signal, now)
            except Exception as exc:
                # No new requests without a fresh completed-close signal, but the
                # engine still settles requests it already sent.
                signal_error = "signal refresh failed: " + (str(exc) if isinstance(exc, Stop) else type(exc).__name__)
        regular_window = schedule.execution_window(now)
        session_row = schedule.trading_session(now)
        # The broker is asked only while the calendar has a regular session;
        # outside it nothing may be traded or cancelled anyway.
        market = confirm_regular_session(schedule, broker.market_status, now) if session_row else None
        may_manage = bool(market and market["regular"])
        may_trade = regular_window and may_manage
        # Inside the new-order window an unconfirmed session must be visible,
        # not a silent idle; own-order status reads still run first.
        market_block = (None if not regular_window or may_manage else
                        "US regular session not confirmed: " + "; ".join(market["reasons"]))
        if market is not None:
            journal.put_meta("market_check", market)
        recovery = schedule.recovery_window(now, settings.recovery_close_buffer_minutes)
        # Switch recovery after the open window, before the close buffer. New-deposit
        # buys keep the open window only (the engine enforces that).
        may_recover = bool(recovery and may_manage and not regular_window and recovery[0] <= now < recovery[1])
        # Carry-over is recorded once recovery can no longer place orders today.
        window_over = bool(session_row) and now >= (recovery[1] if recovery else session_row[1] + timedelta(minutes=15))
        session_key = schedule.session_key(now)
        engine = Engine(broker, journal, settings)
        # Reads can be slow: quote freshness and the window are re-checked against
        # the wall clock right before a request is sent.
        engine.clock = lambda: datetime.now(timezone.utc)
        try:
            _, engine.tax_plan, engine.tax_blockers_fn = make_plan(journal, cfg, now)
        except JournalError:
            raise
        except Exception as exc:
            # Tax tables unusable: the engine still settles its own requests and
            # may send a confirmed TQQQ exit sell; new buys and FX are held.
            engine.tax_unavailable = "tax plan unavailable (" + type(exc).__name__ + ")"
            journal.event("tax_plan_failed", {"error_type": type(exc).__name__}, now)
        engine.reverse_fx_verified = (cfg.get("verified") or {}).get("reverse_fx") is True
        signal = signal or {"target": None, "signal_date": None}
        result = engine.tick(target=signal["target"], signal_date=signal["signal_date"], signal_error=signal_error,
                             expected_signal_date=expected, session=session_key,
                             now=now, may_exchange=schedule.exchange_window(now), may_trade=may_trade,
                             may_manage_orders=may_manage, market_block=market_block, window_over=window_over,
                             trade_deadline=schedule.execution_deadline(now) if may_trade else None,
                             fx_deadline=schedule.exchange_deadline(now),
                             may_recover=may_recover, recovery_deadline=recovery[1] if may_recover else None)
        journal.put_meta("auth_failures", 0)
        last_message = result
        if signal_error:
            # Own requests were settled above; the run still ends as a hold.
            journal.event("result", {"message": result}, now)
            raise Stop("New orders blocked: " + signal_error)
        if engine.tax_unavailable:
            journal.event("result", {"message": result}, now)
            raise Stop(f"New buys and FX held: {engine.tax_unavailable}; tax reserves cannot be checked")
        journal.put_meta("health", {"at": now.isoformat(), "result": result,
                                   "target": signal["target"], "mode": settings.mode})
        after_success(journal, notifier, result, signal, session_key, now)
        outcome = "ok"
        previous = journal.meta("last_result")
        if result != previous:
            journal.event("result", {"message": result}, now)
            journal.put_meta("last_result", result)
            output({"at": now.isoformat(), "target": signal["target"], "result": result, "mode": settings.mode})
        return 0
    except Exception as exc:
        # Exception strings from third-party code could contain secrets; only
        # our controlled Stop messages are published, otherwise the type name.
        message = str(exc) if isinstance(exc, Stop) else "Internal error: " + type(exc).__name__
        last_message = message
        try:
            if isinstance(exc, AuthError) and exc.explicit:
                count = (journal.meta("auth_failures") or 0) + 1
                journal.put_meta("auth_failures", count)
                if count >= AUTH_LOCK_COUNT:
                    set_lock(journal, "auth_refused", f"Token issue refused {count} times in a row", now,
                             required="Check the app key/secret and API permission in the Meritz portal, then `unlock`")
            journal.event("error", {"message": message}, now)
            locked = journal.meta("halt")
            outcome = "lock" if locked else ("hold" if isinstance(exc, Stop) else "error")
            if locked:
                info = journal.meta("lock_info") or {}
                notifier.send("lock", info.get("code", "lock"), locked, now, required=info.get("required"))
            else:
                notifier.send("hold", hold_code(message), message, now)
                if not journal.meta("active_hold"):
                    journal.put_meta("active_hold", {"message": message, "since": now.isoformat()})
            journal.put_meta("health", {"at": now.isoformat(), "error": message, "locked": bool(locked),
                                       "mode": settings.mode})
        except Exception:
            pass   # bookkeeping failure must not mask the original stop
        output({"error": message, "mode": settings.mode})
        return 1
    finally:
        try:
            # Switch progress and delay alerts: notifications only, never trading state.
            progress.observe(journal, notifier, schedule or Schedule(now), now, last_message, mode=settings.mode,
                             recovery_buffer_minutes=settings.recovery_close_buffer_minutes,
                             alerts_cfg=cfg.get("alerts"), locked=bool(journal.meta("halt")))
        except Exception as exc:
            try:
                journal.event("progress_failed", {"error_type": type(exc).__name__}, now)
            except Exception:
                pass
        housekeeping(journal, notifier, cfg, broker, schedule, now)
        # Last, after trading and housekeeping: a monitor outage cannot delay anything.
        heartbeat.publish(journal, now=datetime.now(timezone.utc), run_at=now, outcome=outcome,
                          work_done=outcome == "ok")
        journal.db.close()


LIVE_CHECK_POLL_SECONDS = 5
LIVE_CHECK_MAX_MINUTES = 16          # one open window (minutes 1-15) plus settling
LIVE_CHECK_MAX_TICKS = 400           # hard bound on one invocation's loop


def live_check_sleep(seconds):
    time.sleep(seconds)


def tax_reserved_usd(journal, cfg, now):
    """USD held for tax; the live check never spends it. An unusable tax plan
    means the reserve is unknown, so the check holds."""
    try:
        _, plan, _ = make_plan(journal, cfg, now)
        return plan.reserved(now)[1]
    except Exception as exc:
        raise Stop("Tax reserves cannot be checked (" + type(exc).__name__ + "); live check holds") from exc


def live_check_command(args):
    """plan (read-only) / execute / resume / stop / status. Financial requests
    only with --plan and --confirm-live, within that plan."""
    cfg, state, settings = load_config(args.config, allow_unverified_live=True)
    if args.action in ("execute", "resume") and not (args.plan and args.confirm_live):
        raise Stop("execute/resume need --plan ID and --confirm-live; nothing was sent")
    if args.action == "stop" and not args.plan:
        raise Stop("stop needs --plan ID")
    if args.action == "plan" and args.budget_usd is None:
        raise Stop("plan needs --budget-usd (there is no default budget)")
    journal = Journal(state / "journal.sqlite")
    try:
        livecheck.ensure_schema(journal.db)            # outside any transaction
        if args.action == "stop":
            # No lock: a running executor holds them and picks the request up on
            # its next tick; the request and the cancel result are shown apart.
            livecheck.request_stop(journal, args.plan, datetime.now(timezone.utc))
    finally:
        journal.db.close()
    if args.action == "status":
        journal = Journal(state / "journal.sqlite")
        try:
            ids = [args.plan] if args.plan else [r[0] for r in journal.db.execute(
                "SELECT plan_id FROM live_checks ORDER BY created DESC LIMIT 5")]
            output({"live_checks": [livecheck.report(journal, i) for i in ids],
                    "blocking": livecheck.blocking(journal, account_marker_path(cfg))})
        finally:
            journal.db.close()
        return 0
    if args.action == "stop" and not args.confirm_live:
        return _stop_report(state, args.plan, "cancel not sent by this command: a running execute/resume "
                            "cancels it on its next tick, or run stop/resume with --confirm-live")
    load_env(Path(args.config).resolve().parent / "secrets.env")
    with open(state / "run.lock", "a") as lock, open(account_lock_path(cfg), "a") as account:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(account, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.action == "stop":
                return _stop_report(state, args.plan, "a running execute/resume holds the account and cancels "
                                    "on its next tick")
            raise Stop("Another process is working on this account; nothing was sent") from None
        journal = Journal(state / "journal.sqlite")
        try:
            return live_check_locked(args, cfg, state, settings, journal)
        finally:
            journal.db.close()


def _stop_report(state, plan_id, note):
    journal = Journal(state / "journal.sqlite")
    try:
        data = dict(livecheck.report(journal, plan_id), note=note)
    finally:
        journal.db.close()
    livecheck.write_report(state, plan_id, data)
    output(data)
    return 0


def live_check_locked(args, cfg, state, settings, journal):
    from .core import Locked, UnknownOutcome
    now = datetime.now(timezone.utc)
    marker = account_marker_path(cfg)
    live = args.action in ("execute", "resume", "stop")
    client = Client(state, allow_mutations=live)
    if args.action == "plan":
        broker = Meritz(client, journal, settings.instrument_markets)
        plan, reasons = livecheck.create_plan(journal, broker, settings, budget=args.budget_usd, now=now,
                                              tax_reserved_usd=tax_reserved_usd(journal, cfg, now), state_dir=state)
        if plan:
            livecheck.write_report(state, plan["plan_id"], livecheck.report(journal, plan["plan_id"]))
        output({"plan": plan, "refused": reasons, "financial_requests_sent": 0})
        return 0 if plan else 1
    row = livecheck._row(journal, args.plan)
    if row is None:
        raise Stop("Unknown live-check plan")
    if args.action == "execute" and row["status"] != "planned":
        raise Stop(f"Plan is {row['status']}; execute accepts a fresh plan only")
    if args.action in ("resume", "stop") and row["status"] not in livecheck.BLOCKING:
        raise Stop(f"Plan is {row['status']}; nothing to resume (a new plan is needed for new trades)")
    client.mutation_filter = livecheck.transport_filter(journal, args.plan, row["plan"]["routes"])
    broker = Meritz(client, journal, settings.instrument_markets)
    notifier = Notifier(journal, enabled=cfg.get("notifications", True))
    engine = Engine(broker, journal, settings)
    engine.clock = lambda: datetime.now(timezone.utc)
    if args.action == "execute":
        livecheck.start_checked(journal, broker, args.plan, now, marker=marker, state_dir=state)
    results, started = [], now
    done_before = set(livecheck.ledger(journal, row["plan"])["done_steps"])
    try:
        while True:
            now = engine.clock()
            schedule = Schedule(now)
            session_row = schedule.trading_session(now)
            market = confirm_regular_session(schedule, broker.market_status, now) if session_row else None
            may_manage = bool(market and market["regular"])
            may_trade = schedule.execution_window(now) and may_manage
            try:
                reserved = tax_reserved_usd(journal, cfg, now)
            except Stop:
                reserved = None          # unknown reserve: own orders still settle, new buys hold
            result = livecheck.tick(engine, journal, args.plan, now, may_trade=may_trade, may_manage=may_manage,
                                    deadline=schedule.execution_deadline(now) if may_trade else None,
                                    session=schedule.session_key(now), tax_reserved_usd=reserved, marker=marker,
                                    unwind=bool(args.unwind))
            results.append({"at": now.isoformat(), "result": result})
            done = set(livecheck.ledger(journal, row["plan"])["done_steps"])
            for step in sorted(done - done_before):
                side, symbol = livecheck.STEPS[step - 1]
                notifier.send("check", f"{args.plan}:{step}",
                              f"실계좌 검증 {step}/6단계 체결 확인: {symbol} {'매수' if side == 'buy' else '매도'}", now)
            done_before = done
            if result in ("complete",) or result.startswith(("stopped", "stopping:", "idle:")):
                break
            if (now - started).total_seconds() > LIVE_CHECK_MAX_MINUTES * 60 or len(results) >= LIVE_CHECK_MAX_TICKS:
                results.append({"at": now.isoformat(), "result": "paused: time limit; resume later"})
                break
            live_check_sleep(LIVE_CHECK_POLL_SECONDS)
    except (Stop, Locked, UnknownOutcome) as exc:
        results.append({"at": engine.clock().isoformat(), "error": str(exc)})
        notifier.send("check", f"{args.plan}:hold:{hold_code(str(exc))}", "실계좌 검증 보류: " + str(exc),
                      engine.clock())
        data = dict(livecheck.report(journal, args.plan), run=results)
        livecheck.write_report(state, args.plan, data)
        output(data)
        return 1
    data = dict(livecheck.report(journal, args.plan), run=results)
    if data["status"] in ("completed", "stopped"):
        notifier.send("check", f"{args.plan}:{data['status']}",
                      "실계좌 검증 " + ("완료: 테스트 보유 없음, USD로 종료" if data["status"] == "completed"
                                  else "중단 완료: 테스트 보유 없음"), engine.clock())
    livecheck.write_report(state, args.plan, data)
    output(data)
    return 0


def service_status_command(args):
    """Read-only: which launchd service is installed/loaded, FileVault state,
    last run age and the external monitor's configuration on this Mac."""
    cfg, state, _ = load_config(args.config, allow_unverified_live=True)
    load_env(Path(args.config).resolve().parent / "secrets.env")
    journal = Journal(state / "journal.sqlite")
    try:
        output(ops.service_status(ROOT, journal.db, datetime.now(timezone.utc)))
    finally:
        journal.db.close()
    return 0


def utcnow():
    return datetime.now(timezone.utc)


def doctor(args):
    """Read-only diagnosis. Never builds an Engine or a mutation-capable client."""
    from .diagnose import CREDENTIAL_KEYS, collect, render, sanitize
    cfg, state, settings = load_config(args.config, allow_unverified_live=True)
    load_env(Path(args.config).resolve().parent / "secrets.env")
    with open(state / "run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("Another run is using this state directory; retry shortly")
        journal = Journal(state / "journal.sqlite")
        try:
            now = utcnow()
            report = collect(cfg=cfg, state=state, settings=settings, now=now, schedule=Schedule(now),
                             journal=journal, root=ROOT,
                             client_factory=lambda: Client(state, allow_mutations=False),
                             broker_factory=lambda client: Meritz(client, journal, settings.instrument_markets))
        finally:
            journal.db.close()
    secrets = [os.environ.get(k, "") for k in CREDENTIAL_KEYS + ("MERITZ_MAC_ADDRESS", "ALERT_SMTP_PASSWORD",
                                                                  "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                                                                  "MONITOR_SECRET")]
    try:
        secrets.append(json.loads((state / "token.json").read_text()).get("token", ""))
    except (OSError, ValueError, AttributeError):
        pass
    report = sanitize(json.loads(json.dumps(report, default=str)), secrets)
    folder = state / "doctor"
    folder.mkdir(exist_ok=True, mode=0o700)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    stamp = report["generated_at"][:19].replace(":", "").replace("-", "")
    for name in (f"doctor-{stamp}.json", "latest.json"):
        path = folder / name
        path.write_text(text)
        os.chmod(path, 0o600)
    if args.json:
        print(text)
    else:
        print(sanitize(render(report), secrets))
        print(f"\nJSON 보고서: {folder / 'latest.json'}")
    return 0


def demo(args):
    path = Path(args.state).resolve()
    if path.exists():
        raise Stop("Demo output directory already exists; choose a new one")
    path.mkdir(parents=True, mode=0o700)
    broker = Paper(krw="10000000")
    journal = Journal(path / "journal.sqlite")
    engine = Engine(broker, journal, Settings(mode="paper"))
    now = datetime(2026, 10, 15, 13, 31, tzinfo=timezone.utc)
    rows = []
    for i in range(7):
        result = engine.tick(target="TQQQ", signal_date="2026-10-14", expected_signal_date="2026-10-14",
                             session="2026-10-15", now=now + timedelta(seconds=10*i),
                             may_exchange=i == 0, may_trade=True)
        rows.append({"step": i + 1, "result": result})
        for number, order in list(broker.orders.items()):
            if order["state"] == "open":
                broker.fill(number)
    report = {"network_used": False, "real_orders_sent": False, "steps": rows,
              "final": vars(broker.snapshot()), "actions": journal.rows()}
    (path / "demo.json").write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    output(report)
    return 0


def local_status(args):
    cfg, state, _ = load_config(args.config)
    journal = Journal(state / "journal.sqlite")
    output({"health": journal.meta("health"), "signal": journal.meta("signal"),
            "halt": journal.meta("halt"), "lock": journal.meta("lock_info"), "hold": journal.meta("active_hold"),
            "failures": journal.meta("failures"), "auth_failures": journal.meta("auth_failures"),
            "monitor": {"instance": heartbeat.instance_id(journal), "last_send": journal.meta("monitor_last_send")},
            "pending": [{k: r[k] for k in ("id", "kind", "status", "created", "session", "filled", "attempt")}
                        for r in journal.pending()],
            "live_check": livecheck.blocking(journal, account_marker_path(cfg)),
            "switch_progress": {k: v for k, v in progress.summary(journal).items() if k != "fingerprint"},
            "issues": ops.issues_lines(journal.db, datetime.now(timezone.utc),
                                       monitor_configured=bool(os.environ.get("MONITOR_URL"))),
            "books": books.diagnose(journal, datetime.now(timezone.utc))})
    return 0


def resolve(args):
    _, state, _ = load_config(args.config)
    with open(state / "run.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = Journal(state / "journal.sqlite")
        action = journal.db.execute("SELECT * FROM actions WHERE id=?", (args.action,)).fetchone()
        if not action or action["status"] not in {"unknown", "submitting", "accepted", "partial", "cancelling",
                                                  "rejected", "done"}:
            raise Stop("Action not found or unsupported state")
        journal.update(args.action, "operator_resolved", result={"note": args.note, "outcome": args.outcome})
        info = journal.meta("lock_info") or {}
        if not journal.pending() and info.get("action") in (None, args.action):
            journal.put_meta("halt", None)
            journal.put_meta("lock_info", None)
        journal.event("operator_resolution", {"action": args.action, "note": args.note, "outcome": args.outcome}, datetime.now(timezone.utc))
        output({"resolved": args.action, "account_reconciliation_required_before_this_command": True,
                "still_locked": bool(journal.meta("halt"))})
    return 0


def unlock(args):
    """Clear a hard lock after a person fixed its cause. Refused while any request
    is still in flight: those need `resolve --action` with the account checked."""
    _, state, _ = load_config(args.config)
    with open(state / "run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("Another run is using this state directory; retry shortly")
        journal = Journal(state / "journal.sqlite")
        try:
            unresolved = journal.with_status(SUBMITTING, UNKNOWN)
            if unresolved:
                raise Stop("Unresolved requests remain; reconcile them in the broker app and run "
                           "`resolve --action` for each: " + ", ".join(r["id"] for r in unresolved))
            previous = journal.meta("lock_info")
            if previous:
                mark_resolved(journal, "lock:" + str(previous.get("code", "lock")), datetime.now(timezone.utc))
            for key, value in (("halt", None), ("lock_info", None), ("failures", {"count": 0}), ("auth_failures", 0)):
                journal.put_meta(key, value)
            journal.db.execute("DELETE FROM metadata WHERE key LIKE 'seen:%'")
            journal.db.commit()
            journal.event("operator_unlock", {"note": args.note, "previous": previous}, datetime.now(timezone.utc))
            output({"unlocked": True, "previous_lock": (previous or {}).get("code")})
        finally:
            journal.db.close()
    return 0


def tax_command(args):
    """Local estimate from the stored ledger. No network request."""
    cfg, state, _ = load_config(args.config, allow_unverified_live=True)
    journal = Journal(state / "journal.sqlite")
    try:
        tax.ensure_schema(journal.db)
        settings = tax.TaxSettings.from_config(cfg.get("tax"))
        now = datetime.now(timezone.utc)
        years = [now.year] if now.month > 5 else [now.year - 1, now.year]
        _, plan, blockers = make_plan(journal, cfg, now)
        report = {str(y): plan.estimate_fn(y) for y in years}
        active = plan.active_years(now)
        plans = {}
        for year in active or [now.astimezone(KST).year]:
            status = plan.status(year, now)
            status.pop("estimate", None)
            plans[str(year)] = dict(status, tax_sell_blockers=blockers(year))
        output({"estimates": report, "active_years": active, "plans": plans, "sync": journal.meta("tax_sync")})
    finally:
        journal.db.close()
    return 0


def tax_sync_command(args):
    """Read-only ledger sync: GET requests and token issue only."""
    cfg, state, _ = load_config(args.config, allow_unverified_live=True)
    load_env(Path(args.config).resolve().parent / "secrets.env")
    with open(state / "run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("Another run is using this state directory; retry shortly")
        journal = Journal(state / "journal.sqlite")
        try:
            tax.ensure_schema(journal.db)
            now = datetime.now(timezone.utc)
            broker = Meritz(Client(state, allow_mutations=False), journal)
            schedule = Schedule(now)
            result = tax.sync(journal.db, broker, [s[0] for s in schedule.sessions], now,
                              str((cfg.get("tax") or {}).get("ledger_start", "00000000")))
            journal.put_meta("tax_sync", dict(result, day=now.astimezone(KST).date().isoformat(), ok=True))
            output(result)
        finally:
            journal.db.close()
    return 0


def telegram_command(args):
    """Process pending Telegram commands once (tax plan only; never trades)."""
    cfg, state, _ = load_config(args.config, allow_unverified_live=True)
    load_env(Path(args.config).resolve().parent / "secrets.env")
    journal = Journal(state / "journal.sqlite")
    try:
        now = datetime.now(timezone.utc)
        updates = telegram_bot.fetch(journal)              # network wait: no lock held
        with open(state / "run.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # A run holds the state lock (and reads/moves reserves): answer the
                # leading read-only messages now; plan changes wait for the run.
                done = telegram_bot.process(journal, updates, tax.TaxSettings(), now, readonly_only=True)
                output({"processed": done, "deferred_to_run": len(updates) - done})
                return 0
            try:
                settings, plan, _ = make_plan(journal, cfg, now)
            except Exception as exc:
                journal.event("tax_failed", {"stage": "telegram", "error_type": type(exc).__name__}, now)
                output({"processed": telegram_bot.process(journal, updates, tax.TaxSettings(), now,
                                                          plan_unavailable=True), "plan_unavailable": True})
                return 0
            lines, line_now = telegram_bot.plan_lines(plan)
            line_now[0] = now
            output({"processed": telegram_bot.process(journal, updates, settings, now, extra_lines=lines,
                                                      handlers=telegram_bot.plan_handlers(plan),
                                                      estimate_fn=plan.estimate_fn)})
    finally:
        journal.db.close()
    return 0


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description="Meritz DD15/12 automation; defaults to read-only observation")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("live-check", help="Small real-account check of TQQQ/SGOV with a named USD budget; "
                                           "existing holdings are never ordered")
    s.add_argument("action", choices=["plan", "execute", "resume", "stop", "status"])
    s.add_argument("--config", default="config.json")
    s.add_argument("--budget-usd", help="plan: USD budget for the whole check (required, no default)")
    s.add_argument("--plan", help="plan id (execute/resume/stop/status)")
    s.add_argument("--confirm-live", action="store_true", help="execute/resume: allow this plan's real requests")
    s.add_argument("--unwind", action="store_true", help="resume after stop: sell only the check's own shares")
    for name in ("run", "doctor", "status", "resolve", "unlock", "tax", "tax-sync", "telegram", "service-status"):
        s = sub.add_parser(name)
        s.add_argument("--config", default="config.json")
        if name == "doctor":
            s.add_argument("--json", action="store_true", help="Print the sanitized JSON report instead of the summary")
        if name == "resolve":
            s.add_argument("--action", required=True)
            s.add_argument("--outcome", required=True, choices=["account_reconciled", "confirmed_not_sent"])
            s.add_argument("--note", required=True)
        if name == "unlock":
            s.add_argument("--note", required=True, help="What was checked and fixed")
    s = sub.add_parser("demo")
    s.add_argument("--state", default="demo_output")
    s = sub.add_parser("replay", help="Persistent offline deposit/switch/restart scenario; no broker connection")
    s.add_argument("--scenario", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--sessions", type=int, help="Process this many sessions, then stop; rerun to resume")
    args = p.parse_args()
    try:
        if args.command == "replay":
            from .replay import replay
            output(replay(args.scenario, args.state, max_sessions=args.sessions))
            return 0
        return {"run": run, "doctor": doctor, "status": local_status, "resolve": resolve,
                "unlock": unlock, "demo": demo, "tax": tax_command, "tax-sync": tax_sync_command,
                "telegram": telegram_command, "live-check": live_check_command,
                "service-status": service_status_command}[args.command](args)
    except (Stop, OSError, ValueError) as exc:
        output({"error": str(exc) if isinstance(exc, Stop) else type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
