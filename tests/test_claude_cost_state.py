import importlib.util
import hashlib
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

    def resumed_ledger(self, second_model_usage, second_usage):
        def counters(inputs, outputs, cost):
            return {"haiku": dict(inputTokens=inputs, outputTokens=outputs, cacheCreationInputTokens=0,
                                  cacheReadInputTokens=0, costUSD=cost)}
        first_usage = dict(input_tokens=10, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        records = [
            {"type": "scorebench_invocation", "id": "first"},
            {"type": "result", "uuid": "a", "session_id": "session-1", "modelUsage": counters(10, 1, 1.5e-05),
             "usage": first_usage},
            {"type": "scorebench_invocation", "id": "second"},
            {"type": "result", "uuid": "b", "session_id": "session-1",
             "modelUsage": counters(*second_model_usage), "usage": dict(first_usage, **second_usage)},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            return usage.claude_result_ledger(path)["modelUsage"]["haiku"]

    def test_cumulative_resume_report_is_not_added_twice(self):
        # Claude Code 2.1.281: the resumed result reports session totals (20/2, $3e-05)
        # while its own usage is just this invocation (10/1).
        result = self.resumed_ledger((20, 2, 3e-05), {})
        self.assertEqual((result["inputTokens"], result["outputTokens"]), (20, 2))
        self.assertAlmostEqual(result["costUSD"], 3e-05)

    def test_per_invocation_resume_report_is_still_added(self):
        # Claude Code 2.1.259: the resumed result reports only its own invocation.
        result = self.resumed_ledger((10, 1, 1.5e-05), {})
        self.assertEqual((result["inputTokens"], result["outputTokens"]), (20, 2))
        self.assertAlmostEqual(result["costUSD"], 3e-05)
        # A larger resumed invocation that is not previous + its own usage is added, never netted.
        result = self.resumed_ledger((30, 3, 4.5e-05), dict(input_tokens=30, output_tokens=3))
        self.assertEqual(result["inputTokens"], 40)

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

    def parse(self, *events, resume_after=()):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            results = Path(directory) / "results.jsonl"
            boundaries = []
            for index in resume_after:
                prefix = "".join(json.dumps(event) + "\n" for event in events[:index]).encode()
                boundaries.append(dict(type="scorebench_invocation", id=f"resume-{index}", session_id="session-1",
                                       native_transcript_prefix=[len(prefix), hashlib.sha256(prefix).hexdigest()]))
            results.write_text("".join(json.dumps(event) + "\n" for event in boundaries))
            return usage.claude_jsonl_snapshot(path, results_path=results if resume_after else None)

    def test_auxiliary_usage_and_cache_priced_once_without_terminal_result(self):
        result = self.parse(message(), ledger(), ledger())
        self.assertEqual(result.total_tokens, 330)
        self.assertEqual(result.input_tokens, 110)
        self.assertEqual(result.cache_read_tokens, 550)
        self.assertAlmostEqual(result.cost_usd, .21)

    def test_message_and_cumulative_ledger_are_not_added(self):
        result = self.parse(message(), ledger(), message(identifier="message-2"), ledger(main=200, cost=.4),
                            resume_after=(2,))
        self.assertEqual(result.total_tokens, 630)
        self.assertAlmostEqual(result.cost_usd, .41)

    def test_new_message_beyond_ledger_is_counted_but_stale_cost_is_not_reported(self):
        result = self.parse(message(), ledger(), message(5, identifier="message-2"), resume_after=(2,))
        self.assertEqual(result.total_tokens, 345)
        self.assertIsNone(result.cost_usd)

    def test_live_tail_advances_even_below_checkpoint_counters(self):
        # The checkpoint includes requests missing from the native message log.
        result = self.parse(message(90), ledger(), message(5, identifier="new"), resume_after=(2,))
        self.assertEqual(result.input_tokens, 115)
        self.assertEqual(result.output_tokens, 115)
        self.assertEqual(result.cache_read_tokens, 575)
        self.assertIsNone(result.cost_usd)

    def test_duplicate_message_after_checkpoint_is_not_live_tail(self):
        result = self.parse(message(90), ledger(), message(90))
        self.assertEqual(result.total_tokens, 330)
        self.assertAlmostEqual(result.cost_usd, .21)

    def test_stale_checkpoint_cannot_hide_already_observed_live_tail(self):
        with self.assertRaisesRegex(SystemExit, "preceding native usage"):
            self.parse(message(90), ledger(), message(5, identifier="new"), ledger(), resume_after=(2,))

    def test_late_old_invocation_message_inside_prefix_is_ambiguous(self):
        with self.assertRaisesRegex(SystemExit, "no verified next-invocation boundary"):
            self.parse(message(90), ledger(), message(5, identifier="late"),
                       message(5, identifier="new"), resume_after=(3,))

    def test_unproved_live_tail_is_refused_even_if_timestamp_looks_new(self):
        late = dict(message(5, identifier="late"), timestamp="2099-01-01T00:00:00Z")
        with self.assertRaisesRegex(SystemExit, "no verified next-invocation boundary"):
            self.parse(message(90), ledger(), late)

    def test_duplicate_old_message_after_verified_boundary_is_not_new_usage(self):
        result = self.parse(message(90), ledger(), message(90), message(5, identifier="new"), resume_after=(2,))
        self.assertEqual(result.total_tokens, 345)

    def test_prefix_checks_hash_size_session_and_record_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            native, results = Path(directory) / "native.jsonl", Path(directory) / "results.jsonl"
            prefix = (json.dumps(ledger()) + "\n").encode()
            native.write_bytes(prefix + (json.dumps(message(5)) + "\n").encode())
            good = [len(prefix), hashlib.sha256(prefix).hexdigest()]
            for proof, session in (([len(prefix), "0" * 64], "session-1"),
                                   ([len(prefix) - 1, hashlib.sha256(prefix[:-1]).hexdigest()], "session-1"),
                                   ([native.stat().st_size + 1, "0" * 64], "session-1"),
                                   (good, "other-session"), ([True, "0" * 64], "session-1")):
                with self.subTest(proof=proof, session=session):
                    results.write_text(json.dumps(dict(type="scorebench_invocation", id="resume",
                        session_id=session, native_transcript_prefix=proof)) + "\n")
                    with self.assertRaises(SystemExit):
                        usage.claude_jsonl_snapshot(native, results_path=results)

    def test_initial_empty_prefix_must_match_the_session_even_before_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            native, results = Path(directory) / "native.jsonl", Path(directory) / "results.jsonl"
            boundary = dict(type="scorebench_invocation", id="first", session_id="other-session",
                            native_transcript_prefix=[0, hashlib.sha256(b"").hexdigest()])
            results.write_text(json.dumps(boundary) + "\n")
            for event in (ledger(), dict(message(5), sessionId="session-1")):
                with self.subTest(event=event["type"]):
                    native.write_text(json.dumps(event) + "\n")
                    with self.assertRaisesRegex(SystemExit, "session"):
                        usage.claude_jsonl_snapshot(native, results_path=results)

    def test_checkpoint_cost_regression_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "cost decreased"):
            self.parse(ledger(), ledger(main=110, cost=.1))

    def test_repeated_message_cannot_change_its_model(self):
        with self.assertRaisesRegex(SystemExit, "conflicting Claude model"):
            self.parse(message(), message(model="haiku"), ledger())

    def test_interrupted_resume_uses_matching_native_checkpoints(self):
        first, second = ledger(), ledger(main=250, cost=.5)
        own_usage = message(140)["message"]["usage"]
        records = [
            {"type": "scorebench_invocation", "id": "first"},
            {"type": "result", "uuid": "a", "session_id": "session-1",
             "modelUsage": first["modelUsage"], "usage": message()["message"]["usage"]},
            {"type": "scorebench_invocation", "id": "second"},
            {"type": "result", "uuid": "b", "session_id": "session-1",
             "modelUsage": second["modelUsage"], "usage": own_usage,
             "subtype": "error_during_execution", "is_error": True},
        ]
        native = [message(), first, message(140, identifier="second"), second]
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "native.jsonl"
            results = Path(directory) / "results.jsonl"
            results.write_text("".join(json.dumps(record) + "\n" for record in records))
            transcript.write_text("".join(json.dumps(record) + "\n" for record in native))
            stopped = usage.claude_jsonl_snapshot(transcript, results_path=results)
            self.assertEqual(stopped.total_tokens, 780)
            self.assertAlmostEqual(stopped.cost_usd, .51)
            result_ledger = usage.claude_result_ledger(results, native_path=transcript)
            self.assertEqual(result_ledger["modelUsage"], second["modelUsage"])
            prefix = transcript.read_bytes()
            with results.open("a") as output:
                output.write(json.dumps(dict(type="scorebench_invocation", id="third", session_id="session-1",
                    native_transcript_prefix=[len(prefix), hashlib.sha256(prefix).hexdigest()])) + "\n")
            with transcript.open("a") as output:
                output.write(json.dumps(message(5, identifier="third")) + "\n")
            live = usage.claude_jsonl_snapshot(transcript, results_path=results)
            self.assertEqual(live.total_tokens, 795)
            self.assertEqual(live.cache_read_tokens, 1325)
            self.assertIsNone(live.cost_usd)
            mismatched = dict(second, sessionId="another-session")
            with self.assertRaisesRegex(SystemExit, "do not match"):
                usage.claude_result_ledger(results, native_checkpoints=[first, mismatched])
            # A partial checkpoint chain cannot prove the scope of earlier results.
            with self.assertRaisesRegex(SystemExit, "do not match"):
                usage.claude_result_ledger(results, native_checkpoints=[second])

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
