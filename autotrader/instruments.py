"""Meritz quote market and order exchange per instrument.

The quote market code (mrkt_cls_code) and the order exchange code
(ovrs_exch_vndr_idno) are separate broker identifiers. Their pairing for the US
is documented by the exchange reference API (/reference/v1/overseas/exchanges:
ovrs_stck_exch_idno <-> ovrs_exch_vndr_idno): OQ <-> 0537 (NASDAQ),
NY <-> 0321 (NYSE), AX <-> 0066 (AMEX). The spec has no API that tells which
market a symbol belongs to, so each instrument's market is either a reviewed
static entry below or a value the user confirmed with `doctor` and entered in
`execution.instrument_markets`. A listing exchange name (e.g. "NYSE Arca") is
not a broker routing code. An instrument without a confirmed market gets no
default: new quotes and orders for it are blocked.
"""
from __future__ import annotations

from dataclasses import dataclass

from .core import Stop

US_ROUTES = {"OQ": "0537", "NY": "0321", "AX": "0066"}
CURRENCY = "USD"

# Reviewed static routes, used by this code before the SGOV change. QQQ/TQQQ
# quotes and holdings were read on the real account with doctor (2026-10-18).
KNOWN = {
    "TQQQ": ("OQ", "NASDAQ listing; real-account quotes via doctor"),
    "QQQ": ("OQ", "NASDAQ listing; real-account quotes via doctor; also the signal's daily closes"),
    "QQQM": ("OQ", "NASDAQ listing; existing route (quotes not yet read on the real account)"),
    "BRK.B": ("NY", "NYSE listing; existing route (quotes not yet read on the real account)"),
}


class RouteUnverified(Stop):
    """No confirmed Meritz market for this instrument: new requests are blocked."""


@dataclass(frozen=True)
class Route:
    symbol: str
    quote_market: str
    order_exchange: str
    currency: str
    source: str


def validate_overrides(overrides):
    """User-confirmed markets: {"SGOV": "AX"}. They may add instruments but never
    change a reviewed static route."""
    clean = {}
    for symbol, market in (overrides or {}).items():
        if market is None:
            continue
        symbol, market = str(symbol), str(market)
        if symbol != symbol.upper() or not symbol.replace(".", "").isalnum():
            raise ValueError(f"instrument_markets: invalid symbol {symbol!r}")
        if market not in US_ROUTES:
            raise ValueError(f"instrument_markets[{symbol}] must be one of {sorted(US_ROUTES)}")
        if symbol in KNOWN and KNOWN[symbol][0] != market:
            raise ValueError(f"instrument_markets[{symbol}] conflicts with its reviewed route {KNOWN[symbol][0]}")
        clean[symbol] = market
    return clean


def resolve(symbol, overrides=None):
    if symbol in KNOWN:
        market, basis = KNOWN[symbol]
        return Route(symbol, market, US_ROUTES[market], CURRENCY, "reviewed static route: " + basis)
    market = (overrides or {}).get(symbol)
    if market in US_ROUTES:
        return Route(symbol, market, US_ROUTES[market], CURRENCY,
                     "user-confirmed market from doctor (execution.instrument_markets); order exchange from the "
                     "documented pairing")
    raise RouteUnverified(f"No confirmed Meritz market for {symbol}: run doctor, check the market probe, then set "
                          f"execution.instrument_markets.{symbol}; new {symbol} requests are blocked until then")
