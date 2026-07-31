import gzip
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "scorebench"
    / "scripts"
    / "run_trace.py"
)
SPEC = importlib.util.spec_from_file_location("scorebench_run_trace", SCRIPT)
TRACE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TRACE)


def append_jsonl(path, *events):
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def read_trace(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class RunTraceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state = self.root / "state.json"
        self.state_root = self.root / "trace-state"
        self.state_patch = mock.patch.object(TRACE, "state_root", return_value=self.state_root)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.tempdir.cleanup()

    def start(self, source, provider):
        return TRACE.trace_start(
            provider=provider,
            source=source,
            cwd=self.root,
            state_path=self.state,
        )

    def build(self, **kwargs):
        state = TRACE.load_state(self.state)
        return TRACE.build_artifact(
            state,
            state_path=self.state,
            output=kwargs.get("output"),
            max_event_bytes=kwargs.get("max_event_bytes", 64 * 1024),
            max_trace_bytes=kwargs.get("max_trace_bytes", 256 * 1024),
        )

    def test_codex_finish_filters_private_reasoning_and_redacts_secrets(self):
        source = self.root / "codex.jsonl"
        append_jsonl(
            source,
            {
                "timestamp": "2026-07-30T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "cwd": str(self.root),
                    "session_id": "codex-session",
                },
            },
        )
        initial_size = source.stat().st_size
        started = self.start(source, "codex")
        self.assertEqual(started["source_offset"], initial_size)

        append_jsonl(
            source,
            {
                "timestamp": "2026-07-30T10:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "use SCOREBENCH_RUN_TOKEN=hrun_supersecretvalue",
                },
            },
            {
                "timestamp": "2026-07-30T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "encrypted_content": "private-reasoning-ciphertext",
                    "summary": [],
                },
            },
            {
                "timestamp": "2026-07-30T10:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 100}},
                },
            },
            {
                "timestamp": "2026-07-30T10:00:04Z",
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "stdout": "duplicated-patch-payload",
                },
            },
            {
                "timestamp": "2026-07-30T10:00:05Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": {
                        "cmd": "curl -H 'Authorization: Bearer visible-secret-token'"
                    },
                },
            },
            {
                "timestamp": "2026-07-30T10:00:06Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "command completed",
                },
            },
            {
                "timestamp": "2026-07-30T10:00:07Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": "candidate validated",
                },
            },
        )

        artifact, path = self.build()
        rows = read_trace(path)
        manifest, events = rows[0], rows[1:]
        rendered = json.dumps(rows)

        self.assertEqual(manifest["format"], "scorebench-run-trace")
        self.assertEqual(manifest["provider"], "codex")
        self.assertFalse(
            manifest["normalization"]["private_reasoning_included"]
        )
        self.assertEqual(artifact["event_count"], 4)
        self.assertEqual(
            {event["kind"] for event in events},
            {"user_message", "assistant_message", "tool_call", "tool_result"},
        )
        self.assertNotIn("private-reasoning-ciphertext", rendered)
        self.assertNotIn("duplicated-patch-payload", rendered)
        self.assertNotIn("hrun_supersecretvalue", rendered)
        self.assertNotIn("visible-secret-token", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_claude_finish_removes_thinking_and_preserves_tools(self):
        source = self.root / "claude.jsonl"
        source.touch()
        self.start(source, "claude")
        append_jsonl(
            source,
            {
                "timestamp": "2026-07-30T11:00:00Z",
                "type": "assistant",
                "sessionId": "claude-session",
                "cwd": str(self.root),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "hidden chain of thought",
                            "signature": "signed-private-data",
                        },
                        {"type": "text", "text": "I will test the candidate."},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Bash",
                            "input": {"command": "pytest -q"},
                        },
                    ],
                },
            },
            {
                "timestamp": "2026-07-30T11:00:01Z",
                "type": "user",
                "sessionId": "claude-session",
                "cwd": str(self.root),
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "12 passed",
                        }
                    ],
                },
            },
        )

        artifact, path = self.build()
        rows = read_trace(path)
        rendered = json.dumps(rows)

        self.assertEqual(artifact["event_count"], 3)
        self.assertEqual(
            [event["kind"] for event in rows[1:]],
            ["assistant_message", "tool_call", "tool_result"],
        )
        self.assertNotIn("hidden chain of thought", rendered)
        self.assertNotIn("signed-private-data", rendered)
        self.assertIn("I will test the candidate.", rendered)
        self.assertIn("12 passed", rendered)

    def test_repeated_start_preserves_the_original_boundary(self):
        source = self.root / "codex-retry.jsonl"
        append_jsonl(
            source,
            {
                "timestamp": "2026-07-30T11:30:00Z",
                "type": "session_meta",
                "payload": {
                    "cwd": str(self.root),
                    "session_id": "codex-retry-session",
                },
            },
        )
        first = self.start(source, "codex")
        append_jsonl(
            source,
            {
                "timestamp": "2026-07-30T11:30:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "work after the first boundary",
                },
            },
        )

        second = self.start(source, "codex")
        artifact, path = self.build()
        rendered = json.dumps(read_trace(path))

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["source_offset"], second["source_offset"])
        self.assertEqual(artifact["event_count"], 1)
        self.assertIn("work after the first boundary", rendered)

    def test_finish_rejects_a_replaced_source_file(self):
        source = self.root / "codex-replaced.jsonl"
        source.touch()
        self.start(source, "codex")
        replacement = self.root / "replacement.jsonl"
        replacement.write_text("", encoding="utf-8")
        replacement.replace(source)

        with self.assertRaisesRegex(TRACE.TraceError, "source file was replaced"):
            self.build()

    def test_large_events_are_compacted_and_artifact_is_idempotent(self):
        source = self.root / "codex-large.jsonl"
        source.touch()
        self.start(source, "codex")
        for index in range(100):
            append_jsonl(
                source,
                {
                    "timestamp": f"2026-07-30T12:00:{index:03d}Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"call-{index}",
                        "output": "x" * 4096,
                    },
                },
            )

        first, first_path = self.build(
            max_event_bytes=1024,
            max_trace_bytes=64 * 1024,
        )
        second, second_path = self.build(
            max_event_bytes=1024,
            max_trace_bytes=64 * 1024,
        )
        rows = read_trace(first_path)

        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first_path, second_path)
        self.assertLessEqual(first["uncompressed_event_bytes"], 64 * 1024)
        self.assertGreater(
            rows[0]["normalization"]["dropped"].get("global_budget_event", 0)
            + rows[0]["compacted_event_count"],
            0,
        )
        self.assertTrue(
            any(event.get("truncated") or event.get("compacted") for event in rows[1:])
        )

    def test_upload_streams_the_gzip_with_run_token(self):
        artifact_path = self.root / "trace.jsonl.gz"
        artifact_path.write_bytes(b"compressed-trace")
        artifact = {
            "trace_id": "trace_abc123",
            "sha256": TRACE.sha256_file(artifact_path),
        }
        observed = {}

        class FakeResponse:
            status = 200
            reason = "OK"

            @staticmethod
            def read(_limit):
                return b'{"trace":{"trace_id":"trace_abc123"}}'

        class FakeConnection:
            def __init__(self, host, port, timeout):
                observed.update(host=host, port=port, timeout=timeout, body=b"")

            def putrequest(self, method, path):
                observed.update(method=method, path=path)

            def putheader(self, name, value):
                observed.setdefault("headers", {})[name] = value

            def endheaders(self):
                pass

            def send(self, value):
                observed["body"] += value

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        with mock.patch.object(TRACE.http.client, "HTTPSConnection", FakeConnection):
            response = TRACE.upload_once(
                artifact=artifact,
                path=artifact_path,
                base_url="https://scorebench.dev/",
                token="hrun_not-logged",
                timeout=10,
            )

        self.assertEqual(response["trace"]["trace_id"], "trace_abc123")
        self.assertEqual(observed["method"], "POST")
        self.assertEqual(observed["path"], "/run/trace")
        self.assertEqual(observed["headers"]["Authorization"], "Bearer hrun_not-logged")
        self.assertEqual(observed["body"], b"compressed-trace")


if __name__ == "__main__":
    unittest.main()
