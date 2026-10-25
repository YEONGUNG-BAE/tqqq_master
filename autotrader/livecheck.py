"""Small real-account check that keeps every existing holding.

A user-approved plan buys and sells ONE share at a time in a fixed order
(TQQQ buy, TQQQ sell, SGOV buy, SGOV sell, TQQQ buy, TQQQ sell) with a USD
budget the user names, to observe real quotes, orders, fills, cancels and
restarts. It is not a strategy switch: the QQQ signal, switch intents and tax
work are never touched, and no FX, reverse FX or tax sell is sent.

Safety model
- `plan` is read-only (GETs and token only) and records the account, the exact
  baseline of every other holding, routes, budget and the six steps.
- Requests are sent only by `execute`/`resume` with the plan id and an explicit
  live flag. The plan's authorization is checked when the intent is stored,
  right before the HTTP write (engine hook) and at the transport (POST
  allowlist): only TQQQ/SGOV, one share, the plan's next step, within the
  budget, and cancels of the plan's own orders.
- Every request goes through the general engine's path (intent before send,
  send-start record, unknown-outcome matching, cancel, back-off, locks) in the
  same account journal, tagged with the plan as owner. The general engine never
  touches owned rows and the check never touches the general engine's rows.
- While a check runs (or a stop leaves test shares), the general `run` sends
  nothing: the journal row and an account-level marker next to the account
  lock (seen from any state folder) block it.
- The check's money is derived from its own journal rows, so a fill is never
  counted twice and a restart loses nothing:
    available = budget - confirmed buy cost (fee buffer included)
                - open buy reservations + confirmed sell proceeds (fee buffer deducted)
  Account cash beyond that, deposits and distributions never raise it.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from uuid import uuid4

from . import oslock
from .core import DONE, PENDING, PREPARED, D, Locked, Stop, check_quote, limit_price, money
from .signal import DEFENSIVE_SYMBOL, RISK_SYMBOL

SYMBOLS = (RISK_SYMBOL, DEFENSIVE_SYMBOL)
STEPS = (("buy", RISK_SYMBOL), ("sell", RISK_SYMBOL), ("buy", DEFENSIVE_SYMBOL),
         ("sell", DEFENSIVE_SYMBOL), ("buy", RISK_SYMBOL), ("sell", RISK_SYMBOL))
# Statuses that keep the general run paused (requests or test shares may remain).
BLOCKING = ("running", "stopping")
SCHEMA = """
CREATE TABLE IF NOT EXISTS live_checks(
  plan_id TEXT PRIMARY KEY, created TEXT NOT NULL, status TEXT NOT NULL, plan TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}', updated TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS live_checks_one_blocking ON live_checks((1)) WHERE status IN ('running', 'stopping');
"""
UNVERIFIED = [
    "partial fills, cancel races and broker warnings beyond what this run happens to meet",
    "liquidity and spreads on a crash day",
    "official open price versus fill (no reliable official open data is read)",
    "actual commissions until the tax ledger sync reads them (a conservative fee buffer is kept)",
    "KRW->USD FX and USD->KRW reverse FX (not part of this check)",
]


def ensure_schema(db):
    """Create the table. Commits: call it at command start, never inside a
    transaction. Every other helper here only reads or writes rows."""
    db.executescript(SCHEMA)
    db.commit()


def _has_table(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_checks'").fetchone() is not None


def tag(plan_id):
    return "live_check:" + plan_id


def _row(journal, plan_id):
    if not _has_table(journal.db):
        return None
    row = journal.db.execute("SELECT plan_id, status, plan, evidence, created, updated FROM live_checks "
                             "WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        return None
    return {"plan_id": row[0], "status": row[1], "plan": json.loads(row[2]), "evidence": json.loads(row[3]),
            "created": row[4], "updated": row[5]}


def blocking(journal, marker=None):
    """The check that pauses the general run, or None. The marker covers a run
    started from another state folder on the same account. A marker whose plan
    this journal knows never sent anything (planned/superseded) or finished is
    stale and released."""
    if _has_table(journal.db):
        row = journal.db.execute(f"SELECT plan_id, status FROM live_checks WHERE status IN "
                                 f"({','.join('?' * len(BLOCKING))})", BLOCKING).fetchone()
        if row:
            return {"plan_id": row[0], "status": row[1], "source": "journal"}
    owner = _marker_owner(marker)
    if owner is None:
        return None
    if _stale(journal, owner):
        _remove_marker(marker)
        return None
    return {"plan_id": owner.get("plan_id"), "status": "marker", "source": "account marker"}


def _marker_owner(marker):
    if marker is None or not Path(marker).exists():
        return None
    try:
        return json.loads(Path(marker).read_text(encoding="utf-8"))
    except Exception:
        return {"plan_id": None}                       # unreadable: treated as owned by someone else


def _stale(journal, owner):
    row = _row(journal, owner.get("plan_id")) if owner.get("plan_id") else None
    return row is not None and row["status"] not in BLOCKING


def _remove_marker(marker):
    try:
        Path(marker).unlink()
    except FileNotFoundError:
        pass


def claim_marker(marker, plan_id, state_dir, journal):
    """Account-wide ownership, created exclusively (O_EXCL) next to the account
    lock so every state folder sees it. Re-claiming one's own marker is a no-op;
    another plan's marker refuses unless this journal shows it is stale."""
    if marker is None:
        return
    for _ in range(2):
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            owner = _marker_owner(marker)
            if owner.get("plan_id") == plan_id:
                return
            if _stale(journal, owner):
                _remove_marker(marker)
                continue
            raise Stop(f"Another live check owns this account (plan {owner.get('plan_id')}, state folder "
                       f"{owner.get('state_dir')}); finish or stop it there first. Nothing was sent")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"plan_id": plan_id, "state_dir": str(state_dir or "")}))
            f.flush()
            os.fsync(f.fileno())
        return
    raise Stop("Could not claim the account for the live check; nothing was sent")


def _clear_marker(marker, plan_id):
    owner = _marker_owner(marker)
    if owner is not None and owner.get("plan_id") in (plan_id, None):
        _remove_marker(marker)


# ---- ledger derived from the journal ----

def ledger(journal, plan):
    """Money, shares and step progress of one plan, from its journal rows only."""
    owner = tag(plan["plan_id"])
    fee = money(plan["fee_buffer"])
    budget = money(plan["budget_usd"])
    out = {"budget": budget, "spent": D(0), "reserved": D(0), "proceeds": D(0), "buy_total": D(0),
           "sell_total": D(0), "fee_reserve": D(0), "owned": {s: D(0) for s in SYMBOLS}, "done_steps": [],
           "open_steps": [], "reserved_by_step": {}, "orders": [], "receipts": [], "unresolved": False,
           "open_cancels": 0}
    for r in journal.rows():
        payload = json.loads(r["payload"] or "{}")
        if payload.get("owner") != owner:
            continue
        if r["kind"] == "cancel":
            if r["status"] in PENDING or r["status"] == PREPARED:
                out["unresolved"] = True
                out["open_cancels"] += 1
            continue
        if r["kind"] != "order":
            continue
        side, symbol, qty, price = payload["side"], payload["symbol"], money(payload["qty"]), money(payload["price"])
        filled = money(r["filled"] or 0)
        step = int(payload.get("step", 0))
        receipt = json.loads(r["receipt"] or "null")
        if receipt:
            out["receipts"].append(receipt)
        out["orders"].append({"id": r["id"], "step": step, "side": side, "symbol": symbol, "qty": str(qty),
                              "limit": str(price), "bid": payload.get("bid"), "ask": payload.get("ask"),
                              "status": r["status"], "filled": str(filled), "created": r["created"],
                              "updated": r["updated"], "receipt": receipt})
        if r["status"] == DONE:
            if side == "buy":
                out["spent"] += filled * price * (1 + fee)
                out["buy_total"] += filled * price
                out["owned"][symbol] += filled
            else:
                out["proceeds"] += filled * price * (1 - fee)
                out["sell_total"] += filled * price
                out["owned"][symbol] -= filled
            out["fee_reserve"] += filled * price * fee
            if filled == qty and step:
                out["done_steps"].append(step)
        elif r["status"] in PENDING or r["status"] == PREPARED:
            out["open_steps"].append(step)
            if r["status"] in ("submitting", "unknown"):
                out["unresolved"] = True
            if side == "buy":
                cost = qty * price * (1 + fee)
                out["reserved"] += cost
                out["reserved_by_step"][step] = out["reserved_by_step"].get(step, D(0)) + cost
    out["available"] = budget - out["spent"] - out["reserved"] + out["proceeds"]
    done = set(out["done_steps"])
    out["next_step"] = next((n for n in range(1, len(STEPS) + 1) if n not in done), None)
    return out


# ---- authorization ----

class Authorization:
    """What the plan allows; consulted before the intent row is stored and again
    right before the HTTP write. Anything else raises Stop."""

    def __init__(self, journal, plan_id, *, unwind=False):
        self.j, self.plan_id, self.unwind = journal, plan_id, unwind

    def permit(self, kind, payload):
        row = _row(self.j, self.plan_id)
        if row is None or payload.get("owner") != tag(self.plan_id):
            raise Stop("Request is not owned by this live-check plan")
        led = ledger(self.j, row["plan"])
        if kind == "cancel":
            original = payload.get("original") or {}
            if not any(r.get("number") == original.get("number") and r.get("date") == original.get("date")
                       for r in led["receipts"]):
                raise Stop("Cancel of an order this plan does not own")
            return
        if kind != "order":
            raise Stop(f"Live check never sends {kind} requests")
        side, symbol, qty = payload.get("side"), payload.get("symbol"), money(payload.get("qty", 0))
        step = int(payload.get("step", 0))
        if symbol not in SYMBOLS or qty != 1:
            raise Stop("Live check trades one TQQQ or SGOV share at a time only")
        if row["status"] == "stopping":
            if not (self.unwind and side == "sell" and led["owned"].get(symbol, 0) >= qty):
                raise Stop("Stop requested: only an explicit unwind may sell the plan's own shares")
            return
        if row["status"] != "running":
            raise Stop(f"Plan is {row['status']}; a new plan and an explicit execute are required")
        if step != led["next_step"] or STEPS[step - 1] != (side, symbol):
            raise Stop("Request does not match the plan's next step")
        if side == "sell" and led["owned"].get(symbol, 0) < qty:
            raise Stop("Live check sells only shares it bought")
        if side == "buy":
            cost = qty * money(payload["price"]) * (1 + money(row["plan"]["fee_buffer"]))
            if cost > led["available"] + led["reserved_by_step"].get(step, D(0)):
                raise Stop("Order exceeds the live-check budget")


def transport_filter(journal, plan_id, routes):
    """Transport allowlist for the live-check client: plan orders for TQQQ/SGOV
    (one share, the plan's exchange code) and cancels of its own orders."""
    def check(path, body):
        if path in ("/trading/v1/overseas/orders/buy", "/trading/v1/overseas/orders/sell"):
            symbol = body.get("ovrs_stck_symb_iscd")
            if symbol not in SYMBOLS or int(body.get("odqt", 0)) != 1:
                raise Stop("Transport refused: live check sends one TQQQ/SGOV share only")
            if body.get("ovrs_exch_vndr_idno") != routes[symbol]["order_exchange"]:
                raise Stop("Transport refused: exchange code differs from the plan")
            return
        if path == "/trading/v1/overseas/orders/cancel":
            row = _row(journal, plan_id)
            led = ledger(journal, row["plan"]) if row else {"receipts": []}
            if not any(str(r.get("number")) == str(body.get("orgl_oder_no")) and r.get("date") == body.get("oder_date")
                       for r in led["receipts"]):
                raise Stop("Transport refused: cancel of an order the plan does not own")
            return
        raise Stop("Transport refused: live check sends no " + path)
    return check


# ---- plan ----

def create_plan(journal, broker, settings, *, budget, now, tax_reserved_usd, state_dir=None):
    """Read-only checks; stores the plan only if every condition holds.
    Returns (plan or None, reasons)."""
    if not _has_table(journal.db):
        ensure_schema(journal.db)
    reasons = []
    try:
        budget = Decimal(str(budget))
    except Exception:
        budget = None
    if budget is None or not budget.is_finite() or budget <= 0 or budget != budget.quantize(D("0.01")):
        return None, ["budget must be a positive USD amount with at most two decimals (no default exists)"]
    if journal.meta("halt"):
        reasons.append("account is hard-locked: " + str(journal.meta("halt")))
    if journal.pending() or journal.with_status(PREPARED):
        reasons.append("an earlier request is unresolved or being prepared")
    if blocking(journal):
        reasons.append("another live check is running or stopping")
    intent = journal.meta("switch_intent")
    if intent and intent.get("status") not in ("done", "superseded"):
        reasons.append("a strategy switch is in progress")
    snap = broker.snapshot()
    if snap.external_open_orders:
        reasons.append("an open order not tracked by this program exists")
    held = [s for s in SYMBOLS if snap.positions.get(s, D(0)) > 0]
    if held:
        reasons.append("TQQQ/SGOV already held (" + ", ".join(held) + "): test and existing shares would mix")
    try:
        lookup = broker.active_reservations(now, settings.reservation_lookup_start)
        if lookup.live:
            reasons.append(f"{len(lookup.live)} active reservation order(s)")
        if not lookup.complete:
            reasons.append("reservation coverage unverified: set execution.reservation_lookup_start")
    except Exception as exc:
        reasons.append("reservation lookup failed: " + (str(exc) if isinstance(exc, Stop) else type(exc).__name__))
    routes, estimates = {}, {}
    fee = settings.fee_buffer
    for symbol in SYMBOLS:
        try:
            route = broker.route(symbol) if hasattr(broker, "route") else None
            routes[symbol] = ({"quote_market": route.quote_market, "order_exchange": route.order_exchange,
                               "source": route.source} if route else {"quote_market": "paper", "order_exchange": "paper",
                                                                       "source": "paper broker"})
        except Stop as exc:
            reasons.append(str(exc))
            continue
        try:
            quote = broker.quote(symbol, now)
            price = limit_price(quote, "buy", settings)
            estimates[symbol] = {"indicative_buy_limit": str(price), "cost_with_fee_buffer": str(price * (1 + fee)),
                                 "quote_time": quote.at.isoformat()}
        except Exception as exc:
            reasons.append(f"{symbol} price unavailable for the estimate: "
                           + (str(exc) if isinstance(exc, Stop) else type(exc).__name__))
    if estimates and len(estimates) == len(SYMBOLS):
        largest = max(money(e["cost_with_fee_buffer"]) for e in estimates.values())
        if budget < largest:
            reasons.append("budget below one share plus the fee buffer (no top-up or fractional shares)")
    cash = snap.cash_usd if snap.cash_usd is not None else snap.usd
    free = min(cash, snap.buying_power) - settings.usd_reserve - money(tax_reserved_usd)
    if budget > free:
        reasons.append("budget exceeds own USD after the fixed and tax reserves (no credit, loan, KRW or "
                       "other-currency margin is used)")
    if reasons:
        return None, reasons
    baseline = sorted(({"symbol": s, "qty": str(q)} for s, q in snap.positions.items() if q > 0 and s not in SYMBOLS),
                      key=lambda h: h["symbol"])
    plan_id = uuid4().hex[:12]
    est_trade = sum(money(e["cost_with_fee_buffer"]) for e in estimates.values()) / len(estimates)
    plan = {"plan_id": plan_id, "created_at": now.isoformat(),
            "account": (journal.meta("broker_key_fingerprint") or "paper")[:16], "state_dir": str(state_dir or ""),
            "budget_usd": str(budget), "fee_buffer": str(fee),
            "baseline": baseline, "protected_note": "these holdings are never ordered, cancelled or mapped to TQQQ/SGOV",
            "steps": [{"n": i + 1, "side": side, "symbol": symbol, "qty": "1"} for i, (side, symbol) in enumerate(STEPS)],
            "routes": routes, "estimates": estimates,
            "estimated_fee_buffer_total_usd": str((est_trade * fee * len(STEPS)).quantize(D("0.01"))),
            "conditions": ["confirmed US regular session and the open window (minutes 1-15) only",
                           "fresh quote, spread limit, per-side quantity unit and sell-only checks",
                           "no halt/VCM, full reservation lookup with no active reservation",
                           "every other holding equal to the baseline; TQQQ/SGOV equal to the plan's own shares",
                           "each step starts only after the previous order is terminal and reflected in holdings",
                           "no FX, reverse FX, tax sell or strategy switch"],
            "unverified": UNVERIFIED}
    with journal.atomic():
        journal.db.execute("UPDATE live_checks SET status='superseded', updated=? WHERE status='planned'",
                           (now.isoformat(),))
        journal.db.execute("INSERT INTO live_checks VALUES (?,?,?,?,?,?)",
                           (plan_id, now.isoformat(), "planned", json.dumps(plan), "{}", now.isoformat()))
        journal.event("live_check_planned", {"plan_id": plan_id}, now)
    return plan, []


def start(journal, plan_id, now, *, marker=None, state_dir=None):
    """planned -> running (one-shot). A finished plan never runs again."""
    row = _row(journal, plan_id)
    if row is None:
        raise Stop("Unknown live-check plan")
    if row["status"] != "planned":
        raise Stop(f"Plan is {row['status']}; execute accepts a fresh plan only (use resume for a running one)")
    account = (journal.meta("broker_key_fingerprint") or "paper")[:16]
    if account != row["plan"]["account"]:
        raise Stop("Plan belongs to a different account")
    # The account is claimed BEFORE the plan is marked running: a crash in
    # between leaves a marker of a plan that sent nothing, which is released.
    claimed = marker is not None and _marker_owner(marker) is None
    claim_marker(marker, plan_id, state_dir, journal)
    try:
        with journal.atomic():
            journal.db.execute("UPDATE live_checks SET status='running', updated=? WHERE plan_id=? AND status='planned'",
                               (now.isoformat(), plan_id))
            journal.event("live_check_started", {"plan_id": plan_id}, now)
    except Exception as exc:
        if claimed:
            _clear_marker(marker, plan_id)
        raise Stop("Another live check is running on this account") from exc


def start_checked(journal, broker, plan_id, now, *, marker=None, state_dir=None):
    """Re-check the plan's starting conditions on the live account, then start."""
    row = _row(journal, plan_id)
    if row is None:
        raise Stop("Unknown live-check plan")
    if journal.meta("halt") or journal.pending() or journal.with_status(PREPARED):
        raise Stop("Account is locked or a request is unresolved; the check does not start")
    intent = journal.meta("switch_intent")
    if intent and intent.get("status") not in ("done", "superseded"):
        raise Stop("A strategy switch is in progress; the check does not start")
    snap = broker.snapshot()
    if any(snap.positions.get(s, D(0)) > 0 for s in SYMBOLS):
        raise Stop("TQQQ/SGOV are held now; test and existing shares would mix")
    changes = baseline_changes(row["plan"], snap)
    if changes:
        raise Stop("Protected holdings differ from the plan baseline: " + ", ".join(c["symbol"] for c in changes))
    start(journal, plan_id, now, marker=marker, state_dir=state_dir)


def request_stop(journal, plan_id, now):
    row = _row(journal, plan_id)
    if row is None or row["status"] not in ("running", "stopping"):
        raise Stop("Only a running plan can be stopped")
    with journal.atomic():
        journal.db.execute("UPDATE live_checks SET status='stopping', updated=? WHERE plan_id=?", (now.isoformat(), plan_id))
        journal.event("live_check_stop_requested", {"plan_id": plan_id}, now)


def _finish(journal, plan_id, status, now, marker):
    with journal.atomic():
        journal.db.execute("UPDATE live_checks SET status=?, updated=? WHERE plan_id=? AND status IN ('running','stopping')",
                           (status, now.isoformat(), plan_id))
        journal.event("live_check_" + status, {"plan_id": plan_id}, now)
    _clear_marker(marker, plan_id)


# ---- one step ----

def baseline_changes(plan, snap):
    """Differences between the protected holdings and the account now. Prices and
    cash (dividends) are not compared; an integer-ratio change is flagged as a
    possible split for a person to check."""
    expected = {h["symbol"]: money(h["qty"]) for h in plan["baseline"]}
    actual = {s: q for s, q in snap.positions.items() if q > 0 and s not in SYMBOLS}
    changes = []
    for symbol in sorted(set(expected) | set(actual)):
        a, b = expected.get(symbol, D(0)), actual.get(symbol, D(0))
        if a != b:
            ratio = (b / a) if a else None
            split = ratio is not None and ratio > 0 and (ratio == ratio.to_integral_value()
                                                         or (1 / ratio) == (1 / ratio).to_integral_value())
            changes.append({"symbol": symbol, "baseline": str(a), "now": str(b),
                            "note": "possible split or corporate action: check, never adjusted automatically"
                            if split else "quantity changed (manual trade?)"})
    return changes


def tick(engine, journal, plan_id, now, *, may_trade, may_manage, deadline, session, tax_reserved_usd,
         marker=None, unwind=False):
    """Settle the plan's own requests, then at most one new one-share request."""
    row = _row(journal, plan_id)
    if row is None or row["status"] not in BLOCKING:
        raise Stop("Plan is not running")
    plan = row["plan"]
    # Account-wide ownership before any request, cancels included (re-claimed if
    # a crash lost the marker; another plan's marker refuses).
    claim_marker(marker, plan_id, plan.get("state_dir"), journal)
    engine.owner, engine.strict_reservations = tag(plan_id), True
    engine.live_check = Authorization(journal, plan_id, unwind=unwind)
    engine.cancel_open = row["status"] == "stopping"
    locked = journal.meta("halt")
    snap = engine.broker.snapshot()
    result = engine._reconcile(snap, now, target=None, may_cancel=may_manage and not locked, session=session)
    if result:
        return result
    if locked:
        raise Locked(locked)
    changes = baseline_changes(plan, snap)
    if changes:
        raise Stop("Protected holdings differ from the plan baseline; new check requests held: "
                   + ", ".join(c["symbol"] for c in changes))
    led = ledger(journal, plan)
    for symbol in SYMBOLS:
        if snap.positions.get(symbol, D(0)) != led["owned"][symbol]:
            key = f"livecheck_reflect:{plan_id}:{symbol}"
            if engine._persisting(key, now, engine.cfg.inconsistency_lock_minutes):
                engine._lock("live_check_holdings", f"{symbol} holdings never matched the live check's own fills",
                             now, required="Compare the broker app with `live-check status`, then `unlock`")
            return "waiting: holdings not yet reflected"
        engine._forget(f"livecheck_reflect:{plan_id}:{symbol}")
    if snap.external_open_orders:
        raise Stop("An open order not tracked by this program exists; live check sends nothing")
    if row["status"] == "stopping":
        if not any(led["owned"].values()):
            _finish(journal, plan_id, "stopped", now, marker)
            return "stopped: no test shares remain"
        if not unwind:
            return "stopping: test shares remain; resume with --unwind to sell only them"
        step_side, step_symbol = "sell", next(s for s in SYMBOLS if led["owned"][s] > 0)
        step = 0
    else:
        if led["next_step"] is None:
            if any(led["owned"].values()):
                raise Stop("All steps done but test shares remain; check the broker app")
            _finish(journal, plan_id, "completed", now, marker)
            return "complete"
        step = led["next_step"]
        step_side, step_symbol = STEPS[step - 1]
    if not may_trade:
        return "idle: outside the open window (resume during the next one)"
    wait = engine._retry_wait(now)
    if wait:
        return wait
    count = sum(r["kind"] == "order" and r["session"] == session for r in journal.rows())
    if count >= engine.cfg.max_orders_per_session:
        raise Stop("Order count limit reached; live check resumes next session")
    engine._require_no_reservations(now)
    quote = engine.broker.quote(step_symbol, engine._current(now))
    check_quote(quote, engine._current(now), engine.cfg)
    price = limit_price(quote, step_side, engine.cfg)
    evidence = None
    if step_side == "buy":
        if tax_reserved_usd is None:
            raise Stop("New check buys held: tax reserves cannot be checked (own orders are still reconciled)")
        cost = price * (1 + engine.cfg.fee_buffer)
        if cost > led["available"]:
            raise Stop("Live-check budget cannot cover one share with the fee buffer; held (no top-up)")
        cash = snap.cash_usd if snap.cash_usd is not None else snap.usd
        free = min(cash, snap.buying_power) - engine.cfg.usd_reserve - money(tax_reserved_usd)
        previous = STEPS[step - 2] if step >= 2 else None
        last_sale = (_last_proceeds(journal, plan, step - 1) if previous and previous[0] == "sell" else None)
        if cost > free:
            if last_sale is not None:
                return "waiting: sale proceeds not yet orderable"
            raise Stop("Own USD after reserves cannot cover one share; held")
        if last_sale is not None:
            without = snap.buying_power - last_sale
            evidence = {"step": step, "after_sell_step": step - 1, "buying_power_before": str(snap.buying_power),
                        "sale_proceeds_conservative": str(last_sale), "cost_with_fee_buffer": str(cost),
                        "classification": ("inconclusive: existing cash alone covered the buy" if without >= cost
                                           else "proceeds needed: confirmed once this buy fills")}
    else:
        if led["owned"][step_symbol] != 1:
            raise Stop("Live check sells exactly the one share it bought")
        if snap.sellable.get(step_symbol, D(0)) < 1:
            return "waiting: shares become sellable"

    def prepared(action):
        # Runs inside the engine's intent transaction: stored with the intent or not at all.
        if evidence is not None:
            _store_evidence(journal, plan_id, step, dict(evidence, action=action), now)
    return engine._send_order(symbol=step_symbol, side=step_side, qty=D(1), quote=quote, price=price, snap=snap,
                              target=None, signal_date=None, session=session, now=now, deadline=deadline,
                              extra={"owner": tag(plan_id), "step": step, "bid": str(quote.bid), "ask": str(quote.ask)},
                              prepared=prepared)


def _store_evidence(journal, plan_id, step, item, now):
    stored = _row(journal, plan_id)["evidence"]
    stored[str(step)] = item
    journal.db.execute("UPDATE live_checks SET evidence=?, updated=? WHERE plan_id=?",
                       (json.dumps(stored), now.isoformat(), plan_id))


def _last_proceeds(journal, plan, step):
    owner = tag(plan["plan_id"])
    fee = money(plan["fee_buffer"])
    for r in reversed(journal.rows()):
        payload = json.loads(r["payload"] or "{}")
        if payload.get("owner") == owner and r["kind"] == "order" and int(payload.get("step", 0)) == step \
                and r["status"] == DONE and money(r["filled"] or 0) > 0:
            return (money(r["filled"]) * money(payload["price"]) * (1 - fee)).quantize(D("0.01"), rounding=ROUND_DOWN)
    return None


# ---- report ----

def report(journal, plan_id):
    """Local report: never sent anywhere. Broker order numbers are left out."""
    row = _row(journal, plan_id)
    if row is None:
        raise Stop("Unknown live-check plan")
    plan = row["plan"]
    led = ledger(journal, plan)
    fills = {}
    if journal.db.execute("SELECT 1 FROM sqlite_master WHERE name='tax_fills'").fetchone():
        for key, price, fee in journal.db.execute("SELECT fill_key, price, fee FROM tax_fills"):
            fills[key] = (price, fee)
    orders = []
    for o in led["orders"]:
        receipt = o.pop("receipt") or {}
        actual = fills.get(f"{receipt.get('date')}:{receipt.get('number')}") if receipt else None
        quote_ref = o["ask"] if o["side"] == "buy" else o["bid"]
        o.update(actual_fill_price=actual[0] if actual else None, actual_fee=actual[1] if actual else None,
                 vs_quote=(str(money(actual[0]) - money(quote_ref)) if actual and quote_ref else None),
                 vs_official_open="not computed: no reliable official open price is read")
        o.pop("id", None)
        orders.append(o)
    evidence = row["evidence"]
    for step, item in evidence.items():
        status = journal.status(item.get("action")) if item.get("action") else None
        filled = next((o for o in led["orders"] if o["step"] == int(step) and o["status"] == DONE
                       and money(o["filled"]) > 0), None)
        if item["classification"].startswith("proceeds needed"):
            item["result"] = ("confirmed: the buy needed the sale proceeds and filled" if filled
                              else f"not yet confirmed (order {status})")
        else:
            item["result"] = "inconclusive: existing cash was enough; needs another check"
    blocking_now = row["status"] in BLOCKING
    open_orders = [o for o in led["orders"] if o["status"] in PENDING or o["status"] == PREPARED]
    stop = None
    if row["status"] == "stopping":
        if led["open_cancels"]:
            state = "cancel sent: awaiting confirmation"
        elif open_orders:
            state = "requested: own open order not yet cancelled"
        elif any(led["owned"].values()):
            state = "requested: no open order; test shares remain (resume --unwind sells only them)"
        else:
            state = "requested: no open order, no test shares (ends on the next resume)"
        stop = {"state": state, "requested_at": row["updated"]}
    elif row["status"] == "stopped":
        stop = {"state": "done: no open order, no test shares"}
    return {"stop": stop,"plan_id": plan_id, "status": row["status"], "created": row["created"], "updated": row["updated"],
            "next_step": led["next_step"] if row["status"] in BLOCKING else None,
            "steps": plan["steps"], "budget_usd": plan["budget_usd"],
            "money_usd": {"allocated": str(led["budget"]), "reserved_open_buys": str(led["reserved"]),
                          "spent_confirmed_buys": str(led["spent"]), "recovered_confirmed_sells": str(led["proceeds"]),
                          "available": str(led["available"]), "buy_total": str(led["buy_total"]),
                          "sell_total": str(led["sell_total"]), "fee_buffer_reserved": str(led["fee_reserve"]),
                          "note": "the budget is a funding limit, not a loss limit or gap-risk guarantee"},
            "orders": orders, "test_shares_remaining": {s: str(q) for s, q in led["owned"].items()},
            "unresolved_requests": led["unresolved"], "protected_baseline": plan["baseline"],
            "proceeds_reuse": evidence, "routes": plan["routes"],
            "general_run_paused": blocking_now,
            "follow_up": ("resume with --confirm-live --unwind during an open window to sell only the test shares"
                          if row["status"] == "stopping" and any(led["owned"].values()) else None),
            "verification_levels": {"simulated_tests": "passed (fake broker/HTTP)",
                                    "observed_on_real_account": [o["side"] + " " + o["symbol"] for o in orders
                                                                 if o["status"] == DONE and money(o["filled"]) > 0],
                                    "not_verified": plan["unverified"]}}


def write_report(state, plan_id, data):
    folder = Path(state) / "live_check"
    folder.mkdir(mode=0o700, exist_ok=True)
    oslock.private(folder, directory=True)
    path = folder / f"{plan_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    oslock.private(tmp)
    oslock.replace(tmp, path)
    return path

