"""Persistent offline workflow replay, NOT a return/tax backtest or fill model.

Uses the same Engine and SQLite intent journal as the REST adapter. All prices,
deposits and fills are synthetic inputs. Does not load keys or create a Client.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import D, Engine, Journal, Settings, Stop, money
from .paper import Paper
from .signal import RULE, SIGNAL_SYMBOL, STRATEGY_VERSION, decisions


class PersistentPaper(Paper):
    """Persist broker-side effects independently of the intent receipt checkpoint."""

    def __init__(self, journal, *, initial=None):
        if journal.meta("mode") not in {None, "paper"}:
            raise Stop("Offline replay requires a separate paper state directory")
        self.journal = journal
        saved = journal.meta("paper_account")
        if saved is None:
            super().__init__(**(initial or {}))
            self._save()
        else:
            super().__init__(saved["krw"], saved["usd"], saved["positions"])
            self.orders = {int(n): dict(row, qty=money(row["qty"]), price=money(row["price"]),
                                      filled=money(row["filled"])) for n,row in saved["orders"].items()}
            self.calls = saved["calls"]
            # States saved before SGOV keep their prices; new symbols get the defaults.
            self.prices.update({s: money(v) for s, v in saved["prices"].items()})
            self.fee = money(saved["fee"])
            self.power_fraction = money(saved["power_fraction"])
            self.fx_history = saved.get("fx_history", [])     # absent in older saved states
            self.cancel_log = saved.get("cancel_log", [])
        self.journal.db.execute("CREATE TABLE IF NOT EXISTS paper_deposits(id TEXT PRIMARY KEY, amount TEXT NOT NULL)")
        self.journal.db.commit()

    def _save(self):
        self.journal.put_meta("paper_account", json.loads(json.dumps(dict(
            krw=self.krw, usd=self.usd, positions=self.positions, orders=self.orders,
            calls=self.calls, prices=self.prices, fee=self.fee,
            power_fraction=self.power_fraction, fx_history=self.fx_history,
            cancel_log=self.cancel_log), default=str)))

    def deposit(self, event_id, krw):
        amount = money(krw)
        if amount < 0 or amount != amount.to_integral_value():
            raise Stop("Paper KRW deposit must be a nonnegative integer")
        previous = self.journal.db.execute("SELECT amount FROM paper_deposits WHERE id=?", (event_id,)).fetchone()
        if previous:
            if money(previous[0]) != amount:
                raise Stop("Deposit ID reused with a different amount")
            return False
        # Deposit ID and changed balance commit together in the same SQLite transaction.
        self.journal.db.execute("INSERT INTO paper_deposits VALUES (?,?)", (event_id, str(amount)))
        self.krw += amount
        self._save()
        return True

    def market(self, prices):
        for symbol, value in prices.items():
            px = money(value)
            if symbol not in self.prices or px <= 0:
                raise Stop("Invalid synthetic market input")
            self.prices[symbol] = px
        self._save()

    def exchange(self, krw, quote):
        if krw <= 0 or krw > self.krw:
            raise Stop("Paper FX exceeds available KRW")
        result = super().exchange(krw, quote)
        self._save()
        return result

    def exchange_to_krw(self, usd, quote):
        if usd <= 0 or usd > self.usd:
            raise Stop("Paper reverse FX exceeds available USD")
        result = super().exchange_to_krw(usd, quote)
        self._save()
        return result

    def order(self, symbol, side, qty, price, now):
        if side not in {"buy", "sell"} or qty <= 0 or qty != qty.to_integral_value() or price <= 0:
            raise Stop("Invalid whole-share paper order")
        result = super().order(symbol, side, qty, price, now)
        self._save()
        return result

    def fill(self, number, quantity=None):
        row = self.orders[number]
        if row["state"] != "open":
            raise Stop("Paper order is not open")
        qty = row["qty"] - row["filled"] if quantity is None else money(quantity)
        if qty <= 0 or qty != qty.to_integral_value() or qty > row["qty"] - row["filled"]:
            raise Stop("Invalid paper fill quantity")
        if row["side"] == "sell" and qty > self.positions.get(row["symbol"], D(0)):
            raise Stop("Paper fill would create a short position")
        if row["side"] == "buy" and qty * row["price"] * (1+self.fee) > self.usd:
            raise Stop("Paper fill exceeds available USD")
        super().fill(number, qty)
        self._save()

    def cancel(self, original, qty):
        result = super().cancel(original, qty)
        self._save()
        return result


def load_scenario(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    seed = data["seed"]
    history = [money(v) for v in seed["closes"]]
    if len(history) < 252 or any(v <= 0 for v in history):
        raise Stop("Synthetic scenario needs at least 252 positive seed closes")
    last = datetime.fromisoformat(seed["last_session"]).date().isoformat()
    for event in data["sessions"]:
        day = datetime.fromisoformat(event["session"]).date().isoformat()
        opened = datetime.fromisoformat(event["open_at"])
        if day <= last or opened.tzinfo is None or money(event["close"]) <= 0:
            raise Stop("Scenario sessions must be ascending with aware opening timestamps")
        if opened.astimezone(ZoneInfo("America/New_York")).date().isoformat() != day:
            raise Stop("Scenario timestamp does not belong to its US session")
        if money(event.get("deposit_krw", "0")) < 0:
            raise Stop("Negative synthetic deposit")
        last = day
    return data, hashlib.sha256(raw).hexdigest()


def replay(scenario_path, state, *, max_sessions=None):
    if max_sessions is not None and max_sessions < 1:
        raise Stop("Session count must be positive")
    scenario, fingerprint = load_scenario(scenario_path)
    path = Path(state).resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    with open(path/"run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Stop("Another process is using this state") from exc
        return _replay_locked(scenario, fingerprint, path, max_sessions)


def _replay_locked(scenario, fingerprint, path, max_sessions):
    journal = Journal(path/"journal.sqlite")
    try:
        prior = journal.meta("scenario_sha256")
        if prior and prior != fingerprint:
            raise Stop("Scenario differs from saved run; use a separate state directory")
        broker = PersistentPaper(journal, initial=scenario.get("initial"))
        settings = Settings(**dict(scenario.get("execution", {}), mode="paper"))
        engine = Engine(broker, journal, settings)
        journal.put_meta("scenario_sha256", fingerprint)
        start = journal.meta("replay_next", 0)
        end = min(len(scenario["sessions"]), start+max_sessions) if max_sessions else len(scenario["sessions"])
        closes = scenario["seed"]["closes"] + [e["close"] for e in scenario["sessions"][:start]]
        previous_session = scenario["sessions"][start-1]["session"] if start else scenario["seed"]["last_session"]
        for index in range(start, end):
            event = scenario["sessions"][index]
            opened = datetime.fromisoformat(event["open_at"])
            broker.market(event.get("prices", {}))
            broker.deposit(f"session:{index}", event.get("deposit_krw", "0"))
            signal = asdict(decisions(closes)[-1])
            signal.update(signal_date=previous_session, rule=RULE, strategy=STRATEGY_VERSION,
                          signal_symbol=SIGNAL_SYMBOL, execution="SYNTHETIC_OFFLINE_WORKFLOW")
            journal.put_meta("signal", signal)
            journal.event("signal", signal, opened)
            common = dict(target=signal["target"], signal_date=previous_session,
                          expected_signal_date=previous_session, session=event["session"])
            # The synthetic daytime phase tests FX logic without claiming holiday
            # availability. Actual live scheduling is tested separately.
            for step in range(3):
                engine.tick(**common, now=opened-timedelta(hours=8)+timedelta(seconds=step),
                            may_exchange=True, may_trade=False)
            # Fill broker-side effects left before a previous process stopped.
            for number, order in list(broker.orders.items()):
                if order["state"] == "open":
                    broker.fill(number)
            for step in range(64):
                now = opened + timedelta(minutes=1, seconds=step)
                result = engine.tick(**common, now=now, may_trade=True)
                journal.event("result", {"message": result}, now)
                for number, order in list(broker.orders.items()):
                    if order["state"] == "open":
                        broker.fill(number)
                if result.startswith("idle:"):
                    break
            else:
                raise Stop("Synthetic workflow did not reach an idle state")
            closes.append(event["close"])
            previous_session = event["session"]
            # Durable session checkpoint; a repeat uses the same deposit IDs and
            # broker journal. End-of-session close is never used before trading.
            journal.put_meta("replay_next", index+1)
            journal.put_meta("health", dict(at=now.isoformat(), result=result, mode="paper"))
            # Exercise restart recovery every session, rather than carrying only
            # Python objects across the entire scenario.
            journal.db.close()
            journal = Journal(path/"journal.sqlite")
            broker = PersistentPaper(journal)
            engine = Engine(broker, journal, settings)
        processed = journal.meta("replay_next", 0)
        deposits = journal.db.execute("SELECT amount FROM paper_deposits").fetchall()
        report = dict(name=scenario.get("name", "offline"), mode="paper", network_used=False,
                      real_orders_sent=False, sessions_completed=processed,
                      sessions_total=len(scenario["sessions"]), complete=processed==len(scenario["sessions"]),
                      deposited_krw=str(sum((money(r[0]) for r in deposits), D(0))),
                      signal=journal.meta("signal"), final=vars(broker.snapshot()),
                      actions=journal.rows(), scenario_sha256=fingerprint)
        tmp = path/"replay.tmp"
        tmp.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2))
        tmp.replace(path/"replay.json")
        return report
    finally:
        journal.db.close()
