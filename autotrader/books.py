"""Read-only diagnosis of books an older version may have left inconsistent.

Nothing here writes. The same detection functions drive the repairs, which run
on their own, idempotently and with an audit record:

  done_without_switch_effects  next `run` (engine reconcile): fills replayed into
                               the switch intent; a falsely completed intent reopens
  done_without_tax_effects     next `run`: the tax sell / reverse FX result is booked
  orphan_reservations          next `run`: in-progress tax money returns to its state
  external_funds_migration     next `run`/`tax`: both columns, deposited external money
                               rebuilt from its audit moves (the older version counted
                               it twice) and the payment-source flags, in one transaction;
                               a half-applied table is completed
  settlement_dates             next ledger sync (`tax-sync` or the daily housekeeping
                               sync): date recomputed, that row's FX cleared and fetched again

Never repaired automatically, because the evidence is not enough:
  open_requests                requests not yet terminal; those marked unresolved
                               (submitting/unknown) were maybe sent with no broker
                               match yet: the existing lock rules apply, settle with
                               `resolve` after checking the broker app
  needs_user                   e.g. payments an older version recorded as 'outside'
                               (external funds or other money?): /tax_paid_fix 0 <year>,
                               then /tax_paid ... 출금|외부|기타 <year>; the issue
                               closes, with a resolution row, once no money of
                               unknown source can remain
"""
from __future__ import annotations

from .core import PENDING, SUBMITTING, UNKNOWN, _table, missing_switch_effects, missing_tax_effects, orphan_reservations
from .tax import settlement_mismatches

UNRESOLVED = (SUBMITTING, UNKNOWN)


def _count(db, sql, *args):
    return db.execute(sql, args).fetchone()[0]


def _unmigrated_external(db):
    """Years in an external-funds table whose deposit-tracking migration is not
    marked complete (missing either column, or applied only in part)."""
    if not _table(db, "tax_external_funds"):
        return []
    columns = {r[1] for r in db.execute("PRAGMA table_info(tax_external_funds)")}
    done = _table(db, "tax_migrations") and db.execute(
        "SELECT 1 FROM tax_migrations WHERE name='external_funds_v2'").fetchone()
    if done and {"deposited_krw", "paid_ext_krw"} <= columns:
        return []
    return [y for (y,) in db.execute("SELECT year FROM tax_external_funds ORDER BY year")]


def diagnose(journal, now):
    db = journal.db
    held = set()
    if _table(db, "tax_inprogress"):
        held = {r[0] for r in db.execute("SELECT action_id FROM tax_inprogress")}
    requests = [{"id": r["id"], "kind": r["kind"], "status": r["status"], "unresolved": r["status"] in UNRESOLVED,
                 "tax_reservation": r["id"] in held} for r in journal.with_status(*PENDING)]
    needs_user = []
    if _table(db, "tax_issues"):
        resolved = ("WHERE issue_key NOT IN (SELECT issue_key FROM tax_issue_resolutions)"
                    if _table(db, "tax_issue_resolutions") else "")
        needs_user = [{"kind": k, "detail": d}
                      for k, d in db.execute(f"SELECT kind, detail FROM tax_issues {resolved} ORDER BY at")]
    events = {}
    for (category,) in db.execute("SELECT category FROM events WHERE category IN ('effects_replayed', 'switch_reopened')"):
        events[category] = events.get(category, 0) + 1
    recorded = {"effects_replayed": events.get("effects_replayed", 0),
                "switch_reopened": events.get("switch_reopened", 0),
                "reservations_released": _count(db, "SELECT COUNT(*) FROM tax_moves WHERE to_state LIKE 'released:%'")
                if _table(db, "tax_moves") else 0,
                "corrections": _count(db, "SELECT COUNT(*) FROM tax_corrections")
                if _table(db, "tax_corrections") else 0}
    auto = {"done_without_switch_effects": [r["id"] for r in missing_switch_effects(journal)],
            "done_without_tax_effects": [r["id"] for r in missing_tax_effects(journal)],
            "orphan_reservations": [a for a, _ in orphan_reservations(journal)],
            "external_funds_migration": _unmigrated_external(db),
            "settlement_dates": len(settlement_mismatches(db, now))}
    return {"auto_repair_pending": auto, "open_requests": requests, "needs_user": needs_user,
            "repairs_recorded": recorded,
            "clean": not any(auto.values()) and not any(r["unresolved"] for r in requests) and not needs_user}

