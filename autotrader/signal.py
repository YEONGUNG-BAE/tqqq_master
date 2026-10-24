"""Frozen DD15/12 signal. Prices: split-adjusted QQQ CLOSE, dividends excluded.

Targets are decisions at the close, not fills. Historical benchmark executes
at the NEXT US session close. The automation adapter records its execution
method separately; switching to an open window requires integration review.
"""
from dataclasses import dataclass, asdict
import argparse
import csv
import json
from decimal import Decimal
from datetime import date

WINDOW = 252
EXIT_DD = -0.15
ENTRY_DD = -0.12
# The only place the two strategy symbols are defined. The defensive symbol is
# QQQ in this version; replacing it (e.g. by SGOV) is a separate, reviewed change.
RISK_SYMBOL = "TQQQ"
DEFENSIVE_SYMBOL = "QQQ"


@dataclass(frozen=True)
class Decision:
    index: int
    close: float
    rolling_high: float
    drawdown: float
    previous_target: str
    target: str
    transition: str | None
    expiry_driven: bool
    nonexpiring_high: float | None
    near_threshold: bool
    full_window: bool


def decisions(closes, initial_target=RISK_SYMBOL):
    """Replay all available history; persist/replay targets independently of fills.

    Early rows have partial windows for historical initialization. A live
    decision requires at least 252 observations (checked by the CLI).
    nonexpiring_high is diagnostic ONLY and never changes the trading rule.
    """
    if initial_target not in {DEFENSIVE_SYMBOL, RISK_SYMBOL}:
        raise ValueError(f"initial_target must be {DEFENSIVE_SYMBOL} or {RISK_SYMBOL}")
    prices = [Decimal(str(p)) for p in closes]
    if not prices or any(not p.is_finite() or p <= 0 for p in prices):
        raise ValueError("closes must be nonempty, finite, positive")
    target = initial_target
    frozen_high = None
    output = []
    for i, price in enumerate(prices):
        high = max(prices[max(0, i - WINDOW + 1):i + 1])
        # Compare products, not rounded float ratios at exactly -15%/-12%.
        dd = price / high - 1
        previous = target
        expiry = False
        if previous == DEFENSIVE_SYMBOL:
            frozen_high = max(frozen_high or high, price)
        diagnostic_high = frozen_high
        if previous == RISK_SYMBOL and price <= high * Decimal("0.85"):
            target = DEFENSIVE_SYMBOL
            frozen_high = high
            diagnostic_high = high
        elif previous == DEFENSIVE_SYMBOL and price >= high * Decimal("0.88"):
            target = RISK_SYMBOL
            expiry = price < frozen_high * Decimal("0.88")
            frozen_high = None
        output.append(Decision(i, float(price), float(high), float(dd), previous, target,
                               f"{previous}->{target}" if previous != target else None,
                               expiry, float(diagnostic_high) if diagnostic_high is not None else None,
                               min(abs(dd + Decimal("0.15")), abs(dd + Decimal("0.12"))) <= Decimal("0.005"),
                               i + 1 >= WINDOW))
    return output


def main():
    parser = argparse.ArgumentParser(description="Evaluate frozen QQQ close signals; no orders.")
    parser.add_argument("csv", help="date,close columns; unique ascending US session dates")
    parser.add_argument("--expected-session", required=True,
                        help="Expected last completed US session YYYY-MM-DD; caller uses exchange calendar")
    parser.add_argument("--initial-target", choices=[DEFENSIVE_SYMBOL, RISK_SYMBOL], default=RISK_SYMBOL)
    args = parser.parse_args()
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    dates = [date.fromisoformat(row["date"]) for row in rows]
    if len(rows) < WINDOW or any(a >= b for a, b in zip(dates, dates[1:])):
        parser.error("need >=252 unique ascending observations")
    if dates[-1] != date.fromisoformat(args.expected_session):
        parser.error("last date differs from expected completed US session")
    decision = asdict(decisions([r["close"] for r in rows], args.initial_target)[-1])
    decision.update(signal_date=dates[-1].isoformat(), rule="DD15_12_252_v1",
                    execution="NEXT_US_SESSION_CLOSE", order_submitted=False)
    print(json.dumps(decision, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
