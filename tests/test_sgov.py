"""TQQQ <-> SGOV: the signal stays on QQQ closes; SGOV gets its own verified
Meritz route or is blocked; doctor separates signal and holdings. Fake
transports, temporary DBs, fixed clocks; no network. SGOV's market in these
fakes (TEST_SGOV_MARKET) is a fixture, not a verified broker route."""
import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader import instruments
from autotrader.core import D, Engine, Journal, Settings, Stop
from autotrader.diagnose import holdings_state
from autotrader.history import recompute, refresh
from autotrader.meritz import Client, Meritz
from autotrader.schedule import Schedule
from autotrader.signal import DEFENSIVE_SYMBOL, RISK_SYMBOL, SIGNAL_SYMBOL, STRATEGY_VERSION, decisions
from test_doctor import KRW_MORNING, SECRET_ENV, TEST_SGOV_MARKET, TOKEN, US_OPEN, FakeMeritz, Harness, holding

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests/fixtures/legacy_qqq_signal.json").read_text())
SIGNAL = dict(signal_date="2026-10-14", expected_signal_date="2026-10-14", session="2026-10-15")


class SignalPreservationTests(unittest.TestCase):
    def test_same_qqq_history_gives_the_same_switch_dates_and_expiry_flags(self):
        rows = list(csv.DictReader(open(ROOT / "qqq_seed.csv")))
        to_new = {"QQQ": DEFENSIVE_SYMBOL, "TQQQ": RISK_SYMBOL}
        for run in FIXTURE["runs"]:
            start = to_new[run["initial_target"]]
            got = [d for d in decisions([r["close"] for r in rows], start) if d.transition]
            expected = [(t["date"], "->".join(to_new[s] for s in t["transition"].split("->")), t["expiry_driven"])
                        for t in run["transitions"]]
            self.assertEqual([(rows[d.index]["date"], d.transition, d.expiry_driven) for d in got], expected)
            self.assertGreater(len(expected), 20)

    def test_symbols_are_defined_once_and_the_signal_is_qqq(self):
        self.assertEqual((SIGNAL_SYMBOL, RISK_SYMBOL, DEFENSIVE_SYMBOL), ("QQQ", "TQQQ", "SGOV"))
        self.assertNotEqual(SIGNAL_SYMBOL, DEFENSIVE_SYMBOL)

    def test_refresh_reads_only_qqq_closes_and_sgov_prices_cannot_move_the_signal(self):
        now = US_OPEN
        fake = FakeMeritz(now)
        calls = []
        original = fake.responses["/market/v1/overseas/candles/days"]

        def candles(q):
            calls.append((q["iscd"], q["mrkt_cls_code"]))
            return original(q)
        fake.responses["/market/v1/overseas/candles/days"] = candles
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "qqq_close.csv"
        path.write_text((ROOT / "qqq_seed.csv").read_text())
        journal = Journal(Path(tmp.name) / "j.sqlite")
        self.addCleanup(journal.db.close)
        schedule = Schedule(now)
        expected = schedule.expected_close(now)
        with patch.dict(os.environ, SECRET_ENV), patch.object(Client, "_http", lambda _s, *a, **k: fake(*a, **k)):
            broker = Meritz(Client(Path(tmp.name), allow_mutations=False), journal)
            first = refresh(broker, path, schedule, expected)
            fake.price = staticmethod(lambda symbol: "1.00" if symbol == "SGOV" else FakeMeritz.price(symbol))
            second = refresh(broker, path, schedule, expected)
        self.assertEqual({c for c in calls}, {(SIGNAL_SYMBOL, "OQ")})
        self.assertEqual((first["target"], first["drawdown"]), (second["target"], second["drawdown"]))
        self.assertEqual((first["strategy"], first["signal_symbol"]), (STRATEGY_VERSION, "QQQ"))
        self.assertEqual(recompute(path, expected)["target"], first["target"])


class RouteTests(unittest.TestCase):
    def test_reviewed_routes_and_documented_pairs(self):
        self.assertEqual(instruments.resolve("TQQQ").order_exchange, "0537")
        self.assertEqual(instruments.resolve("QQQ").quote_market, "OQ")
        self.assertEqual(instruments.resolve("BRK.B").order_exchange, "0321")
        self.assertEqual(instruments.resolve("SGOV", {"SGOV": "AX"}).order_exchange, "0066")
        self.assertEqual(instruments.resolve("SGOV", {"SGOV": "NY"}).order_exchange, "0321")

    def test_example_config_leaves_sgov_unconfirmed_and_migrates_nothing(self):
        example = json.loads((ROOT / "config.example.json").read_text())
        settings = Settings(**example["execution"])
        self.assertEqual((settings.instrument_markets, settings.migrate_symbols, settings.mode), ({}, [], "observe"))
        self.assertTrue(all(v is False for v in example["verified"].values()))

    def test_no_default_market_for_an_unconfirmed_instrument(self):
        with self.assertRaises(instruments.RouteUnverified):
            instruments.resolve("SGOV")
        with self.assertRaises(instruments.RouteUnverified):
            instruments.resolve("SGOV", {"SGOV": None})

    def test_overrides_cannot_redirect_reviewed_routes_or_use_unknown_codes(self):
        with self.assertRaises(ValueError):
            Settings(instrument_markets={"TQQQ": "AX"})
        with self.assertRaises(ValueError):
            Settings(instrument_markets={"SGOV": "ARCA"})
        with self.assertRaises(ValueError):
            Settings(instrument_markets={"sgov": "AX"})
        self.assertEqual(Settings(instrument_markets={"SGOV": "AX"}).instrument_markets, {"SGOV": "AX"})
        self.assertEqual(Settings(migrate_symbols=("QQQ",)).migrate_symbols, ("QQQ",))
        self.assertEqual(instruments.resolve("TQQQ", {"TQQQ": "AX"}).quote_market, "OQ")      # never redirected


class OrderFake(FakeMeritz):
    """Accepts order and cancel POSTs, keeps the ledger, records each body."""

    def __init__(self, now, today, side="buy"):
        super().__init__(now)
        self.today, self.ledger, self.bodies, self.queries = today, [], [], []
        self.responses["/trading/v1/overseas/orders/today"] = lambda q: dict(
            rsp_cd="5766" if self.ledger else "5820", data=[dict(r) for r in self.ledger])

    def __call__(self, method, path, query=None, body=None, *, token=None, form=False, paced=False):
        if path in ("/market/v1/overseas/prices", "/market/v1/overseas/orderbook"):
            self.queries.append((query["iscd"], query["mrkt_cls_code"]))
        if method == "POST" and path != "/oauth2/token":
            self.calls.append((method, path))
            self.bodies.append((path, dict(body)))
            number = str(len(self.ledger) + 1)
            side = "1" if path.endswith("/sell") else "2"
            self.ledger.append(dict(oder_date=self.today, oder_no=number, oder_rctf_cncl_cls_code="0",
                                    ovrs_stck_symb_iscd=body["ovrs_stck_symb_iscd"], seln_byov_clcd=side,
                                    odqt=str(body["odqt"]), cntg_qty="0", ovrs_stck_oder_stat_cls_code="0",
                                    cncl_cnfr_qty="0"))
            return dict(rsp_cd="0001", data=dict(warn_cls_code="0", oder_no=number))
        return super().__call__(method, path, query, body, token=token, form=form)


class MeritzBase(unittest.TestCase):
    """Fake transport and network block; holds no tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.journal = Journal(self.state / "j.sqlite")
        self.addCleanup(self.journal.db.close)
        env = patch.dict(os.environ, SECRET_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.net = patch("autotrader.meritz.urlopen", side_effect=AssertionError("network forbidden"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def broker(self, fake, markets):
        patcher = patch.object(Client, "_http", lambda _s, *a, **k: fake(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)
        client = Client(self.state, allow_mutations=True)
        client.sleep = lambda s: None
        return Meritz(client, self.journal, markets)


class MeritzSgovPathTests(MeritzBase):
    def test_sgov_quote_and_order_use_the_confirmed_market_and_its_documented_exchange(self):
        from autotrader.meritz import KST
        fake = OrderFake(US_OPEN, US_OPEN.astimezone(KST).strftime("%Y%m%d"))
        broker = self.broker(fake, {"SGOV": TEST_SGOV_MARKET})
        quote = broker.quote("SGOV", US_OPEN)
        self.assertEqual(quote.bid, D("100.50"))
        receipt = broker.order("SGOV", "buy", D(3), D("100.51"), US_OPEN)
        self.assertEqual(fake.bodies[-1][1]["ovrs_exch_vndr_idno"], instruments.US_ROUTES[TEST_SGOV_MARKET])
        self.assertEqual((receipt["market"], receipt["exchange"]), (TEST_SGOV_MARKET, "0066"))
        broker.order("TQQQ", "sell", D(1), D("69.99"), US_OPEN)
        self.assertEqual(fake.bodies[-1][1]["ovrs_exch_vndr_idno"], "0537")
        self.assertIn(("SGOV", TEST_SGOV_MARKET), fake.queries)
        self.assertNotIn(("SGOV", "OQ"), fake.queries)

    def test_unconfirmed_sgov_sends_nothing(self):
        fake = OrderFake(US_OPEN, "20261015")
        broker = self.broker(fake, {})
        with self.assertRaises(instruments.RouteUnverified):
            broker.quote("SGOV", US_OPEN)
        with self.assertRaises(instruments.RouteUnverified):
            broker.order("SGOV", "buy", D(1), D("100.51"), US_OPEN)
        self.assertEqual(fake.bodies, [])
        self.assertFalse([q for q in fake.queries if q[0] == "SGOV"])

    def test_wrong_market_unit_or_restriction_is_refused_without_a_fallback(self):
        fake = OrderFake(US_OPEN, "20261015")
        wrong = "NY" if TEST_SGOV_MARKET != "NY" else "OQ"
        with self.assertRaisesRegex(Stop, "mismatch"):
            self.broker(fake, {"SGOV": wrong}).quote("SGOV", US_OPEN)
        prices = fake.responses["/market/v1/overseas/prices"]
        for field, value, message in (("crnc_code", "EUR", "currency"), ("vcm_stat_cls_code", "Y", "restriction")):
            fake.responses["/market/v1/overseas/prices"] = (
                lambda q, f=field, v=value: dict(prices(q), data=dict(prices(q)["data"], **{f: v})))
            with self.assertRaisesRegex(Stop, message):
                self.broker(fake, {"SGOV": TEST_SGOV_MARKET}).quote("SGOV", US_OPEN)
        fake.responses["/market/v1/overseas/prices"] = prices


class EngineSgovTests(unittest.TestCase):
    """TQQQ -> SGOV -> TQQQ on the paper broker, restarting between runs."""

    def setUp(self):
        from test_fault_injection import FaultBroker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "j.sqlite"
        self.broker = FaultBroker(usd="0")
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.journal = None
        self.open()

    def open(self):
        if self.journal is not None:
            self.journal.db.close()
        self.journal = Journal(self.path)
        self.addCleanup(lambda: self.journal.db.close())
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper"))

    def tick(self, target, at):
        from datetime import timedelta
        return self.engine.tick(**dict(SIGNAL, target=target, now=US_OPEN + timedelta(seconds=at), may_trade=True))

    def settle(self, target, at):
        self.broker.fill(max(self.broker.orders))
        self.open()
        return self.tick(target, at)

    def test_round_trip_never_routes_through_qqq(self):
        self.assertEqual(self.tick(DEFENSIVE_SYMBOL, 0), "submitted: order")
        self.settle(DEFENSIVE_SYMBOL, 10)
        self.assertEqual(self.tick(DEFENSIVE_SYMBOL, 20), "submitted: order")
        self.settle(DEFENSIVE_SYMBOL, 30)
        self.assertEqual(set(self.broker.positions) - {s for s, q in self.broker.positions.items() if q == 0},
                         {DEFENSIVE_SYMBOL})
        self.assertEqual(self.tick(RISK_SYMBOL, 40), "submitted: order")
        self.settle(RISK_SYMBOL, 50)
        self.assertEqual(self.tick(RISK_SYMBOL, 60), "submitted: order")
        self.settle(RISK_SYMBOL, 70)
        symbols = [c[1] for c in self.broker.calls if c[0] in ("buy", "sell")]
        self.assertEqual(symbols, [RISK_SYMBOL, DEFENSIVE_SYMBOL, DEFENSIVE_SYMBOL, RISK_SYMBOL])
        self.assertNotIn("QQQ", symbols)
        payloads = [json.loads(r["payload"]) for r in self.journal.rows() if r["kind"] == "order"]
        self.assertTrue(all(p["strategy"] == STRATEGY_VERSION for p in payloads))


class DoctorSgovTests(Harness):
    def test_signal_and_holdings_are_shown_apart_and_the_route_probe_is_evidence_only(self):
        report, fake = self.run_doctor(now=US_OPEN, **{"/accounts/v1/me/overseas/holdings": dict(
            rsp_cd="5766", data=[holding("QQQ", 3)])})
        sig = report["signal"]
        self.assertEqual((sig["signal_symbol"], sig["defensive_symbol"]), ("QQQ", "SGOV"))
        self.assertIn("exit_to_defensive_at_or_below_qqq_close", sig)
        self.assertNotIn("switch_to_qqq_at_or_below", sig)
        route = report["routes"]["SGOV"]
        self.assertIsNone(route["configured"])
        self.assertEqual(route["probe_matches"], [TEST_SGOV_MARKET])
        self.assertIn("route_unverified", self.codes(report))
        hs = report["holdings_state"]
        self.assertEqual(hs["actual_strategy_holding"], "none")                # QQQ is not SGOV
        self.assertEqual(hs["legacy_defensive_holding"]["symbols"], ["QQQ"])
        self.assertIn("non_strategy_holdings", self.codes(report))

    def test_confirmed_route_is_shown_and_a_conflicting_one_is_flagged(self):
        report, _ = self.run_doctor(now=US_OPEN, settings=Settings(instrument_markets={"SGOV": TEST_SGOV_MARKET}))
        route = report["routes"]["SGOV"]
        self.assertEqual(route["configured"]["order_exchange"], instruments.US_ROUTES[TEST_SGOV_MARKET])
        self.assertNotIn("route_unverified", self.codes(report))
        wrong = "NY" if TEST_SGOV_MARKET != "NY" else "OQ"
        report, _ = self.run_doctor(now=US_OPEN, settings=Settings(instrument_markets={"SGOV": wrong}))
        self.assertIn("route_conflict", self.codes(report))

    def test_holdings_state_never_counts_qqq_as_the_defensive_holding(self):
        report = {"account": {"holdings": [{"symbol": "QQQ"}, {"symbol": "SGOV"}]}}
        state = holdings_state(report, {"target": "SGOV"})
        self.assertEqual((state["actual_strategy_holding"], state["aligned"]), ("SGOV", True))
        self.assertEqual(state["non_strategy"], ["QQQ"])


if __name__ == "__main__":
    unittest.main()


class SellOnlyTests(unittest.TestCase):
    """`sell_only` (spec: 매도만 가능한 종목) blocks buys only; the exit sell
    and the return sell still go out. Real halts still stop both sides."""

    def setUp(self):
        from test_fault_injection import FaultBroker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "j.sqlite")
        self.addCleanup(self.journal.db.close)
        self.broker = FaultBroker(usd="2000")
        self.engine = Engine(self.broker, self.journal, Settings(mode="paper"))
        self.sell_only = set()
        original = self.broker.quote

        def quote(symbol, now):
            q = original(symbol, now)
            if symbol in self.sell_only:
                q.blocked = (("buy", "sell-only at the broker"),)
            return q
        self.broker.quote = quote

    def tick(self, target, at=0, **kw):
        from datetime import timedelta
        args = dict(SIGNAL, target=target, now=US_OPEN + timedelta(seconds=at), may_trade=True)
        args.update(kw)
        return self.engine.tick(**args)

    def test_tqqq_exit_sell_goes_out_and_the_sgov_buy_is_held(self):
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.sell_only = {RISK_SYMBOL, DEFENSIVE_SYMBOL}
        self.assertEqual(self.tick(DEFENSIVE_SYMBOL), "submitted: order")
        self.assertEqual(self.broker.calls[-1], ("sell", RISK_SYMBOL, D(10)))
        self.broker.fill(max(self.broker.orders))
        self.tick(DEFENSIVE_SYMBOL, 10)
        with self.assertRaisesRegex(Stop, "buy blocked: sell-only"):
            self.tick(DEFENSIVE_SYMBOL, 20)
        self.assertEqual([c for c in self.broker.calls if c[0] == "buy"], [])

    def test_sgov_return_sell_goes_out_and_the_tqqq_buy_is_held(self):
        self.broker.positions = {DEFENSIVE_SYMBOL: D(10)}
        self.sell_only = {RISK_SYMBOL, DEFENSIVE_SYMBOL}
        self.assertEqual(self.tick(RISK_SYMBOL), "submitted: order")
        self.assertEqual(self.broker.calls[-1], ("sell", DEFENSIVE_SYMBOL, D(10)))
        self.broker.fill(max(self.broker.orders))
        self.tick(RISK_SYMBOL, 10)
        with self.assertRaisesRegex(Stop, "buy blocked: sell-only"):
            self.tick(RISK_SYMBOL, 20)
        self.assertEqual([c for c in self.broker.calls if c[0] == "buy"], [])

    def test_intraday_recovery_sell_is_allowed_while_sell_only(self):
        from datetime import timedelta
        self.broker.positions = {RISK_SYMBOL: D(10)}
        self.sell_only = {RISK_SYMBOL}
        recovery = dict(may_trade=False, may_manage_orders=True, may_recover=True,
                        recovery_deadline=US_OPEN + timedelta(hours=6))
        self.assertEqual(self.tick(DEFENSIVE_SYMBOL, 1000, **recovery), "submitted: order")
        self.assertEqual(self.broker.calls[-1][:2], ("sell", RISK_SYMBOL))


class MeritzSellOnlyTests(MeritzBase):
    def with_fields(self, fake, **fields):
        prices = fake.responses["/market/v1/overseas/prices"]
        fake.responses["/market/v1/overseas/prices"] = lambda q: dict(prices(q), data=dict(prices(q)["data"], **fields))

    def test_sell_only_and_units_are_per_side_and_halts_stop_both(self):
        from autotrader.core import check_side
        fake = OrderFake(US_OPEN, "20261015")
        self.with_fields(fake, sell_only="1")
        quote = self.broker(fake, {"SGOV": TEST_SGOV_MARKET}).quote(RISK_SYMBOL, US_OPEN)
        check_side(quote, "sell")
        with self.assertRaisesRegex(Stop, "buy blocked: sell-only"):
            check_side(quote, "buy")
        fake = OrderFake(US_OPEN, "20261015")
        self.with_fields(fake, seln_oder_qty_unit="10")
        quote = self.broker(fake, {}).quote(RISK_SYMBOL, US_OPEN)
        check_side(quote, "buy")
        with self.assertRaisesRegex(Stop, "sell blocked"):
            check_side(quote, "sell")
        fake = OrderFake(US_OPEN, "20261015")
        self.with_fields(fake, issu_stat_cls_code="S")
        with self.assertRaisesRegex(Stop, "trading restriction"):
            self.broker(fake, {}).quote(RISK_SYMBOL, US_OPEN)

    def test_live_path_sells_the_held_tqqq_when_it_is_sell_only(self):
        from autotrader.meritz import KST
        fake = OrderFake(US_OPEN, US_OPEN.astimezone(KST).strftime("%Y%m%d"))
        fake.responses["/accounts/v1/me/overseas/holdings"] = dict(rsp_cd="5766", data=[holding(RISK_SYMBOL, 10)])
        self.with_fields(fake, sell_only="1")
        engine = Engine(self.broker(fake, {"SGOV": TEST_SGOV_MARKET}), self.journal,
                        Settings(mode="live", reservation_lookup_start="20260101",
                                 instrument_markets={"SGOV": TEST_SGOV_MARKET}))
        result = engine.tick(**dict(SIGNAL, target=DEFENSIVE_SYMBOL, now=US_OPEN, may_trade=True))
        self.assertEqual(result, "submitted: order")
        path, body = fake.bodies[-1]
        self.assertEqual((path, body["ovrs_stck_symb_iscd"], body["odqt"]),
                         ("/trading/v1/overseas/orders/sell", RISK_SYMBOL, 10))


class DoctorSellOnlyTests(Harness):
    def test_plan_shows_the_buy_held_and_the_sell_planned_when_sell_only(self):
        fake_prices = FakeMeritz(US_OPEN).responses["/market/v1/overseas/prices"]
        report, _ = self.run_doctor(now=US_OPEN, **{
            "/accounts/v1/me/overseas/holdings": dict(rsp_cd="5766", data=[holding(RISK_SYMBOL, 10)]),
            "/market/v1/overseas/prices": lambda q: dict(fake_prices(q), data=dict(fake_prices(q)["data"], sell_only="1"))})
        plan = report["plan"]
        self.assertEqual(plan["target"], RISK_SYMBOL)                          # fixture signal: hold TQQQ
        self.assertIn("buy blocked: sell-only", plan["buy"]["engine_quote_check"])
