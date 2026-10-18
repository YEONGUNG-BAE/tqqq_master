"""Deterministic execution engine; broker I/O is injected for offline testing."""
from __future__ import annotations

import json
import random
import sqlite3
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


def limit_price(quote, side, cfg):
    price = (quote.ask * (1 + cfg.limit_padding) if side == "buy"
             else quote.bid * (1 - cfg.limit_padding))
    return (price / quote.tick).quantize(D(1), rounding=ROUND_UP if side == "buy" else ROUND_DOWN) * quote.tick


STRATEGY_SYMBOLS = frozenset({"QQQ", "TQQQ"})


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
    # YYYYMMDD on/before the account opening date, from the broker app. Without
    # it reservation coverage is unverified and live new actions are blocked.
    reservation_lookup_start: str | None = None
    # Recovery policy (see WORK_STATUS.md for the reasoning behind each default).
    unknown_outcome_lock_minutes: int = 360   # unresolved send -> hard lock after this
    inconsistency_lock_minutes: int = 120     # holdings/fill contradictions -> hard lock
    max_consecutive_failures: int = 5         # refused/not-sent sends in a row -> hard lock
    retry_base_seconds: int = 15              # back-off after a refused/not-sent send
    retry_max_seconds: int = 120

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
        if (self.unknown_outcome_lock_minutes < 30 or self.inconsistency_lock_minutes < 10
                or self.max_consecutive_failures < 1 or self.retry_base_seconds < 1
                or self.retry_max_seconds < self.retry_base_seconds):
            raise ValueError("recovery limits must be positive and conservative")
        if not set(self.migrate_symbols).issubset({"QQQM", "BRK.B"}):
            raise ValueError("migration allowlist supports QQQM and BRK.B only")
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

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value, default=str)))
        self.db.commit()

    def event(self, category, value, now):
        self.db.execute("INSERT INTO events VALUES (?,?,?)",
                        (now.isoformat(), category, json.dumps(value, default=str)))
        self.db.commit()

    def create(self, kind, payload, now, session, status=SUBMITTING, **columns):
        action = str(uuid4())
        values = dict(columns, updated=now.isoformat(), inflight=1 if status in IN_FLIGHT else None)
        names = ["id", "kind", "status", "created", "session", "payload"] + list(values)
        try:
            self.db.execute(f"INSERT INTO actions({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                            (action, kind, status, now.isoformat(), session,
                             json.dumps(payload, default=str), *values.values()))
            self.db.commit()
        except sqlite3.IntegrityError as exc:
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
        self.db.commit()

    def transition(self, action, old, new):
        """Compare-and-set status change; False if another writer changed the row."""
        cursor = self.db.execute("UPDATE actions SET status=?, inflight=?, updated=? WHERE id=? AND status=?",
                                 (new, 1 if new in IN_FLIGHT else None, datetime.now(UTC).isoformat(), action, old))
        self.db.commit()
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
            self.j.db.commit()

    # ---- sending ----

    def _submit(self, kind, payload, now, session, call, guard=None):
        if self.cfg.mode == "observe":
            self.j.event("plan", dict(kind=kind, **payload), now)
            return "observe: " + kind
        if self.j.meta("halt"):
            raise Locked(self.j.meta("halt"))
        attempt = 1 + sum(r["kind"] == kind and r["session"] == session for r in self.j.rows())
        target = payload.get("target")
        action = self.j.create(kind, payload, now, session, status=PREPARED, attempt=attempt, run_id=self.run_id,
                               account=(self.j.meta("broker_key_fingerprint") or "paper")[:16],
                               target_version=f"{payload.get('signal_date')}:{target}" if target else None)

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
            self.j.update(action, NOT_SENT, result={"reason": "aborted before send: " + str(exc)})
            return "idle: " + str(exc)
        except NotSent as exc:
            self.j.update(action, NOT_SENT, result={"reason": str(exc)})
            self._register_failure(now, "not sent: " + str(exc))
            raise
        except Refused as exc:
            self.j.update(action, REFUSED, result={"code": exc.code, "reason": str(exc)})
            self._register_failure(now, "refused: " + str(exc))
            raise
        except Rejected as exc:
            self.j.update(action, REJECTED, result={"reason": str(exc)})
            set_lock(self.j, "broker_warning", "Broker rejected action " + action + ": " + str(exc), now,
                     action=action, required="Read the warning in the broker app; the bot never acknowledges warnings")
            raise
        except Exception as exc:
            if self.j.status(action) == PREPARED:
                # Failed before the send-start record (e.g. token issue): never transmitted.
                self.j.update(action, NOT_SENT, result={"error_type": type(exc).__name__})
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
            if r["kind"] == "fx" and receipt.get("ref") is not None:
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

    def _reconcile(self, snap, now, *, target=None, may_cancel=True):
        for row in self.j.with_status(PREPARED):
            if row["run_id"] == self.run_id:
                continue
            if (now - datetime.fromisoformat(row["created"])).total_seconds() < PREPARED_STALE_SECONDS:
                raise Stop("Another process may be about to send a request; waiting")
            # An earlier process stopped before its send-start record: never sent.
            if self.j.transition(row["id"], PREPARED, NOT_SENT):
                self.j.update(row["id"], NOT_SENT, result={"reason": "send never started before restart"})
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
                state = self.broker.order_status(receipt["original"])
                if state.state not in {"filled", "cancelled", "rejected"}:
                    return "waiting: cancellation confirmation"
                self.j.update(row["id"], DONE, result=state.raw)
                # The original accepted order remains pending until its fill and
                # holdings are reconciled on the NEXT tick. Never buy here.
                return "confirmed: cancellation"
            try:
                status = self.broker.order_status(receipt)
            except Stop:
                age = (now - datetime.fromisoformat(row["created"])).total_seconds()
                if age >= self.cfg.unknown_outcome_lock_minutes * 60:
                    self._lock("order_state_unresolved", f"State of order {row['id']} cannot be established",
                               now, row["id"], required="Check the order in the broker app, then `resolve --action`")
                raise
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
                self.j.update(row["id"], DONE, filled=str(filled),
                              result={"filled": str(filled), "state": status.state, "raw": status.raw})
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
            conflicting = target is not None and payload.get("target") not in (None, target)
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

    def tick(self, *, target, signal_date, expected_signal_date, session, now,
             may_exchange=False, may_trade=False, may_manage_orders=None, market_block=None, window_over=False,
             signal_error=None, trade_deadline=None, fx_deadline=None):
        if now.tzinfo is None:
            raise ValueError("timezone-aware now required")
        # A missing or stale signal blocks NEW requests only; requests already sent
        # are still settled below (status reads, fills, own-order cancels).
        signal_ok = target in {"QQQ", "TQQQ"} and signal_date == expected_signal_date and not signal_error
        if not signal_ok:
            target = None
        locked = self.j.meta("halt")
        snap = self.broker.snapshot()
        # Read-only settling of requests this engine already sent runs first, even
        # under a lock, reservations or an unconfirmed market. Own-order cancels
        # are allowed only while unlocked and the market permits them.
        may_cancel = (may_trade if may_manage_orders is None else may_manage_orders) and not locked
        result = self._reconcile(snap, now, target=target, may_cancel=may_cancel)
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
        note = self._note_carryover(snap, target, session, now) if window_over and not may_trade else None
        if market_block:
            raise Stop("New orders blocked: " + market_block)
        if may_exchange:
            krw = fx_amount(snap.krw, fx_spent(self.j.rows(), session), self.cfg)
            if krw >= self.cfg.min_fx_krw:
                wait = self._retry_wait(now)
                if wait:
                    return wait
                self._require_no_reservations(now)
                if self._past(fx_deadline, now):
                    return "idle: FX window closed during this run"
                quote = self.broker.fx_quote(self._current(now))
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
        if self._past(trade_deadline, now):
            return "idle: execution window closed during this run"
        quote = self.broker.quote(symbol, self._current(now))
        check_quote(quote, self._current(now), self.cfg)   # fresh time: reads may have been slow
        price = limit_price(quote, side, self.cfg)
        if side == "buy":
            qty = buy_quantity(snap.usd, snap.buying_power, price, self.cfg)
            if qty < 1:
                aligned = snap.positions.get(target, D(0)) > 0
                self.j.put_meta("position_state", {"target": target, "aligned": aligned, "session": session})
                if buy_quantity(snap.usd, snap.usd, price, self.cfg) >= 1:
                    # Sale proceeds are in the estimate but not yet orderable: re-read
                    # on the next run inside the window; carried over after it.
                    return "waiting: sale proceeds not yet orderable"
                return "idle: insufficient USD for one share"
        if self._past(trade_deadline, now):
            return "idle: execution window closed during this run"
        payload = dict(symbol=symbol, side=side, qty=str(qty), price=str(price),
                       held_before=str(snap.positions.get(symbol, D(0))), target=target,
                       signal_date=signal_date, quoted_at=quote.at.isoformat())
        sent_at = self._current(now)

        def order_guard():
            if self._past(trade_deadline, now):
                raise Aborted("execution window closed before send")
            try:
                check_quote(quote, self._current(now), self.cfg)
            except Stop as exc:
                raise Aborted("quote no longer valid at send time: " + str(exc)) from exc
        return self._submit("order", payload, sent_at, session,
                            lambda: self.broker.order(symbol, side, qty, price, sent_at), order_guard)


def _ref(receipt):
    """Broker-side identifier for the journal column; never shown in reports."""
    if not isinstance(receipt, dict):
        return None
    if "date" in receipt and "number" in receipt:
        return f"{receipt['date']}:{receipt['number']}"
    if receipt.get("ref") is not None:
        return str(receipt["ref"])
    return str(receipt["number"]) if receipt.get("number") is not None else None
