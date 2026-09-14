import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/scorebench/scripts"
sys.path.insert(0, str(SCRIPTS))
from openrouter_accounting import AccountingError, Publisher
import openrouter_run as runner


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "usage.jsonl"
        self.log.touch()
        self.state = self.root / "state.json"
        self.env = {**os.environ, "SCOREBENCH_OPENROUTER_LOG": str(self.log),
                    "SCOREBENCH_TOKEN_STATE": str(self.state)}
        self.publisher = Publisher(self.root, self.env)

    def test_baseline_is_pinned_and_cannot_be_reset_by_model(self):
        # Run the actual helper but capture the publication at the CLI boundary.
        original = subprocess.run
        publications = []
        def run(command, **kwargs):
            if command[:3] == ["scorebench", "run", "usage"]:
                publications.append(command)
                return subprocess.CompletedProcess(command, 0, "{}", "")
            return original(command, **kwargs)
        with mock.patch("openrouter_accounting.subprocess.run", side_effect=run):
            self.publisher.initialize()
            baseline = self.state.read_bytes()
            self.log.write_text(json.dumps({"id": "gen1", "usage": {
                "prompt_tokens": 100, "completion_tokens": 20, "cost": 0.01}}) + "\n")
            self.publisher.helper("start")
            self.assertEqual(baseline, self.state.read_bytes())
            self.publisher.publish()
            self.publisher.publish()
        self.assertEqual(len(publications), 2)
        last = publications[-1]
        self.assertEqual(last[last.index("--cost-usd") + 1], "0.01")
        self.assertEqual(last[last.index("--total-tokens") + 1], "120")

    def test_nonempty_ledger_cannot_be_reset(self):
        self.log.write_text('{"usage":{"prompt_tokens":5,"completion_tokens":1}}\n')
        with self.assertRaisesRegex(AccountingError, "nonempty"):
            self.publisher.initialize()
        self.assertFalse(self.state.exists())

    def test_missing_cost_is_not_published(self):
        with mock.patch.object(self.publisher, "helper", return_value="--total-tokens 10"), \
             mock.patch("openrouter_accounting.subprocess.run") as post:
            with self.assertRaisesRegex(AccountingError, "omitted billed cost"):
                self.publisher.publish()
            post.assert_not_called()

    def test_failed_publication_retries_without_advancing_watermark(self):
        with mock.patch.object(self.publisher, "helper", return_value="--total-tokens 10 --cost-usd 0.01"), \
             mock.patch("openrouter_accounting.subprocess.run", side_effect=[
                 subprocess.TimeoutExpired("scorebench", 15),
                 subprocess.CompletedProcess("scorebench", 0, "{}", ""),
             ]) as post:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.publisher.publish()
            self.assertIsNone(self.publisher.last_flags)
            self.publisher.publish()
            self.assertEqual(post.call_count, 2)

    def test_watchdog_survives_exception_then_stops_at_budget(self):
        publisher = mock.Mock()
        publisher.publish.side_effect = [RuntimeError("transient"), None]
        with mock.patch.object(runner, "_runtime_control_reason", side_effect=[TimeoutError(), "budget_reached"]):
            code = runner._run_supervised([sys.executable, "-c", "import time; time.sleep(60)"],
                workspace=self.root, env={**self.env, "SCOREBENCH_RUNTIME_CONTROL_POLL_SECONDS": "0.05"},
                publisher=publisher)
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads((self.root / ".scorebench/runtime-control.json").read_text())["reason"], "budget_reached")

    def test_repeated_accounting_failures_stop_instead_of_running_unmetered(self):
        publisher = mock.Mock()
        publisher.publish.side_effect = RuntimeError("missing usage")
        with mock.patch.object(runner, "_runtime_control_reason", return_value=None):
            code = runner._run_supervised([sys.executable, "-c", "import time; time.sleep(60)"],
                workspace=self.root, env={**self.env, "SCOREBENCH_RUNTIME_CONTROL_POLL_SECONDS": "0.05"},
                publisher=publisher)
        self.assertNotEqual(code, 0)
        self.assertEqual(publisher.publish.call_count, 3)
        self.assertEqual(json.loads((self.root / ".scorebench/runtime-control.json").read_text())["reason"], "accounting_unavailable")

    def test_completion_requires_server_acceptance_even_after_clean_exit(self):
        with mock.patch("openrouter_accounting.subprocess.run", side_effect=[
            subprocess.CompletedProcess("scorebench", 1, "", "HTTP 400: budget not reached"),
            subprocess.CompletedProcess("scorebench", 0, "{}", ""),
        ]) as post:
            self.assertEqual(self.publisher.finalize(0, accounting_ok=True), 1)
        self.assertIn("finish", post.call_args_list[0].args[0])
        self.assertIn("failed", post.call_args_list[1].args[0])
        self.assertFalse(json.loads((self.root / "result.json").read_text())["completion_confirmed"])

    def test_controlled_budget_stop_finishes_after_reconciliation(self):
        control = self.root / ".scorebench/runtime-control.json"
        control.parent.mkdir()
        control.write_text('{"reason":"budget_reached"}')
        with mock.patch("openrouter_accounting.subprocess.run", return_value=subprocess.CompletedProcess("scorebench", 0, "{}", "")) as post:
            self.assertEqual(self.publisher.finalize(-15, accounting_ok=True), 0)
        self.assertIn("finish", post.call_args.args[0])
        with mock.patch("openrouter_accounting.subprocess.run", return_value=subprocess.CompletedProcess("scorebench", 0, "{}", "")) as post:
            self.assertEqual(self.publisher.finalize(-15, accounting_ok=False), 1)
        self.assertIn("failed", post.call_args.args[0])
