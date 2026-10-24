"""Tax ledger and estimate for overseas-stock capital gains (stage 2).

Not tax advice and not a filing tool. It keeps the records needed to prepare the
yearly payment and marks every figure that rests on missing or unverified data.

Rules applied (configurable; the basis text is stored next to each value):
- Cost method: FIFO by fill time, per symbol. Broker average prices are never
  used to compute FIFO gains.
- Timing and currency: a fill belongs to the year of its settlement date; USD
  amounts convert at the settlement-date KRW per USD rate. Settlement dates are
  computed as US T+1 business days (exchange calendar) and marked as computed.
- Fees: buy fees add to cost, sell fees reduce proceeds.
- Annual netting across all disposals (this account plus user-entered other
  brokers), one basic deduction, then the rate. Dividends are recorded apart
  (yearly totals only) and never mixed into capital gains.

Records are append-only by broker identity. The FIFO ledger is recomputed from
them deterministically, so re-reads, duplicates and restarts cannot double count.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN

D = Decimal
FX_SANE = (D(500), D(3000))          # KRW per USD outside this range = unit error
ANOMALY_ABS_KRW = D(500_000)         # broker P/L reference: flag only gross gaps ...
ANOMALY_REL = D("0.5")               # ... larger than both 500k KRW and 50 %

SCHEMA = """
CREATE TABLE IF NOT EXISTS tax_fills(
  fill_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL, qty TEXT NOT NULL,
  price TEXT NOT NULL, amount TEXT NOT NULL, fee TEXT, fee_source TEXT, filled_at TEXT NOT NULL,
  trade_date TEXT NOT NULL, settle_date TEXT, settle_source TEXT, fx TEXT, fx_source TEXT,
  source TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_dividends(
  row_key TEXT PRIMARY KEY, year INTEGER NOT NULL, paid_date TEXT NOT NULL, symbol TEXT,
  amount_usd TEXT NOT NULL, withheld TEXT, fx TEXT, fx_source TEXT, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_external(
  entry_id TEXT PRIMARY KEY, year INTEGER NOT NULL, broker TEXT NOT NULL, net_gain_krw TEXT NOT NULL,
  complete INTEGER NOT NULL, entered_at TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_corrections(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, fill_key TEXT NOT NULL, field TEXT NOT NULL, old TEXT, new TEXT,
  reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_issues(
  issue_key TEXT PRIMARY KEY, kind TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);
"""


@dataclass
class TaxSettings:
    basic_deduction_krw: Decimal = D(2_500_000)
    rate: Decimal = D("0.22")
    cost_method: str = "FIFO"
    basis: dict = field(default_factory=lambda: {
        "basic_deduction_krw": "user setting; overseas-stock capital gains basic deduction as configured",
        "rate": "user setting; 20% national + 2% local as configured",
        "cost_method": "FIFO by fill time; confirm against the broker's tax report",
        "fx": "KRW per USD base rate (deal_stnd_exrt) on the computed settlement date",
        "settlement": "computed US T+1 business day from the exchange calendar (not broker-reported)"})

    @classmethod
    def from_config(cls, raw):
        raw = raw or {}
        s = cls()
        if "basic_deduction_krw" in raw:
            s.basic_deduction_krw = D(str(raw["basic_deduction_krw"]))
        if "rate" in raw:
            s.rate = D(str(raw["rate"]))
        if s.basic_deduction_krw < 0 or not D(0) <= s.rate < 1:
            raise ValueError("tax settings out of range")
        s.basis.update(raw.get("basis", {}))
        return s


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


# US equity settlement cycles (SEC): T+3 from 1995-06-07, T+2 from 2017-09-05,
# T+1 from 2024-05-28 (trade date). Earlier trades: no verified rule here.
SETTLEMENT_CYCLES = (("2024-05-28", 1), ("2017-09-05", 2), ("1995-06-07", 3))
SETTLEMENT_BASIS = "computed from the exchange calendar and the SEC cycle for the trade date (not broker-reported)"


def settlement_sessions(first, last):
    """Session dates covering `first`..`last` plus margin, from the exchange
    calendar itself (independent of the signal's short calendar)."""
    import exchange_calendars as xc
    start = (date.fromisoformat(first[:10]) - timedelta(days=10)).isoformat()
    end = (date.fromisoformat(last[:10]) + timedelta(days=15)).isoformat()
    calendar = xc.get_calendar("XNYS", start=max(start, "1990-01-02"), end=end)
    return [d.date().isoformat() for d in calendar.sessions]


def settlement_date(trade_date, sessions):
    """(settlement date, source). None when the calendar does not cover the
    trade, the trade date is not a session, or no verified cycle applies:
    a far later session is never used as a stand-in."""
    if not sessions or trade_date < sessions[0] or trade_date > sessions[-1]:
        return None, "unverified: calendar does not cover the trade date"
    if trade_date not in set(sessions):
        return None, "unverified: trade date is not an exchange session"
    cycle = next((n for start, n in SETTLEMENT_CYCLES if trade_date >= start), None)
    if cycle is None:
        return None, "unverified: no verified settlement cycle for this date"
    later = [d for d in sessions if d > trade_date]
    if len(later) < cycle:
        return None, "unverified: calendar too short after the trade date"
    return later[cycle - 1], f"computed T+{cycle} ({SETTLEMENT_BASIS})"


def settlement_mismatches(db, now):
    """Read-only: (fill key, stored date, correct date or None, basis) for every
    stored fill whose settlement date differs from the era rules on a calendar
    covering every stored trade."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tax_fills'").fetchone() is None:
        return []
    rows = db.execute("SELECT fill_key, trade_date, settle_date, settle_source FROM tax_fills").fetchall()
    trades = [r[1] for r in rows if r[1] >= "1990-01-02"]
    sessions = settlement_sessions(min(trades), now.date().isoformat()) if trades else []
    found = []
    for key, trade, stored, source in rows:
        settle, basis = settlement_date(trade, sessions)
        if settle == stored and (settle is not None or str(source or "").startswith("unverified")):
            continue
        found.append((key, stored, settle, basis))
    return found


def repair_settlements(db, now):
    """Recompute stored settlement dates. A changed date clears that row's FX
    (it is fetched again for the right day); a date without a verified basis is
    removed (quarantined as unverified). Every change is audited. Idempotent."""
    found = settlement_mismatches(db, now)
    for key, stored, settle, basis in found:
        db.execute("UPDATE tax_fills SET settle_date=?, settle_source=?, fx=NULL, fx_source=NULL WHERE fill_key=?",
                   (settle, basis, key))
        db.execute("INSERT INTO tax_corrections(fill_key, field, old, new, reason, at) VALUES (?,?,?,?,?,?)",
                   (key, "settle_date", stored, settle, basis, now.isoformat()))
    db.commit()
    return len(found)


def _issue(db, key, kind, detail, at):
    db.execute("INSERT OR IGNORE INTO tax_issues VALUES (?,?,?,?)", (key, kind, detail, at))


def record_fill(db, fill, now):
    """Insert a fill once. The same key with different figures is never
    overwritten; it is recorded as a conflict that blocks tax-motivated sells."""
    row = db.execute("SELECT symbol, side, qty, price FROM tax_fills WHERE fill_key=?", (fill["fill_key"],)).fetchone()
    if row is not None:
        same = (row[0], row[1], D(row[2]), D(row[3])) == (fill["symbol"], fill["side"], D(fill["qty"]), D(fill["price"]))
        if not same:
            _issue(db, "conflict:" + fill["fill_key"], "duplicate_fill_conflict",
                   f"{fill['fill_key']} reported with different figures", now.isoformat())
        return False
    db.execute("INSERT INTO tax_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (fill["fill_key"], fill["symbol"], fill["side"], str(fill["qty"]), str(fill["price"]),
                str(fill["amount"]), None if fill.get("fee") is None else str(fill["fee"]), fill.get("fee_source"),
                fill["filled_at"], fill["trade_date"], fill.get("settle_date"), fill.get("settle_source"),
                None if fill.get("fx") is None else str(fill["fx"]), fill.get("fx_source"),
                fill.get("source", "broker_history"), now.isoformat()))
    return True


def set_fill_fx(db, fill_key, fx, source):
    db.execute("UPDATE tax_fills SET fx=?, fx_source=? WHERE fill_key=? AND fx IS NULL", (str(fx), source, fill_key))


def set_fill_fee(db, fill_key, fee, source):
    db.execute("UPDATE tax_fills SET fee=?, fee_source=? WHERE fill_key=? AND fee IS NULL", (str(fee), source, fill_key))


def record_dividend(db, row_key, paid_date, symbol, amount_usd, withheld, fx, fx_source, now):
    db.execute("INSERT OR IGNORE INTO tax_dividends VALUES (?,?,?,?,?,?,?,?,?)",
               (row_key, int(paid_date[:4]), paid_date, symbol, str(amount_usd),
                None if withheld is None else str(withheld), None if fx is None else str(fx), fx_source,
                now.isoformat()))


def record_external(db, entry_id, year, broker, net_gain_krw, complete, source, now):
    """Other-broker net gain for a year (user input). Replaces the same entry id."""
    db.execute("INSERT OR REPLACE INTO tax_external VALUES (?,?,?,?,?,?,?)",
               (entry_id, int(year), broker, str(D(str(net_gain_krw))), 1 if complete else 0, now.isoformat(), source))


def _fx_ok(fx):
    return fx is not None and FX_SANE[0] <= D(fx) <= FX_SANE[1]


def compute(db, settings, holdings=None):
    """Deterministic FIFO ledger from stored records. Returns per-year results,
    open lots and every reason a figure is incomplete."""
    fills = [dict(zip(("fill_key", "symbol", "side", "qty", "price", "amount", "fee", "fee_source", "filled_at",
                       "trade_date", "settle_date", "settle_source", "fx", "fx_source"), r))
             for r in db.execute("SELECT fill_key, symbol, side, qty, price, amount, fee, fee_source, filled_at, "
                                 "trade_date, settle_date, settle_source, fx, fx_source FROM tax_fills "
                                 "ORDER BY filled_at, fill_key")]
    issues = [dict(kind=r[0], detail=r[1]) for r in db.execute("SELECT kind, detail FROM tax_issues ORDER BY at")]
    lots, years = {}, {}

    def year_entry(y):
        return years.setdefault(y, {"gains_krw": D(0), "losses_krw": D(0), "disposals": 0, "incomplete": []})

    for f in fills:
        qty, amount = D(f["qty"]), D(f["amount"])
        fee = D(f["fee"]) if f["fee"] is not None else None
        fx = D(f["fx"]) if _fx_ok(f["fx"]) else None
        problems = []
        if fee is None:
            problems.append(f"fee missing for {f['fill_key']}")
        if f["fx"] is not None and fx is None:
            problems.append(f"FX rate out of range (unit error?) for {f['fill_key']}")
        elif fx is None:
            problems.append(f"settlement FX rate missing for {f['fill_key']}")
        if f["settle_date"] is None:
            problems.append(f"settlement date unknown for {f['fill_key']}")
        if fx is not None and str(f["fx_source"] or "").startswith("fallback"):
            problems.append(f"FX rate from an earlier posting for {f['fill_key']}")
        if f["side"] == "buy":
            cost_usd = amount + (fee or D(0))
            lots.setdefault(f["symbol"], []).append(
                {"fill_key": f["fill_key"], "qty": qty, "open": qty, "cost_usd": cost_usd,
                 "cost_krw": cost_usd * fx if fx is not None else None, "problems": problems,
                 "acquired": f["filled_at"]})
            continue
        year = int((f["settle_date"] or f["trade_date"])[:4])
        entry = year_entry(year)
        entry["disposals"] += 1
        proceeds_usd = amount - (fee or D(0))
        proceeds_krw = proceeds_usd * fx if fx is not None else None
        entry["incomplete"].extend(problems)
        remaining, cost_krw, cost_known = qty, D(0), True
        for lot in lots.get(f["symbol"], []):
            if remaining <= 0:
                break
            take = min(lot["open"], remaining)
            if take <= 0:
                continue
            if lot["cost_krw"] is None:
                cost_known = False
            else:
                cost_krw += lot["cost_krw"] * take / lot["qty"]
            entry["incomplete"].extend(lot["problems"])
            lot["open"] -= take
            remaining -= take
        if remaining > 0:
            cost_known = False
            entry["incomplete"].append(f"acquisition missing for {remaining} {f['symbol']} sold in {f['fill_key']} "
                                       "(bought before the ledger start or transferred in)")
        if proceeds_krw is None or not cost_known:
            continue
        gain = proceeds_krw - cost_krw
        if gain >= 0:
            entry["gains_krw"] += gain
        else:
            entry["losses_krw"] += -gain
    open_lots = {s: [l for l in ls if l["open"] > 0] for s, ls in lots.items()}
    open_problems = sorted({p for ls in open_lots.values() for l in ls for p in l["problems"]})
    unexplained = None
    if holdings is not None:
        # Both sides: a symbol that vanished from the account still has ledger
        # lots, and must be compared as 0 held.
        unexplained = {}
        for symbol in sorted(set(holdings) | {s for s, ls in open_lots.items() if ls}):
            held = D(str(holdings.get(symbol, 0)))
            ledger_qty = sum((l["open"] for l in open_lots.get(symbol, [])), D(0))
            if held != ledger_qty:
                unexplained[symbol] = str(held - ledger_qty)
    for y, entry in years.items():
        entry["net_krw"] = entry["gains_krw"] - entry["losses_krw"]
        entry["incomplete"] = sorted(set(entry["incomplete"]))
    return {"years": years, "open_lots": open_lots, "open_lot_problems": open_problems, "issues": issues,
            "unexplained_holdings": unexplained}


def dividends(db, year):
    rows = list(db.execute("SELECT amount_usd, withheld, fx FROM tax_dividends WHERE year=?", (int(year),)))
    usd = sum((D(r[0]) for r in rows), D(0))
    krw = sum((D(r[0]) * D(r[2]) for r in rows if _fx_ok(r[2])), D(0))
    return {"count": len(rows), "amount_usd": usd, "amount_krw_known": krw,
            "complete": all(_fx_ok(r[2]) for r in rows)}


SYNC_STALE = timedelta(days=3)
NOT_GIVEN = object()


def estimate(db, settings, year, ledger=None, holdings=NOT_GIVEN, sync=NOT_GIVEN, now=None):
    """Estimated tax for `year`: this account + entered other brokers, one deduction.

    holdings: {} = account checked and empty; None = not checked (incomplete).
    sync: the last ledger sync record; failed, stale or missing = incomplete.
    (NOT_GIVEN skips a check; the engine and /tax always pass both.) Matching
    holdings cannot prove completeness: a round trip missing from the ledger
    leaves holdings equal. Completeness of the fill import itself rests on the
    sync reading every page (it raises otherwise) over the configured range."""
    checked = holdings is not NOT_GIVEN
    ledger = ledger or compute(db, settings, holdings if checked else {})
    entry = ledger["years"].get(int(year), {"net_krw": D(0), "gains_krw": D(0), "losses_krw": D(0),
                                            "disposals": 0, "incomplete": []})
    external = list(db.execute("SELECT broker, net_gain_krw, complete FROM tax_external WHERE year=?", (int(year),)))
    external_net = sum((D(r[1]) for r in external), D(0))
    total = entry["net_krw"] + external_net
    taxable = max(D(0), total - settings.basic_deduction_krw)
    tax = (taxable * settings.rate).quantize(D(1), rounding=ROUND_DOWN)
    reasons = list(entry["incomplete"])
    reasons += [f"{i['kind']}: {i['detail']}" for i in ledger["issues"]]
    if checked and holdings is None:
        reasons.append("holdings not checked (no recent account snapshot)")
    elif checked and ledger["unexplained_holdings"]:
        reasons.append("holdings not explained by the ledger: " + ", ".join(sorted(ledger["unexplained_holdings"])))
    if sync is not NOT_GIVEN:
        if not sync:
            reasons.append("ledger sync never ran")
        elif not sync.get("ok"):
            reasons.append("ledger sync failed: " + str(sync.get("error", "unknown"))[:80])
        elif now is not None and sync.get("synced_at") and \
                now - datetime.fromisoformat(sync["synced_at"]) > SYNC_STALE:
            reasons.append("ledger sync is stale")
    if not external:
        reasons.append("no other-broker figures entered: this account only")
    elif not all(r[2] for r in external):
        reasons.append("other-broker figures marked incomplete")
    return {"year": int(year), "account_net_krw": entry["net_krw"], "external_net_krw": external_net,
            "total_net_krw": total, "deduction_krw": settings.basic_deduction_krw, "taxable_krw": taxable,
            "rate": settings.rate, "estimated_tax_krw": tax, "disposals": entry["disposals"],
            "complete": not reasons or reasons == ["no other-broker figures entered: this account only"],
            "this_account_only": not external, "reasons": reasons,
            # Data gaps in lots still held: they do not change this year's figure
            # but a tax-motivated sale would realize them.
            "open_lot_problems": ledger["open_lot_problems"],
            "dividends": dividends(db, year), "basis": settings.basis}


def compare_with_broker(our_net_krw, broker_net_krw):
    """Reference check only. The broker's realized P/L uses its own cost method
    and FX timing, so a small gap proves nothing and is never a validation. Only a
    gross gap (e.g. a wrong FX unit or a missing year of fills) is flagged."""
    ours, theirs = D(our_net_krw), D(broker_net_krw)
    gap = abs(ours - theirs)
    scale = max(abs(ours), abs(theirs))
    anomaly = gap > ANOMALY_ABS_KRW and (scale == 0 or gap / scale > ANOMALY_REL)
    return {"gap_krw": gap, "anomaly": anomaly, "validates_ledger": False,
            "tolerance": f"flag if gap > {ANOMALY_ABS_KRW} KRW and > {ANOMALY_REL:.0%} of the larger figure"}


def tax_sell_blockers(est, comparison=None):
    """Reasons a tax-motivated automatic sell must wait. Never blocks strategy switches."""
    blockers = [r for r in est["reasons"] if r != "no other-broker figures entered: this account only"]
    blockers += ["held lot: " + p for p in est.get("open_lot_problems", [])]
    if comparison and comparison["anomaly"]:
        blockers.append("broker realized P/L differs grossly from the ledger")
    return blockers


# ---- synchronisation from broker records (GET requests only) ----

def _num(value):
    text = str(value if value is not None else "0").replace(",", "").strip() or "0"
    return D(text)


def _symbol(row):
    return row.get("ovrs_stck_symb_iscd") or str(row.get("iscd", "")).rsplit(".", 1)[0]


def sync(db, broker, sessions, now, start="00000000", on_ledger=None):
    """Pull completed fills, fees, dividends and settlement FX rates into the ledger.

    Only fills from US sessions that have already ended are recorded, so an order
    still filling today can never be stored with a partial quantity. Fees are
    matched to fills only when exactly one cash-ledger row fits; FX rates are the
    base rate posted for the settlement date, falling back to an earlier posting
    (marked) when that date has none. Returns a short summary."""
    from zoneinfo import ZoneInfo
    ny, kst = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")
    today_ny = now.astimezone(ny).date().isoformat()
    last = now.astimezone(kst).strftime("%Y%m%d")
    added = 0
    history = list(broker.fill_history(start, last))
    trade_days = []
    for r in history:
        try:
            trade_days.append(datetime.strptime(str(r.get("cntg_ymdh")), "%Y-%m-%d %H:%M:%S")
                              .replace(tzinfo=kst).astimezone(ny).date().isoformat())
        except ValueError:
            pass
    stored = db.execute("SELECT MIN(trade_date) FROM tax_fills").fetchone()[0]
    first_day = min([d for d in trade_days + ([stored] if stored else []) if d >= "1990-01-02"] or [today_ny])
    sessions = settlement_sessions(first_day, today_ny)        # covers every trade being settled
    repair_settlements(db, now)
    for r in history:
        qty = _num(r.get("cntg_dqty"))
        if qty <= 0:
            continue
        stamp = str(r.get("cntg_ymdh") or "")
        try:
            filled_at = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=kst)
        except ValueError:
            _issue(db, f"time:{r['oder_date']}:{r['oder_no']}", "fill_time_missing",
                   f"fill time missing for order {r['oder_date']}:{r['oder_no']}", now.isoformat())
            continue
        trade_date = filled_at.astimezone(ny).date().isoformat()
        if trade_date >= today_ny:
            continue   # session not finished: figures may still change
        settle, settle_source = settlement_date(trade_date, sessions)
        side = {"1": "sell", "2": "buy"}.get(str(r.get("seln_byov_clcd")))
        if side is None or str(r.get("crcd", "USD")) != "USD":
            continue
        added += record_fill(db, {"fill_key": f"{r['oder_date']}:{int(r['oder_no'])}", "symbol": _symbol(r),
                                  "side": side, "qty": qty, "price": _num(r.get("cntg_crnc_prc")),
                                  "amount": _num(r.get("cntg_camt")), "filled_at": filled_at.isoformat(),
                                  "trade_date": trade_date, "settle_date": settle, "settle_source": settle_source,
                                  "source": "broker_history"}, now)
    ledger = list(broker.transactions(start, last))
    if on_ledger is not None:
        on_ledger(ledger)
    trades = [t for t in ledger if str(t.get("tr_type_code")) in ("05", "06") and str(t.get("cncl_yn", "N")) != "Y"]
    for fill_key, symbol, side, qty, trade_date, settle in db.execute(
            "SELECT fill_key, symbol, side, qty, trade_date, settle_date FROM tax_fills WHERE fee IS NULL").fetchall():
        window = {trade_date.replace("-", ""), (settle or trade_date).replace("-", "")}
        code = "05" if side == "buy" else "06"
        matches = [t for t in trades if str(t.get("tr_type_code")) == code and _symbol(t) == symbol
                   and _num(t.get("if_tr_qty", t.get("tr_qty"))) == D(qty) and str(t.get("tr_date")) in window]
        if len(matches) == 1:
            set_fill_fee(db, fill_key, _num(matches[0].get("ovsc_deal_fee")) + _num(matches[0].get("fee")),
                         "cash ledger (unique match)")
    for t in ledger:
        name = str(t.get("smcd_name", ""))
        if "배당" in name and _num(t.get("frcr_tr_amt")) > 0 and str(t.get("cncl_yn", "N")) != "Y":
            day = str(t["tr_date"])
            record_dividend(db, f"div:{t.get('tran_srno')}", f"{day[:4]}-{day[4:6]}-{day[6:]}", _symbol(t) or None,
                            _num(t["frcr_tr_amt"]), _num(t.get("intx")), None, None, now)
    rate_cache = {}

    def rate_for(day):
        if day not in rate_cache:
            found, source = None, None
            for back in range(0, 6):
                probe = (date.fromisoformat(day) - timedelta(days=back)).strftime("%Y%m%d")
                value = broker.base_rate(probe)
                if value is not None:
                    found = value
                    source = "base rate on settlement date" if back == 0 else f"fallback: posting {back} day(s) earlier"
                    break
            rate_cache[day] = (found, source)
        return rate_cache[day]
    today_iso = now.astimezone(kst).date().isoformat()
    for fill_key, settle in db.execute("SELECT fill_key, settle_date FROM tax_fills WHERE fx IS NULL").fetchall():
        if settle and settle <= today_iso:
            value, source = rate_for(settle)
            if value is not None:
                set_fill_fx(db, fill_key, value, source)
    for row_key, paid in db.execute("SELECT row_key, paid_date FROM tax_dividends WHERE fx IS NULL").fetchall():
        value, source = rate_for(paid)
        if value is not None:
            db.execute("UPDATE tax_dividends SET fx=?, fx_source=? WHERE row_key=?", (str(value), source, row_key))
    db.commit()
    return {"fills_added": added, "synced_at": now.isoformat()}
