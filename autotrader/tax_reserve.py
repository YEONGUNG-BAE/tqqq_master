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
CREATE TABLE IF NOT EXISTS tax_external_deposits(
  request_id TEXT PRIMARY KEY, year INTEGER NOT NULL, amount_krw TEXT NOT NULL, expected_date TEXT NOT NULL,
  status TEXT NOT NULL, matched_ref TEXT UNIQUE, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_issues(
  issue_key TEXT PRIMARY KEY, kind TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_deadlines(
  year INTEGER PRIMARY KEY, deadline TEXT NOT NULL, set_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tax_moves(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, year INTEGER NOT NULL, at TEXT NOT NULL, from_state TEXT NOT NULL,
  to_state TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL, ref TEXT);
"""
COLUMNS = {"reserve_krw", "reserve_usd", "withdrawn_krw", "paid_krw", "sold_for_tax_krw"}


def ensure_schema(db):
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(tax_external_funds)")}
    if "deposited_krw" not in have:
        # Additive migration. Money an older version moved from external funds
        # into the account is known from its audit rows; payments it recorded as
        # 'outside' cannot be split between external funds and other money, so
        # they are flagged for the user instead of being guessed.
        db.execute("ALTER TABLE tax_external_funds ADD COLUMN deposited_krw TEXT NOT NULL DEFAULT '0'")
        db.execute("ALTER TABLE tax_external_funds ADD COLUMN paid_ext_krw TEXT NOT NULL DEFAULT '0'")
        for year, amount in db.execute("SELECT year, SUM(CAST(amount AS REAL)) FROM tax_moves WHERE "
                                       "from_state='external_secured' AND to_state='reserve_krw' GROUP BY year").fetchall():
            db.execute("UPDATE tax_external_funds SET deposited_krw=? WHERE year=?", (str(D(str(amount))), year))
        for (year,) in db.execute("SELECT DISTINCT year FROM tax_moves WHERE from_state='outside' AND to_state='paid_krw'"):
            db.execute("INSERT OR IGNORE INTO tax_issues VALUES (?,?,?,?)",
                       (f"payment_source:{year}", "payment_source_unknown",
                        f"{year}: payments recorded by an older version may have come from external funds; "
                        "re-enter them with /tax_paid_fix and a source", datetime.now().isoformat()))
    db.commit()


class TaxPlan:
    """Reads and moves tax money states. Methods that change state do NOT commit
    unless `commit=True`; callers group them with their own bookkeeping."""

    def __init__(self, db, settings_raw, estimate_fn, rate_fn=None):
        self.db = db
        raw = settings_raw or {}
        # Deadlines are per tax year. The legacy single `payment_deadline` applies
        # only to the tax year it belongs to (deadline year - 1), never to others.
        self.deadlines_cfg = raw.get("payment_deadlines") or {}
        self.legacy_deadline = raw.get("payment_deadline")
        self.legacy_verified = bool(raw.get("payment_deadline_verified", False))
        # Calendar days before the deadline for the final installment. This is a
        # conservative margin, NOT a guarantee of business days for settlement,
        # FX or withdrawal (those are unverified on the real account).
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

    def deadline_for(self, year):
        """{'date', 'verified', 'source'} for this tax year only, or None."""
        entry = self.deadlines_cfg.get(str(year))
        if entry:
            entry = entry if isinstance(entry, dict) else {"date": entry}
            return {"date": entry["date"], "verified": bool(entry.get("verified", False)), "source": "config"}
        if self.legacy_deadline and date.fromisoformat(self.legacy_deadline).year == int(year) + 1:
            return {"date": self.legacy_deadline, "verified": self.legacy_verified, "source": "config (legacy)"}
        row = self.db.execute("SELECT deadline FROM tax_deadlines WHERE year=?", (int(year),)).fetchone()
        if row:
            return {"date": row[0], "verified": False, "source": "user input"}
        return None

    def set_deadline(self, year, deadline, now):
        deadline = date.fromisoformat(str(deadline)).isoformat()
        self.db.execute("INSERT OR REPLACE INTO tax_deadlines VALUES (?,?,?)", (int(year), deadline, now.isoformat()))

    def active_years(self, now):
        """Tax years still being prepared or owed, oldest first. A year stays
        active while its estimate exceeds what is paid and it is not released:
        the previous year always during January-May, and any of the last two
        years once it has activity (money moved) or a deadline. Reaching June
        never drops an unpaid year or swaps it for the current one."""
        local = now.astimezone(_KST()).date()
        years = []
        for year in (local.year - 2, local.year - 1):
            row = self._row(year)
            if row["released"]:
                continue
            if D(self.estimate_fn(year)["estimated_tax_krw"]) - row["paid_krw"] <= 0:
                continue
            prep = year == local.year - 1 and local.month <= 5
            activity = (self.db.execute("SELECT 1 FROM tax_moves WHERE year=? LIMIT 1", (year,)).fetchone()
                        or self.deadline_for(year) is not None)
            if prep or activity:
                years.append(year)
        return years

    def rounds(self, year):
        """Installment dates: the 15th of January-April of the following year,
        then the final date: May 15, or `settlement_buffer_days` calendar days
        before THIS year's deadline if earlier. Without a deadline for this year
        the final round is May 15 and the deadline is shown as unset."""
        dates = [date(year + 1, m, self.round_day) for m in (1, 2, 3, 4)]
        final = date(year + 1, 5, self.round_day)
        deadline = self.deadline_for(year)
        if deadline:
            final = min(final, date.fromisoformat(deadline["date"]) - timedelta(days=self.buffer_days))
        dates = [d for d in dates if d < final] + [final]
        return dates

    def rounds_elapsed(self, year, now):
        today = now.astimezone(_KST()).date()
        return sum(d <= today for d in self.rounds(year)), len(self.rounds(year))

    # ---- external funds ----

    def _ext(self, year):
        row = self.db.execute("SELECT planned_krw, planned_date, secured_krw, deposited_krw, paid_ext_krw "
                              "FROM tax_external_funds WHERE year=?", (int(year),)).fetchone()
        if not row:
            return None
        return {"planned": D(row[0]), "date": row[1], "secured": D(row[2]), "deposited": D(row[3]),
                "paid_ext": D(row[4])}

    def set_external(self, year, planned_krw, planned_date, now):
        """planned_krw is the TOTAL external contribution planned for the year.
        Money already moved into the account or paid from it stays counted in
        deposited/paid_ext, so changing the plan never resurrects it."""
        if self._ext(year) is None:
            self.db.execute("INSERT INTO tax_external_funds(year, planned_krw, planned_date, secured_krw, updated, "
                            "deposited_krw, paid_ext_krw) VALUES (?,?,?,?,?,?,?)",
                            (int(year), str(D(planned_krw)), planned_date, "0", now.isoformat(), "0", "0"))
        else:
            self.db.execute("UPDATE tax_external_funds SET planned_krw=?, planned_date=?, updated=? WHERE year=?",
                            (str(D(planned_krw)), planned_date, now.isoformat(), int(year)))

    def set_external_secured(self, year, secured_krw, now):
        """secured_krw: external money secured and still unused outside the account."""
        ext = self._ext(year)
        if ext is None:
            self.set_external(year, secured_krw, now.astimezone(_KST()).date().isoformat(), now)
            ext = self._ext(year)
        self.db.execute("UPDATE tax_external_funds SET secured_krw=?, planned_krw=?, updated=? WHERE year=?",
                        (str(D(secured_krw)), str(max(ext["planned"], D(secured_krw) + ext["deposited"] + ext["paid_ext"])),
                         now.isoformat(), int(year)))

    def expect_external_deposit(self, year, amount, expected_date, now):
        """The user moves secured external money into the trading account. It
        becomes KRW reserve only when the cash ledger shows the deposit; until
        then the amount is held back from FX and automatic allocation."""
        ext = self._ext(year)
        if ext is None or ext["secured"] < D(amount):
            raise ValueError("external money must be secured (/tax_secured) before it is deposited")
        rid = str(uuid4())[:8]
        self.db.execute("INSERT INTO tax_external_deposits VALUES (?,?,?,?,?,NULL,?)",
                        (rid, int(year), str(D(amount)), expected_date, "expected", now.isoformat()))
        return rid

    def _pending_deposits(self, now):
        today = now.astimezone(_KST()).date()
        rows = self.db.execute("SELECT request_id, year, amount_krw, expected_date FROM tax_external_deposits "
                               "WHERE status='expected'").fetchall()
        return [r for r in rows if today <= date.fromisoformat(r[3]) + timedelta(days=7)]

    def match_deposits(self, ledger_rows, now):
        """Link a cash-ledger deposit to an expected external deposit: same amount,
        from one day before to seven days after the expected date, exactly one
        unused ledger row. Moves secured -> KRW reserve once, atomically."""
        unique = {}
        for r in ledger_rows:
            if str(r.get("tr_type_code")) == "01" and str(r.get("cncl_yn", "N")) != "Y":
                unique.setdefault(str(r.get("tran_srno")), r)
        used = {r[0] for r in self.db.execute("SELECT matched_ref FROM tax_external_deposits WHERE matched_ref IS NOT NULL")}
        moved = []
        for rid, year, amount, expected in self._pending_deposits(now):
            start = date.fromisoformat(expected)
            fits = [w for w in unique.values() if str(w.get("tran_srno")) not in used
                    and D(str(w.get("tr_amt", "0")).replace(",", "")) == D(amount)
                    and start - timedelta(days=1) <= _ymd(w.get("tr_date")) <= start + timedelta(days=7)]
            if len(fits) != 1:
                continue
            ref = str(fits[0].get("tran_srno"))
            ext = self._ext(year)
            take = min(D(amount), ext["secured"])
            self.db.execute("UPDATE tax_external_funds SET secured_krw=?, deposited_krw=?, updated=? WHERE year=?",
                            (str(ext["secured"] - take), str(ext["deposited"] + take), now.isoformat(), year))
            self._move(year, now, "external_secured", "reserve_krw", take, "KRW", ref)
            self.db.execute("UPDATE tax_external_deposits SET status='matched', matched_ref=? WHERE request_id=?", (ref, rid))
            used.add(ref)
            moved.append(rid)
        return moved

    def external_valid(self, year, now):
        """Usable external money still outside the account. Before the planned
        date: the larger of the plan's unused remainder and what is secured;
        after it: only what is secured."""
        ext = self._ext(year)
        if not ext:
            return {"planned": D(0), "secured": D(0), "valid": D(0), "overdue": False, "date": None,
                    "deposited": D(0), "paid_ext": D(0)}
        remaining = max(D(0), ext["planned"] - ext["deposited"] - ext["paid_ext"])
        overdue = now.astimezone(_KST()).date() > date.fromisoformat(ext["date"]) and ext["secured"] < remaining
        valid = ext["secured"] if overdue else max(remaining, ext["secured"])
        return {"planned": ext["planned"], "secured": ext["secured"], "valid": valid, "overdue": overdue,
                "date": ext["date"], "deposited": ext["deposited"], "paid_ext": ext["paid_ext"]}

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
                "final_round": rounds[-1].isoformat(),
                "deadline": (self.deadline_for(year) or {}).get("date"),
                "deadline_verified": bool((self.deadline_for(year) or {}).get("verified")), "cap": cap,
                "sold_for_tax": row["sold_for_tax_krw"], "released": row["released"]}

    # ---- securing money ----

    def reserved(self, now):
        """KRW and USD the engine must neither convert nor invest, including
        expected external deposits not yet confirmed in the cash ledger."""
        rows = list(self.db.execute("SELECT reserve_krw, reserve_usd FROM tax_state WHERE released=0"))
        hold = sum((D(r[2]) for r in self._pending_deposits(now)), D(0))
        return sum((D(r[0]) for r in rows), D(0)) + hold, sum((D(r[1]) for r in rows), D(0))

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

    def cancel_progress(self, action_id, now=None, reason="cancelled"):
        """Release a reservation whose request did not and cannot execute; the
        release is audited."""
        row = self.db.execute("SELECT year, krw_equiv FROM tax_inprogress WHERE action_id=?", (action_id,)).fetchone()
        if not row:
            return
        self.db.execute("DELETE FROM tax_inprogress WHERE action_id=?", (action_id,))
        self.db.execute("INSERT INTO tax_moves(year, at, from_state, to_state, amount, currency, ref) VALUES (?,?,?,?,?,?,?)",
                        (row[0], (now.isoformat() if now else datetime.now().isoformat()), "in_progress",
                         "released: " + reason, row[1], "KRW", action_id))

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

    def record_payment(self, year, amount, now, source=None):
        """Payment confirmed by the user, from exactly one named source:
        'withdrawn' (money withdrawn for tax), 'external' (secured external
        money) or 'other' (money outside the plan). Without a source it is
        inferred only when exactly one source can cover it; otherwise the user
        is asked. No state may go negative."""
        amount = D(amount)
        if amount <= 0:
            raise ValueError("payment must be positive")
        row = self._row(year)
        ext = self._ext(year) or {"secured": D(0), "paid_ext": D(0)}
        if source is None:
            fits = [name for name, room in (("withdrawn", row["withdrawn_krw"]), ("external", ext["secured"]))
                    if room >= amount]
            if len(fits) != 1:
                raise ValueError("payment source is ambiguous or uncovered; name it (출금, 외부 or 기타)")
            source = fits[0]
        if source == "withdrawn":
            if row["withdrawn_krw"] < amount:
                raise ValueError("more than the money withdrawn for tax")
            self._move(year, now, "withdrawn_krw", "paid_krw", amount, "KRW", "payment")
        elif source == "external":
            if ext["secured"] < amount:
                raise ValueError("more than the secured external money")
            self.db.execute("UPDATE tax_external_funds SET secured_krw=?, paid_ext_krw=?, updated=? WHERE year=?",
                            (str(ext["secured"] - amount), str(ext["paid_ext"] + amount), now.isoformat(), int(year)))
            self._move(year, now, "external_secured", "paid_krw", amount, "KRW", "payment (external)")
        elif source == "other":
            self._move(year, now, "outside", "paid_krw", amount, "KRW", "payment (other money)")
        else:
            raise ValueError("unknown payment source")
        return source

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
