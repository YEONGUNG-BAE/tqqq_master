"""`status` reads only local records and must run on any state folder."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autotrader import __main__ as cli


class StatusCommandTests(unittest.TestCase):
    def test_status_runs_and_reports_the_live_check_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps({"state_dir": "state", "notifications": False,
                                                          "account_lock_dir": str(root / "locks")}))
            out = io.StringIO()
            with patch("sys.argv", ["autotrader", "status", "--config", str(root / "config.json")]), \
                 redirect_stdout(out):
                self.assertEqual(cli.main(), 0)
            report = json.loads(out.getvalue())
            self.assertIn("health", report)
            self.assertIn("live_check", report)


if __name__ == "__main__":
    unittest.main()
