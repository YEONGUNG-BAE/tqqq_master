"""Which account an installation trades, checked before any broker request.

What can be verified: the Meritz REST spec returns no account number (only the
WebSocket execution notice carries `acno`, which this program does not use),
so an account is identified by its API key. Two checks run before the first
request of every command that talks to the broker:
  1. config `account.key_fingerprint` (optional, first 16 hex of SHA-256 of
     MERITZ_CLIENT_ID; not a secret) must match the loaded key: catches a
     secrets.env from another installation.
  2. the journal's bound key fingerprint must match: catches a state folder
     that belongs to another key/account.
This proves "this installation uses the key it was set up with", not the
account number itself.

Binding: a new, empty journal binds to the key on first use. A journal that
already holds records but no binding is never adopted silently: `account link
--confirm` binds it after the user checked it. A journal created before this
module already stores the key fingerprint and keeps working. When the same
account's key is reissued, `account relink --confirm-same-account` records
the new key with an audit event (a user statement, not an API proof).
"""
from __future__ import annotations

import hashlib
import re

from .core import Stop

ALIAS_MAX = 20
_DIGITS = re.compile(r"\d{4,}")


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()


def validate_alias(alias):
    """A display name such as "아버지 계좌". Account numbers, e-mail addresses
    and long digit runs are refused so they never reach a notification."""
    if alias is None:
        return None
    alias = str(alias).strip()
    if not alias or len(alias) > ALIAS_MAX or _DIGITS.search(alias) or "@" in alias:
        raise ValueError("account.alias must be a short name without account numbers (e.g. 아버지 계좌)")
    return alias


def validate_config(cfg_account):
    cfg_account = dict(cfg_account or {})
    validate_alias(cfg_account.get("alias"))
    expected = cfg_account.get("key_fingerprint")
    if expected is not None and not re.fullmatch(r"[0-9a-f]{16}", str(expected)):
        raise ValueError("account.key_fingerprint must be the 16 hex characters shown by `account show`")
    return cfg_account


def _has_history(journal):
    db = journal.db
    for table in ("actions", "tax_fills", "live_checks"):
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists and db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            return True
    return False


def bind(journal, key_fp, cfg_account=None, now=None):
    """Check (and on a new journal, record) the binding. Raises Stop before
    any request when the installation and the key disagree."""
    expected = (cfg_account or {}).get("key_fingerprint")
    if expected and not key_fp.startswith(expected):
        raise Stop("The loaded API key differs from config account.key_fingerprint: this installation's secrets.env "
                   "belongs to another account. Nothing was sent")
    bound = journal.meta("broker_key_fingerprint")
    if bound and bound != key_fp:
        raise Stop("State belongs to a different API key; use the matching account state. If the SAME account's "
                   "key was reissued, run `account relink --confirm-same-account --note ...`. Nothing was sent")
    if not bound:
        if _has_history(journal):
            raise Stop("This journal has records but no account binding; check it belongs to this account, then run "
                       "`account link --confirm`. Nothing was sent")
        journal.put_meta("broker_key_fingerprint", key_fp)
        journal.put_meta("account_binding", {"key": key_fp[:16], "source": "first use of a new journal",
                                             "at": now.isoformat() if now else None})
    elif journal.meta("account_binding") is None:
        journal.put_meta("account_binding", {"key": key_fp[:16], "source": "key fingerprint already in the journal",
                                             "at": now.isoformat() if now else None})
    return journal.meta("account_binding")


def link(journal, key_fp, now):
    """Explicitly bind a journal that has records but no binding."""
    if journal.meta("broker_key_fingerprint"):
        raise Stop("The journal is already bound; use relink for a reissued key of the same account")
    with journal.atomic():
        journal.put_meta("broker_key_fingerprint", key_fp)
        journal.put_meta("account_binding", {"key": key_fp[:16], "source": "user confirmed (account link)",
                                             "at": now.isoformat()})
        journal.event("account_linked", {"key": key_fp[:16]}, now)


def relink(journal, key_fp, now, note):
    """Same account, reissued key: user-confirmed, audited, refused while a
    request is unresolved."""
    old = journal.meta("broker_key_fingerprint")
    if not old:
        raise Stop("The journal is not bound yet; use `account link --confirm`")
    if old == key_fp:
        return False
    if journal.pending():
        raise Stop("A request is unresolved; settle it before changing the key binding")
    if not note or not str(note).strip():
        raise Stop("relink needs --note describing how you confirmed it is the same account")
    with journal.atomic():
        journal.put_meta("broker_key_fingerprint", key_fp)
        journal.put_meta("account_binding", {"key": key_fp[:16], "source": "user confirmed key reissue (relink)",
                                             "at": now.isoformat(), "previous": old[:16]})
        journal.event("account_relinked", {"previous": old[:16], "key": key_fp[:16], "note": str(note)[:200]}, now)
    return True

