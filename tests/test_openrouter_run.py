import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "scorebench" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import openrouter_run as runner  # noqa: E402


class FakeOpenRouter(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = (
            '{"id":"fake-generation","model":"test/model","choices":[],'
            '"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15,'
            '"cost":0.0000042,"prompt_tokens_details":{"cached_tokens":2,'
            '"cache_write_tokens":1}}}'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OpenRouterRunTests(unittest.TestCase):
    def test_key_alone_does_not_claim_openrouter_routing(self):
        enabled, protocol, reason = runner.detect_openrouter(
            "Codex", ["codex"], {"OPENROUTER_API_KEY": "secret"}, "auto"
        )
        self.assertFalse(enabled)
        self.assertEqual(protocol, "openai")
        self.assertIn("no OpenRouter route", reason)

    def test_claude_endpoint_selects_anthropic_skin(self):
        enabled, protocol, reason = runner.detect_openrouter(
            "Claude Code",
            ["claude"],
            {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api"},
            "auto",
        )
        self.assertTrue(enabled)
        self.assertEqual(protocol, "anthropic")
        self.assertIn("ANTHROPIC_BASE_URL", reason)

    def test_unrelated_harness_endpoint_does_not_trigger_detection(self):
        enabled, protocol, _reason = runner.detect_openrouter(
            "Codex",
            ["codex"],
            {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api"},
            "auto",
        )
        self.assertFalse(enabled)
        self.assertEqual(protocol, "openai")

    def test_codex_config_provider_is_detected_and_rerouted(self):
        with tempfile.TemporaryDirectory() as root:
            config_root = Path(root)
            (config_root / "config.toml").write_text(
                'model_provider = "router"\n'
                '[model_providers.router]\n'
                'base_url = "https://openrouter.ai/api/v1"\n',
                encoding="utf-8",
            )
            env = {"CODEX_HOME": root, "OPENROUTER_API_KEY": "secret"}
            enabled, protocol, _reason = runner.detect_openrouter(
                "Codex", ["codex", "prompt"], env, "auto"
            )
            command = runner._route_child(
                "Codex",
                ["codex", "prompt"],
                env,
                protocol,
                "http://127.0.0.1:1234",
            )
        self.assertTrue(enabled)
        self.assertEqual(command[:3], [
            "codex",
            "-c",
            'model_providers.router.base_url="http://127.0.0.1:1234/api/v1"',
        ])

    def test_inactive_launcher_executes_command_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "openrouter_run.py"),
                    "--harness",
                    "Codex",
                    "--workspace",
                    root,
                    "--",
                    sys.executable,
                    "-c",
                    "print('child-ok')",
                ],
                text=True,
                capture_output=True,
                check=True,
                env={key: value for key, value in os.environ.items() if key != "OPENROUTER_API_KEY"},
            )
        self.assertEqual(completed.stdout.strip(), "child-ok")
        self.assertIn("accounting inactive", completed.stderr)

    def test_detected_route_without_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            env = {key: value for key, value in os.environ.items() if key != "OPENROUTER_API_KEY"}
            env["OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "openrouter_run.py"),
                    "--harness",
                    "Custom",
                    "--workspace",
                    root,
                    "--",
                    sys.executable,
                    "-c",
                    "print('must-not-run')",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
        self.assertEqual(completed.returncode, 1)
        self.assertNotIn("must-not-run", completed.stdout)
        self.assertIn("OPENROUTER_API_KEY is not set", completed.stderr)

    def test_launcher_captures_exact_cost_for_child_process(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenRouter)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        host, port = upstream.server_address
        try:
            with tempfile.TemporaryDirectory() as root:
                child = """
import json, os, subprocess, sys, urllib.request
helper = os.environ['TOKEN_HELPER']
state = os.environ['SCOREBENCH_TOKEN_STATE']
subprocess.run([sys.executable, helper, 'start', '--state', state], check=True, stdout=subprocess.DEVNULL)
request = urllib.request.Request(
    os.environ['OPENAI_BASE_URL'] + '/chat/completions',
    data=b'{\"model\":\"test/model\",\"messages\":[]}',
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer placeholder'},
    method='POST',
)
with urllib.request.urlopen(request) as response:
    response.read()
subprocess.run([sys.executable, helper, 'flags', '--state', state], check=True)
"""
                env = {
                    **os.environ,
                    "OPENROUTER_API_KEY": "test-key",
                    "OPENROUTER_BASE": f"http://{host}:{port}",
                    "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
                    "TOKEN_HELPER": str(SCRIPTS / "token_usage.py"),
                }
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS / "openrouter_run.py"),
                        "--harness",
                        "Custom OpenAI harness",
                        "--workspace",
                        root,
                        "--",
                        sys.executable,
                        "-c",
                        child,
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                    env=env,
                )
                usage_log = Path(root) / ".scorebench" / "openrouter" / "usage.jsonl"
                records = [line for line in usage_log.read_text(encoding="utf-8").splitlines() if line]
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)

        self.assertEqual(len(records), 1)
        self.assertIn("--total-tokens 13", completed.stdout)
        self.assertIn("--input-tokens 9", completed.stdout)
        self.assertIn("--cache-read-tokens 2", completed.stdout)
        self.assertIn("--cache-creation-tokens 1", completed.stdout)
        self.assertIn("--cost-usd 4.2e-06", completed.stdout)
        self.assertIn("--usage-source openrouter", completed.stdout)


if __name__ == "__main__":
    unittest.main()
