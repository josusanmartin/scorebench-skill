import json
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/scorebench/scripts"
sys.path.insert(0, str(SCRIPTS))
import openrouter_agents as agents
import openrouter_run as runner
import token_usage as usage


class NativeRoutingTests(unittest.TestCase):
    def test_explicit_routes_and_no_inference_from_other_provider_env(self):
        for harness, command in (
            ("OpenCode", ["opencode", "run", "--model", "openrouter/deepseek/flash"]),
            ("Pi", ["pi", "--provider=openrouter", "--model=deepseek/flash"]),
        ):
            with self.subTest(harness=harness):
                self.assertTrue(runner.detect_openrouter(harness, command, {}, "auto")[0])
                agents.validate_route(harness, command, "deepseek/flash")
                with self.assertRaisesRegex(SystemExit, "assigned"):
                    agents.validate_route(harness, command, "other/model")
                for mode in ("off", "invalid"):
                    with self.assertRaises(SystemExit):
                        runner.detect_openrouter(harness, command, {}, mode)
        self.assertFalse(runner.detect_openrouter(
            "Pi", ["pi", "--provider", "anthropic", "--model", "sonnet"],
            {"OPENAI_BASE_URL": "https://openrouter.ai/api/v1"}, "auto",
        )[0])

    def test_ambiguous_launches_fail_before_any_side_effect(self):
        for command in (
            ["opencode", "run"],
            ["opencode", "run", "-m", "deepseek"],
            ["opencode", "run", "-m", "openrouter/a", "--model", "openrouter/b"],
            ["opencode", "run", "-m", "openrouter/a", "--attach=http://localhost:4000"],
        ):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                agents.validate_route("OpenCode", command)
        with self.assertRaisesRegex(SystemExit, "--provider"):
            agents.validate_route("Pi", ["pi", "--model", "sonnet"])
        for harness, command, effort_flag in (
            ("Pi", ["pi", "--provider", "openrouter", "--model", "a"], "--thinking"),
            ("OpenCode", ["opencode", "run", "--model", "openrouter/a"], "--variant"),
        ):
            with self.subTest(harness=harness), self.assertRaisesRegex(SystemExit, "reasoning effort"):
                agents.validate_route(harness, [*command, effort_flag, "low"], expected_effort="high")
            with self.assertRaisesRegex(SystemExit, "low, medium or high"):
                agents.validate_route(harness, [*command, effort_flag, "max"])

    def test_opencode_overrides_native_route_without_writing_global_config(self):
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps({"instructions": ["AGENTS.md"],
            "provider": {"openrouter": {"options": {"timeout": 5000}}}})}
        command = ["opencode", "run", "-m", "openrouter/deepseek/flash"]
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(agents.route_agent("OpenCode", command, env, "http://127.0.0.1:3123", Path(root)), command)
            self.assertEqual(list(Path(root).iterdir()), [])
            self.assertEqual(env["XDG_DATA_HOME"], str(Path(root) / ".scorebench/openrouter/opencode-data"))
            self.assertEqual(env["XDG_STATE_HOME"], str(Path(root) / ".scorebench/openrouter/opencode-state"))
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["instructions"], ["AGENTS.md"])
        self.assertEqual(config["small_model"], "openrouter/deepseek/flash")
        self.assertEqual(config["enabled_providers"], ["openrouter"])
        self.assertEqual(config["provider"]["openrouter"]["options"], {
            "timeout": 5000, "baseURL": "http://127.0.0.1:3123/api/v1",
            "apiKey": "{env:OPENROUTER_API_KEY}",
        })

    def test_pi_extension_is_private_and_has_no_key(self):
        with tempfile.TemporaryDirectory() as root:
            env = {"OPENROUTER_API_KEY": "secret-probe-key"}
            command = agents.route_agent("Pi", ["pi", "--provider", "openrouter", "--model", "a"], env,
                                         "http://127.0.0.1:3123", Path(root))
            extension = Path(command[2])
            self.assertEqual(extension.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("secret-probe-key", extension.read_text())
            self.assertIn("registerProvider('openrouter'", extension.read_text())
            self.assertEqual(env["OPENAI_BASE_URL"], "http://127.0.0.1:3123/api/v1")
            self.assertEqual(env["PI_CODING_AGENT_DIR"], str(Path(root) / ".scorebench/openrouter/pi-agent"))

    def test_preflight_missing_key_and_success_never_create_ledger_or_child(self):
        with tempfile.TemporaryDirectory() as root:
            args = ["--check", "--harness", "Pi", "--workspace", root, "--expected-model", "a",
                    "--", "pi", "--provider", "openrouter", "--model", "a"]
            with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(SystemExit, "API_KEY"):
                runner.main(args)
            with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "probe"}, clear=True), \
                 mock.patch.object(runner, "model_metadata", return_value={}), \
                 mock.patch.object(runner, "check_installation"):
                self.assertEqual(runner.main(args), 0)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_inherited_worker_ledger_fails_before_metadata_or_model(self):
        for name in ("SCOREBENCH_OPENROUTER_LOG", "SCOREBENCH_TOKEN_STATE"):
            with tempfile.TemporaryDirectory() as root, self.subTest(name=name), \
                 mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "probe", name: "/other-worker/usage"}, clear=True), \
                 mock.patch.object(runner, "model_metadata") as metadata:
                with self.assertRaisesRegex(SystemExit, "private workspace"):
                    runner.main(["--check", "--harness", "Pi", "--workspace", root,
                                 "--", "pi", "--provider", "openrouter", "--model", "a"])
                metadata.assert_not_called()
                self.assertEqual(list(Path(root).iterdir()), [])

    def test_broken_cli_is_not_reported_ready(self):
        with mock.patch.object(agents.subprocess, "run", side_effect=OSError("bad executable")):
            with self.assertRaisesRegex(SystemExit, "repair its installation"):
                agents.check_installation(["pi", "--model", "a"], {})

    def test_public_metadata_supports_new_tool_models_and_fails_closed(self):
        data = {"id": "provider/new-model", "name": "New model", "endpoints": [
            {"supported_parameters": ["tools", "reasoning"], "context_length": 32000,
             "max_completion_tokens": 8000, "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
            {"supported_parameters": ["tools"], "context_length": 16000,
             "max_completion_tokens": 4000, "pricing": {"prompt": "0.000001"}},
        ]}
        with mock.patch.object(agents.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"data": data}).encode())):
            result = agents.model_metadata("provider/new-model", "https://openrouter.ai")
        self.assertEqual((result["contextWindow"], result["maxTokens"]), (16000, 4000))
        self.assertEqual(result["cost"]["input"], 1)
        self.assertEqual(result["compat"]["thinkingFormat"], "openrouter")
        for invalid in ({**data, "id": "other/model"}, {**data, "endpoints": []}):
            with mock.patch.object(agents.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"data": invalid}).encode())):
                with self.assertRaisesRegex(SystemExit, "metadata unavailable"):
                    agents.model_metadata("provider/new-model", "https://openrouter.ai")

    def test_opencode_larger_output_does_not_inherit_smallest_provider_limit(self):
        data = {"id": "deepseek/flash", "name": "Flash", "endpoints": [
            {"supported_parameters": ["tools", "reasoning"], "context_length": 1000000,
             "max_completion_tokens": limit, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
            for limit in (32768, 131072, 384000)
        ]}
        with mock.patch.object(agents.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"data": data}).encode())):
            metadata = agents.model_metadata("deepseek/flash", "https://openrouter.ai", output_target=128000)
        self.assertEqual(metadata["maxTokens"], 128000)
        self.assertEqual(metadata["contextWindow"], 1000000)
        self.assertAlmostEqual(metadata["resumeCostUpperBound"], 1.256)
        env = {"OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": "32000"}
        agents.route_agent("OpenCode", ["opencode", "run", "-m", "openrouter/deepseek/flash"],
                           env, "http://localhost:1", Path("/tmp/worker"), metadata)
        self.assertEqual(env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"], "128000")
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]["openrouter"]["models"]
                         ["deepseek/flash"]["limit"]["output"], 128000)

    def test_opencode_output_never_exceeds_provider_limit(self):
        data = {"id": "small/model", "name": "Small", "endpoints": [
            {"supported_parameters": ["tools"], "context_length": 8192,
             "max_completion_tokens": 4096, "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        ]}
        with mock.patch.object(agents.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"data": data}).encode())):
            metadata = agents.model_metadata("small/model", "https://openrouter.ai", output_target=128000)
        self.assertEqual(metadata["maxTokens"], 4096)


class LedgerTests(unittest.TestCase):
    def snapshot(self, records):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "usage.jsonl"
            log.write_text("".join(json.dumps(item) + "\n" for item in records))
            return usage.openrouter_jsonl_snapshot(log)

    def test_responses_cache_and_reasoning_are_disjoint(self):
        snapshot = self.snapshot([{"usage": {"input_tokens": 140, "output_tokens": 12,
            "input_tokens_details": {"cached_tokens": 90},
            "output_tokens_details": {"reasoning_tokens": 4}, "cost": 0.003}}])
        self.assertEqual((snapshot.total_tokens, snapshot.input_tokens, snapshot.output_tokens,
                          snapshot.cache_read_tokens, snapshot.reasoning_output_tokens), (62, 50, 12, 90, 4))
        self.assertEqual(snapshot.cost_usd, 0.003)

    def test_partial_cost_is_never_reported_as_complete(self):
        snapshot = self.snapshot([
            {"usage": {"prompt_tokens": 10, "completion_tokens": 1, "cost": 0.01}},
            {"usage": {"prompt_tokens": 30, "completion_tokens": 2}},
        ])
        self.assertEqual(snapshot.total_tokens, 43)
        self.assertIsNone(snapshot.cost_usd)
        for cost in (float("inf"), float("nan"), -1, True):
            self.assertIsNone(usage.usage_cost({"cost": cost}))

    def test_duplicate_response_is_counted_once_and_conflicts_fail(self):
        record = {"id": "gen1", "usage": {"prompt_tokens": 10, "completion_tokens": 1, "cost": 0.01}}
        self.assertEqual(self.snapshot([record, record]).total_tokens, 11)
        with self.assertRaisesRegex(SystemExit, "conflicting"):
            self.snapshot([record, {**record, "model": "different"}])

    def test_missing_usage_is_not_zero(self):
        for record in ({"accounting_error": "missing usage"}, {"usage": {}}, []):
            with self.subTest(record=record), self.assertRaises(SystemExit):
                self.snapshot([record])


if __name__ == "__main__":
    unittest.main()
