import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


path = Path(__file__).resolve().parents[1] / "skills/scorebench/scripts/token_usage.py"
sys.path.insert(0, str(path.parent))
spec = importlib.util.spec_from_file_location("cost_state_token_usage", path)
usage = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = usage
spec.loader.exec_module(usage)


def ledger(main=100, helper=10, cost=0.2):
    def model(count, price):
        return dict(inputTokens=count, outputTokens=count, cacheCreationInputTokens=count,
                    cacheReadInputTokens=count * 5, costUSD=price)
    return dict(type="cost-state", sessionId="session-1", totalCostUSD=cost+0.01,
                modelUsage={"fable": model(main, cost), "haiku": model(helper, 0.01)})


def message(count=100, model="fable", identifier="message-1"):
    return dict(type="assistant", message=dict(id=identifier, model=model, usage=dict(
        input_tokens=count, output_tokens=count, cache_creation_input_tokens=count,
        cache_read_input_tokens=count * 5)))


class ClaudeCostStateTests(unittest.TestCase):
    def test_native_results_include_helpers_and_accumulate_distinct_reentries(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript, results = Path(directory) / "session.jsonl", Path(directory) / "results.jsonl"
            transcript.write_text(json.dumps(message()) + "\n")
            result = {"type": "result", "uuid": "first", "session_id": "session-1", "modelUsage": ledger()["modelUsage"]}
            results.write_text(json.dumps({"type":"scorebench_invocation","id":"first-process"}) + "\n" + json.dumps(result) + "\n" + json.dumps(result) + "\n")
            snapshot = usage.claude_jsonl_snapshot(transcript, results_path=results)
            self.assertEqual(snapshot.total_tokens, 330)
            self.assertAlmostEqual(snapshot.cost_usd, .21)
            with results.open("a") as output:
                output.write(json.dumps({"type":"scorebench_invocation","id":"second-process"}) + "\n")
                output.write(json.dumps({**result, "uuid": "second"}) + "\n")
            snapshot = usage.claude_jsonl_snapshot(transcript, results_path=results)
            self.assertEqual(snapshot.total_tokens, 660)
            self.assertAlmostEqual(snapshot.cost_usd, .42)

    def test_dated_model_alias_does_not_double_count(self):
        result = self.parse(message(model="fable-20260909"), ledger())
        self.assertEqual(result.total_tokens, 330)

    def test_helper_and_main_usage_under_dated_and_undated_names(self):
        event = ledger()
        event["modelUsage"]["fable-20260909"] = event["modelUsage"].pop("haiku")
        result = self.parse(message(model="fable-20260909"), event)
        self.assertEqual(result.total_tokens, 330)
        self.assertEqual(result.cache_read_tokens, 550)
        self.assertAlmostEqual(result.cost_usd, .21)

    def test_multiple_results_in_one_invocation_are_cumulative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            records = [{"type":"scorebench_invocation","id":"one"}]
            for key, count in (("first", 10), ("last", 100)):
                records.append({"type":"result","uuid":key,"session_id":"session-1","modelUsage":ledger(main=count)["modelUsage"]})
            path.write_text("".join(json.dumps(record)+"\n" for record in records))
            result = usage.claude_result_ledger(path)
            self.assertEqual(result["modelUsage"]["fable"]["inputTokens"],100)

    def test_empty_native_report_certifies_only_zero_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            results = Path(directory) / "results.jsonl"
            records = [
                {"type": "scorebench_invocation", "id": "one"},
                {"type": "result", "uuid": "auth-error", "session_id": "session-1", "modelUsage": {}},
            ]
            results.write_text("".join(json.dumps(record) + "\n" for record in records))
            transcript.write_text(json.dumps(message(0, model="<synthetic>")) + "\n")
            snapshot = usage.claude_jsonl_snapshot(transcript, results_path=results)
            self.assertEqual(snapshot.total_tokens, 0)
            self.assertEqual(snapshot.cost_usd, 0)
            transcript.write_text(json.dumps(message()) + "\n")
            with self.assertRaises(SystemExit):
                usage.claude_jsonl_snapshot(transcript, results_path=results)

    def parse(self, *events):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            return usage.claude_jsonl_snapshot(path)

    def test_auxiliary_usage_and_cache_priced_once_without_terminal_result(self):
        result = self.parse(message(), ledger(), ledger())
        self.assertEqual(result.total_tokens, 330)
        self.assertEqual(result.input_tokens, 110)
        self.assertEqual(result.cache_read_tokens, 550)
        self.assertAlmostEqual(result.cost_usd, .21)

    def test_message_and_cumulative_ledger_are_not_added(self):
        result = self.parse(message(), ledger(), message(identifier="message-2"), ledger(main=200, cost=.4))
        self.assertEqual(result.total_tokens, 630)
        self.assertAlmostEqual(result.cost_usd, .41)

    def test_new_message_beyond_ledger_is_counted_but_stale_cost_is_not_reported(self):
        result = self.parse(message(), ledger(), message(5, identifier="message-2"))
        self.assertEqual(result.total_tokens, 345)
        self.assertIsNone(result.cost_usd)

    def test_partial_or_regressing_ledger_fails_instead_of_undercounting(self):
        with self.assertRaises(SystemExit):
            self.parse(ledger(), ledger(main=90))
        event = ledger()
        del event["modelUsage"]["haiku"]["inputTokens"]
        with self.assertRaises(SystemExit):
            self.parse(event)

    def test_mixed_sessions_rejected(self):
        event = ledger()
        event["sessionId"] = "other-session"
        with self.assertRaises(SystemExit):
            self.parse(ledger(), event)

    def test_synthetic_zero_auth_error_does_not_erase_cost(self):
        result = self.parse(message(), ledger(), message(0, model="<synthetic>", identifier="error"))
        self.assertAlmostEqual(result.cost_usd, .21)

    def test_unknown_model_cost_not_reported_as_zero(self):
        event = ledger()
        event["hasUnknownModelCost"] = True
        result = self.parse(event)
        self.assertEqual(result.total_tokens, 330)
        self.assertIsNone(result.cost_usd)


if __name__ == "__main__":
    unittest.main()
