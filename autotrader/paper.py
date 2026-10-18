"""Offline simulator used by demo and failure-injection tests. No network."""
from datetime import datetime, timedelta
from .core import CLOCK_SKEW_SECONDS, D, FXQuote, OrderStatus, Quote, ReservationLookup, Snapshot


class Paper:
    def __init__(self, krw="0", usd="0", positions=None):
        self.krw, self.usd = D(krw), D(usd)
        self.positions = {s: D(str(q)) for s, q in (positions or {}).items()}
        self.orders = {}
        self.calls = []
        self.fee = D("0.0007")
        self.prices = {"QQQ": D("700"), "TQQQ": D("70"), "QQQM": D("400"), "BRK.B": D("500")}
        self.power_fraction = D(1)
        self.reservations = []  # tests inject live reservations or an exception
        self.fx_history = []    # broker-side FX records, independent of receipts
        self.cancel_log = []    # broker-side cancel records
        self._send_hook = None

    def set_send_hook(self, hook):
        self._send_hook = hook

    def _sending(self):
        # Mirrors the HTTP transport: the engine's send-start record is written
        # immediately before the simulated request changes broker state.
        if self._send_hook is not None:
            self._send_hook()

    def active_reservations(self, now, since=None):
        # The simulator owns its whole history, so its coverage is complete.
        if isinstance(self.reservations, Exception):
            raise self.reservations
        return ReservationLookup(list(self.reservations), complete=True, reason="paper account")

    def snapshot(self):
        return Snapshot(self.krw, self.usd, self.usd * self.power_fraction,
                        dict(self.positions), dict(self.positions))

    def quote(self, symbol, now):
        price = self.prices[symbol]
        return Quote(price, price + D("0.01"), D("0.01"), now)

    def fx_quote(self, now):
        return FXQuote(D("1400"), D("1400"), now, now.isoformat())

    def exchange(self, krw, quote):
        self._sending()
        self.calls.append(("fx", krw))
        self.krw -= krw
        usd = krw / quote.ask
        self.usd += usd
        number = len(self.calls)
        self.fx_history.append({"ref": str(number), "krw": str(krw), "usd": str(usd), "at": quote.at.isoformat()})
        return {"number": number, "ref": str(number), "usd_after": str(self.usd)}

    def order(self, symbol, side, qty, price, now):
        self._sending()
        self.calls.append((side, symbol, qty))
        number = len(self.calls)
        self.orders[number] = dict(symbol=symbol, side=side, qty=qty, price=price,
                                   filled=D(0), state="open", at=now.isoformat(), date=now.strftime("%Y%m%d"))
        return {"number": number, "date": now.strftime("%Y%m%d")}

    def fill(self, number, quantity=None):
        r = self.orders[number]
        remaining = r["qty"] - r["filled"]
        qty = remaining if quantity is None else min(D(str(quantity)), remaining)
        delta = qty if r["side"] == "buy" else -qty
        self.positions[r["symbol"]] = self.positions.get(r["symbol"], D(0)) + delta
        if self.positions[r["symbol"]] == 0:
            del self.positions[r["symbol"]]
        amount = qty * r["price"]
        self.usd += -amount * (1 + self.fee) if r["side"] == "buy" else amount * (1 - self.fee)
        r["filled"] += qty
        r["state"] = "filled" if r["filled"] == r["qty"] else "open"

    def order_status(self, receipt):
        r = self.orders[receipt["number"]]
        return OrderStatus(r["state"], r["filled"], dict(r))

    def cancel(self, original, qty):
        self._sending()
        self.calls.append(("cancel", original["number"], qty))
        self.orders[original["number"]]["state"] = "cancelled"
        number = len(self.calls)
        self.cancel_log.append({"number": number, "original": original["number"], "at": self.orders[original["number"]]["at"]})
        return {"number": number, "original": original}

    def find_candidates(self, kind, payload, created, tracked):
        """Broker-side records that could be an unresolved request, by the same
        rules as the REST adapter: matching fields, not before the intent (allowing
        clock skew), and not already attributed. The engine decides what to do
        with zero or several."""
        floor = created - timedelta(seconds=CLOCK_SKEW_SECONDS)
        if kind == "order":
            return [{"number": n, "date": o["date"]} for n, o in self.orders.items()
                    if o["symbol"] == payload.get("symbol") and o["side"] == payload.get("side")
                    and str(o["qty"]) == str(payload.get("qty")) and str(o["price"]) == str(payload.get("price"))
                    and datetime.fromisoformat(o["at"]) >= floor and (o["date"], n) not in tracked["orders"]]
        if kind == "fx":
            return [{"ref": e["ref"], "usd": e["usd"], "krw": e["krw"]} for e in self.fx_history
                    if datetime.fromisoformat(e["at"]) >= floor and e["ref"] not in tracked["fx"]
                    and D(e["krw"]) <= D(payload.get("krw", "0"))]
        if kind == "cancel":
            original = payload.get("original", {})
            return [{"number": c["number"], "date": original.get("date"), "original": original}
                    for c in self.cancel_log if c["original"] == original.get("number")
                    and (original.get("date"), c["number"]) not in tracked["orders"]]
        return []
