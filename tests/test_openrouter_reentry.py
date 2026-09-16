import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/scorebench/scripts"))
from openrouter_accounting import AccountingError
from openrouter_reentry import SessionOutput, admit_reentry, inspect_session, resume_command

COMMAND = ["opencode", "run", "--pure", "--auto", "--model", "openrouter/a/b",
           "--variant", "high", "--format", "json", "original goal"]


class ReentryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / ".scorebench/openrouter"
        self.directory.mkdir(parents=True)
        self.assignment = {"run": {"run_id": "run-1", "metadata": {
            "coding_harness": "OpenCode", "model": "openrouter/a/b", "effort": "high",
            "completion_policy": "budget_or_target"}}}
        (self.root / ".scorebench/supervisor-run-start.json").write_text(json.dumps(self.assignment))
        self.progress = {"scope": {"kind": "run_token"}, "run": {"run_id": "run-1", "status": "failed"},
                         "progress": {"budget": {"available": True, "reached": False,
                            "remaining": 0.8, "type": "cost", "target": 1.0}}}
        self.publisher = mock.Mock(workspace=self.root, log=self.directory / "usage.jsonl", env={})
        self.publisher.read.side_effect = lambda name: {"progress": self.progress, "current": self.assignment,
                                                       "gate": {"ready": True}}[name]
        self.publisher.ping.return_value = True
        self.metadata = {"resumeCostUpperBound": 0.1}
        self.session = {"info": {"id": "ses_probe", "directory": str(self.root)}, "messages": [
            {"info": {"role": "assistant", "providerID": "openrouter", "modelID": "a/b", "finish": "length"}}]}

    def admit(self, **kwargs):
        with mock.patch("openrouter_reentry.inspect_session", return_value="length"):
            return admit_reentry(self.publisher, self.metadata, COMMAND, "ses_probe", **kwargs)

    def export(self, *args, **kwargs):
        kwargs["stdout"].write(json.dumps(self.session).encode())
        return subprocess.CompletedProcess(args, 0, stderr=b"")

    def test_resume_reuses_exact_session_and_preserves_options(self):
        cmd = resume_command(COMMAND, "ses_probe")
        self.assertEqual(cmd[:10], COMMAND[:10])
        self.assertNotIn("original goal", cmd)
        self.assertEqual(cmd[-3:-1], ["--session", "ses_probe"])
        for args in (["--fork"], ["--continue"], ["--session", "ses_wrong"], ["--dir", "/other"]):
            with self.subTest(args=args), self.assertRaises(AccountingError):
                resume_command([*COMMAND[:-1], *args, COMMAND[-1]], "ses_probe")
        with self.assertRaises(AccountingError):
            resume_command(COMMAND, "invalid")
        resumed = resume_command(COMMAND, "ses_probe", finish_reason="stop")
        self.assertIn("compaction", resumed[-1])
        self.assertNotIn("output limit", resumed[-1])
        self.assertNotIn("original goal", resumed)

    def test_parses_only_structured_root_terminal_events_not_tool_or_prompt_text(self):
        output = SessionOutput(self.directory)
        for event in ({"type": "text", "sessionID": "ses_other", "part": {"text": '"reason":"length"'}},
                      {"type": "tool_use", "sessionID": "ses_other", "part": {"reason": "length"}},
                      "step_finish length", []):
            output.observe(json.dumps(event).encode())
        self.assertEqual(output.session, "")
        output.observe(b'{"type":"step_finish","sessionID":"ses_probe","part":{"reason":"length"}}')
        self.assertEqual((output.session, output.reason, output.error), ("ses_probe", "length", False))
        output.observe(b'{"type":"error","sessionID":"ses_probe","error":{"message":"401"}}')
        self.assertTrue(output.error)

    def test_other_session_cannot_replace_bound_session(self):
        output = SessionOutput(self.directory, "ses_probe")
        output.observe(b'{"type":"step_finish","sessionID":"ses_other","part":{"reason":"length"}}')
        self.assertEqual(output.session, "ses_probe")
        self.assertTrue(output.error)

    def test_admission_records_three_attempts_and_refuses_fourth(self):
        for i in range(3):
            self.assertEqual(len(self.admit()["attempts"]), i + 1)
        with self.assertRaisesRegex(AccountingError, "limit"):
            self.admit()
        self.assertEqual(self.publisher.ping.call_count, 3)
        self.assertEqual((self.directory / "reentries.json").stat().st_mode & 0o777, 0o600)

    def test_read_only_check_does_not_publish_reopen_or_consume_attempt(self):
        result = self.admit(manual=True, check=True)
        self.assertTrue(result["recoverable"])
        self.publisher.publish.assert_not_called()
        self.publisher.ping.assert_not_called()
        self.assertFalse((self.directory / "reentries.json").exists())

    def test_recovery_refuses_inconclusive_exhausted_unlimited_or_tiny_budget(self):
        for change in ({"available": False}, {"reached": True}, {"remaining": 0}, {"remaining": None},
                       {"remaining": float("nan")}, {"remaining": 0.01}, {"type": "none"}):
            with self.subTest(change=change):
                budget = self.progress["progress"]["budget"]
                old = dict(budget)
                budget.update(change)
                with self.assertRaises(AccountingError):
                    self.admit()
                budget.clear()
                budget.update(old)
        self.publisher.ping.assert_not_called()

    def test_recorded_budget_admits_reported_case_without_changing_budget(self):
        budget = self.progress["progress"]["budget"]
        budget.update(remaining=.2148, used=.7852, continuation={"policy": "cost-tail-v1", "admission": "recorded_budget"})
        self.metadata["resumeCostUpperBound"] = .2903
        before = dict(budget)
        result = self.admit(check=True)
        self.assertEqual(result["admission_method"], "recorded_budget")
        self.assertNotIn("continuation_allowance_usd", result)
        self.assertEqual(budget, before)
        for i in range(3):
            self.assertEqual(len(self.admit()["attempts"]), i + 1)
        with self.assertRaisesRegex(AccountingError, "limit"):
            self.admit()

    def test_recorded_budget_never_waives_unknown_cost(self):
        budget = self.progress["progress"]["budget"]
        budget.update(remaining=.2148, continuation={"policy": "cost-tail-v1", "admission": "recorded_budget"})
        self.metadata["resumeCostUpperBound"] = .01
        # Explicit partial recovery keeps its conservative admission check.
        with self.assertRaises(AccountingError):
            self.metadata["resumeCostUpperBound"] = .2903
            self.admit(accounting_gap=True)
        self.metadata["resumeCostUpperBound"] = .01
        for change in ({"accounting_complete": False},
                       {"used_is_lower_bound": True}, {"remaining_is_upper_bound": True},
                       {"reached": True}, {"target": True}, {"target": float("inf")},
                       {"remaining": 0}, {"remaining": 2}, {"remaining": float("nan")},
                       {"continuation": {"policy": "cost-tail-v1", "admission": "unknown"}}):
            old = dict(budget)
            budget.update(change)
            with self.subTest(change=change), self.assertRaises(AccountingError):
                self.admit()
            budget.clear()
            budget.update(old)
        self.publisher.ping.assert_not_called()

    def test_estimates_are_not_a_cutoff_for_new_protocol(self):
        budget = self.progress["progress"]["budget"]
        budget.update(continuation={"policy": "cost-tail-v1", "admission": "recorded_budget"})
        for remaining in (.2148, .000001):
            budget["remaining"] = remaining
            for estimate in (.2903, .32, 100, None):
                self.metadata["resumeCostUpperBound"] = estimate
                with self.subTest(remaining=remaining, estimate=estimate):
                    result = self.admit(check=True)
                    self.assertTrue(result["recoverable"])
                    self.assertEqual(result["admission_method"], "recorded_budget")
        self.publisher.ping.assert_not_called()

    def test_wrong_run_or_owner_session_fails_closed(self):
        self.progress["run"]["run_id"] = "wrong-run"
        with self.assertRaises(AccountingError):
            self.admit()
        self.progress["run"]["run_id"] = "run-1"
        self.progress["scope"]["kind"] = "admin"
        with self.assertRaises(AccountingError):
            self.admit()
        self.publisher.ping.assert_not_called()

    def test_finished_runs_and_changed_assignments_are_not_reopened(self):
        for status in ("finished", "stopped", "revoked"):
            self.progress["run"]["status"] = status
            with self.subTest(status=status), self.assertRaises(AccountingError):
                self.admit()
        self.progress["run"]["status"] = "failed"
        self.assignment["run"]["metadata"]["effort"] = "low"
        with self.assertRaises(AccountingError):
            self.admit()

    def test_network_failure_cannot_launch_model_or_consume_attempt(self):
        self.publisher.read.side_effect = AccountingError("HTTP 401")
        with self.assertRaises(AccountingError):
            self.admit()
        self.assertFalse((self.directory / "reentries.json").exists())
        self.publisher.ping.assert_not_called()

    def test_failed_resume_ping_remains_a_reserved_attempt(self):
        self.publisher.ping.return_value = False
        with self.assertRaisesRegex(AccountingError, "heartbeat"):
            self.admit()
        self.assertEqual(len(json.loads((self.directory / "reentries.json").read_text())["attempts"]), 1)

    def test_native_export_checks_workspace_model_and_terminal_reason(self):
        def check():
            with mock.patch("openrouter_reentry.subprocess.run", side_effect=self.export):
                inspect_session(COMMAND, "ses_probe", self.root, {})
        check()
        for key, value in (("finish", "unknown"), ("error", {"name": "APIError"}), ("modelID", "other")):
            with self.subTest(key=key):
                last = self.session["messages"][-1]["info"]
                old = dict(last)
                last[key] = value
                with self.assertRaises(AccountingError):
                    check()
                last.clear()
                last.update(old)
        self.session["info"]["directory"] = "/other"
        with self.assertRaises(AccountingError):
            check()

    def test_gap_recovery_preserves_native_identity_but_accepts_interrupted_response(self):
        self.session["messages"][-1]["info"].update(finish=None, error={"name": "APIError"})
        with mock.patch("openrouter_reentry.subprocess.run", side_effect=self.export):
            inspect_session(COMMAND, "ses_probe", self.root, {}, accounting_gap=True)
        with mock.patch("openrouter_reentry.subprocess.run", side_effect=self.export):
            with self.assertRaises(AccountingError):
                inspect_session(COMMAND, "ses_probe", self.root, {})
        result = self.admit(check=True, accounting_gap=True)
        self.assertFalse(result["accounting_complete"])
        self.assertTrue(result["remaining_is_upper_bound"])
        resumed = resume_command(COMMAND, "ses_probe", accounting_gap=True)
        self.assertIn("lower bounds", resumed[-1])
        self.assertNotIn("original goal", resumed)

    def test_early_stop_is_audited_with_native_reason_and_bounded(self):
        self.session["messages"][-1]["info"]["finish"] = "stop"
        with mock.patch("openrouter_reentry.subprocess.run", side_effect=self.export):
            check = admit_reentry(self.publisher, self.metadata, COMMAND, "ses_probe", manual=True, check=True)
            self.assertEqual((check["reason"], check["native_finish_reason"], check["attempts_remaining"]),
                             ("early_stop", "stop", 3))
            self.publisher.publish.assert_not_called()
            self.publisher.ping.assert_not_called()
            for _ in range(3):
                record = admit_reentry(self.publisher, self.metadata, COMMAND, "ses_probe", expected_finish="stop")
                self.assertEqual(record["attempts"][-1]["reason"], "early_stop")
            with self.assertRaisesRegex(AccountingError, "limit"):
                admit_reentry(self.publisher, self.metadata, COMMAND, "ses_probe")

    def test_capture_and_export_must_agree_before_resuming(self):
        with self.assertRaisesRegex(AccountingError, "disagrees"):
            self.admit(expected_finish="stop")
        self.publisher.ping.assert_not_called()
        self.assertFalse((self.directory / "reentries.json").exists())

    def test_large_export_uses_private_regular_file_not_pipe(self):
        self.session["padding"] = "long transcript " * 100000
        def export(*args, **kwargs):
            import stat
            mode = os.fstat(kwargs["stdout"].fileno()).st_mode
            self.assertTrue(stat.S_ISREG(mode))
            self.assertEqual(mode & 0o777, 0o600)
            return self.export(*args, **kwargs)
        with mock.patch("openrouter_reentry.subprocess.run", side_effect=export):
            self.assertEqual(inspect_session(COMMAND, "ses_probe", self.root, {}), "length")

    def test_real_export_subprocess_keeps_complete_large_json(self):
        self.session["padding"] = "a" * 1000000
        payload = self.root / "export.json"
        payload.write_text(json.dumps(self.session))
        binary = self.root / "opencode"
        binary.write_text(f'#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n'
                          f'sys.stdout.write(Path({str(payload)!r}).read_text())\nos._exit(0)\n')
        binary.chmod(0o700)
        self.assertEqual(inspect_session([str(binary), *COMMAND[1:]], "ses_probe", self.root, {}), "length")

    def test_malformed_and_oversized_export_remain_blocked(self):
        def bad_export(*args, **kwargs):
            kwargs["stdout"].write(b'{"info":')
            return subprocess.CompletedProcess(args, 0, stderr=b"")
        with mock.patch("openrouter_reentry.subprocess.run", side_effect=bad_export):
            with self.assertRaisesRegex(AccountingError, "valid JSON"):
                inspect_session(COMMAND, "ses_probe", self.root, {})
        with mock.patch("openrouter_reentry.subprocess.run", side_effect=self.export), \
                mock.patch("openrouter_reentry.MAX_EXPORT_BYTES", 1):
            with self.assertRaisesRegex(AccountingError, "size limit"):
                inspect_session(COMMAND, "ses_probe", self.root, {})
