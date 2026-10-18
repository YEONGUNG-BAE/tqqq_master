"""Adapter written against the vendored official Meritz OpenAPI schema.

Account-specific behaviour remains unverified until run by the account owner.
Transport never retries FX/order/cancel POSTs, including expired-token errors.
"""
from __future__ import annotations

import fcntl
import json
import hashlib
import os
import random
import socket
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .core import (CLOCK_SKEW_SECONDS, D, FXQuote, NotSent, OrderStatus, Quote, Refused, RefusedUnconfirmed,
                   Rejected, ReservationLookup, Snapshot, Stop, UnknownOutcome, money)

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
BASE = "https://openapi.imeritz.com:9443"
# No documented maximum reservation period, query-range limit or account-opening
# date API. Without a configured start the look-back is a best effort only.
RESERVATION_LOOKBACK_DAYS = 365
RESERVATION_CHUNK_DAYS = 365   # each request covers at most this many acceptance days
RESERVATION_FIELDS = ("rsev_acpt_date", "seln_byov_clcd", "odqt", "rsev_cncl_yn")
# Documented: any order/cancel response other than 0001 means "not accepted".
ORDER_PATHS = ("/trading/v1/overseas/orders/buy", "/trading/v1/overseas/orders/sell",
               "/trading/v1/overseas/orders/cancel")
TOKEN_INVALID = {"EGW00121", "EGW00123"}   # EGW00121 documented as expired/invalid token
RATE_LIMITED = "EGW00200"                  # documented: not a failure, retry shortly
READ_ATTEMPTS = 3                          # first try + 2 retries per read
READ_BACKOFF_SECONDS = 0.5                 # 0.5s, 1s (x jitter 1.0-1.25)


class AuthError(Stop):
    """Token issue failed. `explicit` = the server answered without a token
    (credentials rejected/revoked), as opposed to a transport failure."""

    def __init__(self, message, explicit):
        super().__init__(message)
        self.explicit = explicit


DEBT_FIELDS = ("crdt_loan_amt", "crdt_stln_amt", "cash_unam", "cash_unam_ovfe", "nnrd_loan_amt")


class Client:
    def __init__(self, state, *, allow_mutations=False):
        self.state = Path(state)
        self.allow_mutations = allow_mutations
        self.key = os.environ.get("MERITZ_CLIENT_ID", "")
        self.secret = os.environ.get("MERITZ_CLIENT_SECRET", "")
        self.mac = os.environ.get("MERITZ_MAC_ADDRESS", f"{uuid.getnode():012X}").upper()
        if not self.key or not self.secret or len(self.mac) != 12 or any(c not in "0123456789ABCDEF" for c in self.mac):
            raise Stop("Configure MERITZ_CLIENT_ID, MERITZ_CLIENT_SECRET and a 12-digit hex MAC locally")
        self.token_file = self.state / "token.json"
        self.key_fingerprint = hashlib.sha256(self.key.encode()).hexdigest()
        self._next_call = 0.0
        self.on_send = None          # engine's send-start checkpoint, set per request
        self.sleep = time.sleep      # injectable for tests: no real waiting
        self.rng = random.Random()
        self.token_issued = False

    def _http(self, method, path, query=None, body=None, *, token=None, form=False):
        # Conservative call pacing; no busy looping and no speculative mutation retries.
        delay = self._next_call - time.monotonic()
        if delay > 0:
            self.sleep(delay)
        self._next_call = time.monotonic() + 0.3
        url = BASE + path + ("?" + urlencode(query) if query else "")
        headers = {"mac_address": self.mac, "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            data = (urlencode(body) if form else json.dumps(body)).encode("utf-8")
        req = Request(url, data=data, headers=headers, method=method)
        mutation = method == "POST" and path != "/oauth2/token"
        try:
            with urlopen(req, timeout=15) as response:
                raw = response.read(2_000_000)
        except HTTPError as exc:
            # Meritz returns application errors as HTTP 500 JSON.
            raw = exc.read(2_000_000)
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if mutation and isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
                # TCP refused or DNS failed: no request bytes reached the broker.
                raise NotSent("Connection refused before the request was sent") from exc
            if mutation:
                raise UnknownOutcome("No reliable response to mutating request") from exc
            raise Stop("Broker connection failed: " + type(exc).__name__) from exc
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            if mutation:
                raise UnknownOutcome("Non-JSON response to mutating request") from exc
            raise Stop("Broker returned non-JSON response") from exc
        if not isinstance(result, dict):
            if mutation:
                raise UnknownOutcome("Unexpected envelope for mutating request")
            raise Stop("Unexpected API envelope")
        return result

    def token(self):
        """Cached client_credentials token. Issue is serialized per state folder
        with an OS lock so concurrent processes never issue in parallel. There is
        no refresh-token grant in the spec; a new token is simply issued."""
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / "token.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                cached = json.loads(self.token_file.read_text())
                if cached.get("key_fingerprint") == self.key_fingerprint and cached["expires"] > time.time() + 120:
                    return cached["token"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            response = self._http("POST", "/oauth2/token", body=dict(
                grant_type="client_credentials", client_id=self.key,
                client_secret=self.secret, scope="login"), form=True)
            if not response.get("access_token"):
                raise AuthError("Token issue refused; check local API credentials/application", explicit=True)
            token = response["access_token"]
            tmp = self.token_file.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"token": token, "expires": time.time() + int(response["expires_in"]),
                           "key_fingerprint": self.key_fingerprint}, f)
            os.chmod(tmp, 0o600)
            tmp.replace(self.token_file)
            self.token_issued = True
            return token

    def _read(self, path, query):
        """GET with bounded exponential back-off and jitter for transient failures
        (connection errors, non-JSON bodies, documented rate limiting)."""
        for attempt in range(READ_ATTEMPTS):
            try:
                response = self._http("GET", path, query, None, token=self.token())
                if str(response.get("rsp_cd")) in TOKEN_INVALID:
                    self.token_file.unlink(missing_ok=True)
                    response = self._http("GET", path, query, None, token=self.token())
                if str(response.get("rsp_cd")) != RATE_LIMITED:
                    return response
                failure = Stop("Broker rate limit persisted")
            except AuthError:
                raise
            except Stop as exc:
                failure = exc
            if attempt + 1 < READ_ATTEMPTS:
                self.sleep(READ_BACKOFF_SECONDS * 2 ** attempt * (1 + self.rng.random() * 0.25))
        raise failure

    def call(self, method, path, *, query=None, body=None, success=("0000", "5766", "5820"), allow_paginated=False):
        if method != "GET" and not self.allow_mutations:
            raise Stop("Observe mode prohibits trading/FX/cancellation calls")
        if method == "GET":
            response = self._read(path, query)
        else:
            token = self.token()          # a failure here is before any send-start record
            if self.on_send is not None:
                self.on_send()            # durable send-start; raises NotSent if it cannot be saved
            response = self._http(method, path, query, body, token=token)
        if str(response.get("rsp_cd")) not in success:
            code = str(response.get("rsp_cd", "missing"))
            if method != "GET":
                if code in TOKEN_INVALID:
                    self.token_file.unlink(missing_ok=True)   # next run issues a new token
                if path in ORDER_PATHS:
                    raise Refused("Order request not accepted by broker (code " + code + ")", code)
                # FX: a non-success reply is not documented as "not processed";
                # settle it against the FX history before treating it as refused.
                raise RefusedUnconfirmed("Mutation returned non-success API code " + code, code)
            raise Stop("API read failed: " + code)
        if not allow_paginated and str(response.get("tr_cont", "0")) not in {"0", "", "None"}:
            raise Stop("Incomplete paginated response; no trading on a partial snapshot")
        return response

    def pages(self, path, *, query):
        """History-only cursor protocol: preserve whitespace, bound loops, finish all pages."""
        params = dict(query)
        rows, cursors = [], set()
        for _ in range(100):
            response = self.call("GET", path, query=params,
                                 success=("5762", "5766", "5820"), allow_paginated=True)
            chunk = response.get("data", [])
            if not isinstance(chunk, list):
                raise Stop("Invalid paginated ledger payload")
            rows.extend(chunk)
            continuation = str(response.get("tr_cont", "0"))
            if continuation == "0":
                return rows
            if continuation != "1":
                raise Stop("Unknown ledger continuation marker")
            cursor = response.get("tr_cont_key")
            if not isinstance(cursor, str) or not cursor or cursor in cursors or not chunk:
                raise Stop("Missing/repeated ledger cursor; no trading on partial history")
            cursors.add(cursor)
            params = dict(query, tr_cont="1", tr_cont_key=cursor)
        raise Stop("Ledger pagination exceeded page limit")


class Meritz:
    def __init__(self, client: Client, journal):
        self.client, self.j = client, journal
        fingerprint = getattr(client, "key_fingerprint", None)
        if fingerprint:
            bound = journal.meta("broker_key_fingerprint")
            if bound and bound != fingerprint:
                raise Stop("State belongs to a different API key; use the matching account state")
            journal.put_meta("broker_key_fingerprint", fingerprint)

    def _get(self, path, **query):
        response = self.client.call("GET", path, query=query or None)
        if response["rsp_cd"] == "5820":
            return []
        if "data" not in response:
            raise Stop("Successful API response missing data")
        return response["data"]

    def set_send_hook(self, hook):
        self.client.on_send = hook

    # ---- unresolved-send reconciliation (read-only) ----

    @staticmethod
    def _accepted_at(row):
        text = row.get("oder_exec_ymdh")
        try:
            if text:
                return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            return datetime.strptime(str(row["oder_date"]) + str(row["oder_pros_time"]).zfill(6),
                                     "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except (KeyError, ValueError, TypeError):
            return None   # unknown time: kept as a candidate (more candidates -> lock, never a guess)

    def _order_rows(self, first, last):
        rows = {}
        for r in self.order_history(first, last) + (self.today() if last == datetime.now(KST).strftime("%Y%m%d") else []):
            rows[(str(r["oder_date"]), int(r["oder_no"]))] = r
        return rows

    def fx_history(self, first, last):
        return self.client.pages("/forex/v1/exchanges/history",
                                 query=dict(iqry_sta_date=first, iqry_fin_date=last))

    def find_candidates(self, kind, payload, created, tracked):
        """Broker records that could be the bot's unresolved request.

        No client order ID or idempotency key exists in the spec, so identity is
        inferred: same symbol/side/quantity/price (or FX direction/amount), placed
        no earlier than the intent minus clock skew, and not already attributed.
        The engine adopts exactly one candidate, locks on several, and keeps
        waiting on none. Every page must arrive or this raises Stop."""
        floor = created - timedelta(seconds=CLOCK_SKEW_SECONDS)
        first, last = created.astimezone(KST).strftime("%Y%m%d"), datetime.now(KST).strftime("%Y%m%d")
        if kind == "fx":
            result = []
            for r in self.fx_history(first, last):
                if str(r.get("mnec_clcd")) != "1" or r.get("crcd") != "USD":
                    continue
                if str(r.get("chan_type_cls_code", "120")) != "120" or str(r.get("rspn_cntt", "정상")).strip() != "정상":
                    continue
                try:
                    at = datetime.strptime(str(r["aplt_date"]) + str(r["aplt_hour"]).zfill(6), "%Y%m%d%H%M%S").replace(tzinfo=KST)
                except (KeyError, ValueError):
                    at = None
                ref = f"{r.get('aplt_date')}:{r.get('aplt_srno')}"
                krw, requested = money(r.get("tr_amt", 0)), money(payload["krw"])
                if (at is None or at >= floor) and ref not in tracked["fx"] and requested * D("0.99") <= krw <= requested + 1:
                    result.append({"ref": ref, "usd": str(money(r.get("frcr_tr_amt", 0))), "krw": str(krw),
                                   "rate": str(r.get("aply_exrt"))})
            return result
        rows = self._order_rows(first, last)
        result = []
        for (date, number), r in rows.items():
            at = self._accepted_at(r)
            if (date, number) in tracked["orders"] or (at is not None and at < floor):
                continue
            if kind == "cancel":
                original = payload["original"]
                if (str(r.get("oder_rctf_cncl_cls_code")) == "2" and str(r.get("oder_date")) == str(original["date"])
                        and int(r.get("orgl_oder_no", 0)) == int(original["number"])):
                    result.append({"number": number, "date": date, "original": original})
                continue
            symbol = r.get("ovrs_stck_symb_iscd") or str(r.get("iscd", "")).rsplit(".", 1)[0]
            side = {"1": "sell", "2": "buy"}.get(str(r.get("seln_byov_clcd")))
            qty = r.get("odqt", r.get("oder_dqty"))
            if (str(r.get("oder_rctf_cncl_cls_code")) == "0" and symbol == payload["symbol"] and side == payload["side"]
                    and qty is not None and money(qty) == money(payload["qty"])
                    and money(r.get("ovrs_stck_oder_prc", 0)) == money(payload["price"])):
                result.append({"number": number, "date": date, "symbol": symbol, "side": side, "qty": str(payload["qty"])})
        return result

    def today(self):
        return self._get("/trading/v1/overseas/orders/today", cntg_cls_code="0", seln_byov_clcd="0") or []

    def read_account(self):
        """All read-only account responses; validation happens in build_snapshot."""
        return dict(deposit=self._get("/accounts/v1/me/deposit"),
                    holdings=self._get("/accounts/v1/me/overseas/holdings") or [],
                    currencies=self._get("/accounts/v1/me/overseas/currencies") or [],
                    estimates=self._get("/trading/v1/overseas/orders/estimate", intg_marg_iqry_yn="N") or [],
                    today=self.today())

    def snapshot(self):
        return self.build_snapshot(self.read_account())

    def build_snapshot(self, parts, problems=None):
        """Engine view of the account. Without `problems`, any unsupported state
        raises Stop. Diagnostics pass a list to collect them and keep going."""
        def stop(message):
            if problems is None:
                raise Stop(message)
            if message not in problems:
                problems.append(message)
        deposit = parts["deposit"]
        for name in DEBT_FIELDS:
            if money(deposit.get(name, 0)) != 0:
                stop("Credit/loan/unsettled cash debt found; cash-only dedicated account required")
        positions, sellable = {}, {}
        for r in parts["holdings"]:
            qty = money(r["rmnd_dqty"])
            if qty == 0:
                continue
            if r.get("rmnd_clcd") != "1" or r.get("crcd") != "USD" or r.get("loan_dtl_type_code", "0000") != "0000":
                stop("Unsupported currency/fractional/loan holding")
                continue
            symbol = r["ovrs_stck_symb_iscd"]
            if symbol in positions:
                stop("Duplicate holding rows require broker reconciliation")
                continue
            positions[symbol] = qty
            sellable[symbol] = money(r["seln_able_dqty"])
        curr = next((r for r in parts["currencies"] if r["crcd"] == "USD"), None)
        est = next((r for r in parts["estimates"] if r["crcd"] == "USD"), None)
        if est is None:
            raise Stop("USD buying-power row missing")
        if money(est["othr_crnc_evdn_camt"]) != 0:
            stop("Other-currency collateral detected; verify USD-only order funding")
        power = max(D(0), money(est["oder_crnc_oder_able_camt"]))
        usd = max(D(0), money(est["frcr_amt"]))
        krw = max(D(0), min(money(deposit["drng_able_amt"]), money(deposit["oder_able_cash_amt"])))
        known = self._tracked_orders()
        external = False
        for row in parts["today"]:
            if str(row.get("oder_rctf_cncl_cls_code")) != "0":
                continue
            if self._state(row).state == "open" and (row["oder_date"], int(row["oder_no"])) not in known:
                external = True
        return Snapshot(krw, usd, power, positions, sellable, external,
                        cash_usd=money(curr["frcr_amt"]) if curr else D(0))

    def _tracked_orders(self):
        known = set()
        for action in self.j.rows():
            if action["kind"] == "order" and action["receipt"]:
                receipt = json.loads(action["receipt"])
                known.add((receipt["date"], int(receipt["number"])))
        return known

    # ---- read-only diagnostics; neutral keys, never raw rows or identifiers ----

    def account_report(self, parts):
        def amount(row, key):
            try:
                return str(money(row[key])) if row is not None and key in row else None
            except Stop:
                return None
        deposit = parts["deposit"]
        curr = next((r for r in parts["currencies"] if r.get("crcd") == "USD"), None)
        est = next((r for r in parts["estimates"] if r.get("crcd") == "USD"), None)
        krw = {"deposit_total": amount(deposit, "tfam"),
               "withdrawable": amount(deposit, "drng_able_amt"),
               "order_cash": amount(deposit, "oder_able_cash_amt"),
               "deposit_d2": amount(deposit, "d2_aft_tfam")}
        usd = {"deposit_current": amount(curr, "frcr_amt"),
               "deposit_orderable": amount(curr, "estm_frcr_amt"),
               "deposit_standard": amount(est, "stnd_deps_camt"),
               "deposit_estimated_with_unsettled": amount(est, "frcr_amt"),
               "order_available": amount(est, "oder_crnc_oder_able_camt"),
               "rebuy_available": amount(est, "camt"),
               "order_margin_locked": amount(est, "stnd_evdn_camt"),
               "other_currency_margin": amount(est, "othr_crnc_evdn_camt")}
        debts = {}
        for name in DEBT_FIELDS + ("fee_unam",):
            value = amount(deposit, name)
            if value is not None and money(value) != 0:
                debts[name] = value
        missing = [f"krw.{k}" for k, v in krw.items() if v is None] + [f"usd.{k}" for k, v in usd.items() if v is None]
        holdings = []
        for r in parts["holdings"]:
            qty = money(r.get("rmnd_dqty", 0))
            if qty == 0:
                continue
            kind = {"1": "whole", "2": "fractional", "3": "loan"}.get(str(r.get("rmnd_clcd")), "unknown")
            holdings.append({"symbol": r.get("ovrs_stck_symb_iscd") or str(r.get("iscd", "")).rsplit(".", 1)[0],
                             "quantity": str(qty), "sellable": amount(r, "seln_able_dqty"),
                             "balance_kind": kind, "currency": r.get("crcd"),
                             "loan": r.get("loan_dtl_type_code", "0000") != "0000",
                             "average_price": amount(r, "avrg_unpr"), "current_price": amount(r, "psnt_prc"),
                             "value": amount(r, "frcr_vltn_amt")})
        return {"krw": krw, "usd": usd, "debts": debts, "missing_fields": missing, "holdings": holdings}

    def order_report(self, parts):
        """Today's original orders: open ones and fee observations from fills."""
        known = self._tracked_orders()
        orders, fees = [], []
        for row in parts["today"]:
            if str(row.get("oder_rctf_cncl_cls_code")) != "0":
                continue
            try:
                state = self._state(row).state
            except Stop as exc:
                state = "invalid: " + str(exc)
            symbol = row.get("ovrs_stck_symb_iscd") or str(row.get("iscd", "")).rsplit(".", 1)[0]
            orders.append({"symbol": symbol, "side": {"1": "sell", "2": "buy"}.get(str(row.get("seln_byov_clcd")), "unknown"),
                           "quantity": str(row.get("odqt")), "filled": str(row.get("cntg_qty")),
                           "price": str(row.get("ovrs_stck_oder_prc")), "state": state,
                           "tracked_by_engine": (row.get("oder_date"), int(row.get("oder_no", 0))) in known})
            try:
                amount, fee = money(row.get("cntg_camt", 0)), money(row.get("oder_cfee", 0))
            except Stop:
                continue
            if amount > 0:
                fees.append({"symbol": symbol, "filled_amount": str(amount), "fee": str(fee),
                             "rate": str((fee / amount).quantize(D("0.000001")))})
        return {"orders": orders, "open": [o for o in orders if o["state"] == "open"], "fee_observations": fees}

    def active_reservations(self, now, since=None):
        """Live overseas reservations (iqry_idnt_clcd=2), shared by the engine gate
        and doctor. Read-only: reservations are never cancelled here.

        The API filters by reservation ACCEPTANCE date, so an old long-period
        reservation is found only if the search reaches back to its acceptance.
        `since` (YYYYMMDD, on/before the account opening date) makes the search
        complete; without it the last RESERVATION_LOOKBACK_DAYS are searched and
        the result is marked incomplete. The range is split into non-overlapping
        RESERVATION_CHUNK_DAYS windows because no range limit is documented.

        Stop on any sign of a partial answer: unfinished pages, missing documented
        fields, a row outside its requested window (filter ignored) or a row
        repeated across windows."""
        last = now.astimezone(KST).date()
        if since is not None:
            first = datetime.strptime(since, "%Y%m%d").date()
            if first > last:
                raise Stop("Reservation lookup start date is in the future")
        else:
            first = last - timedelta(days=RESERVATION_LOOKBACK_DAYS - 1)   # inclusive window
        result, seen, chunks = [], set(), 0
        start = first
        while start <= last:
            end = min(last, start + timedelta(days=RESERVATION_CHUNK_DAYS - 1))
            lo, hi = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
            # All three filter fields are always sent: the API answers an empty
            # array, not an error, when one is missing.
            rows = self.client.pages("/trading/v1/overseas/reserved-orders", query=dict(
                iqry_sta_date=lo, iqry_fin_date=hi, seln_byov_clcd="0", iqry_idnt_clcd="2"))
            chunks += 1
            for r in rows:
                if not isinstance(r, dict) or any(k not in r for k in RESERVATION_FIELDS):
                    raise Stop("Reservation row missing documented fields")
                accepted = str(r["rsev_acpt_date"])
                if not (lo <= accepted <= hi):
                    raise Stop("Reservation row outside requested acceptance dates; filter not applied")
                identity = (accepted, str(r.get("rsev_acpt_srno", "")))
                if identity in seen:
                    raise Stop("Reservation row repeated in lookup")
                seen.add(identity)
                symbol = r.get("ovrs_stck_symb_iscd") or str(r.get("iscd") or "").rsplit(".", 1)[0]
                if not symbol:
                    raise Stop("Reservation row has no symbol")
                # Documented: "Y" is cancelled; non-cancelled rows carry a blank value.
                if str(r["rsev_cncl_yn"]).strip() == "Y":
                    continue
                result.append({"symbol": symbol, "market": str(r.get("ovrs_stck_mrkt_tr_cls_code", "")),
                               "side": {"1": "sell", "2": "buy"}.get(str(r["seln_byov_clcd"]), "unknown"),
                               "quantity": str(r["odqt"]), "filled": str(r.get("acml_cntg_qty")),
                               "price": str(r.get("ovrs_stck_oder_prc")), "order_type": str(r.get("rsev_aspr_tpcd")),
                               "accepted": accepted, "start": r.get("oder_sta_date"), "end": r.get("oder_fin_date"),
                               "cancellable_in_app": r.get("cncl_able_yn")})
            start = end + timedelta(days=1)
        reason = (None if since is not None else
                  f"searched only acceptances since {first.strftime('%Y%m%d')}; set "
                  "execution.reservation_lookup_start to the account opening date (or earlier)")
        return ReservationLookup(result, complete=since is not None, searched_from=first.strftime("%Y%m%d"),
                                 searched_to=last.strftime("%Y%m%d"), chunks=chunks, reason=reason)

    def _quote_raw(self, symbol):
        mrkt = "NY" if symbol == "BRK.B" else "OQ"
        query = dict(mrkt_div_code="OV", mrkt_cls_code=mrkt, iscd=symbol, dely_rltm_cls_code="0")
        book = self._get("/market/v1/overseas/orderbook", **query)
        px = self._get("/market/v1/overseas/prices", **query)
        return mrkt, book, px

    @staticmethod
    def _quote_from(symbol, mrkt, book, px, now):
        if px["iscd"] not in {symbol, symbol + "." + mrkt} or px.get("crnc_code") != "USD":
            raise Stop("Quote symbol/currency mismatch")
        if px.get("issu_stat_cls_code", "N") not in {"N", ""} or px.get("vcm_stat_cls_code", "N") not in {"N", ""}:
            raise Stop("Security/market trading restriction")
        stamp = datetime.strptime(book["acpl_hour"], "%H%M%S").time()
        local = now.astimezone(NY)
        at = datetime.combine(local.date(), stamp, NY)
        tick = max(D("0.01"), money(px.get("shnu_aspr_unit", 0)), money(px.get("seln_aspr_unit", 0)))
        return Quote(money(book["bidp1"]), money(book["askp1"]), tick, at)

    def quote(self, symbol, now):
        return self._quote_from(symbol, *self._quote_raw(symbol), now)

    def quote_report(self, symbol, now):
        """(engine Quote or None, display info, reason the engine quote failed)."""
        mrkt, book, px = self._quote_raw(symbol)
        info = {"last": str(px.get("prpr")), "previous_close": str(px.get("prdy_clpr")),
                "bid": str(book.get("bidp1")), "ask": str(book.get("askp1")),
                "quote_time_local": book.get("acpl_hour"), "sell_only": str(px.get("sell_only", "")),
                "security_status": px.get("issu_stat_cls_code"), "leverage": px.get("lvrg"),
                "qty_unit": str(px.get("shnu_oder_qty_unit", ""))}
        try:
            return self._quote_from(symbol, mrkt, book, px, now), info, None
        except (Stop, ValueError, KeyError) as exc:
            return None, info, str(exc) if isinstance(exc, Stop) else "Unparseable quote: " + type(exc).__name__

    @staticmethod
    def _fx_from(r):
        at = datetime.strptime(r["exrt_stnd_time"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
        mid = (money(r["tltr_byrt"]) + money(r["tltr_slrt"])) / 2
        return FXQuote(money(r["tltr_aply_byrt"]), mid, at, r["exrt_stnd_time"])

    def fx_quote(self, now):
        return self._fx_from(self._get("/forex/v1/rates/applied", crcd="USD", mnec_clcd="1"))

    def fx_report(self, now):
        """Customer-applied KRW->USD rate, spread and discount, as the account sees them."""
        r = self._get("/forex/v1/rates/applied", crcd="USD", mnec_clcd="1")
        quote = self._fx_from(r)
        base = money(r["tltr_byrt"])
        return quote, {"rate_time_kst": r.get("exrt_stnd_time"), "base_buy_rate": str(base),
                       "applied_buy_rate": str(quote.ask), "applied_sell_rate": str(r.get("tltr_aply_slrt")),
                       "spread_krw": str(r.get("spead")), "base_spread_krw": str(r.get("basc_spead")),
                       "discount_pct": str(r.get("dcrt")), "grade_name": r.get("cust_grad_code_name"),
                       "preference": r.get("prft_aply_cls_val"),
                       "spread_ratio": str(((quote.ask - base) / base).quantize(D("0.000001"))) if base > 0 else None}

    def exchange(self, krw, quote):
        response = self.client.call("POST", "/forex/v1/exchanges", body=dict(
            mnec_clcd="1", wncr_amt=int(krw), frcr_amt=0, crcd="USD",
            exrt_stnd_time=quote.server_time, iqry_exrt=str(quote.ask)), success=("0000",))
        r = response["data"]
        if money(r["tr_amt"]) <= 0 or money(r["frcr_tr_amt"]) <= 0:
            raise UnknownOutcome("Unexpected FX receipt")
        return {"number": r["tr_srno"], "usd_after": str(r["frcr_tfam_nwrm"]), "raw": r}

    @staticmethod
    def _receipt(response, now):
        r = response["data"]
        if str(r.get("warn_cls_code")) != "0":
            raise Rejected("Broker warning/hold code " + str(r.get("warn_cls_code")) + "; review warning in broker application")
        if money(r["oder_no"]) <= 0:
            raise UnknownOutcome("Accepted order has no valid order number")
        # API's order day is KST. Confirm it from today's ledger rather than infer
        # an order number's uniqueness across different trading dates.
        return {"number": int(r["oder_no"]), "date": now.astimezone(KST).strftime("%Y%m%d"), "raw": r}

    def order(self, symbol, side, qty, price, now):
        exchange = "0321" if symbol == "BRK.B" else "0537"
        response = self.client.call("POST", "/trading/v1/overseas/orders/" + side, body=dict(
            ovrs_stck_symb_iscd=symbol, ovrs_exch_vndr_idno=exchange,
            oder_cls_code="01", oder_cond_cls_code="0", odqt=int(qty),
            ovrs_stck_oder_prc=str(price), warn_cnfr_yn="N"), success=("0001",))
        receipt = self._receipt(response, now)
        ledger = self.today()
        matches = [r for r in ledger if int(r["oder_no"]) == receipt["number"]
                   and r["ovrs_stck_symb_iscd"] == symbol and r["seln_byov_clcd"] == ("2" if side == "buy" else "1")
                   and money(r["odqt"]) == qty and str(r["oder_rctf_cncl_cls_code"]) == "0"]
        if len(matches) != 1:
            raise UnknownOutcome("New order not uniquely visible in broker ledger")
        receipt.update(date=matches[0]["oder_date"], symbol=symbol, side=side, qty=str(qty))
        return receipt

    @staticmethod
    def _state(row):
        qty, filled = money(row["odqt"]), money(row["cntg_qty"])
        if not (D(0) <= filled <= qty) or qty <= 0:
            raise Stop("Invalid broker fill quantity")
        code = str(row["ovrs_stck_oder_stat_cls_code"])
        if code not in {"0", "1", "2", "3", "4"}:
            raise Stop("Unknown broker order state")
        rejected = code in {"1", "2", "4"}
        cancelled = code == "3"
        if rejected:
            state = "rejected"
        elif filled == qty:
            state = "filled"
        elif cancelled or money(row.get("cncl_cnfr_qty", 0)) >= qty - filled:
            state = "cancelled"
        else:
            state = "open"
        return OrderStatus(state, filled, row)

    def order_status(self, receipt):
        now = datetime.now(KST)
        day = datetime.strptime(receipt["date"], "%Y%m%d").date()
        if day > now.date():
            raise Stop("Tracked order date is in the future")
        historical = day != now.date()
        rows = (self._get("/trading/v1/overseas/orders/detail",
                          oder_sta_date=receipt["date"], oder_no=receipt["number"])
                if historical else self.today())
        matching = [r for r in rows if r["oder_date"] == receipt["date"]
                    and int(r["oder_no"]) == receipt["number"]
                    and str(r["oder_rctf_cncl_cls_code"]) == "0"]
        if len(matching) != 1:
            raise Stop("Tracked order is missing/ambiguous in broker ledger")
        row = dict(matching[0])
        if historical:
            row.update(odqt=row["oder_dqty"], cntg_qty=row["cntg_dqty"])
        symbol = row.get("ovrs_stck_symb_iscd") or row["iscd"].rsplit(".", 1)[0]
        if (receipt.get("symbol", symbol) != symbol
            or receipt.get("side", {"1": "sell", "2": "buy"}.get(str(row["seln_byov_clcd"])))
            != {"1": "sell", "2": "buy"}.get(str(row["seln_byov_clcd"]))
            or money(receipt.get("qty", row["odqt"])) != money(row["odqt"])):
            raise Stop("Tracked order identity differs from broker ledger")
        status = self._state(row)
        if historical and status.state == "open":
            # Detail has no confirmed-cancel quantity. A cancel request or an
            # elapsed day is not evidence of cancellation. Do not infer expiry.
            raise Stop("Historical order is not explicitly terminal; reconcile cancellation in account")
        return status

    def order_history(self, first, last):
        start, end = datetime.strptime(first, "%Y%m%d"), datetime.strptime(last, "%Y%m%d")
        if start > end:
            raise Stop("Invalid order-history date range")
        rows = self.client.pages("/trading/v1/overseas/orders/history", query=dict(
            oder_sta_date=first, oder_fin_date=last, seln_byov_clcd="0", cntg_cls_code="0"))
        identities = [(r["oder_date"], int(r["oder_no"])) for r in rows]
        if len(set(identities)) != len(identities):
            raise Stop("Duplicate orders across ledger pages")
        return rows

    def cancel(self, original, qty):
        r = self.client.call("POST", "/trading/v1/overseas/orders/cancel", body=dict(
            oder_date=original["date"], orgl_oder_no=original["number"],
            oder_cls_code="01", odqt=int(qty), warn_cnfr_yn="N"), success=("0001",))
        receipt = self._receipt(r, datetime.now(KST))
        receipt["original"] = original
        return receipt

    def completed_candles(self, first, last):
        rows = self._get("/market/v1/overseas/candles/days", mrkt_div_code="OV", mrkt_cls_code="OQ",
                         iscd="QQQ", dely_rltm_cls_code="0", mod_stpr_cls_code="1",
                         **{"from": first.replace("-", ""), "to": last.replace("-", "")})
        result = {}
        for r in rows or []:
            day = datetime.strptime(r["date"], "%Y%m%d").date().isoformat()
            if first <= day <= last:
                if day in result:
                    raise Stop("Duplicate daily candle")
                price = money(r["prpr"])
                if price <= 0:
                    raise Stop("Invalid daily close")
                result[day] = price
        return result

    def market_status(self):
        """Raw US market-hours fields. Interpretation lives in
        schedule.regular_session_check; the code alone never means "regular"."""
        r = self._get("/reference/v1/overseas/market-hours", ovrs_stck_mrkt_tr_cls_code="01")
        if not isinstance(r, dict) or not r.get("ovrs_mkop_cls_code"):
            raise Stop("Market-hours response missing status")
        market = r.get("ovrs_stck_mrkt_tr_cls_code")
        return {"market": None if market is None else str(market), "code": str(r["ovrs_mkop_cls_code"]),
                "begin_kst": str(r.get("mrkt_begn_time", "")), "end_kst": str(r.get("mrkt_fin_time", ""))}
