"""Deterministic execution engine; broker I/O is injected for offline testing."""
from __future__ import annotations

import json
import random
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from uuid import uuid4

D = Decimal
UTC = timezone.utc


class Stop(Exception):
    """Temporary hold: no new financial request now; re-evaluated on the next run."""


class UnknownOutcome(Stop):
    """A mutation may have succeeded. It must never be blindly repeated."""


class RefusedUnconfirmed(UnknownOutcome):
    """The broker answered a mutation with a non-success code whose meaning is not
    documented as "not processed" (FX). Treated as unknown until broker records
    confirm it did not execute."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class Rejected(Stop):
    """Broker warning/hold that needs a person; never auto-acknowledged. Locks."""


class Refused(Stop):
    """Broker answered and the spec documents the request as NOT accepted (order and
    cancel responses other than 0001). Nothing is live; work may be recomputed
    after a retry back-off."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class NotSent(Stop):
    """Failure provably before the request left this machine (no send-start
    record, connection refused, DNS failure)."""


class Aborted(Stop):
    """The last check right before the request leaves (window, quote freshness)
    failed after token issue/pacing delays. Nothing was sent; not a failure."""


class PendingConfirmation(Stop):
    """The broker answered and the order is known, but it is not yet shown as
    terminal (e.g. a past-session order without a confirmed cancel). Not a lookup
    failure: passing nights or holidays alone never turns it into a lock."""


class Locked(Stop):
    """Hard lock that automatic recovery cannot clear; persisted until a person
    reconciles the account and runs `unlock`/`resolve`. Reads continue."""


class JournalError(Exception):
    """State database missing, unreadable or corrupt: no financial request at all."""


def money(value):
    result = D(str(value))
    if not result.is_finite():
        raise Stop("Non-finite amount from broker")
    return result


def fx_spent(rows, session):
    """KRW already sent to FX in this session; same journal rows the engine uses."""
    return sum((money(json.loads(r["payload"])["krw"]) for r in rows
                if r["kind"] == "fx" and r["session"] == session
                and r["status"] in {"accepted", "done"}), D(0))


def fx_amount(krw, spent, cfg):
    """Whole-won amount to convert; caller compares against cfg.min_fx_krw."""
    return min(max(D(0), krw - cfg.krw_reserve),
               max(D(0), cfg.max_fx_krw_per_day - spent)).quantize(D(1), rounding=ROUND_DOWN)


def check_fx_quote(quote, now, cfg):
    age = (now - quote.at).total_seconds()
    if age < -5 or age > 60 or quote.ask <= 0 or quote.midpoint <= 0:
        raise Stop("Invalid or stale FX quote")
    if abs(quote.ask / quote.midpoint - 1) > cfg.max_fx_spread:
        raise Stop("Applied FX spread exceeds configured limit")


def check_quote(quote, now, cfg):
    age = (now - quote.at).total_seconds()
    if age < -5 or age > 30 or not (D(0) < quote.bid <= quote.ask) or quote.tick <= 0:
        raise Stop("Invalid or stale real-time bid/ask")
    if (quote.ask - quote.bid) / quote.ask > cfg.max_quote_spread:
        raise Stop("Bid/ask spread exceeds configured limit")


def check_side(quote, side):
    """Direction-specific limits; halts and VCM already stopped the quote."""
    reason = dict(getattr(quote, "blocked", ()) or ()).get(side)
    if reason:
        raise Stop(f"{side} blocked: {reason}")


def limit_price(quote, side, cfg):
    price = (quote.ask * (1 + cfg.limit_padding) if side == "buy"
             else quote.bid * (1 - cfg.limit_padding))
    return (price / quote.tick).quantize(D(1), rounding=ROUND_UP if side == "buy" else ROUND_DOWN) * quote.tick


from .signal import (DEFENSIVE_SYMBOL, RISK_SYMBOL, STRATEGY_VERSION,  # noqa: E402  (single definition)
                     logical_target)

STRATEGY_SYMBOLS = frozenset({RISK_SYMBOL, DEFENSIVE_SYMBOL})
# Holdings that may be sold only when the user lists them in migrate_symbols.
# QQQ is the defensive holding of strategy version 1 (now only the signal).
MIGRATABLE = frozenset({"QQQM", "BRK.B", "QQQ"})


def unsupported_holdings(positions, cfg):
    """Holdings the engine refuses to touch; their presence stops trading."""
    return sorted(set(positions) - STRATEGY_SYMBOLS - set(cfg.migrate_symbols))


def symbols_to_sell(positions, target):
    return sorted(s for s, q in positions.items() if s != target and q > 0)


def buy_quantity(usd, buying_power, price, cfg):
    """Whole shares; fee_buffer is reserved because order-available USD excludes fees."""
    budget = max(D(0), min(usd, buying_power) - cfg.usd_reserve)
    return (budget / (price * (1 + cfg.fee_buffer))).quantize(D(1), rounding=ROUND_DOWN)


@dataclass
class Snapshot:
    krw: Decimal
    usd: Decimal
    buying_power: Decimal
    positions: dict[str, Decimal] = field(default_factory=dict)
    sellable: dict[str, Decimal] = field(default_factory=dict)
    external_open_orders: bool = False
    cash_usd: Decimal | None = None


@dataclass
class Quote:
    bid: Decimal
    ask: Decimal
    tick: Decimal
    at: datetime
    # Side-specific broker limits as ((side, reason), ...), e.g. a sell-only
    # security blocks buys but still lets held shares be sold.
    blocked: tuple = ()


@dataclass
class FXQuote:
    ask: Decimal
    midpoint: Decimal
    at: datetime
    server_time: str


@dataclass
class OrderStatus:
    state: str  # open, filled, cancelled, rejected
    filled: Decimal
    raw: dict = field(default_factory=dict)


@dataclass
class ReservationLookup:
    """Active reservations plus how much of the account's history was searched.

    `complete` is True only when the lookup started at a user-confirmed date on
    or before the account's first possible reservation (e.g. the account opening
    date). A fixed look-back cannot prove completeness: the API filters by
    ACCEPTANCE date and no maximum reservation period is documented."""
    live: list
    complete: bool
    searched_from: str | None = None
    searched_to: str | None = None
    chunks: int = 0
    reason: str | None = None


@dataclass
class Settings:
    mode: str = "observe"
    krw_reserve: Decimal = D("0")
    usd_reserve: Decimal = D("0")
    min_fx_krw: Decimal = D("10000")
    max_fx_krw_per_day: Decimal = D("20000000")
    fee_buffer: Decimal = D("0.0015")
    limit_padding: Decimal = D("0.001")
    max_quote_spread: Decimal = D("0.003")
    max_fx_spread: Decimal = D("0.001")
    order_timeout_seconds: int = 120
    max_orders_per_session: int = 20
    migrate_symbols: tuple[str, ...] = ()
    # Meritz quote market per instrument without a reviewed static route, as
    # confirmed by the user with doctor (e.g. {"SGOV": "AX"}); see instruments.py.
    instrument_markets: dict = field(default_factory=dict)
    # YYYYMMDD on/before the account opening date, from the broker app. Without
    # it reservation coverage is unverified and live new actions are blocked.
    reservation_lookup_start: str | None = None
    # Recovery policy (see WORK_STATUS.md for the reasoning behind each default).
    unknown_outcome_lock_minutes: int = 360   # unresolved send -> hard lock after this
    inconsistency_lock_minutes: int = 120     # holdings/fill contradictions -> hard lock
    max_consecutive_failures: int = 5         # refused/not-sent sends in a row -> hard lock
    retry_base_seconds: int = 15              # back-off after a refused/not-sent send
    retry_max_seconds: int = 120
    # Same-session recovery of a confirmed switch after the open window (see
    # WORK_STATUS.md). Limited to the switch's own holdings and sale proceeds.
    intraday_recovery: bool = True
    recovery_close_buffer_minutes: int = 10   # no new recovery order this close to the close
    recovery_retry_seconds: int = 300         # minimum spacing between recovery orders

    def __post_init__(self):
        if self.mode not in {"observe", "paper", "live"}:
            raise ValueError("mode must be observe, paper or live")
        for name in ("krw_reserve", "usd_reserve", "min_fx_krw", "max_fx_krw_per_day",
                     "fee_buffer", "limit_padding", "max_quote_spread", "max_fx_spread"):
            value = money(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            setattr(self, name, value)
        if self.min_fx_krw <= 0 or self.order_timeout_seconds < 30:
            raise ValueError("positive FX minimum and >=30s order timeout required")
        if self.max_orders_per_session < 1:
            raise ValueError("positive order cap required")
        if self.recovery_close_buffer_minutes * 60 < self.order_timeout_seconds + 60 or self.recovery_retry_seconds < 60:
            raise ValueError("recovery must stop early enough to time out and confirm a cancel before the close")
        if (self.unknown_outcome_lock_minutes < 30 or self.inconsistency_lock_minutes < 10
                or self.max_consecutive_failures < 1 or self.retry_base_seconds < 1
                or self.retry_max_seconds < self.retry_base_seconds):
            raise ValueError("recovery limits must be positive and conservative")
        if not set(self.migrate_symbols).issubset(MIGRATABLE):
            raise ValueError("migration allowlist supports " + ", ".join(sorted(MIGRATABLE)) + " only")
        from .instruments import validate_overrides
        self.instrument_markets = validate_overrides(self.instrument_markets)
        if self.reservation_lookup_start is not None:
            text = str(self.reservation_lookup_start)
            try:
                day = datetime.strptime(text, "%Y%m%d").date()
            except ValueError:
                raise ValueError("reservation_lookup_start must be a real YYYYMMDD date") from None
            if len(text) != 8 or day.year < 1990:
                raise ValueError("reservation_lookup_start must be a real YYYYMMDD date from 1990")
            self.reservation_lookup_start = text


# Action lifecycle. Every financial request (fx, order, cancel) is one row.
PREPARED = "prepared"        # intent durable; send not started (restart -> not_sent)
SUBMITTING = "submitting"    # send-start durable; outcome pending (crash -> unknown)
UNKNOWN = "unknown"          # sent or maybe sent, no receipt: reconcile, never resend
NOT_SENT = "not_sent"        # terminal: provably never transmitted
REFUSED = "refused"          # terminal: broker documented "not accepted"
REJECTED = "rejected"        # terminal: warning/hold needing a person (locks)
ACCEPTED = "accepted"        # receipt confirmed, live (orders) / awaiting reflection (fx)
PARTIAL = "partial"          # live order with some fills
CANCELLING = "cancelling"    # live order whose cancel request was accepted
DONE = "done"                # terminal; result.state = filled/cancelled/rejected (orders)
IN_FLIGHT = (PREPARED, SUBMITTING, UNKNOWN)
LIVE = (ACCEPTED, PARTIAL, CANCELLING)
PENDING = (SUBMITTING, UNKNOWN) + LIVE
EXTRA_COLUMNS = {"updated": "TEXT", "filled": "TEXT", "attempt": "INTEGER", "broker_ref": "TEXT",
                 "target_version": "TEXT", "run_id": "TEXT", "account": "TEXT", "reason": "TEXT",
                 "inflight": "INTEGER"}


class Journal:
    """Persist intent BEFORE any mutating HTTP request, with FULL durability.

    At most one row may be in flight (prepared/submitting/unknown): a UNIQUE index
    on `inflight` makes a second concurrent intent fail inside SQLite itself."""

    def __init__(self, path):
        self._depth = 0
        try:
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            check = self.db.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise JournalError("State database integrity check failed")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS actions(
                  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                  created TEXT NOT NULL, session TEXT NOT NULL,
                  payload TEXT NOT NULL, receipt TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(at TEXT, category TEXT, body TEXT);
            """)
            have = {r[1] for r in self.db.execute("PRAGMA table_info(actions)")}
            for name, kind in EXTRA_COLUMNS.items():   # additive migration of older journals
                if name not in have:
                    self.db.execute(f"ALTER TABLE actions ADD COLUMN {name} {kind}")
            if "inflight" not in have:
                self.db.execute("UPDATE actions SET inflight=1 WHERE rowid=(SELECT min(rowid) FROM actions "
                                "WHERE status IN (?,?,?))", IN_FLIGHT)
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_in_flight ON actions(inflight)")
            self.db.commit()
        except sqlite3.DatabaseError as exc:
            raise JournalError("State database unreadable: " + type(exc).__name__) from exc

    @contextmanager
    def atomic(self, immediate=False):
        """One transaction for several writes. Inside it no helper commits; the
        whole block commits at the end or rolls back on any exception, so a kill
        between two related writes can never leave only one of them on disk.
        immediate=True takes SQLite's write lock up front (BEGIN IMMEDIATE), so a
        check-then-write by another connection cannot interleave."""
        if immediate and self._depth == 0:
            if self.db.in_transaction:
                self.db.commit()
            self.db.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.db.rollback()
            raise
        self._depth -= 1
        if self._depth == 0:
            self.db.commit()

    def commit(self):
        if self._depth == 0:
            self.db.commit()

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value, default=str)))
        self.commit()

    def event(self, category, value, now):
        self.db.execute("INSERT INTO events VALUES (?,?,?)",
                        (now.isoformat(), category, json.dumps(value, default=str)))
        self.commit()

    def create(self, kind, payload, now, session, status=SUBMITTING, **columns):
        action = str(uuid4())
        values = dict(columns, updated=now.isoformat(), inflight=1 if status in IN_FLIGHT else None)
        names = ["id", "kind", "status", "created", "session", "payload"] + list(values)
        try:
            self.db.execute(f"INSERT INTO actions({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                            (action, kind, status, now.isoformat(), session,
                             json.dumps(payload, default=str), *values.values()))
            self.commit()
        except sqlite3.IntegrityError as exc:
            if self._depth == 0:
                self.db.rollback()
            raise Stop("Another financial request is already in flight; no new request") from exc
        return action

    def update(self, action, status, receipt=None, result=None, **columns):
        values = dict(columns, inflight=1 if status in IN_FLIGHT else None,
                      updated=datetime.now(UTC).isoformat())
        sets = "".join(f", {name}=?" for name in values)
        self.db.execute(f"UPDATE actions SET status=?, receipt=COALESCE(?,receipt), result=COALESCE(?,result){sets} WHERE id=?",
                        (status, json.dumps(receipt, default=str) if receipt is not None else None,
                         json.dumps(result, default=str) if result is not None else None,
                         *values.values(), action))
        self.commit()

    def transition(self, action, old, new):
        """Compare-and-set status change; False if another writer changed the row."""
        cursor = self.db.execute("UPDATE actions SET status=?, inflight=?, updated=? WHERE id=? AND status=?",
                                 (new, 1 if new in IN_FLIGHT else None, datetime.now(UTC).isoformat(), action, old))
        self.commit()
        return cursor.rowcount == 1

    def status(self, action):
        row = self.db.execute("SELECT status FROM actions WHERE id=?", (action,)).fetchone()
        return row[0] if row else None

    def with_status(self, *statuses):
        marks = ",".join("?" * len(statuses))
        return list(self.db.execute(f"SELECT * FROM actions WHERE status IN ({marks}) ORDER BY created", statuses))

    def pending(self):
        return self.with_status(*PENDING)

    def rows(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM actions ORDER BY created")]



# ---- detection shared by the engine's repairs and the read-only diagnosis ----

def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _belongs(intent, row, payload):
    if payload.get("switch"):
        return payload["switch"] == intent["id"]
    # Orders from before intents carried their id: same switch if placed after
    # the intent started and on one of its symbols.
    if row["created"] < intent["created_at"]:
        return False
    return ((payload.get("side") == "sell" and payload.get("symbol") in intent["from"])
            or (payload.get("side") == "buy" and payload.get("symbol") == intent["to"]))


def missing_switch_effects(journal):
    """DONE orders of the current switch intent that it never counted."""
    intent = journal.meta("switch_intent")
    if not intent or intent.get("status") == "superseded":
        return []
    return [r for r in journal.with_status(DONE) if r["kind"] == "order" and r["id"] not in intent["counted"]
            and _belongs(intent, r, json.loads(r["payload"]))]


def missing_tax_effects(journal):
    """DONE tax sells / reverse FX whose reservation is still in progress."""
    if not _table(journal.db, "tax_inprogress"):
        return []
    open_ids = {row[0] for row in journal.db.execute("SELECT action_id FROM tax_inprogress")}
    return [r for r in journal.with_status(DONE) if r["kind"] in ("order", "fx_out") and r["id"] in open_ids]


def orphan_reservations(journal):
    """(action id, status) of in-progress tax reservations whose request is
    provably unsent, refused, rejected, operator-resolved, or has no row."""
    if not _table(journal.db, "tax_inprogress"):
        return []
    rows = journal.db.execute("SELECT action_id FROM tax_inprogress").fetchall()
    return [(a, journal.status(a)) for (a,) in rows
            if journal.status(a) in (None, NOT_SENT, REFUSED, REJECTED, "operator_resolved")]


def set_lock(journal, code, message, now, *, action=None, required=None):
    """Persist a hard lock (first cause wins). Reads and reconciliation continue;
    no new financial request is sent until a person clears it."""
    if not journal.meta("halt"):
        journal.put_meta("halt", message)
        journal.put_meta("lock_info", {"code": code, "message": message, "action": action,
                                       "since": now.isoformat(), "required": required})
        journal.event("lock", {"code": code, "message": message, "action": action}, now)
    return journal.meta("halt")


CLOCK_SKEW_SECONDS = 120          # broker record may precede our clock by this much
PREPARED_STALE_SECONDS = 300       # another process's unsent intent is abandoned only after this


class Engine:
    """One tick = read account -> settle own requests -> gates -> at most one new request.

    Priority: settle existing requests (receipts, fills, cancels) -> cancel own
    orders that conflict with the current target -> switch -> buy with what is
    left. Observe mode runs the same decisions and records plans instead."""

    def __init__(self, broker, journal: Journal, settings: Settings):
        self.broker, self.j, self.cfg = broker, journal, settings
        recorded_mode = self.j.meta("mode")
        if recorded_mode is not None and recorded_mode != self.cfg.mode:
            raise Stop("Use a separate state directory for each execution mode")
        self.j.put_meta("mode", self.cfg.mode)
        self.run_id = str(uuid4())
        self.rng = random.Random()
        self.clock = None   # wall clock for checks made late in a run; None = use the tick's `now`
        # Tax reserve plan (stage 3). None = no tax behaviour (paper replay, tests).
        self.tax_plan = None
        self.tax_blockers_fn = lambda year: []
        # Set when the tax plan could not be built: reserves are then unknown.
        self.tax_unavailable = None
        self.reverse_fx_verified = False
        self.last_action_id = None

    def _current(self, now):
        """Time for freshness and window checks made after slow reads. Never earlier
        than the tick's start, so a skewed clock cannot reopen a closed window."""
        return max(now, self.clock()) if self.clock is not None else now

    def _past(self, deadline, now):
        return deadline is not None and self._current(now) >= deadline

    # ---- locks, failures and back-off (all persisted: a restart resets nothing) ----

    def _lock(self, code, message, now, action=None, required=None):
        raise Locked(set_lock(self.j, code, message, now, action=action, required=required))

    def _register_failure(self, now, reason):
        count = self.j.meta("failures", {}).get("count", 0) + 1
        delay = min(self.cfg.retry_max_seconds, self.cfg.retry_base_seconds * 2 ** (count - 1))
        delay *= 1 + self.rng.random() * 0.25   # jitter only lengthens the wait
        self.j.put_meta("failures", {"count": count, "last": reason, "at": now.isoformat(),
                                     "retry_after": (now + timedelta(seconds=delay)).isoformat()})
        if count >= self.cfg.max_consecutive_failures:
            self._lock("repeated_failures", f"{count} consecutive financial requests were not accepted: {reason}",
                       now, required="Check the broker app for the refusal reason, then run `unlock`")

    def _clear_failures(self):
        if self.j.meta("failures", {}).get("count"):
            self.j.put_meta("failures", {"count": 0})

    def _retry_wait(self, now):
        after = self.j.meta("failures", {}).get("retry_after")
        if after and now < datetime.fromisoformat(after):
            return "waiting: retry back-off after a request that was not accepted"
        return None

    def _persisting(self, key, now, minutes):
        """True once a contradiction has been seen continuously for `minutes`
        and at least 10 times (a sleeping Mac alone does not trip it)."""
        seen = self.j.meta("seen:" + key) or {"since": now.isoformat(), "count": 0}
        seen["count"] += 1
        self.j.put_meta("seen:" + key, seen)
        return seen["count"] >= 10 and (now - datetime.fromisoformat(seen["since"])).total_seconds() >= minutes * 60

    def _forget(self, key):
        if self.j.meta("seen:" + key) is not None:
            self.j.db.execute("DELETE FROM metadata WHERE key=?", ("seen:" + key,))
            self.j.commit()

    # ---- sending ----

    def _submit(self, kind, payload, now, session, call, guard=None, prepared=None, abandoned=None):
        if self.cfg.mode == "observe":
            self.j.event("plan", dict(kind=kind, **payload), now)
            return "observe: " + kind
        if self.j.meta("halt"):
            raise Locked(self.j.meta("halt"))
        attempt = 1 + sum(r["kind"] == kind and r["session"] == session for r in self.j.rows())
        target = payload.get("target")
        with self.j.atomic():
            # The intent and its money reservation exist together or not at all;
            # the reservation exists BEFORE the request can leave.
            action = self.j.create(kind, payload, now, session, status=PREPARED, attempt=attempt, run_id=self.run_id,
                                   account=(self.j.meta("broker_key_fingerprint") or "paper")[:16],
                                   target_version=f"{payload.get('signal_date')}:{target}" if target else None)
            if prepared is not None:
                prepared(action)

        def give_up(status, result):
            with self.j.atomic():
                self.j.update(action, status, result=result)
                if abandoned is not None:
                    abandoned(action)

        def mark_sending():
            # Runs after token issue and call pacing, immediately before the HTTP
            # write: the last chance to drop a request whose window just closed.
            if guard is not None:
                guard()
            # Durable send-start immediately before the transport writes the request.
            try:
                started = self.j.transition(action, PREPARED, SUBMITTING)
            except Exception as exc:
                raise NotSent("Could not record send start; request not sent") from exc
            if not started:
                raise NotSent("Intent changed by another writer; request not sent")

        hook = getattr(self.broker, "set_send_hook", None)
        if hook is None:
            mark_sending()      # adapter without a hook: mark right before the call
        else:
            hook(mark_sending)
        try:
            receipt = call()
        except Aborted as exc:
            give_up(NOT_SENT, {"reason": "aborted before send: " + str(exc)})
            return "idle: " + str(exc)
        except NotSent as exc:
            give_up(NOT_SENT, {"reason": str(exc)})
            self._register_failure(now, "not sent: " + str(exc))
            raise
        except Refused as exc:
            give_up(REFUSED, {"code": exc.code, "reason": str(exc)})
            self._register_failure(now, "refused: " + str(exc))
            raise
        except Rejected as exc:
            give_up(REJECTED, {"reason": str(exc)})
            set_lock(self.j, "broker_warning", "Broker rejected action " + action + ": " + str(exc), now,
                     action=action, required="Read the warning in the broker app; the bot never acknowledges warnings")
            raise
        except Exception as exc:
            if self.j.status(action) == PREPARED:
                # Failed before the send-start record (e.g. token issue): never transmitted.
                give_up(NOT_SENT, {"error_type": type(exc).__name__})
                self._register_failure(now, "not sent: " + type(exc).__name__)
                raise NotSent("Request not sent: " + (str(exc) if isinstance(exc, Stop) else type(exc).__name__)) from exc
            # A timeout, malformed JSON, programming exception or crash after the
            # send-start checkpoint can all leave a successful broker action behind.
            result = {"error_type": type(exc).__name__}
            if isinstance(exc, RefusedUnconfirmed):
                result["refused_code"] = exc.code
            self.j.update(action, UNKNOWN, result=result)
            self.j.event("unknown_outcome", {"action": action, "kind": kind}, now)
            raise UnknownOutcome("Unknown outcome for action " + action + "; reconciling with broker records") from exc
        finally:
            if hook is not None:
                hook(None)
        self.j.update(action, ACCEPTED, receipt=receipt, broker_ref=_ref(receipt))
        self.last_action_id = action
        self._clear_failures()
        return "submitted: " + kind

    # ---- settling existing requests (read-only except own-order cancels) ----

    def _tracked(self):
        refs = {"orders": set(), "fx": set()}
        for r in self.j.rows():
            if not r["receipt"]:
                continue
            receipt = json.loads(r["receipt"])
            if r["kind"] in ("order", "cancel") and "number" in receipt and "date" in receipt:
                refs["orders"].add((str(receipt["date"]), int(receipt["number"])))
            if r["kind"] in ("fx", "fx_out") and receipt.get("ref") is not None:
                refs["fx"].add(str(receipt["ref"]))
        return refs

    def _resolve_unknown(self, row, now):
        """Attribute an unresolved send to exactly one broker record, or wait.

        Matching fields only select candidates. Several candidates are never
        narrowed by guessing; an empty but successful lookup is not proof of
        non-acceptance. Nothing is ever resent from here."""
        payload, result = json.loads(row["payload"]), json.loads(row["result"] or "{}")
        created = datetime.fromisoformat(row["created"])
        age = (now - created).total_seconds()
        finder = getattr(self.broker, "find_candidates", None)
        candidates = None
        if finder is not None:
            try:
                candidates = finder(row["kind"], payload, created, self._tracked())
            except Exception:
                candidates = None   # lookup failure: keep the hold, try again next run
        if candidates is not None:
            if len(candidates) == 1:
                receipt = candidates[0]
                self.j.update(row["id"], ACCEPTED, receipt=receipt, broker_ref=_ref(receipt),
                              result=dict(result, recovered_at=now.isoformat(), recovered_from="broker records"))
                self.j.event("recovered", {"action": row["id"], "kind": row["kind"]}, now)
                self._clear_failures()
                return "recovered: " + row["kind"]
            if len(candidates) > 1:
                self._lock("ambiguous_attribution",
                           f"{len(candidates)} broker records match unresolved action {row['id']}; none attributed",
                           now, row["id"], required="Identify the bot's request in the broker app, then `resolve --action`")
            if row["kind"] == "cancel":
                original = payload["original"]
                state = self.broker.order_status(original)
                if state.state in {"filled", "cancelled", "rejected"}:
                    self.j.update(row["id"], DONE, result=dict(result, original_terminal=state.state))
                    return "resolved: cancel no longer needed"
        if age >= self.cfg.unknown_outcome_lock_minutes * 60:
            self._lock("unknown_outcome", f"Outcome of action {row['id']} ({row['kind']}) still unknown after "
                       f"{self.cfg.unknown_outcome_lock_minutes} minutes", now, row["id"],
                       required="Check the broker app for this request, then `resolve --action` and `unlock`")
        raise UnknownOutcome("Unresolved action " + row["id"] + "; reconciling with broker records, no resubmission")

    def _order_effects(self, action_id, payload, filled, state, now):
        """Derived bookkeeping of a terminal order. Called inside the same
        transaction as the DONE status, and idempotent (keyed by action id)."""
        self._switch_fill({"id": action_id}, payload, filled, state, now)
        if payload.get("tax_year") and self.tax_plan is not None:
            # Conservative: a sell fills at or above its limit; fee buffer deducted.
            self.tax_plan.finish_sell(action_id, filled * money(payload["price"]) * (1 - self.cfg.fee_buffer), now)

    def _replay_effects(self, now):
        """Repair books left incomplete by an older version (status committed,
        derived entries not). Evidence is the journal's own DONE rows; every
        replay is idempotent and recorded as an event."""
        missing = missing_switch_effects(self.j)
        if missing:
            intent = self.j.meta("switch_intent")
            with self.j.atomic():
                if intent["status"] == "done":
                    # Completed without counting its own fills: reopen; the
                    # next run re-checks holdings and money before acting.
                    intent.update(status="buying", completed_at=None, completed_by=None)
                    self._save_intent(intent)
                    self.j.event("switch_reopened", {"id": intent["id"], "missing": [r["id"] for r in missing]}, now)
                for r in missing:
                    payload, result = json.loads(r["payload"]), json.loads(r["result"] or "{}")
                    self._switch_fill(r, payload, money(r["filled"] or result.get("filled", 0)),
                                      result.get("state", "filled"), now)
                    self.j.event("effects_replayed", {"action": r["id"], "what": "switch"}, now)
        if self.tax_plan is not None:
            for r in missing_tax_effects(self.j):
                payload, result = json.loads(r["payload"]), json.loads(r["result"] or "{}")
                receipt = json.loads(r["receipt"] or "{}")
                with self.j.atomic():
                    if r["kind"] == "order":
                        filled = money(r["filled"] or result.get("filled", 0))
                        self.tax_plan.finish_sell(r["id"], filled * money(payload["price"]) * (1 - self.cfg.fee_buffer), now)
                    else:
                        self.tax_plan.finish_fx_out(r["id"], money(payload["usd"]), money(receipt.get("krw", "0")), now)
                    self.j.event("effects_replayed", {"action": r["id"], "what": "tax"}, now)

    def _release_orphans(self, now):
        """In-progress tax reservations whose request can no longer execute.
        Requests that may have executed keep their reservation until reconciled
        with the broker."""
        if self.tax_plan is None:
            return
        for action_id, status in orphan_reservations(self.j):
            with self.j.atomic():
                self.tax_plan.cancel_progress(action_id, now, reason=f"request {status or 'missing'}")

    def _order_status(self, row, receipt, now, session):
        """Order state for settling. A known order merely not yet shown as terminal
        (PendingConfirmation) stays a hold across nights and holidays; it locks only
        if it is still not terminal while a LATER regular session is running. Real
        lookup failures keep the existing time-based lock."""
        try:
            status = self.broker.order_status(receipt)
        except PendingConfirmation:
            key = "pending:" + row["id"]
            seen = self.j.meta(key) or {"sessions": []}
            if session and not str(session).startswith("KRW:") and session != row["session"] \
                    and session not in seen["sessions"]:
                seen["sessions"].append(session)
                self.j.put_meta(key, seen)
            if seen["sessions"]:
                self._lock("order_never_terminal",
                           f"Order {row['id']} from session {row['session']} is still not shown as terminal "
                           f"during session {seen['sessions'][0]}", now, row["id"],
                           required="Check whether the order expired or was cancelled in the broker app, then `resolve --action`")
            raise
        except Stop:
            age = (now - datetime.fromisoformat(row["created"])).total_seconds()
            if age >= self.cfg.unknown_outcome_lock_minutes * 60:
                self._lock("order_state_unresolved", f"State of order {row['id']} cannot be established",
                           now, row["id"], required="Check the order in the broker app, then `resolve --action`")
            raise
        if self.j.meta("pending:" + row["id"]) is not None:
            self.j.db.execute("DELETE FROM metadata WHERE key=?", ("pending:" + row["id"],))
            self.j.commit()
        return status

    def _reconcile(self, snap, now, *, target=None, may_cancel=True, session=None):
        for row in self.j.with_status(PREPARED):
            if row["run_id"] == self.run_id:
                continue
            if (now - datetime.fromisoformat(row["created"])).total_seconds() < PREPARED_STALE_SECONDS:
                raise Stop("Another process may be about to send a request; waiting")
            # An earlier process stopped before its send-start record: never sent.
            with self.j.atomic():
                if self.j.transition(row["id"], PREPARED, NOT_SENT):
                    self.j.update(row["id"], NOT_SENT, result={"reason": "send never started before restart"})
                    if self.tax_plan is not None:
                        self.tax_plan.cancel_progress(row["id"], now, reason="send never started")
        self._replay_effects(now)
        self._release_orphans(now)
        pending = self.j.pending()
        pending.sort(key=lambda r: 0 if r["kind"] == "cancel" else 1)
        for row in pending:
            if row["status"] in (SUBMITTING, UNKNOWN):
                if self.j.meta("halt"):
                    # Under a lock (e.g. an ambiguous match) later data is never used
                    # to attribute it automatically; a person settles it with resolve.
                    raise Locked(self.j.meta("halt"))
                return self._resolve_unknown(row, now)
        for row in pending:
            payload, receipt = json.loads(row["payload"]), json.loads(row["receipt"])
            if row["kind"] == "fx_out":
                # USD->KRW for tax: wait until the KRW is visible, then move the
                # USD reserve to the KRW reserve exactly once.
                received = money(receipt.get("krw", "0"))
                if snap.krw + 1 < money(payload["krw_before"]) + received:
                    if self._persisting("reflect:" + row["id"], now, self.cfg.inconsistency_lock_minutes):
                        self._lock("fx_not_reflected", f"Reverse FX {row['id']} accepted but KRW never reflected it",
                                   now, row["id"], required="Compare the FX history with balances in the broker app")
                    return "waiting: reverse FX balance reflection"
                self._forget("reflect:" + row["id"])
                with self.j.atomic():          # status and reserve move commit together
                    self.j.update(row["id"], DONE, result={"balance_confirmed": True})
                    if self.tax_plan is not None:
                        self.tax_plan.finish_fx_out(row["id"], money(payload["usd"]), received, now)
                continue
            if row["kind"] == "fx":
                # Require balance reflection before anything else. Balances are
                # never used to guess a lost receipt; that is _resolve_unknown's job.
                cash_usd = snap.cash_usd if snap.cash_usd is not None else snap.usd
                expected = (money(receipt["usd_after"]) if "usd_after" in receipt
                            else money(payload.get("cash_usd_before", payload["usd_before"])) + money(receipt["usd"]))
                if cash_usd + D("0.001") < expected:
                    if self._persisting("reflect:" + row["id"], now, self.cfg.inconsistency_lock_minutes):
                        self._lock("fx_not_reflected", f"FX {row['id']} accepted but USD balance never reflected it",
                                   now, row["id"], required="Compare the FX history with balances in the broker app")
                    return "waiting: FX balance reflection"
                self._forget("reflect:" + row["id"])
                self.j.update(row["id"], DONE, result={"balance_confirmed": True})
                continue
            if row["kind"] == "cancel":
                state = self._order_status(row, receipt["original"], now, session)
                if state.state not in {"filled", "cancelled", "rejected"}:
                    return "waiting: cancellation confirmation"
                self.j.update(row["id"], DONE, result=state.raw)
                # The original accepted order remains pending until its fill and
                # holdings are reconciled on the NEXT tick. Never buy here.
                return "confirmed: cancellation"
            status = self._order_status(row, receipt, now, session)
            if status.state not in {"open", "filled", "cancelled", "rejected"}:
                raise Stop("Unknown normalized order status")
            filled = money(status.filled)
            if not D(0) <= filled <= money(payload["qty"]):
                raise Stop("Fill quantity exceeds recorded order quantity")
            if status.state == "filled" and filled != money(payload["qty"]):
                raise Stop("Filled status does not match recorded order quantity")
            recorded = money(row["filled"] or 0)
            if filled < recorded:
                # Cumulative fills never decrease: a lower figure is a stale or
                # out-of-order report. Keep the recorded value and re-read.
                if self._persisting("fill:" + row["id"], now, self.cfg.inconsistency_lock_minutes):
                    self._lock("fill_decreased", f"Broker keeps reporting fewer fills for {row['id']}",
                               now, row["id"], required="Check fills in the broker app")
                return "waiting: re-checking inconsistent fill report"
            self._forget("fill:" + row["id"])
            symbol = payload["symbol"]
            expected = money(payload["held_before"]) + filled * (1 if payload["side"] == "buy" else -1)
            if status.state in {"filled", "cancelled", "rejected"}:
                if abs(snap.positions.get(symbol, D(0)) - expected) > D("0.000001"):
                    if filled != recorded:
                        self.j.update(row["id"], row["status"], filled=str(filled))
                    if self._persisting("hold:" + row["id"], now, self.cfg.inconsistency_lock_minutes):
                        self._lock("holdings_not_reflected", f"Holdings never matched the final fills of {row['id']}",
                                   now, row["id"], required="Compare holdings and fills in the broker app")
                    return "waiting: holdings reflection"
                self._forget("hold:" + row["id"])
                with self.j.atomic():          # status and every derived book entry commit together
                    self.j.update(row["id"], DONE, filled=str(filled),
                                  result={"filled": str(filled), "state": status.state, "raw": status.raw})
                    self._order_effects(row["id"], payload, filled, status.state, now)
                if status.state == "rejected":
                    self._lock("order_rejected_after_acceptance", "Accepted order later rejected; review " + row["id"],
                               now, row["id"], required="Read the rejection reason in the broker app")
                # A fresh snapshot is required after terminal status; the snapshot
                # used in this tick could precede the terminal execution.
                return "confirmed: " + status.state
            live = CANCELLING if row["status"] == CANCELLING else (PARTIAL if filled > 0 else ACCEPTED)
            if live != row["status"] or filled != recorded:
                self.j.update(row["id"], live, filled=str(filled))
            if row["status"] == CANCELLING or any(r["kind"] == "cancel" for r in pending):
                return "waiting: cancellation"
            elapsed = (now - datetime.fromisoformat(row["created"])).total_seconds()
            # Compare logical targets (an older version's "QQQ" target is the
            # defensive side); a buy of a different security always conflicts.
            recorded_target = payload.get("target")
            conflicting = target is not None and recorded_target is not None and (
                logical_target(recorded_target, payload.get("strategy")) != target
                or (payload.get("side") == "buy" and payload.get("symbol") != target))
            if conflicting or elapsed >= self.cfg.order_timeout_seconds:
                if not may_cancel:
                    return "waiting: market permits cancellation"
                remaining = money(payload["qty"]) - filled
                if remaining <= 0:
                    return "waiting: terminal order state"
                cancel_payload = {"original": receipt, "qty": str(remaining),
                                  "reason": "target changed" if conflicting else "timeout"}
                result = self._submit("cancel", cancel_payload, now, row["session"],
                                      lambda: self.broker.cancel(receipt, remaining))
                if result == "submitted: cancel":
                    self.j.update(row["id"], CANCELLING, filled=str(filled))
                return result
            return "waiting: order fill"
        return None

    # ---- gates for NEW requests ----

    def _require_no_reservations(self, now):
        """Gate for NEW FX/orders only. Reservations are never cancelled here; a
        failed or incomplete lookup blocks exactly like an active reservation.
        Unproven coverage (no confirmed start date) blocks live and is recorded
        as unverified in observe/paper, where nothing is sent."""
        try:
            lookup = self.broker.active_reservations(now, self.cfg.reservation_lookup_start)
        except Stop as exc:
            raise Stop("New orders blocked: reservation lookup failed or incomplete (" + str(exc) + ")") from exc
        except Exception as exc:
            raise Stop("New orders blocked: reservation lookup failed (" + type(exc).__name__ + ")") from exc
        if not isinstance(lookup, ReservationLookup) or not isinstance(lookup.live, list):
            raise Stop("New orders blocked: reservation lookup returned an invalid result")
        if lookup.live:
            shown = ", ".join(f"{r.get('symbol')} {r.get('side')} {r.get('quantity')}" for r in lookup.live[:5])
            raise Stop(f"New orders blocked: {len(lookup.live)} active reservation order(s) ({shown}); "
                       "cancel or let them finish in the broker app. The engine never cancels reservations")
        if not lookup.complete:
            reason = "reservation coverage unverified: " + (lookup.reason or "unknown")
            if self.cfg.mode == "live":
                raise Stop("New orders blocked: " + reason)
            self.j.event("unverified", {"check": "reservations", "reason": reason,
                                        "searched_from": lookup.searched_from}, now)

    # ---- carry-over bookkeeping (execution exception, not the backtest's fill) ----

    def _unfinished(self, snap, target, now):
        if any(s in STRATEGY_SYMBOLS or s in self.cfg.migrate_symbols for s in symbols_to_sell(snap.positions, target)):
            return "switch sell incomplete"
        try:
            price = self.broker.quote(target, now).ask
        except Exception:
            return "unverified: no quote to size the remaining buy"
        if price <= 0:
            return "unverified: no quote to size the remaining buy"
        if buy_quantity(snap.usd, snap.buying_power, price, self.cfg) >= 1:
            return "buy not completed"
        if buy_quantity(snap.usd, snap.usd, price, self.cfg) >= 1:
            return "sale proceeds not yet orderable"
        return None

    def _note_carryover(self, snap, target, session, now):
        key = "carryover:" + session
        if self.j.meta(key) is not None:
            return None
        reason = self._unfinished(snap, target, now)
        record = {"session": session, "target": target, "carried_over": reason, "at": now.isoformat()}
        self.j.put_meta(key, record)
        self.j.put_meta("execution_stats", (self.j.meta("execution_stats", []) + [record])[-60:])
        if reason:
            self.j.event("carryover", record, now)
            return "carried over: " + reason
        return None

    # ---- switch intents and same-session recovery ----
    #
    # A switch intent exists while the confirmed target differs from the strategy
    # holdings. It records which holdings belong to the switch and the money their
    # sale actually produced, so a recovery after the open window can finish the
    # switch WITHOUT investing new deposits or other idle cash.

    def _budget(self, intent):
        # `to_tax`: proceeds later moved into the tax reserve (older intents lack it).
        return max(D(0), money(intent["proceeds"]) + money(intent["carried"]) - money(intent["spent"])
                   - money(intent.get("to_tax", "0")))

    def _save_intent(self, intent):
        intent["events"] = intent["events"][-50:]
        self.j.put_meta("switch_intent", intent)

    def _archive_intent(self, intent):
        history = self.j.meta("switch_history", []) + [{k: v for k, v in intent.items() if k != "events"}]
        self.j.put_meta("switch_history", history[-30:])

    def _track_switch(self, snap, target, signal_date, session, now):
        # Superseding the old intent (carrying its unspent money) and starting the
        # new one commit together, so a restart in between can never lose or
        # double the carried budget.
        with self.j.atomic():
            return self._track_switch_locked(snap, target, signal_date, session, now)

    def _track_switch_locked(self, snap, target, signal_date, session, now):
        intent = self.j.meta("switch_intent")
        if intent and intent["status"] in ("done", "superseded"):
            intent = None
        held = {s: q for s, q in snap.positions.items() if s in STRATEGY_SYMBOLS and s != target and q > 0}
        carried = D(0)
        if intent and intent["to"] != target:
            # The confirmed signal moved again before the switch finished: the old
            # buy is never executed as such. Unspent switch money carries over.
            carried = self._budget(intent)
            intent.update(status="superseded", superseded_at=now.isoformat(), superseded_by=target)
            self._save_intent(intent)
            self._archive_intent(intent)
            self.j.event("switch_superseded", {"id": intent["id"], "to": intent["to"], "new_target": target}, now)
            intent = None
        if intent is None:
            if not held and carried <= 0:
                return None
            intent = {"id": str(uuid4()), "to": target, "from": {s: str(q) for s, q in held.items()},
                      "signal_date": signal_date, "created_session": session, "created_at": now.isoformat(),
                      "status": "selling" if held else "buying", "proceeds": "0", "carried": str(carried),
                      "spent": "0", "counted": [], "events": [], "recovery_orders": 0}
            self._save_intent(intent)
            self.j.event("switch_started", {"id": intent["id"], "to": target, "from": sorted(held),
                                            "signal_date": signal_date}, now)
        elif not held and intent["status"] == "selling":
            intent["status"] = "buying"
            self._save_intent(intent)
        return intent

    def _switch_fill(self, row, payload, filled, state, now):
        intent = self.j.meta("switch_intent")
        if not intent or intent["status"] in ("done", "superseded") or row["id"] in intent["counted"]:
            return
        if payload.get("switch") and payload["switch"] != intent["id"]:
            return
        price, side, symbol = money(payload["price"]), payload["side"], payload["symbol"]
        if side == "sell" and symbol in intent["from"]:
            # Conservative: a sell fills at or above its limit; fee buffer deducted.
            value = filled * price * (1 - self.cfg.fee_buffer)
            for year in self._tax_years(now):
                if value <= 0:
                    break
                # The installment gap comes out of the switch proceeds; no separate tax sell.
                value -= self.tax_plan.earmark_proceeds(year, value, now)
            intent["proceeds"] = str(money(intent["proceeds"]) + value)
        elif side == "buy" and symbol == intent["to"]:
            # Conservative: a buy fills at or below its limit; fee buffer added.
            intent["spent"] = str(money(intent["spent"]) + filled * price * (1 + self.cfg.fee_buffer))
        else:
            return
        intent["counted"].append(row["id"])
        if payload.get("recovery") and state == "filled":
            intent.pop("last_recovery_at", None)   # a complete fill needs no retry spacing
        intent["events"].append({"at": now.isoformat(), "action": row["id"], "symbol": symbol, "side": side,
                                 "qty": payload["qty"], "filled": str(filled), "limit": payload["price"],
                                 "quoted_at": payload.get("quoted_at"), "state": state,
                                 "recovery": bool(payload.get("recovery"))})
        self._save_intent(intent)

    def _complete_switch(self, target, now, how):
        intent = self.j.meta("switch_intent")
        if intent and intent["to"] == target and intent["status"] not in ("done", "superseded"):
            intent.update(status="done", completed_at=now.isoformat(), completed_by=how)
            self._save_intent(intent)
            self._archive_intent(intent)
            self.j.event("switch_done", {"id": intent["id"], "to": target, "how": how}, now)

    def _send_order(self, *, symbol, side, qty, quote, price, snap, target, signal_date, session, now,
                    deadline, extra=None, prepared=None, abandoned=None):
        if self._past(deadline, now):
            return "idle: execution window closed during this run"
        check_side(quote, side)                  # e.g. sell-only: buys held, sells allowed
        payload = dict(symbol=symbol, side=side, qty=str(qty), price=str(price),
                       held_before=str(snap.positions.get(symbol, D(0))), target=target, strategy=STRATEGY_VERSION,
                       signal_date=signal_date, quoted_at=quote.at.isoformat(), **(extra or {}))
        sent_at = self._current(now)

        def order_guard():
            if self._past(deadline, now):
                raise Aborted("execution window closed before send")
            try:
                check_quote(quote, self._current(now), self.cfg)
            except Stop as exc:
                raise Aborted("quote no longer valid at send time: " + str(exc)) from exc
        return self._submit("order", payload, sent_at, session,
                            lambda: self.broker.order(symbol, side, qty, price, sent_at), order_guard,
                            prepared, abandoned)

    def _recover(self, snap, target, signal_date, session, now, deadline):
        """Same-session completion of a confirmed switch after the open window."""
        intent = self.j.meta("switch_intent")
        if not intent or intent["status"] in ("done", "superseded") or intent["to"] != target:
            return None
        count = sum(r["kind"] == "order" and r["session"] == session for r in self.j.rows())
        remaining = self.cfg.max_orders_per_session - count
        if remaining <= 0:
            raise Stop("Order count limit reached during intraday switch recovery; resumes next session")
        last = intent.get("last_recovery_at")
        if last:
            left = (deadline - self._current(now)).total_seconds() if deadline else 0
            # Spread the remaining attempts over the remaining time so the session
            # cap is never the thing that ends the recovery early.
            interval = max(self.cfg.recovery_retry_seconds, left / remaining if left > 0 else 0)
            if (now - datetime.fromisoformat(last)).total_seconds() < interval:
                return "waiting: intraday recovery pacing"
        wait = self._retry_wait(now)
        if wait:
            return wait
        self._require_no_reservations(now)
        sells = [s for s in sorted(intent["from"]) if snap.positions.get(s, D(0)) > 0]
        if self._past(deadline, now):
            return "idle: recovery window closed during this run"
        if sells:
            symbol, side = sells[0], "sell"
            qty = min(snap.positions[symbol], snap.sellable.get(symbol, D(0)), money(intent["from"][symbol]))
            if qty <= 0:
                return "waiting: shares become sellable"
            if qty != qty.to_integral_value():
                raise Stop("Fractional holdings require separate handling")
        else:
            symbol, side = target, "buy"
        self._tax_hold(side, symbol, target)
        quote = self.broker.quote(symbol, self._current(now))
        check_quote(quote, self._current(now), self.cfg)
        price = limit_price(quote, side, self.cfg)
        if side == "buy":
            budget = self._budget(intent)
            reserved_usd = self._reserved(now)[1]
            qty = buy_quantity(min(max(D(0), snap.usd - reserved_usd), budget),
                               max(D(0), snap.buying_power - reserved_usd), price, self.cfg)
            if qty < 1:
                if buy_quantity(budget, budget, price, self.cfg) >= 1:
                    return "waiting: sale proceeds not yet orderable"
                self._complete_switch(target, now, "intraday recovery")
                return "idle: switch complete"
        intent["last_recovery_at"] = now.isoformat()
        intent["recovery_orders"] = intent.get("recovery_orders", 0) + 1
        self._save_intent(intent)
        return self._send_order(symbol=symbol, side=side, qty=qty, quote=quote, price=price, snap=snap,
                                target=target, signal_date=signal_date, session=session, now=now,
                                deadline=deadline, extra={"recovery": True, "switch": intent["id"]})

    # ---- tax reserve (stage 3) ----

    def _tax_years(self, now):
        return self.tax_plan.active_years(now) if self.tax_plan is not None else []

    def _tax_year(self, now, need="installment_due"):
        """Oldest active tax year that still needs something (`need` > 0)."""
        for year in self._tax_years(now):
            if self.tax_plan.status(year, now)[need] > 0:
                return year
        return None

    def _tax_hold(self, side, symbol, target):
        """With the tax plan unavailable the reserves cannot be checked: only a
        sell of the risk symbol toward the defensive target may go out (it
        spends no money). New buys, FX and other sells are held."""
        if self.tax_unavailable is None:
            return
        if side == "sell" and symbol == RISK_SYMBOL and target == DEFENSIVE_SYMBOL:
            return
        raise Stop(f"New buys and FX held: {self.tax_unavailable}; tax reserves cannot be checked")

    def _reserved(self, now):
        if self.tax_plan is None:
            return D(0), D(0)
        return self.tax_plan.reserved(now)

    def _secure_cash(self, snap, now, locked):
        """Earmark unreserved cash for the current installment (no request sent).
        Cash outside an open switch's budget goes first; what is still due then
        comes out of the switch proceeds, and the switch budget shrinks by the
        same amount in the same transaction (so a restart counts it once)."""
        if locked:
            return
        for year in self._tax_years(now):         # oldest obligation first
            krw, usd = self._reserved(now)
            free_usd = max(D(0), snap.usd - self.cfg.usd_reserve - usd)
            intent = self.j.meta("switch_intent")
            open_switch = intent and intent["status"] not in ("done", "superseded")
            budget = self._budget(intent) if open_switch else D(0)
            with self.j.atomic():
                self.tax_plan.allocate_cash(year, max(D(0), snap.krw - self.cfg.krw_reserve - krw),
                                            max(D(0), free_usd - budget), now)
                if budget > 0 and free_usd > 0:
                    taken = self.tax_plan.earmark_proceeds(year, min(budget, free_usd), now)
                    if taken > 0:
                        intent["to_tax"] = str(money(intent.get("to_tax", "0")) + taken)
                        intent["events"].append({"at": now.isoformat(), "action": "tax_reserve", "side": "reserve",
                                                 "usd": str(taken), "year": year})
                        self._save_intent(intent)

    def _tax_sell(self, snap, target, signal_date, session, now, deadline):
        """A whole-share sell of the held strategy symbol for the installment gap,
        only when every independent budget check passes. Otherwise the plan is
        recorded and only tax work waits."""
        year = self._tax_year(now)
        if year is None:
            return None
        intent = self.j.meta("switch_intent")
        if intent and intent["status"] not in ("done", "superseded"):
            return None                     # switches first: never delayed by tax work
        held = min(snap.positions.get(target, D(0)), snap.sellable.get(target, D(0)))
        status = self.tax_plan.status(year, now)
        if status["installment_due"] <= 0 or held < 1:
            return None
        quote = self.broker.quote(target, self._current(now))
        check_quote(quote, self._current(now), self.cfg)
        price = limit_price(quote, "sell", self.cfg)
        plan, reasons, status = self.tax_plan.sell_plan(year, now, price, held, self.cfg.fee_buffer,
                                                         self.tax_blockers_fn(year))
        if plan is None:
            key = f"tax_plan_only:{year}:{now.astimezone(UTC).date()}"
            if self.j.meta(key) is None:
                self.j.put_meta(key, {"reasons": reasons, "due": str(status["installment_due"])})
                self.j.event("tax_plan_only", {"year": year, "reasons": reasons}, now)
            return None
        def prepared(action):
            self.tax_plan.start(year, action, "sell", plan["proceeds_krw"], plan["qty"] * price, now)

        def abandoned(action):
            self.tax_plan.cancel_progress(action, now, reason="not sent or refused")
        return self._send_order(symbol=target, side="sell", qty=plan["qty"], quote=quote, price=price, snap=snap,
                                target=target, signal_date=signal_date, session=session, now=now, deadline=deadline,
                                extra={"tax_year": year, "tax_basis": plan["basis"],
                                       "rounding_excess_krw": str(plan["rounding_excess_krw"])},
                                prepared=prepared, abandoned=abandoned)

    def _reverse_fx(self, snap, session, now, fx_deadline):
        """USD reserve -> KRW reserve, only once the reverse direction is verified
        on the real account. Until then the USD stays reserved and the need is
        recorded for notification."""
        year = self._tax_year(now, need="reserve_usd")
        if year is None:
            return None
        status = self.tax_plan.status(year, now)
        usd = min(status["reserve_usd"], snap.cash_usd if snap.cash_usd is not None else snap.usd)
        usd = usd.quantize(D("0.01"), rounding=ROUND_DOWN)
        if usd < D(10) or self.tax_plan.in_progress(year)["rows"]:
            return None
        if not self.reverse_fx_verified:
            self.j.put_meta("reverse_fx_needed", {"year": year, "usd": str(usd)})
            return None
        wait = self._retry_wait(now)
        if wait:
            return wait
        self._require_no_reservations(now)
        if self._past(fx_deadline, now):
            return "idle: FX window closed during this run"
        quote = self.broker.fx_quote_sell(self._current(now))
        check_fx_quote(quote, self._current(now), self.cfg)

        def guard():
            if self._past(fx_deadline, now):
                raise Aborted("FX window closed before send")
            try:
                check_fx_quote(quote, self._current(now), self.cfg)
            except Stop as exc:
                raise Aborted("FX quote no longer valid at send time: " + str(exc)) from exc
        def prepared(action):
            self.tax_plan.start(year, action, "fx_out", self.tax_plan.usd_krw(usd) or 0, usd, now)

        def abandoned(action):
            self.tax_plan.cancel_progress(action, now, reason="not sent or refused")
        return self._submit("fx_out", {"usd": str(usd), "krw_before": str(snap.krw), "rate": str(quote.ask),
                                       "quote_time": quote.server_time, "tax_year": year},
                            self._current(now), session, lambda: self.broker.exchange_to_krw(usd, quote), guard,
                            prepared, abandoned)

    def tick(self, *, target, signal_date, expected_signal_date, session, now,
             may_exchange=False, may_trade=False, may_manage_orders=None, market_block=None, window_over=False,
             signal_error=None, trade_deadline=None, fx_deadline=None, may_recover=False, recovery_deadline=None):
        if now.tzinfo is None:
            raise ValueError("timezone-aware now required")
        # A missing or stale signal blocks NEW requests only; requests already sent
        # are still settled below (status reads, fills, own-order cancels).
        signal_ok = target in STRATEGY_SYMBOLS and signal_date == expected_signal_date and not signal_error
        if not signal_ok:
            target = None
        locked = self.j.meta("halt")
        snap = self.broker.snapshot()
        # Quantities only (no amounts), for reconciling the tax ledger with holdings.
        self.j.put_meta("last_positions", {"at": now.isoformat(),
                                           "positions": {s: str(q) for s, q in snap.positions.items()}})
        # Read-only settling of requests this engine already sent runs first, even
        # under a lock, reservations or an unconfirmed market. Own-order cancels
        # are allowed only while unlocked and the market permits them.
        may_cancel = (may_trade if may_manage_orders is None else may_manage_orders) and not locked
        result = self._reconcile(snap, now, target=target, may_cancel=may_cancel, session=session)
        if result:
            return result
        if locked:
            raise Locked(locked)
        if not signal_ok:
            raise Stop("Missing/stale completed-close signal" + (": " + signal_error if signal_error else ""))
        if snap.external_open_orders:
            raise Stop("An untracked open order exists; use a dedicated account")
        unsupported = unsupported_holdings(snap.positions, self.cfg)
        if unsupported:
            raise Stop("Existing holdings left untouched: " + ", ".join(unsupported))
        self._track_switch(snap, target, signal_date, session, now)
        self._secure_cash(snap, now, locked)
        reserved_krw, reserved_usd = self._reserved(now)
        free_usd = max(D(0), snap.usd - reserved_usd)
        free_power = max(D(0), snap.buying_power - reserved_usd)
        note = self._note_carryover(snap, target, session, now) if window_over and not may_trade else None
        if market_block:
            raise Stop("New orders blocked: " + market_block)
        if may_exchange and self.tax_plan is not None:
            reverse = self._reverse_fx(snap, session, now, fx_deadline)
            if reverse:
                return reverse
        if may_exchange and self.tax_unavailable is None:
            # KRW reserved for tax is never converted (unknown reserves: no FX).
            krw = fx_amount(max(D(0), snap.krw - reserved_krw), fx_spent(self.j.rows(), session), self.cfg)
            if krw >= self.cfg.min_fx_krw:
                wait = self._retry_wait(now)
                if wait:
                    return wait
                self._require_no_reservations(now)
                if self._past(fx_deadline, now):
                    return "idle: FX window closed during this run"
                quote = self.broker.fx_quote(self._current(now))
                self.j.put_meta("last_usd_rate", {"rate": str(quote.midpoint), "at": now.isoformat()})
                check_fx_quote(quote, self._current(now), self.cfg)
                if self._past(fx_deadline, now):
                    return "idle: FX window closed during this run"
                cash = snap.cash_usd if snap.cash_usd is not None else snap.usd

                def fx_guard():
                    if self._past(fx_deadline, now):
                        raise Aborted("FX window closed before send")
                    try:
                        check_fx_quote(quote, self._current(now), self.cfg)
                    except Stop as exc:
                        raise Aborted("FX quote no longer valid at send time: " + str(exc)) from exc
                return self._submit("fx", {"krw": str(krw), "usd_before": str(snap.usd), "cash_usd_before": str(cash),
                                           "rate": str(quote.ask), "quote_time": quote.server_time},
                                    self._current(now), session, lambda: self.broker.exchange(krw, quote), fx_guard)
        if not may_trade:
            if may_recover and self.cfg.intraday_recovery:
                recovered = self._recover(snap, target, signal_date, session, now, recovery_deadline)
                if recovered:
                    return recovered
            return note or "idle: outside execution window"
        wait = self._retry_wait(now)
        if wait:
            return wait
        count = sum(r["kind"] == "order" and r["session"] == session for r in self.j.rows())
        if count >= self.cfg.max_orders_per_session:
            raise Stop("Order count limit reached; review liquidity/fills")
        self._require_no_reservations(now)
        to_sell = symbols_to_sell(snap.positions, target)
        if to_sell:
            symbol = to_sell[0]
            qty = min(snap.positions[symbol], snap.sellable.get(symbol, D(0)))
            if qty <= 0:
                return "waiting: shares become sellable"
            if qty != qty.to_integral_value():
                raise Stop("Fractional holdings require separate handling")
            side = "sell"
        else:
            symbol, side, qty = target, "buy", D(0)
            if self.tax_plan is not None and not self._past(trade_deadline, now):
                taxed = self._tax_sell(snap, target, signal_date, session, now, trade_deadline)
                if taxed:
                    return taxed
        self._tax_hold(side, symbol, target)
        if self._past(trade_deadline, now):
            return "idle: execution window closed during this run"
        quote = self.broker.quote(symbol, self._current(now))
        check_quote(quote, self._current(now), self.cfg)   # fresh time: reads may have been slow
        price = limit_price(quote, side, self.cfg)
        if side == "buy":
            # Money reserved for tax is never reinvested.
            qty = buy_quantity(free_usd, free_power, price, self.cfg)
            if qty < 1:
                aligned = snap.positions.get(target, D(0)) > 0
                self.j.put_meta("position_state", {"target": target, "aligned": aligned, "session": session})
                if buy_quantity(free_usd, free_usd, price, self.cfg) >= 1:
                    # Sale proceeds are in the estimate but not yet orderable: re-read
                    # on the next run; the switch can still finish intraday.
                    return "waiting: sale proceeds not yet orderable"
                self._complete_switch(target, now, "open window")
                return "idle: insufficient USD for one share"
        intent = self.j.meta("switch_intent")
        extra = ({"switch": intent["id"]} if intent and intent["status"] not in ("done", "superseded")
                 and intent["to"] == target else None)
        return self._send_order(symbol=symbol, side=side, qty=qty, quote=quote, price=price, snap=snap,
                                target=target, signal_date=signal_date, session=session, now=now,
                                deadline=trade_deadline, extra=extra)


def _ref(receipt):
    """Broker-side identifier for the journal column; never shown in reports."""
    if not isinstance(receipt, dict):
        return None
    if "date" in receipt and "number" in receipt:
        return f"{receipt['date']}:{receipt['number']}"
    if receipt.get("ref") is not None:
        return str(receipt["ref"])
    return str(receipt["number"]) if receipt.get("number") is not None else None
