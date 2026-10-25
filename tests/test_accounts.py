"""Per-installation account independence: the loaded key, the config and the
journal must agree before any request; journals with records are never
adopted silently; a reissued key is relinked only on the user's statement;
existing journals keep working; aliases never carry account numbers; locks
and token issue are per installation and safe across real processes."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autotrader import __main__ as cli
from autotrader import account, oslock
from autotrader.core import Journal, Stop
from autotrader.meritz import Client, Meritz
from autotrader.notify import Notifier
from test_doctor import SECRET_ENV, US_OPEN, FakeMeritz

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
MY_FP = account.fingerprint(SECRET_ENV["MERITZ_CLIENT_ID"])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.journal = Journal(self.dir / "j.sqlite")
        self.addCleanup(lambda: self.journal.db.close())
        env = patch.dict(os.environ, SECRET_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.net = patch("autotrader.meritz.urlopen", side_effect=AssertionError("network forbidden"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def broker(self, account_cfg=None):
        fake = FakeMeritz(US_OPEN)
        self.calls = fake.calls
        with patch.object(Client, "_http", lambda _s, *a, **k: fake(*a, **k)):
            return Meritz(Client(self.dir, allow_mutations=False), self.journal, account_cfg=account_cfg)


class BindingTests(Base):
    def test_a_config_for_another_account_is_refused_before_any_request(self):
        with self.assertRaisesRegex(Stop, "another account"):
            self.broker({"key_fingerprint": "0" * 16})
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.journal.meta("broker_key_fingerprint"))

    def test_a_new_journal_binds_and_the_matching_config_passes(self):
        self.broker({"key_fingerprint": MY_FP[:16]})
        self.assertEqual(self.journal.meta("account_binding")["source"], "first use of a new journal")
        self.assertEqual(self.journal.meta("broker_key_fingerprint"), MY_FP)

    def test_a_journal_of_another_key_is_refused(self):
        self.journal.put_meta("broker_key_fingerprint", "f" * 64)
        with self.assertRaisesRegex(Stop, "different API key"):
            self.broker()
        self.assertEqual(self.calls, [])

    def test_records_without_a_binding_are_never_adopted_silently(self):
        self.journal.create("order", {"symbol": "TQQQ"}, NOW, "2026-10-23", status="done")
        with self.assertRaisesRegex(Stop, "account link --confirm"):
            self.broker()
        account.link(self.journal, MY_FP, NOW)
        self.broker()
        self.assertEqual(self.journal.meta("account_binding")["source"], "user confirmed (account link)")

    def test_relink_is_a_user_statement_audited_and_refused_while_a_request_is_open(self):
        self.journal.put_meta("broker_key_fingerprint", "a" * 64)
        self.journal.create("order", {"symbol": "TQQQ"}, NOW, "2026-10-23")        # unresolved
        with self.assertRaisesRegex(Stop, "unresolved"):
            account.relink(self.journal, MY_FP, NOW, "issued a new key in the Meritz portal")
        self.journal.db.execute("UPDATE actions SET status='done', inflight=NULL")
        self.journal.db.commit()
        with self.assertRaisesRegex(Stop, "--note"):
            account.relink(self.journal, MY_FP, NOW, "")
        self.assertTrue(account.relink(self.journal, MY_FP, NOW, "issued a new key in the Meritz portal"))
        self.broker()
        event = self.journal.db.execute("SELECT body FROM events WHERE category='account_relinked'").fetchone()
        self.assertEqual(json.loads(event[0])["previous"], "a" * 16)

    def test_a_journal_from_before_bindings_keeps_working(self):
        # Built here, never copied from a user's state folder: older versions
        # stored only broker_key_fingerprint next to their records.
        path = self.dir / "older.sqlite"
        old = Journal(path)
        old.put_meta("broker_key_fingerprint", MY_FP)
        old.create("order", {"symbol": "TQQQ"}, NOW, "2026-10-23", status="done")
        old.db.close()
        j = Journal(path)
        try:
            self.assertEqual(account.bind(j, MY_FP, None, NOW)["source"], "key fingerprint already in the journal")
            self.assertEqual(j.db.execute("SELECT count(*) FROM actions").fetchone()[0], 1)
            with self.assertRaisesRegex(Stop, "different API key"):
                account.bind(j, "f" * 64, None, NOW)
        finally:
            j.db.close()


class AliasTests(Base):
    def test_aliases_are_names_never_numbers(self):
        self.assertEqual(account.validate_alias(" 아버지 계좌 "), "아버지 계좌")
        for bad in ("계좌 12345678", "dad@example.com", "", "가" * 30):
            with self.assertRaises(ValueError):
                account.validate_alias(bad)
        with self.assertRaises(ValueError):
            account.validate_config({"key_fingerprint": "XYZ"})

    def test_notifications_and_status_name_the_installation(self):
        from autotrader import ops
        self.journal.put_meta("account_alias", "아버지 계좌")
        sent = []
        Notifier(self.journal, channels={"telegram": sent.append}).send("hold", "x", "보류", NOW)
        self.assertTrue(sent[0].startswith("[보류 · 아버지 계좌]"))
        self.assertTrue(ops.status_lines(self.journal.db, NOW)[0].startswith("[상태 · 아버지 계좌]"))

    def test_account_show_reports_scope_without_secrets(self):
        root = self.dir / "inst"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"state_dir": "state", "notifications": False,
                                                       "account": {"alias": "내 계좌", "key_fingerprint": MY_FP[:16]}}))
        (root / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in SECRET_ENV.items()))
        os.chmod(root / "secrets.env", 0o600)
        out = io.StringIO()
        with patch("sys.argv", ["autotrader", "account", "show", "--config", str(root / "config.json")]), \
             redirect_stdout(out):
            self.assertEqual(cli.main(), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["loaded_key_fingerprint"], MY_FP[:16])
        self.assertIn("no account number", report["verified_scope"])
        for value in SECRET_ENV.values():
            self.assertNotIn(value, out.getvalue())


HOLD = r'''
import os, sys, time
sys.path.insert(0, {root!r})
from autotrader import __main__ as cli, oslock
os.environ["MERITZ_CLIENT_ID"] = sys.argv[1]
with oslock.held(cli.account_lock_path({{"account_lock_dir": sys.argv[2]}})):
    open(sys.argv[3], "w").write("held")
    time.sleep(30)
'''

TOKEN = r'''
import os, sys, time, json
sys.path.insert(0, {root!r})
from autotrader.meritz import Client
os.environ.update(json.loads(sys.argv[2]))
def fake(self, method, path, query=None, body=None, *, token=None, form=False, paced=False):
    with open(sys.argv[3], "a") as f:
        f.write("issue\n")
    time.sleep(0.5)
    return {{"access_token": "tok-" + str(os.getpid()), "expires_in": "3600"}}
Client._http = fake
print(Client(sys.argv[1]).token())
'''


class ProcessTests(Base):
    def test_two_installations_with_different_keys_never_lock_each_other(self):
        locks, flag = self.dir / "locks", self.dir / "flag"
        child = subprocess.Popen([sys.executable, "-c", HOLD.format(root=str(ROOT)), "father-key", str(locks), str(flag)])
        try:
            for _ in range(200):
                if flag.exists():
                    break
                import time
                time.sleep(0.05)
            with patch.dict(os.environ, {"MERITZ_CLIENT_ID": "my-key"}):
                mine = cli.account_lock_path({"account_lock_dir": str(locks)})
            with patch.dict(os.environ, {"MERITZ_CLIENT_ID": "father-key"}):
                fathers = cli.account_lock_path({"account_lock_dir": str(locks)})
            self.assertNotEqual(mine, fathers)
            with oslock.held(mine):
                pass                                          # mine is free while father's is held
            with self.assertRaises(oslock.Busy):
                with oslock.held(fathers):
                    pass                                      # the same account stays exclusive
        finally:
            child.kill()
            child.wait(10)

    def test_concurrent_token_issue_in_two_processes_issues_once(self):
        state, issues = self.dir / "state", self.dir / "issues.txt"
        state.mkdir()
        env = json.dumps(SECRET_ENV)
        procs = [subprocess.Popen([sys.executable, "-c", TOKEN.format(root=str(ROOT)), str(state), env, str(issues)],
                                  stdout=subprocess.PIPE, text=True) for _ in range(2)]
        tokens = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(issues.read_text().count("issue"), 1)
        self.assertEqual(len(set(tokens)), 1)


if __name__ == "__main__":
    unittest.main()
