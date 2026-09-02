import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "scorebench"
    / "scripts"
    / "scorebench_observer.py"
)
SPEC = importlib.util.spec_from_file_location("scorebench_observer", SCRIPT)
OBSERVER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(OBSERVER)


def stamp(seconds):
    return OBSERVER.timestamp_iso(1_800_000_000 + seconds)


def apply(state, provider, *events):
    for event in events:
        for transition in OBSERVER.transitions(provider, event):
            OBSERVER.apply_transition(state, transition)


class TransitionTests(unittest.TestCase):
    def test_claude_model_and_tool_operations_form_one_union_interval(self):
        state = {"open_operations": {}, "pending_intervals": []}
        apply(
            state,
            "claude",
            {
                "type": "user",
                "timestamp": stamp(0),
                "message": {"role": "user", "content": "start"},
            },
            {
                "type": "assistant",
                "timestamp": stamp(10),
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "tool-private", "input": {"secret": "do-not-send"}}],
                },
            },
            {
                "type": "user",
                "timestamp": stamp(40),
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "tool-private", "content": "private output"}],
                },
            },
            {
                "type": "assistant",
                "timestamp": stamp(50),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "private answer"}]},
            },
        )

        self.assertEqual(state["pending_intervals"], [[1_800_000_000.0, 1_800_000_050.0]])
        self.assertEqual(state["open_operations"], {})

    def test_codex_task_lifecycle_covers_long_reasoning_and_tools(self):
        state = {"open_operations": {}, "pending_intervals": []}
        apply(
            state,
            "codex",
            {
                "type": "event_msg",
                "timestamp": stamp(0),
                "payload": {"type": "task_started"},
            },
            {
                "type": "response_item",
                "timestamp": stamp(20),
                "payload": {"type": "function_call", "call_id": "call-1", "arguments": "secret"},
            },
            {
                "type": "response_item",
                "timestamp": stamp(80),
                "payload": {"type": "function_call_output", "call_id": "call-1", "output": "secret"},
            },
            {
                "type": "event_msg",
                "timestamp": stamp(120),
                "payload": {"type": "task_complete"},
            },
        )

        self.assertEqual(state["pending_intervals"], [[1_800_000_000.0, 1_800_000_120.0]])
        self.assertEqual(state["open_operations"], {})

    def test_payload_contains_no_session_content_or_provider_ids(self):
        state = {
            "registration_id": "reg-test",
            "provider": "claude",
            "pending_intervals": [[1_800_000_000.0, 1_800_000_020.0]],
            "next_sequence": 1,
            "event_count": 4,
        }
        evidence, cutoff = OBSERVER.interval_payload(state)
        encoded = json.dumps(evidence, sort_keys=True)

        self.assertEqual(cutoff, 1_800_000_020.0)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("private", encoded)
        self.assertNotIn("tool-private", encoded)
        self.assertEqual(evidence[0]["metadata"]["operation_kind"], "model_or_tool")

    def test_open_operation_without_verified_process_waits_for_completion(self):
        state = {
            "open_operations": {"model": {"started_at": 100.0}},
            "active_since": 100.0,
            "pending_intervals": [],
            "agent_pid": 0,
            "agent_start_ticks": "",
        }
        OBSERVER.lease_open_activity(state, 160.0)
        self.assertEqual(state["pending_intervals"], [])
        self.assertEqual(state["active_since"], 100.0)

    def test_open_operation_uses_renewable_lease_only_while_agent_is_alive(self):
        state = {
            "open_operations": {"model": {"started_at": 100.0}},
            "active_since": 100.0,
            "pending_intervals": [],
            "agent_pid": 123,
            "agent_start_ticks": "456",
        }
        with mock.patch.object(OBSERVER, "process_alive", return_value=True):
            OBSERVER.lease_open_activity(state, 160.0)
        self.assertEqual(state["pending_intervals"], [[100.0, 160.0]])
        self.assertEqual(state["active_since"], 160.0)

        with mock.patch.object(OBSERVER, "process_alive", return_value=False):
            OBSERVER.lease_open_activity(state, 220.0)
        self.assertEqual(state["pending_intervals"], [[100.0, 160.0]])
        self.assertEqual(state["open_operations"], {})


class StorageAndUploadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_root_patch = mock.patch.object(
            OBSERVER, "state_root", return_value=self.root / "state"
        )
        self.state_root_patch.start()

    def tearDown(self):
        self.state_root_patch.stop()
        self.tempdir.cleanup()

    def test_reader_advances_only_through_complete_lines(self):
        source = self.root / "session.jsonl"
        first = {
            "type": "event_msg",
            "timestamp": stamp(0),
            "payload": {"type": "task_started"},
        }
        source.write_bytes((json.dumps(first) + "\n" + '{"type":"event_msg"').encode())
        stat = source.stat()
        state = {
            "provider": "codex",
            "source_path": str(source),
            "source_device": stat.st_dev,
            "source_inode": stat.st_ino,
            "source_offset": 0,
            "open_operations": {},
            "pending_intervals": [],
        }

        self.assertEqual(OBSERVER.read_new_events(state), 1)
        self.assertEqual(state["source_offset"], len(json.dumps(first).encode()) + 1)
        self.assertIn("turn", state["open_operations"])

    def test_discovery_prefers_explicit_codex_thread_over_newer_same_cwd(self):
        codex_home = self.root / "codex"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        expected = sessions / "rollout-thread-current.jsonl"
        newer = sessions / "rollout-thread-other.jsonl"
        session_meta = {
            "type": "session_meta",
            "timestamp": stamp(0),
            "payload": {"cwd": str(self.root)},
        }
        expected.write_text(json.dumps(session_meta) + "\n", encoding="utf-8")
        newer.write_text(json.dumps(session_meta) + "\n", encoding="utf-8")
        os.utime(expected, (100, 100))
        os.utime(newer, (200, 200))

        with mock.patch.dict(
            os.environ,
            {"CODEX_HOME": str(codex_home), "CODEX_THREAD_ID": "thread-current"},
            clear=False,
        ):
            provider, source = OBSERVER.discover_source("codex", self.root)

        self.assertEqual(provider, "codex")
        self.assertEqual(source, expected)

    def test_oversized_record_is_discarded_as_one_line_and_clears_open_lease(self):
        source = self.root / "oversized.jsonl"
        events = [
            {
                "type": "event_msg",
                "timestamp": stamp(0),
                "payload": {"type": "task_started"},
            },
            {
                "type": "response_item",
                "timestamp": stamp(10),
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-large",
                    "output": "private" * 100,
                },
            },
            {
                "type": "event_msg",
                "timestamp": stamp(20),
                "payload": {"type": "task_complete"},
            },
        ]
        source.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        stat = source.stat()
        state = {
            "provider": "codex",
            "source_path": str(source),
            "source_device": stat.st_dev,
            "source_inode": stat.st_ino,
            "source_offset": 0,
            "open_operations": {},
            "pending_intervals": [],
        }
        old_limit = OBSERVER.MAX_SOURCE_LINE_BYTES
        OBSERVER.MAX_SOURCE_LINE_BYTES = 100
        try:
            while state["source_offset"] < stat.st_size:
                OBSERVER.read_new_events(state)
        finally:
            OBSERVER.MAX_SOURCE_LINE_BYTES = old_limit

        self.assertEqual(state["source_offset"], stat.st_size)
        self.assertEqual(state.get("dropped_oversized_lines"), 1)
        self.assertEqual(state.get("dropped_invalid_lines", 0), 0)
        self.assertEqual(state["open_operations"], {})
        self.assertEqual(state["pending_intervals"], [])

    def test_failed_upload_preserves_retry_queue(self):
        state = self.upload_state()
        with mock.patch.object(OBSERVER, "post_json", side_effect=OBSERVER.ObserverError("offline")):
            uploaded = OBSERVER.upload_pending(state, 1_800_000_100.0, force=True)
        self.assertFalse(uploaded)
        self.assertEqual(state["pending_intervals"], [[1_800_000_000.0, 1_800_000_080.0]])
        self.assertIn("offline", state["last_error"])

    def test_successful_upload_consumes_only_the_sent_batch(self):
        state = self.upload_state()
        old_limit = OBSERVER.MAX_UPLOAD_SPANS
        OBSERVER.MAX_UPLOAD_SPANS = 1
        try:
            with mock.patch.object(OBSERVER, "post_json", return_value={"timing_v2": {"authoritative": False}}):
                uploaded = OBSERVER.upload_pending(state, 1_800_000_100.0, force=True)
        finally:
            OBSERVER.MAX_UPLOAD_SPANS = old_limit
        self.assertTrue(uploaded)
        self.assertEqual(state["pending_intervals"], [[1_800_000_060.0, 1_800_000_080.0]])
        self.assertEqual(state["uploaded_span_count"], 1)

    def test_status_never_returns_stored_observer_credential(self):
        path = OBSERVER.registrations_dir() / "reg-test.json"
        OBSERVER.atomic_write_json(
            path,
            {
                "schema_version": 1,
                "registration_id": "reg-test",
                "provider": "claude",
                "cwd": str(self.root),
                "enabled": True,
                "token": "hobs_super_secret",
                "pending_intervals": [],
            },
        )
        encoded = json.dumps(OBSERVER.status(), sort_keys=True)
        self.assertNotIn("hobs_super_secret", encoded)
        self.assertNotIn('"token"', encoded)

    def test_unregister_by_workspace_disables_matching_registration(self):
        source = self.root / "session.jsonl"
        source.write_text("", encoding="utf-8")
        stat = source.stat()
        path = OBSERVER.registrations_dir() / "reg-test.json"
        OBSERVER.atomic_write_json(
            path,
            {
                "schema_version": 1,
                "registration_id": "reg-test",
                "cwd": str(self.root.resolve()),
                "enabled": True,
                "provider": "codex",
                "source_path": str(source),
                "source_device": stat.st_dev,
                "source_inode": stat.st_ino,
                "source_offset": 0,
                "agent_pid": 0,
                "open_operations": {},
                "pending_intervals": [],
            },
        )

        result = OBSERVER.unregister(cwd=str(self.root))

        self.assertEqual(result["registration_ids"], ["reg-test"])
        self.assertFalse(OBSERVER.load_json(path)["enabled"])

    def test_dead_agent_registration_disables_after_retry_queue_is_flushed(self):
        source = self.root / "session.jsonl"
        source.write_text("", encoding="utf-8")
        stat = source.stat()
        path = OBSERVER.registrations_dir() / "reg-dead.json"
        OBSERVER.atomic_write_json(
            path,
            {
                "schema_version": 1,
                "registration_id": "reg-dead",
                "provider": "codex",
                "source_path": str(source),
                "source_device": stat.st_dev,
                "source_inode": stat.st_ino,
                "source_offset": 0,
                "enabled": True,
                "agent_pid": 123,
                "agent_start_ticks": "456",
                "open_operations": {},
                "pending_intervals": [],
            },
        )

        with mock.patch.object(OBSERVER, "process_alive", return_value=False):
            state = OBSERVER.process_registration(path)

        self.assertFalse(state["enabled"])
        self.assertIn("agent_exit_observed_at", state)

    @staticmethod
    def upload_state():
        return {
            "registration_id": "reg-test",
            "observer_id": "host-test",
            "observer_boot_id": "boot-test",
            "provider": "claude",
            "scorebench_url": "https://staging.scorebench.dev",
            "token": "hobs_secret",
            "pending_intervals": [[1_800_000_000.0, 1_800_000_080.0]],
            "next_sequence": 1,
            "event_count": 4,
        }


if __name__ == "__main__":
    unittest.main()
