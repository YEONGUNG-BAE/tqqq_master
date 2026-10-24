import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autotrader.core import D, Engine, Journal, Settings, Stop, UnknownOutcome
from autotrader.replay import PersistentPaper, replay
from autotrader.signal import DEFENSIVE_SYMBOL
from test_automation import NOW


def scenario():
    return dict(name="irregular-deposits-and-switches", seed=dict(
        last_session="2026-10-14", closes=["100"]*252),
        initial=dict(krw="0", usd="0"), sessions=[
            dict(session="2026-10-15", open_at="2026-10-15T13:30:00+00:00", close="84", deposit_krw="1000000"),
            dict(session="2026-10-16", open_at="2026-10-16T13:30:00+00:00", close="86", deposit_krw="0"),
            dict(session="2026-10-19", open_at="2026-10-19T13:30:00+00:00", close="88", deposit_krw="300000"),
            dict(session="2026-10-20", open_at="2026-10-20T13:30:00+00:00", close="100", deposit_krw="0")])


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.input = self.root/"scenario.json"
        self.input.write_text(json.dumps(scenario()))

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_is_identical_to_uninterrupted_run_without_network(self):
        with patch("autotrader.meritz.urlopen", side_effect=AssertionError("Network forbidden")):
            whole = replay(self.input, self.root/"whole")
            first = replay(self.input, self.root/"resumed", max_sessions=2)
            self.assertEqual(first["sessions_completed"], 2)
            self.assertFalse(first["complete"])
            resumed = replay(self.input, self.root/"resumed")
        self.assertEqual(whole["final"], resumed["final"])
        self.assertEqual(resumed["deposited_krw"], "1300000")
        self.assertEqual(resumed["sessions_completed"], 4)
        self.assertTrue(resumed["complete"])
        self.assertEqual(len(whole["actions"]), len(resumed["actions"]))
        self.assertEqual(set(resumed["final"]["positions"]), {"TQQQ"})
        self.assertTrue(any(json.loads(a["payload"]).get("symbol")==DEFENSIVE_SYMBOL for a in resumed["actions"]))

    def test_repeated_finished_run_neither_deposits_nor_orders_again(self):
        a = replay(self.input, self.root/"state")
        b = replay(self.input, self.root/"state")
        self.assertEqual(a, b)

    def test_changed_scenario_cannot_reuse_state(self):
        replay(self.input, self.root/"state", max_sessions=1)
        changed = scenario()
        changed["sessions"][0]["deposit_krw"] = "2000000"
        self.input.write_text(json.dumps(changed))
        with self.assertRaises(Stop):
            replay(self.input, self.root/"state")

    def test_close_does_not_leak_into_same_session_signal(self):
        r = replay(self.input, self.root/"state", max_sessions=1)
        self.assertEqual(r["signal"]["target"], "TQQQ")
        self.assertEqual(r["signal"]["signal_date"], "2026-10-14")
        r = replay(self.input, self.root/"state", max_sessions=1)
        self.assertEqual(r["signal"]["target"], DEFENSIVE_SYMBOL)

    def test_persistent_deposit_idempotency(self):
        j = Journal(self.root/"j.sqlite")
        try:
            p = PersistentPaper(j)
            self.assertTrue(p.deposit("salary", "100000"))
            p = PersistentPaper(j)
            self.assertFalse(p.deposit("salary", "100000"))
            self.assertEqual(p.krw, D(100000))
            with self.assertRaises(Stop):
                p.deposit("salary", "200000")
        finally:
            j.db.close()

    def test_persistent_partial_fill_then_restart_then_full_fill(self):
        j = Journal(self.root/"j.sqlite")
        try:
            p = PersistentPaper(j, initial=dict(usd="1000"))
            r = p.order("TQQQ", "buy", D(10), D(70), NOW)
            p.fill(r["number"], 3)
            p = PersistentPaper(j)
            self.assertEqual(p.order_status(r).filled, D(3))
            p.fill(r["number"])
            self.assertEqual(p.positions["TQQQ"], D(10))
            self.assertEqual(p.usd, D("299.5100"))
        finally:
            j.db.close()

    def test_side_effect_before_lost_receipt_is_not_repeated(self):
        j = Journal(self.root/"j.sqlite")
        try:
            p = PersistentPaper(j, initial=dict(usd="1000"))
            engine = Engine(p, j, Settings(mode="paper"))
            submit = p.order
            def lost(*args):
                submit(*args)
                raise TimeoutError()
            p.order = lost
            args = dict(target="TQQQ", signal_date="2026-10-14", expected_signal_date="2026-10-14",
                        session="2026-10-15", now=NOW, may_trade=True)
            with self.assertRaises(UnknownOutcome):
                engine.tick(**args)
            p = PersistentPaper(j)
            engine = Engine(p, j, Settings(mode="paper"))
            # The persisted broker-side order is attributed after restart, not resent.
            self.assertEqual(engine.tick(**args), "recovered: order")
            self.assertEqual(engine.tick(**args), "waiting: order fill")
            self.assertEqual(len(p.orders), 1)
        finally:
            j.db.close()

    def test_paper_does_not_open_observer_state(self):
        j = Journal(self.root/"j.sqlite")
        try:
            j.put_meta("mode", "observe")
            with self.assertRaises(Stop):
                PersistentPaper(j)
        finally:
            j.db.close()


if __name__ == "__main__":
    unittest.main()
