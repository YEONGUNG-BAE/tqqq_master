"""Read-only diagnosis against fake Meritz responses; no network, no mutations."""
import csv
import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autotrader import __main__ as cli
from autotrader.core import D, Engine, FXQuote, Journal, Quote, ReservationLookup, Settings, Snapshot
from autotrader.diagnose import collect, observation_plan, sanitize
from autotrader.meritz import Client, KST, Meritz, NY
from autotrader.schedule import Schedule

ROOT = Path(__file__).resolve().parent.parent
# The official portal export is not redistributed with this repository (see
# README). When present locally, every fake request is checked against it.
SPEC = ROOT / "vendor/meritz-portal-20261015.json"
DOC = json.loads(SPEC.read_text()) if SPEC.exists() else {"groups": []}
APIS = {(a["method"], a["url"]): a for g in DOC["groups"] for a in g["apis"]}
KRW_MORNING = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)   # 10:00 KST, US closed
US_OPEN = datetime(2026, 10, 15, 13, 31, 10, tzinfo=timezone.utc)  # 1 minute after open
SECRET_ENV = {"MERITZ_CLIENT_ID": "client-id-DO-NOT-LEAK", "MERITZ_CLIENT_SECRET": "client-secret-DO-NOT-LEAK",
              "MERITZ_MAC_ADDRESS": "A1B2C3D4E5F6"}
TOKEN = "access-token-DO-NOT-LEAK"
with open(ROOT / "qqq_seed.csv", newline="") as f:
    SEED = {r["date"]: r["close"] for r in csv.DictReader(f)}
SEED["2026-10-14"] = "744.12"


class FakeMeritz:
    """Replaces Client._http. Checks GET fields against the official schema."""

    def __init__(self, now, **overrides):
        self.now, self.calls = now, []
        ny, kst = now.astimezone(NY), now.astimezone(KST)
        quote_time = ny.strftime("%H%M%S") if ny.hour >= 9 and ny.hour < 16 else "160000"
        self.closes = dict(SEED)
        # Market per symbol in this fake only. SGOV's AX is a test fixture, not a
        # verified Meritz route (the user confirms it with doctor's probe).
        self.markets = {"BRK.B": "NY", "SGOV": TEST_SGOV_MARKET}
        self.responses = {
            "/accounts/v1/me/deposit": dict(rsp_cd="5766", data=dict(
                tfam="1500000", drng_able_amt="1500000", oder_able_cash_amt="1500000", d2_aft_tfam="1500000",
                crdt_loan_amt="0", crdt_stln_amt="0", cash_unam="0", cash_unam_ovfe="0", nnrd_loan_amt="0",
                fee_unam="0")),
            "/accounts/v1/me/overseas/holdings": dict(rsp_cd="5766", data=[]),
            "/accounts/v1/me/overseas/currencies": dict(rsp_cd="5766", data=[dict(crcd="USD", frcr_amt="250.00", estm_frcr_amt="250.00")]),
            "/trading/v1/overseas/orders/estimate": dict(rsp_cd="5766", data=[dict(
                crcd="USD", stnd_deps_camt="250.00", frcr_amt="250.00", oder_crnc_oder_able_camt="250.00",
                camt="250.00", stnd_evdn_camt="0", othr_crnc_evdn_camt="0", exrt="1400")]),
            "/trading/v1/overseas/orders/today": dict(rsp_cd="5820", data=[]),
            "/trading/v1/overseas/reserved-orders": dict(rsp_cd="5820", data=[], tr_cont="0"),
            "/forex/v1/rates/applied": dict(rsp_cd="0000", data=dict(
                exrt_stnd_time=kst.strftime("%Y%m%d%H%M%S"), tltr_byrt="1400.0000", tltr_slrt="1400.0000",
                tltr_aply_byrt="1400.7000", tltr_aply_slrt="1399.3000", spead="0.7000", basc_spead="14.0000",
                dcrt="95.00000", cust_grad_code_name="test-grade", prft_aply_cls_val="")),
            # Documented shape: the current segment's KST HHMM bounds, 0000 when closed.
            "/reference/v1/overseas/market-hours": dict(rsp_cd="5766", data=dict(
                ovrs_stck_mrkt_tr_cls_code="01",
                **(dict(ovrs_mkop_cls_code="004", mrkt_begn_time=ny.replace(hour=9, minute=30).astimezone(KST).strftime("%H%M"),
                        mrkt_fin_time=ny.replace(hour=16, minute=0).astimezone(KST).strftime("%H%M"))
                   if quote_time != "160000" else dict(ovrs_mkop_cls_code="012", mrkt_begn_time="0000", mrkt_fin_time="0000")))),
            "/market/v1/overseas/orderbook": lambda q: dict(rsp_cd="0000", data=dict(
                acpl_hour=quote_time, bidp1=self.price(q["iscd"]), askp1=str(D(self.price(q["iscd"])) + D("0.01")))
                if self.listed(q) else dict(acpl_hour="000000", bidp1="0", askp1="0")),
            "/market/v1/overseas/prices": lambda q: dict(rsp_cd="0000", data=dict(
                iscd=q["iscd"], crnc_code="USD", prpr=self.price(q["iscd"]), prdy_clpr=self.price(q["iscd"]),
                issu_stat_cls_code="N", vcm_stat_cls_code="N", shnu_aspr_unit="0", seln_aspr_unit="0",
                sell_only="0", lvrg="3.0" if q["iscd"] == "TQQQ" else "1.0", shnu_oder_qty_unit="1",
                seln_oder_qty_unit="1", etf_yn="Y")
                # Documented: a wrong market answers 0000 with blank values.
                if self.listed(q) else dict(iscd="", crnc_code="", prpr="0", prdy_clpr="0")),
            "/market/v1/overseas/candles/days": lambda q: dict(rsp_cd="0000", data=[
                dict(date=d.replace("-", ""), prpr=p) for d, p in sorted(self.closes.items(), reverse=True)
                if q["from"] <= d.replace("-", "") <= q["to"]]),
        }
        self.responses.update(overrides)

    def listed(self, query):
        return query.get("mrkt_cls_code") == self.markets.get(query["iscd"], "OQ")

    @staticmethod
    def price(symbol):
        return {"TQQQ": "70.00", "QQQ": "600.00", "SGOV": "100.50", "QQQM": "250.00", "BRK.B": "480.00"}.get(symbol, "10.00")

    def __call__(self, method, path, query=None, body=None, *, token=None, form=False, paced=False):
        self.calls.append((method, path))
        if (method, path) == ("POST", "/oauth2/token"):
            return dict(access_token=TOKEN, expires_in="86400")
        assert method == "GET", f"Mutation attempted: {method} {path}"
        if not APIS:
            response = self.responses[path]
            return response(query) if callable(response) else self._filtered(path, query, response)
        fields = APIS[(method, path)]["trs"][0]["request"]["body"]
        allowed = {f["element"] for f in fields}
        required = {f["element"] for f in fields if f["required"] == "Y"}
        params = set(query or {})
        assert params <= allowed, f"Undocumented fields for {path}: {params - allowed}"
        assert required <= params, f"Missing fields for {path}: {required - params}"
        response = self.responses[path]
        if callable(response):
            return response(query)
        return self._filtered(path, query, response)

    @staticmethod
    def _filtered(path, query, response):
        response = json.loads(json.dumps(response))
        if path == "/trading/v1/overseas/reserved-orders" and isinstance(response.get("data"), list):
            # Documented broker filter: rows by reservation ACCEPTANCE date.
            response["data"] = [r for r in response["data"] if "rsev_acpt_date" not in r
                                or query["iqry_sta_date"] <= r["rsev_acpt_date"] <= query["iqry_fin_date"]]
        return response


TEST_SGOV_MARKET = "AX"


def holding(symbol, qty, kind="1"):
    return dict(ovrs_stck_symb_iscd=symbol, iscd=symbol + ".OQ", rmnd_dqty=str(qty), seln_able_dqty=str(qty),
                rmnd_clcd=kind, crcd="USD", loan_dtl_type_code="0000", avrg_unpr="1", psnt_prc="1", frcr_vltn_amt="1")


class Harness(unittest.TestCase):
    """Fake transport, network block and mutation guards; holds no tests."""
    schedules = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.journal = Journal(self.state / "journal.sqlite")
        self.env = patch.dict(os.environ, SECRET_ENV)
        self.env.start()
        # Any real HTTP attempt fails the test.
        self.net = patch("autotrader.meritz.urlopen", side_effect=AssertionError("network forbidden"))
        self.net.start()
        # Mutating adapter methods must never be reached from diagnostics.
        self.guards = [patch.object(Meritz, name, side_effect=AssertionError(name + " called"))
                       for name in ("exchange", "order", "cancel")]
        self.guard_mocks = [g.start() for g in self.guards]

    def tearDown(self):
        for g in self.guards:
            g.stop()
        # Diagnostics catch per-section errors, so check the guards directly.
        self.assertFalse(any(m.called for m in self.guard_mocks), "mutation method reached")
        self.net.stop()
        self.env.stop()
        self.journal.db.close()
        self.tmp.cleanup()

    def schedule(self, now):
        if now not in self.schedules:
            self.schedules[now] = Schedule(now)
        return self.schedules[now]

    def run_doctor(self, now=KRW_MORNING, cfg=None, settings=None, env_missing=False, **responses):
        fake = FakeMeritz(now, **responses)
        cfg = cfg or {"verified": {}}
        settings = settings or Settings()
        ctx = patch.dict(os.environ, {k: "" for k in SECRET_ENV}) if env_missing else patch.dict(os.environ, {})
        with ctx, patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)):
            report = collect(cfg=cfg, state=self.state, settings=settings, now=now, schedule=self.schedule(now),
                             journal=self.journal, root=ROOT,
                             client_factory=lambda: Client(self.state, allow_mutations=False),
                             broker_factory=lambda c: Meritz(c, self.journal, settings.instrument_markets))
        return report, fake

    def codes(self, report):
        return {b["code"] for b in report["blockers"]}


class DoctorTests(Harness):
    def test_full_diagnosis_sends_only_reads_and_token(self):
        report, fake = self.run_doctor()
        self.assertTrue(fake.calls)
        self.assertTrue(all(m == "GET" or p == "/oauth2/token" for m, p in fake.calls))
        self.assertEqual(report["auth"]["status"], "ok")
        self.assertEqual(report["signal"]["status"], "ok")
        self.assertEqual(report["signal"]["price_basis"]["overlap_check"], "passed")
        self.assertEqual(report["signal"]["target"], "TQQQ")
        self.assertFalse(report["send_policy"]["fx_orders_modify_cancel_sent"])
        self.assertEqual(report["account"]["krw"]["withdrawable"], "1500000")
        self.assertEqual(report["account"]["usd"]["order_available"], "250.00")
        self.assertEqual(report["fx"]["spread_krw"], "0.7000")
        self.assertEqual(report["fx"]["discount_pct"], "95.00000")
        plan = report["plan"]
        self.assertEqual(plan["fx"]["amount_krw"], "1500000")
        self.assertEqual(plan["fx"]["estimated_usd"], "1070.89")
        self.assertEqual(plan["buy"]["symbol"], "TQQQ")
        # Same function as engine: (250 / (70.08 * 1.0015)) -> 3 shares
        self.assertEqual(plan["buy"]["with_current_usd"]["quantity"], "3")
        self.assertIn("after_planned_fx_and_sales", plan["buy"])
        # US closed: quote is labelled indicative, not order-grade.
        self.assertNotEqual(plan["buy"]["engine_quote_check"], "pass")
        self.assertIn("indicative", plan["buy"]["price_source"])

    def test_existing_non_strategy_holdings_are_shown_and_never_sold(self):
        holdings = dict(rsp_cd="5766", data=[holding("QQQM", 29), holding("BRK.B", 4), holding("TQQQ", 10)])
        report, fake = self.run_doctor(**{"/accounts/v1/me/overseas/holdings": holdings})
        self.assertEqual({h["symbol"] for h in report["account"]["holdings"]}, {"QQQM", "BRK.B", "TQQQ"})
        self.assertIn("non_strategy_holdings", self.codes(report))
        self.assertEqual(report["holdings_state"]["non_strategy"], ["BRK.B", "QQQM"])
        self.assertEqual(report["holdings_state"]["actual_strategy_holding"], "TQQQ")
        self.assertTrue(report["holdings_state"]["aligned"])
        self.assertFalse(report["plan"]["engine_would_execute"])
        sells = {s["symbol"]: s["status"] for s in report["plan"]["switch_sells"]}
        self.assertTrue(all(v.startswith("left_untouched") for v in sells.values()))
        # No quotes requested for holdings the engine never trades.
        self.assertNotIn("QQQM", report["quotes"])

    def test_fractional_holding_does_not_hide_the_report(self):
        holdings = dict(rsp_cd="5766", data=[holding("QQQM", "0.5", kind="2")])
        report, _ = self.run_doctor(**{"/accounts/v1/me/overseas/holdings": holdings})
        self.assertEqual(report["account"]["holdings"][0]["balance_kind"], "fractional")
        self.assertIn("Unsupported currency/fractional/loan holding",
                      [b["message"] for b in report["blockers"]])
        self.assertIn("buy", report["plan"])

    def test_missing_usd_balance_row_marks_plan_not_computable(self):
        report, _ = self.run_doctor(**{"/trading/v1/overseas/orders/estimate": dict(rsp_cd="5820", data=[])})
        self.assertIn("usd.order_available", report["account"]["missing_fields"])
        self.assertIn("USD buying-power row missing", [b["message"] for b in report["blockers"]])
        self.assertNotIn("buy", report["plan"])
        self.assertTrue(report["plan"]["not_computable"])

    def test_missing_krw_field_is_listed(self):
        fake_deposit = dict(rsp_cd="5766", data=dict(tfam="1000", oder_able_cash_amt="1000"))
        report, _ = self.run_doctor(**{"/accounts/v1/me/deposit": fake_deposit})
        self.assertIn("krw.withdrawable", report["account"]["missing_fields"])
        self.assertIn("Required balance field missing: drng_able_amt", [b["message"] for b in report["blockers"]])
        self.assertNotIn("buy", report["plan"])

    def test_credit_debt_is_listed_and_blocks(self):
        deposit = FakeMeritz(KRW_MORNING).responses["/accounts/v1/me/deposit"]
        deposit["data"]["cash_unam"] = "12000"
        report, _ = self.run_doctor(**{"/accounts/v1/me/deposit": deposit})
        self.assertEqual(report["account"]["debts"], {"cash_unam": "12000"})
        self.assertIn("Credit/loan/unsettled cash debt found; cash-only dedicated account required",
                      [b["message"] for b in report["blockers"]])

    def test_dividend_adjusted_closes_block_signal_but_keep_fx_plan(self):
        adjusted = {d: str((D(p) * D("0.99")).quantize(D("0.0001"))) for d, p in SEED.items()}
        report, _ = self.run_doctor(**{"/market/v1/overseas/candles/days": lambda q: dict(rsp_cd="0000", data=[
            dict(date=d.replace("-", ""), prpr=p) for d, p in adjusted.items() if q["from"] <= d.replace("-", "") <= q["to"]])})
        self.assertEqual(report["signal"]["status"], "unverified")
        self.assertEqual(report["signal"]["price_basis"]["overlap_check"], "failed")
        self.assertIn("close basis differs", report["signal"]["unverified_reason"])
        self.assertIn("signal_unverified", self.codes(report))
        self.assertNotIn("buy", report["plan"])
        self.assertEqual(report["plan"]["fx"]["status"], "planned")

    def test_fees_are_not_verified_and_wide_fx_spread_is_a_blocker(self):
        fake = FakeMeritz(KRW_MORNING)
        rates = fake.responses["/forex/v1/rates/applied"]
        rates["data"].update(tltr_aply_byrt="1414.0000", spead="14.0000", dcrt="0.00000")
        report, _ = self.run_doctor(**{"/forex/v1/rates/applied": rates})
        self.assertEqual(report["fees"]["trading_commission"]["status"], "not_verified")
        self.assertEqual(report["fees"]["other_costs"]["status"], "not_verified")
        self.assertEqual(report["fees"]["fx_spread"]["status"], "observed_from_api")
        self.assertEqual(report["fx"]["engine_quote_check"], "Applied FX spread exceeds configured limit")
        self.assertIn("fx_quote_check", self.codes(report))

    def test_fresh_quote_during_window_is_order_grade(self):
        report, _ = self.run_doctor(now=US_OPEN)
        self.assertEqual(report["plan"]["buy"]["engine_quote_check"], "pass")
        self.assertEqual(report["plan"]["buy"]["price_source"], "quote")
        self.assertTrue(report["market"]["order_possible_now"]["engine_would_place_new_order"])
        self.assertFalse(report["market"]["order_possible_now"]["this_command_sends_orders"])

    def test_untracked_open_order_and_reservation_are_reported(self):
        today = dict(rsp_cd="5766", data=[dict(oder_date="20261015", oder_no="77", oder_rctf_cncl_cls_code="0",
                                               ovrs_stck_symb_iscd="TQQQ", seln_byov_clcd="2", odqt="1", cntg_qty="0",
                                               ovrs_stck_oder_prc="69", ovrs_stck_oder_stat_cls_code="0",
                                               oder_cfee="0", cntg_camt="0")])
        reserved = dict(rsp_cd="5766", tr_cont="0", data=[dict(ovrs_stck_symb_iscd="QQQ", seln_byov_clcd="2", odqt="1",
                                                               acml_cntg_qty="0", ovrs_stck_oder_prc="500",
                                                               rsev_aspr_tpcd="1", rsev_cncl_yn="",
                                                               rsev_acpt_date="20261015", rsev_acpt_srno="1")])
        report, _ = self.run_doctor(**{"/trading/v1/overseas/orders/today": today,
                                       "/trading/v1/overseas/reserved-orders": reserved})
        self.assertEqual(len(report["orders"]["open"]), 1)
        self.assertFalse(report["orders"]["open"][0]["tracked_by_engine"])
        self.assertIn("untracked_open_order", self.codes(report))
        reservation = [b for b in report["blockers"] if b["code"] == "reserved_orders"]
        self.assertEqual(len(reservation), 1)
        # Since the engine pre-check, reservations block the engine itself.
        self.assertTrue(reservation[0]["engine_checks"])
        self.assertNotIn("77", json.dumps(report["orders"]))

    def test_without_credentials_no_request_and_local_signal_unverified(self):
        report, fake = self.run_doctor(env_missing=True)
        self.assertEqual(fake.calls, [])
        self.assertEqual(report["auth"]["status"], "not_configured")
        self.assertEqual(report["signal"]["source"], "local_history_only")
        self.assertFalse(report["signal"]["usable_for_plan"])
        self.assertEqual(report["account"]["status"], "not_available")

    def test_mutation_capable_client_is_refused(self):
        fake = FakeMeritz(KRW_MORNING)
        with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)):
            report = collect(cfg={}, state=self.state, settings=Settings(), now=KRW_MORNING,
                             schedule=self.schedule(KRW_MORNING), journal=self.journal, root=ROOT,
                             client_factory=lambda: Client(self.state, allow_mutations=True),
                             broker_factory=lambda c: Meritz(c, self.journal, settings.instrument_markets))
        self.assertEqual(report["auth"]["status"], "error")
        self.assertEqual(fake.calls, [])

    def test_cli_doctor_with_live_config_is_read_only_and_redacted(self):
        root = self.state / "cli"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"state_dir": "state_live",
                                                       "execution": {"mode": "live"}, "verified": {}}))
        secrets = root / "secrets.env"
        secrets.write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(secrets, 0o600)
        fake = FakeMeritz(KRW_MORNING)
        out = io.StringIO()
        with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)), \
             patch.object(cli, "utcnow", return_value=KRW_MORNING), \
             patch("sys.argv", ["autotrader", "doctor", "--config", str(root / "config.json")]), \
             redirect_stdout(out):
            self.assertEqual(cli.main(), 0)
        self.assertTrue(all(m == "GET" or p == "/oauth2/token" for m, p in fake.calls))
        saved = root / "state_live/doctor/latest.json"
        if os.name != "nt":                      # Windows: ACLs, checked by oslock.acl_report
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        text = out.getvalue() + saved.read_text()
        for secret in list(SECRET_ENV.values()) + [TOKEN]:
            self.assertNotIn(secret, text)
        report = json.loads(saved.read_text())
        self.assertEqual(report["mode"], "live")
        self.assertIn("verification_incomplete", self.codes(report))
        # Doctor never records an execution mode, so the state is not claimed by an Engine.
        j = Journal(root / "state_live/journal.sqlite")
        self.assertIsNone(j.meta("mode"))
        j.db.close()


class PlanMatchesEngineTests(unittest.TestCase):
    """The observation plan and a real Engine tick must size identically."""

    class Stub:
        def __init__(self, snap, quote, fx):
            self.snap, self.q, self.fx, self.sent = snap, quote, fx, []

        def snapshot(self):
            return self.snap

        def quote(self, symbol, now):
            return self.q

        def fx_quote(self, now):
            return self.fx

        def active_reservations(self, now, since=None):
            return ReservationLookup([], complete=True)

        def exchange(self, krw, quote):
            self.sent.append(("fx", krw))
            return {"usd_after": "0"}

        def order(self, symbol, side, qty, price, now):
            self.sent.append((side, symbol, qty, price))
            return {"number": 1, "date": "20261015"}

    def check(self, usd, power, ask, krw="0", fee_buffer="0.0015", usd_reserve="0"):
        now = US_OPEN
        snap = Snapshot(D(krw), D(usd), D(power), {}, {})
        quote = Quote(D(ask) - D("0.02"), D(ask), D("0.01"), now)
        fx = FXQuote(D("1400.7"), D("1400"), now, "x")
        cfg = Settings(mode="paper", fee_buffer=fee_buffer, usd_reserve=usd_reserve)
        plan = observation_plan(snap=snap, target="TQQQ", cfg=cfg, fx_quote=fx, fx_error=None,
                                quotes={"TQQQ": (quote, {}, None)}, now=now, spent=D(0), blockers=[])
        with tempfile.TemporaryDirectory() as tmp:
            j = Journal(Path(tmp) / "j.sqlite")
            try:
                stub = self.Stub(snap, quote, fx)
                Engine(stub, j, cfg).tick(target="TQQQ", signal_date="2026-10-14", expected_signal_date="2026-10-14",
                                          session="2026-10-15", now=now, may_exchange=D(krw) > 0, may_trade=True)
            finally:
                j.db.close()
        return plan, stub.sent

    def test_buy_quantity_and_price_match(self):
        for usd, power, ask in [("250", "250", "70.01"), ("1000", "950", "70.01"), ("71.5", "71.5", "70.99"),
                                ("100000", "99999.99", "612.37")]:
            plan, sent = self.check(usd, power, ask)
            buy = plan["buy"]["with_current_usd"]
            if buy["quantity"] == "0":
                self.assertEqual(sent, [])
            else:
                self.assertEqual(sent, [("buy", "TQQQ", D(buy["quantity"]), D(plan["buy"]["limit_price"]))])

    def test_fx_amount_matches(self):
        plan, sent = self.check("0", "0", "70", krw="1234567")
        self.assertEqual(sent, [("fx", D(plan["fx"]["amount_krw"]))])

    def test_usd_reserve_and_fee_buffer_are_shared(self):
        plan, sent = self.check("500", "500", "70", fee_buffer="0.01", usd_reserve="100")
        self.assertEqual(sent[0][2], D(plan["buy"]["with_current_usd"]["quantity"]))


class SanitizeTests(unittest.TestCase):
    def test_drops_credential_keys_and_masks_values(self):
        raw = {"access_token": "abc", "nested": [{"client_secret": "x", "note": "key=SuperSecret1 used"}],
               "mac_address": "A1B2C3D4E5F6", "oder_no": 5, "ok": 1}
        clean = sanitize(raw, ["SuperSecret1"])
        self.assertEqual(clean, {"nested": [{"note": "key=[REDACTED] used"}], "ok": 1})


if __name__ == "__main__":
    unittest.main()


class RunObserveTests(Harness):
    """Documents what `run` in observe mode actually does, end to end."""

    def cli_run(self, now, execution=None, **responses):
        root = self.state / "cli"
        root.mkdir(exist_ok=True)
        (root / "config.json").write_text(json.dumps({"state_dir": "state_observe", "notifications": False,
                                                       "account_lock_dir": str(self.state / "locks"),
                                                       "execution": dict(mode="observe", **(execution or {}))}))
        secrets = root / "secrets.env"
        secrets.write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(secrets, 0o600)
        fake = FakeMeritz(now, **responses)
        out = io.StringIO()
        with patch.object(Client, "_http", lambda _self, *a, **k: fake(*a, **k)), \
             patch.object(cli, "datetime", wraps=datetime) as clock, \
             patch("sys.argv", ["autotrader", "run", "--config", str(root / "config.json")]), \
             redirect_stdout(out):
            clock.now.return_value = now
            code = cli.main()
        self.assertTrue(all(m == "GET" or p == "/oauth2/token" for m, p in fake.calls))
        j = Journal(root / "state_observe/journal.sqlite")
        try:
            plans = [json.loads(r[0]) for r in j.db.execute("SELECT body FROM events WHERE category='plan'")]
            self.assertEqual(j.rows(), [])  # observe never creates an action record
        finally:
            j.db.close()
        return code, json.loads(out.getvalue()), plans

    def test_korean_daytime_plans_fx_only(self):
        code, out, plans = self.cli_run(KRW_MORNING)
        self.assertEqual((code, out["result"]), (0, "observe: fx"))
        self.assertEqual([p["kind"] for p in plans], ["fx"])
        self.assertEqual(plans[0]["krw"], "1500000")

    def test_us_open_plans_order_from_current_usd_only(self):
        code, out, plans = self.cli_run(US_OPEN)
        self.assertEqual(out["result"], "observe: order")
        # The order plan uses USD already in the account, not pending KRW.
        self.assertEqual((plans[0]["symbol"], plans[0]["side"], plans[0]["qty"]), ("TQQQ", "buy", "3"))

    def test_non_strategy_holdings_stop_before_any_plan(self):
        holdings = dict(rsp_cd="5766", data=[holding("QQQM", 29)])
        code, out, plans = self.cli_run(KRW_MORNING, **{"/accounts/v1/me/overseas/holdings": holdings})
        self.assertEqual(code, 1)
        self.assertEqual(out["error"], "Existing holdings left untouched: QQQM")
        self.assertEqual(plans, [])
