import csv
import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from .core import D, Stop, money
from . import oslock
from .signal import RULE, SIGNAL_SYMBOL, STRATEGY_VERSION, WINDOW, decisions


def read_history(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    result = {}
    previous = ""
    for row in rows:
        day = date.fromisoformat(row["date"]).isoformat()
        price = money(row["close"])
        if day <= previous or price <= 0:
            raise Stop("History must contain positive closes on unique ascending session dates")
        previous = day
        result[day] = price
    if len(result) < 252:
        raise Stop("At least 252 daily observations required")
    return result


def merge_history(old, fresh, required):
    """Verify overlapping split-only prices; never silently accept dividend changes."""
    overlap = sorted(set(old) & set(fresh))
    if len(overlap) < 100:
        raise Stop("Insufficient price overlap to verify broker history")
    changed = [day for day in overlap if abs(old[day] - fresh[day]) > D("0.011")]
    rebase = None
    result = dict(old)
    if changed:
        ratio = old[changed[0]] / fresh[changed[0]]
        allowed = [D(v) for v in ("2", "3", "4", "5", "10", "0.5", "0.333333333333", "0.25", "0.2", "0.1")]
        factor = min(allowed, key=lambda x: abs(x - ratio))
        boundary = max(changed)
        if len(changed) < 100 or abs(ratio / factor - 1) > D("0.00002"):
            raise Stop("Broker close basis differs from seed; verify split-only, ex-dividend treatment")
        for day in overlap:
            adjusted = old[day] / factor if day <= boundary else old[day]
            if abs(adjusted - fresh[day]) > D("0.011"):
                raise Stop("Historical changes are not a uniform split rebase")
        result = {day: price / factor if day <= boundary else price for day, price in old.items()}
        rebase = {"through": boundary, "divide_by": str(factor)}
    result.update(fresh)
    if any(day not in result for day in required):
        raise Stop("Missing trading-session closes in the 252-session window")
    if not fresh or max(fresh) != required[-1]:
        raise Stop("Latest completed daily close is not available")
    return result, rebase


def refresh(broker, path, schedule, expected):
    old = {day: price for day, price in read_history(path).items() if day <= expected}
    required = schedule.recent_dates(expected)
    fresh = {}
    # Replay every missing session, even after >252 sessions offline. Omitting
    # the middle of a long gap changes the hysteresis state and is not recovery.
    gap = [s[0] for s in schedule.sessions if max(old) < s[0] <= expected]
    if max(old) < required[0] and max(old) < schedule.sessions[0][0]:
        raise Stop("History gap exceeds calendar coverage; backfill history before running")
    overlap_dates = sorted(set(old) & {s[0] for s in schedule.sessions})
    if len(overlap_dates) < 100:
        raise Stop("Calendar cannot cover enough existing history for overlap validation")
    first = min(required[0], overlap_dates[-min(252, len(overlap_dates))])
    requested = [s[0] for s in schedule.sessions if first <= s[0] <= expected]
    cursor, last = date.fromisoformat(first), date.fromisoformat(expected)
    # Small chunks avoid the endpoint's default forty-bar response and arbitrary
    # long-range truncation. Every expected session is checked after collection.
    while cursor <= last:
        end = min(last, cursor + timedelta(days=27))
        chunk = broker.completed_candles(cursor.isoformat(), end.isoformat())
        fresh.update(chunk)
        cursor = end + timedelta(days=1)
    if set(fresh) - set(requested):
        raise Stop("Broker returned a non-session close inside the requested range")
    history, rebase = merge_history(old, fresh, required)
    if any(day not in history for day in gap):
        raise Stop("Missing intervening session; cannot replay signal state across history gap")
    tmp = Path(path).with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "close"])
        writer.writerows(sorted(history.items()))
    oslock.replace(tmp, path)
    decision = asdict(decisions([price for _, price in sorted(history.items())])[-1])
    decision.update(signal_date=expected, history_rebase=rebase, **SIGNAL_FIELDS)
    return decision


SIGNAL_FIELDS = dict(rule=RULE, strategy=STRATEGY_VERSION, signal_symbol=SIGNAL_SYMBOL,
                     execution="NEXT_SESSION_OPEN_WINDOW_SEQUENTIAL_LIMIT")


def recompute(path, expected):
    """Signal for `expected` from the stored QQQ closes only (no network): the
    whole stored history is replayed from its first close, exactly as refresh
    does, so the hysteresis state is never re-initialised in the middle. Used
    when a cached signal belongs to an earlier strategy version. Stops when the
    stored history does not end on `expected` or is too short."""
    history = {day: price for day, price in read_history(path).items() if day <= expected}
    if not history or max(history) != expected:
        raise Stop("Stored QQQ history does not reach the expected session; refresh from the broker first")
    if len(history) < WINDOW:
        raise Stop("Stored QQQ history is shorter than 252 sessions; no target is derived")
    decision = asdict(decisions([price for _, price in sorted(history.items())])[-1])
    decision.update(signal_date=expected, history_rebase=False, recomputed_from_stored_history=True, **SIGNAL_FIELDS)
    return decision
