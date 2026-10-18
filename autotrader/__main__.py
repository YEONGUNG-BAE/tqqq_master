import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import (D, SUBMITTING, UNKNOWN, Engine, Journal, JournalError, Settings, Stop, set_lock)
from .history import refresh
from .meritz import AuthError, Client, Meritz
from .notify import Notifier, default_channels, scrub, secret_values
from .paper import Paper
from .schedule import KST, Schedule, confirm_regular_session

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_UNUSABLE = "journal_unusable.json"
AUTH_LOCK_COUNT = 3      # explicit token refusals in a row -> hard lock (revoked key/permission)
ENV_KEYS = {"MERITZ_CLIENT_ID", "MERITZ_CLIENT_SECRET", "MERITZ_MAC_ADDRESS",
            "ALERT_SMTP_HOST", "ALERT_SMTP_PORT", "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD",
            "ALERT_FROM", "ALERT_TO", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}


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
        notifier.send("recovered", "hold-cleared", "보류가 해소되어 자동 운용을 재개했습니다: " + hold["message"], now)
        journal.put_meta("active_hold", None)
    if result.startswith("recovered:"):
        notifier.send("recovered", result + session, "응답이 불명확했던 요청을 증권사 기록으로 확인했습니다 ("
                      + result.split(":", 1)[1].strip() + "). 재전송하지 않았습니다.", now)
    if result.startswith("carried over:"):
        notifier.send("carryover", session, f"{session} 실행창 안에 목표 {signal['target']}를 끝내지 못했습니다: "
                      + result.split(":", 1)[1].strip(), now)
    position = journal.meta("position_state") or {}
    if position.get("aligned") and journal.meta("aligned_target") != position.get("target"):
        notifier.send("done", "aligned:" + position["target"], f"목표 {position['target']} 보유로 정렬을 마쳤습니다.", now)
        journal.put_meta("aligned_target", position["target"])
    day = now.astimezone(KST).date().isoformat()
    if journal.meta("heartbeat_day") != day:
        notifier.send("ok", day, f"자동 운용 중입니다. 현재 신호 목표: {signal['target']}", now)
        journal.put_meta("heartbeat_day", day)


def run(args):
    cfg, state, settings = load_config(args.config)
    load_env(Path(args.config).resolve().parent / "secrets.env")
    with open(state / "run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        with open(account_lock_path(cfg), "a") as account:
            try:
                fcntl.flock(account, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0   # another process (any state folder) is working on this account
            return run_locked(cfg, state, settings)


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
    try:
        client = Client(state, allow_mutations=settings.mode == "live")
        broker = Meritz(client, journal)
        schedule = Schedule(now)
        expected = schedule.expected_close(now)
        history_path = state / "qqq_close.csv"
        if not history_path.exists():
            shutil.copyfile(ROOT / "qqq_seed.csv", history_path)
        signal = journal.meta("signal")
        signal_error = None
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
        window_over = bool(session_row) and now >= session_row[1] + timedelta(minutes=15)
        session_key = schedule.session_key(now)
        engine = Engine(broker, journal, settings)
        # Reads can be slow: quote freshness and the window are re-checked against
        # the wall clock right before a request is sent.
        engine.clock = lambda: datetime.now(timezone.utc)
        signal = signal or {"target": None, "signal_date": None}
        result = engine.tick(target=signal["target"], signal_date=signal["signal_date"], signal_error=signal_error,
                             expected_signal_date=expected, session=session_key,
                             now=now, may_exchange=schedule.exchange_window(now), may_trade=may_trade,
                             may_manage_orders=may_manage, market_block=market_block, window_over=window_over,
                             trade_deadline=schedule.execution_deadline(now) if may_trade else None,
                             fx_deadline=schedule.exchange_deadline(now))
        journal.put_meta("auth_failures", 0)
        if signal_error:
            # Own requests were settled above; the run still ends as a hold.
            journal.event("result", {"message": result}, now)
            raise Stop("New orders blocked: " + signal_error)
        journal.put_meta("health", {"at": now.isoformat(), "result": result,
                                   "target": signal["target"], "mode": settings.mode})
        after_success(journal, notifier, result, signal, session_key, now)
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
        try:
            if isinstance(exc, AuthError) and exc.explicit:
                count = (journal.meta("auth_failures") or 0) + 1
                journal.put_meta("auth_failures", count)
                if count >= AUTH_LOCK_COUNT:
                    set_lock(journal, "auth_refused", f"Token issue refused {count} times in a row", now,
                             required="Check the app key/secret and API permission in the Meritz portal, then `unlock`")
            journal.event("error", {"message": message}, now)
            locked = journal.meta("halt")
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
        journal.db.close()


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
                             broker_factory=lambda client: Meritz(client, journal))
        finally:
            journal.db.close()
    secrets = [os.environ.get(k, "") for k in CREDENTIAL_KEYS + ("MERITZ_MAC_ADDRESS", "ALERT_SMTP_PASSWORD",
                                                                  "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")]
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
    _, state, _ = load_config(args.config)
    journal = Journal(state / "journal.sqlite")
    output({"health": journal.meta("health"), "signal": journal.meta("signal"),
            "halt": journal.meta("halt"), "lock": journal.meta("lock_info"), "hold": journal.meta("active_hold"),
            "failures": journal.meta("failures"), "auth_failures": journal.meta("auth_failures"),
            "pending": [{k: r[k] for k in ("id", "kind", "status", "created", "session", "filled", "attempt")}
                        for r in journal.pending()]})
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
            for key, value in (("halt", None), ("lock_info", None), ("failures", {"count": 0}), ("auth_failures", 0)):
                journal.put_meta(key, value)
            journal.db.execute("DELETE FROM metadata WHERE key LIKE 'seen:%'")
            journal.db.commit()
            journal.event("operator_unlock", {"note": args.note, "previous": previous}, datetime.now(timezone.utc))
            output({"unlocked": True, "previous_lock": (previous or {}).get("code")})
        finally:
            journal.db.close()
    return 0


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description="Meritz DD15/12 automation; defaults to read-only observation")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "doctor", "status", "resolve", "unlock"):
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
                "unlock": unlock, "demo": demo}[args.command](args)
    except (Stop, OSError, ValueError) as exc:
        output({"error": str(exc) if isinstance(exc, Stop) else type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
