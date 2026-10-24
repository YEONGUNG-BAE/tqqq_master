"""Tax reserve plan: installments, separate money states, budget guards (stage 3).

Every amount for a tax year lives in exactly one state, stored separately:
  paid            paid to the tax office (user-confirmed)
  reserve_krw     KRW kept in the trading account for tax (never converted/invested)
  reserve_usd     USD kept in the account for tax (never reinvested)
  withdrawn       withdrawn for tax, payment not yet confirmed
  in progress     a running tax sell or reverse FX (reserved by action id)
  external        planned / secured money outside the account (user input)
Moves between states are single transactions with an audit row; nothing is
ever overwritten into one number.

  shortfall = max(0, tax - paid - reserve_krw - conservative KRW(reserve_usd)
                     - withdrawn - valid external)
The amount secured in a run is not the whole shortfall but the gap between the
installment plan's cumulative target and what is already secured.

Tax-motivated sells have budget checks independent of the ledger: the current
installment gap (plus at most one whole share, with the reason recorded) and a
separately stored yearly cap that only the user or verified broker tax data can
set. Without a cap, tax sells only produce observation plans.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from uuid import uuid4

D = Decimal
USD_HAIRCUT = D("0.03")      # conservative KRW value of USD held for tax
SCHEMA = """
CREATE TABLE IF NOT EXISTS tax_state(
  year INTEGER PRIMARY KEY, reserve_krw TEXT NOT NULL DEFAULT '0', reserve_usd TEXT NOT NULL DEFAULT '0',
  withdrawn_krw TEXT NOT NULL DEFAULT '0', paid_krw TEXT NOT NULL DEFAULT '0',
  sold_for_tax_krw TEXT NOT NULL DEFAULT '0', early INTEGER NOT NULL DEFAULT 0, released INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tax_inprogress(
  action_id TEXT PRIMARY KEY, year INTEGER NOT NULL, kind TEXT NOT NULL, krw_equiv TEXT NOT NULL,
  usd TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_external_funds(
  year INTEGER PRIMARY KEY, planned_krw TEXT NOT NULL, planned_date TEXT NOT NULL,
  secured_krw TEXT NOT NULL DEFAULT '0', updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_caps(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, amount_krw TEXT NOT NULL,
  basis TEXT NOT NULL, source TEXT NOT NULL, set_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_withdrawals(
  request_id TEXT PRIMARY KEY, year INTEGER NOT NULL, amount_krw TEXT NOT NULL, requested_date TEXT NOT NULL,
  status TEXT NOT NULL, matched_ref TEXT UNIQUE, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_moves(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, at TEXT NOT NULL, from_state TEXT NOT NULL,
  to_state TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL, ref TEXT);
"""
COLUMNS = {"reserve_krw", "reserve_usd", "withdrawn_krw", "paid_krw", "sold_for_tax_krw"}


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


class TaxPlan:
    """Reads and moves tax money states. Methods that change state do NOT commit
    unless `commit=True`; callers group them with their own bookkeeping."""

    def __init__(self, db, settings_raw, estimate_fn, rate_fn=None):
        self.db = db
        raw = settings_raw or {}
        self.deadline = raw.get("payment_deadline")                    # user setting, may be None
        self.deadline_verified = bool(raw.get("payment_deadline_verified", False))
        self.buffer_days = int(raw.get("settlement_buffer_days", 7))
        self.round_day = int(raw.get("installment_day", 15))
        self.estimate_fn = estimate_fn
        self.rate_fn = rate_fn or (lambda: None)
        ensure_schema(db)

    # ---- low-level state ----

    def _row(self, year):
        self.db.execute("INSERT OR IGNORE INTO tax_state(year) VALUES (?)", (int(year),))
        r = self.db.execute("SELECT reserve_krw, reserve_usd, withdrawn_krw, paid_krw, sold_for_tax_krw, early, released "
                            "FROM tax_state WHERE year=?", (int(year),)).fetchone()
        return {"reserve_krw": D(r[0]), "reserve_usd": D(r[1]), "withdrawn_krw": D(r[2]), "paid_krw": D(r[3]),
                "sold_for_tax_krw": D(r[4]), "early": bool(r[5]), "released": bool(r[6])}

    def _move(self, year, now, from_state, to_state, amount, currency, ref=None):
        """Move `amount` between two columns (or from/to 'outside'). Rejects
        moves that would make a state negative."""
        amount = D(amount)
        if amount <= 0:
            return D(0)
        row = self._row(year)
        if from_state in COLUMNS and row[from_state] < amount:
            raise ValueError(f"{from_state} has less than {amount}")
        if from_state in COLUMNS:
            self.db.execute(f"UPDATE tax_state SET {from_state}=? WHERE year=?", (str(row[from_state] - amount), int(year)))
        if to_state in COLUMNS:
            current = self._row(year)[to_state]
            self.db.execute(f"UPDATE tax_state SET {to_state}=? WHERE year=?", (str(current + amount), int(year)))
        self.db.execute("INSERT INTO tax_moves(year, at, from_state, to_state, amount, currency, ref) VALUES (?,?,?,?,?,?,?)",
                        (int(year), now.isoformat(), from_state, to_state, str(amount), currency, ref))
        return amount

    # ---- schedule ----

    @staticmethod
    def due_year(now):
        """The tax year being prepared: January-May prepare the previous year."""
        local = now.astimezone(_KST())
        return local.year - 1 if local.month <= 5 else None

    def rounds(self, year):
        """Installment dates: the 15th of January-April, then the final date that
        leaves `settlement_buffer_days` before the payment deadline. Without a
        configured deadline the final round is May 15 and marked unverified."""
        dates = [date(year + 1, m, self.round_day) for m in (1, 2, 3, 4)]
        final = date(year + 1, 5, self.round_day)
        if self.deadline:
            final = min(final, date.fromisoformat(self.deadline) - timedelta(days=self.buffer_days))
        dates = [d for d in dates if d < final] + [final]
        return dates

    def rounds_elapsed(self, year, now):
        today = now.astimezone(_KST()).date()
        return sum(d <= today for d in self.rounds(year)), len(self.rounds(year))

    # ---- external funds ----

    def set_external(self, year, planned_krw, planned_date, now):
        self.db.execute("INSERT INTO tax_external_funds(year, planned_krw, planned_date, secured_krw, updated) "
                        "VALUES (?,?,?,COALESCE((SELECT secured_krw FROM tax_external_funds WHERE year=?),'0'),?) "
                        "ON CONFLICT(year) DO UPDATE SET planned_krw=excluded.planned_krw, "
                        "planned_date=excluded.planned_date, updated=excluded.updated",
                        (int(year), str(D(planned_krw)), planned_date, int(year), now.isoformat()))

    def set_external_secured(self, year, secured_krw, now):
        if not self.db.execute("SELECT 1 FROM tax_external_funds WHERE year=?", (int(year),)).fetchone():
            self.set_external(year, secured_krw, now.astimezone(_KST()).date().isoformat(), now)
        self.db.execute("UPDATE tax_external_funds SET secured_krw=?, updated=? WHERE year=?",
                        (str(D(secured_krw)), now.isoformat(), int(year)))

    def external_deposited(self, year, amount, now):
        """External money moved INTO the trading account: it leaves the external
        state and becomes KRW reserve, so it is never counted twice."""
        row = self.db.execute("SELECT secured_krw FROM tax_external_funds WHERE year=?", (int(year),)).fetchone()
        secured = D(row[0]) if row else D(0)
        amount = min(D(amount), secured)
        self.db.execute("UPDATE tax_external_funds SET secured_krw=?, updated=? WHERE year=?",
                        (str(secured - amount), now.isoformat(), int(year)))
        return self._move(year, now, "external_secured", "reserve_krw", amount, "KRW", "external deposit")

    def external_valid(self, year, now):
        """Secured external money always counts. Planned money counts until its
        date passes; after that only what was confirmed secured."""
        row = self.db.execute("SELECT planned_krw, planned_date, secured_krw FROM tax_external_funds WHERE year=?",
                              (int(year),)).fetchone()
        if not row:
            return {"planned": D(0), "secured": D(0), "valid": D(0), "overdue": False, "date": None}
        planned, secured = D(row[0]), D(row[2])
        overdue = now.astimezone(_KST()).date() > date.fromisoformat(row[1]) and secured < planned
        valid = secured if overdue else max(planned, secured)
        return {"planned": planned, "secured": secured, "valid": valid, "overdue": overdue, "date": row[1]}

    # ---- caps ----

    def set_cap(self, year, amount, basis, source, now):
        if source not in ("user", "verified_broker_tax_report"):
            raise ValueError("a cap is set by the user or verified broker tax data only")
        self.db.execute("INSERT INTO tax_caps(year, amount_krw, basis, source, set_at) VALUES (?,?,?,?,?)",
                        (int(year), str(D(amount)), basis[:200], source, now.isoformat()))

    def cap(self, year):
        row = self.db.execute("SELECT amount_krw, source, set_at FROM tax_caps WHERE year=? ORDER BY seq DESC LIMIT 1",
                              (int(year),)).fetchone()
        return {"amount": D(row[0]), "source": row[1], "set_at": row[2]} if row else None

    # ---- valuation and status ----

    def usd_krw(self, usd):
        rate = self.rate_fn()
        if rate is None or not D(500) <= D(rate) <= D(3000):
            return None
        return (D(usd) * D(rate) * (1 - USD_HAIRCUT)).quantize(D(1), rounding=ROUND_DOWN)

    def in_progress(self, year):
        rows = list(self.db.execute("SELECT action_id, kind, krw_equiv, usd FROM tax_inprogress WHERE year=?",
                                    (int(year),)))
        return {"krw": sum((D(r[2]) for r in rows), D(0)), "rows": rows}

    def status(self, year, now):
        est = self.estimate_fn(year)
        tax_due = D(est["estimated_tax_krw"])
        row = self._row(year)
        external = self.external_valid(year, now)
        usd_krw = self.usd_krw(row["reserve_usd"])
        progress = self.in_progress(year)
        secured_account = (row["paid_krw"] + row["reserve_krw"] + (usd_krw or D(0)) + row["withdrawn_krw"]
                           + progress["krw"])
        shortfall = max(D(0), tax_due - row["paid_krw"] - row["reserve_krw"] - (usd_krw or D(0))
                        - row["withdrawn_krw"] - external["valid"])
        account_need = max(D(0), tax_due - external["valid"])
        elapsed, total = self.rounds_elapsed(year, now)
        if row["early"]:
            elapsed = total
        target = (account_need * elapsed / total).quantize(D(1), rounding=ROUND_CEILING) if total else D(0)
        due_now = max(D(0), target - secured_account)
        if row["released"]:
            due_now = D(0)
        rounds = self.rounds(year)
        upcoming = [d for d in rounds if d > now.astimezone(_KST()).date()]
        cap = self.cap(year)
        return {"year": year, "estimated_tax": tax_due, "estimate_complete": est["complete"],
                "estimate": est, "paid": row["paid_krw"], "reserve_krw": row["reserve_krw"],
                "reserve_usd": row["reserve_usd"], "reserve_usd_krw": usd_krw, "withdrawn": row["withdrawn_krw"],
                "in_progress_krw": progress["krw"], "external": external, "shortfall": shortfall,
                "account_need": account_need, "installment_target": target, "installment_due": due_now,
                "next_round": upcoming[0].isoformat() if upcoming else None,
                "final_round": rounds[-1].isoformat(), "deadline": self.deadline,
                "deadline_verified": self.deadline_verified, "cap": cap,
                "sold_for_tax": row["sold_for_tax_krw"], "released": row["released"]}

    # ---- securing money ----

    def reserved(self, now):
        """KRW and USD the engine must neither convert nor invest."""
        rows = list(self.db.execute("SELECT reserve_krw, reserve_usd FROM tax_state WHERE released=0"))
        return sum((D(r[0]) for r in rows), D(0)), sum((D(r[1]) for r in rows), D(0))

    def allocate_cash(self, year, free_krw, free_usd, now):
        """Earmark existing unreserved cash for the current installment gap: KRW
        (deposits included) first, then USD. Never more than the gap, so future
        installments are not pulled forward unless early securing was chosen."""
        status = self.status(year, now)
        due = status["installment_due"]
        moved = {"krw": D(0), "usd": D(0)}
        if due <= 0:
            return moved
        take_krw = min(max(D(0), D(free_krw)), due).quantize(D(1), rounding=ROUND_DOWN)
        if take_krw > 0:
            moved["krw"] = self._move(year, now, "free_cash", "reserve_krw", take_krw, "KRW", "installment")
            due -= take_krw
        if due > 0 and free_usd > 0:
            rate = self.rate_fn()
            if rate is not None and D(500) <= D(rate) <= D(3000):
                per_usd = D(rate) * (1 - USD_HAIRCUT)
                usd = min(D(free_usd), (due / per_usd)).quantize(D("0.01"), rounding=ROUND_CEILING)
                usd = min(usd, D(free_usd)).quantize(D("0.01"), rounding=ROUND_DOWN)
                if usd > 0:
                    moved["usd"] = self._move(year, now, "free_cash", "reserve_usd", usd, "USD", "installment")
        return moved

    def earmark_proceeds(self, year, proceeds_usd, now):
        """Same-day switch sale: keep the installment gap out of the proceeds
        instead of placing a separate tax sell."""
        status = self.status(year, now)
        rate = self.rate_fn()
        if status["installment_due"] <= 0 or rate is None or not D(500) <= D(rate) <= D(3000):
            return D(0)
        per_usd = D(rate) * (1 - USD_HAIRCUT)
        usd = min(D(proceeds_usd), status["installment_due"] / per_usd).quantize(D("0.01"), rounding=ROUND_DOWN)
        return self._move(year, now, "switch_proceeds", "reserve_usd", usd, "USD", "switch proceeds") if usd > 0 else D(0)

    def sell_plan(self, year, now, price, held, fee_buffer, ledger_blockers):
        """Whole shares to sell for the installment gap, or (None, reasons).
        Checks: ledger completeness, a stored yearly cap, the round gap with at
        most one share of rounding excess, and the cap including in-progress."""
        status = self.status(year, now)
        due = status["installment_due"]
        reasons = []
        if due <= 0:
            return None, ["nothing due this installment"], status
        if ledger_blockers:
            reasons.append("tax ledger incomplete or inconsistent")
        cap = status["cap"]
        if cap is None:
            reasons.append("no yearly tax-sell cap set (user or verified broker tax data)")
        rate = self.rate_fn()
        if rate is None or not D(500) <= D(rate) <= D(3000):
            reasons.append("no valid KRW/USD rate to size the sell")
        if reasons:
            return None, reasons, status
        per_share_krw = D(price) * (1 - D(fee_buffer)) * D(rate) * (1 - USD_HAIRCUT)
        # Whole shares: round up only while the excess stays within one share AND
        # within the gap itself (proceeds <= 2 x gap); otherwise round down; with
        # not even one share inside that bound, wait (the reason is recorded).
        up = (due / per_share_krw).quantize(D(1), rounding=ROUND_CEILING)
        down = (due / per_share_krw).quantize(D(1), rounding=ROUND_DOWN)
        qty = up if up * per_share_krw - due <= min(per_share_krw, due) else down
        qty = min(qty, D(held))
        if qty < 1:
            return None, ["rounding would exceed the installment gap by more than one share"], status
        proceeds_krw = qty * per_share_krw
        excess = proceeds_krw - due
        used = status["sold_for_tax"] + sum((D(r[2]) for r in self.in_progress(year)["rows"] if r[1] == "sell"), D(0))
        if used + proceeds_krw > cap["amount"]:
            return None, ["yearly tax-sell cap would be exceeded"], status
        if qty < 1:
            return None, ["no whole share available"], status
        return {"qty": qty, "proceeds_krw": proceeds_krw, "rounding_excess_krw": max(D(0), excess),
                "basis": f"installment gap {due} KRW; per share {per_share_krw.quantize(D(1))} KRW "
                         f"(bid x (1-fee buffer) x rate x (1-{USD_HAIRCUT}))"}, [], status

    def start(self, year, action_id, kind, krw_equiv, usd, now):
        self.db.execute("INSERT OR IGNORE INTO tax_inprogress VALUES (?,?,?,?,?,?)",
                        (action_id, int(year), kind, str(D(krw_equiv)), str(D(usd)), now.isoformat()))

    def finish_sell(self, action_id, filled_usd, now):
        """Terminal tax sell: the filled proceeds become USD reserve exactly once."""
        row = self.db.execute("SELECT year, krw_equiv FROM tax_inprogress WHERE action_id=? AND kind='sell'",
                              (action_id,)).fetchone()
        if not row:
            return D(0)
        year = row[0]
        self.db.execute("DELETE FROM tax_inprogress WHERE action_id=?", (action_id,))
        if D(filled_usd) > 0:
            self._move(year, now, "tax_sell", "reserve_usd", D(filled_usd).quantize(D("0.01"), rounding=ROUND_DOWN),
                       "USD", action_id)
            value = self.usd_krw(filled_usd) or D(0)
            current = self._row(year)["sold_for_tax_krw"]
            self.db.execute("UPDATE tax_state SET sold_for_tax_krw=? WHERE year=?", (str(current + value), year))
        return D(filled_usd)

    def finish_fx_out(self, action_id, usd_sold, krw_received, now):
        row = self.db.execute("SELECT year FROM tax_inprogress WHERE action_id=? AND kind='fx_out'",
                              (action_id,)).fetchone()
        if not row:
            return
        self.db.execute("DELETE FROM tax_inprogress WHERE action_id=?", (action_id,))
        self._move(row[0], now, "reserve_usd", "reserve_krw_conversion", D(usd_sold), "USD", action_id)
        self._move(row[0], now, "reverse_fx", "reserve_krw", D(krw_received).quantize(D(1), rounding=ROUND_DOWN),
                   "KRW", action_id)

    def cancel_progress(self, action_id):
        self.db.execute("DELETE FROM tax_inprogress WHERE action_id=?", (action_id,))

    # ---- withdrawals and payments ----

    def request_withdrawal(self, year, amount, requested_date, now):
        rid = str(uuid4())[:8]
        self.db.execute("INSERT INTO tax_withdrawals VALUES (?,?,?,?,?,NULL,?)",
                        (rid, int(year), str(D(amount)), requested_date, "requested", now.isoformat()))
        return rid

    def match_withdrawals(self, ledger_rows, now):
        """Link a cash-ledger withdrawal to a user-registered tax withdrawal by
        amount and date (same amount, within 7 days after the request date), only
        when exactly one row fits. Each ledger row moves money at most once.
        Unlinked withdrawals are reported, never assumed to be tax payments."""
        moved, unlinked = [], []
        unique = {}
        for r in ledger_rows:                     # the same ledger row read twice counts once
            if str(r.get("tr_type_code")) == "02" and str(r.get("cncl_yn", "N")) != "Y":
                unique.setdefault(str(r.get("tran_srno")), r)
        withdrawals = list(unique.values())
        used = {r[0] for r in self.db.execute("SELECT matched_ref FROM tax_withdrawals WHERE matched_ref IS NOT NULL")}
        for rid, year, amount, requested in self.db.execute(
                "SELECT request_id, year, amount_krw, requested_date FROM tax_withdrawals WHERE status='requested'").fetchall():
            start = date.fromisoformat(requested)
            fits = [w for w in withdrawals if str(w.get("tran_srno")) not in used
                    and D(str(w.get("tr_amt", "0")).replace(",", "")) == D(amount)
                    and start <= _ymd(w.get("tr_date")) <= start + timedelta(days=7)]
            if len(fits) != 1:
                continue
            ref = str(fits[0].get("tran_srno"))
            take = min(D(amount), self._row(year)["reserve_krw"])
            self._move(year, now, "reserve_krw", "withdrawn_krw", take, "KRW", ref)
            self.db.execute("UPDATE tax_withdrawals SET status='matched', matched_ref=? WHERE request_id=?", (ref, rid))
            used.add(ref)
            moved.append(rid)
        for w in withdrawals:
            if str(w.get("tran_srno")) not in used:
                unlinked.append(str(w.get("tran_srno")))
        return moved, unlinked

    def cancel_withdrawal(self, request_id, now):
        """Cancelled or re-deposited tax withdrawal: matched money returns to the
        KRW reserve; a pending request is simply cancelled."""
        row = self.db.execute("SELECT year, amount_krw, status FROM tax_withdrawals WHERE request_id=?",
                              (request_id,)).fetchone()
        if not row or row[2] == "cancelled":
            return False
        if row[2] == "matched":
            take = min(D(row[1]), self._row(row[0])["withdrawn_krw"])
            self._move(row[0], now, "withdrawn_krw", "reserve_krw", take, "KRW", "withdrawal cancelled " + request_id)
        self.db.execute("UPDATE tax_withdrawals SET status='cancelled' WHERE request_id=?", (request_id,))
        return True

    def record_payment(self, year, amount, now):
        """Payment confirmed by the user: first from money withdrawn for tax, the
        rest from outside the account. Never deducted twice."""
        amount = D(amount)
        from_withdrawn = min(amount, self._row(year)["withdrawn_krw"])
        self._move(year, now, "withdrawn_krw", "paid_krw", from_withdrawn, "KRW", "payment")
        self._move(year, now, "outside", "paid_krw", amount - from_withdrawn, "KRW", "payment (outside funds)")

    def correct_payment(self, year, new_total, now):
        """Correct the paid total. The difference goes to/from 'correction'; the
        reserve is recomputed but nothing is reinvested automatically."""
        current = self._row(year)["paid_krw"]
        new_total = D(new_total)
        if new_total > current:
            self._move(year, now, "correction", "paid_krw", new_total - current, "KRW", "payment correction")
        elif new_total < current:
            self._move(year, now, "paid_krw", "correction", current - new_total, "KRW", "payment correction")

    def release(self, year, now):
        """User-confirmed: remaining reserves return to general cash."""
        row = self._row(year)
        self._move(year, now, "reserve_krw", "released", row["reserve_krw"], "KRW", "user release")
        self._move(year, now, "reserve_usd", "released", row["reserve_usd"], "USD", "user release")
        self.db.execute("UPDATE tax_state SET released=1 WHERE year=?", (int(year),))

    def set_early(self, year, on):
        self._row(year)
        self.db.execute("UPDATE tax_state SET early=? WHERE year=?", (1 if on else 0, int(year)))


def _ymd(text):
    text = str(text)
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _KST():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Asia/Seoul")
