"""Timing observer uploads: replay the frozen batch, settle conflicts, back off."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "scorebench" / "scripts" / "scorebench_observer.py"
spec = importlib.util.spec_from_file_location("scorebench_observer", SCRIPT)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def state_with_activity(start: float = 1_000.0, end: float = 1_150.0) -> dict:
    return {
        "registration_id": "reg-1",
        "provider": "codex",
        "observer_id": "obs-1",
        "observer_boot_id": "boot-1",
        "scorebench_url": "https://scorebench.invalid",
        "token": "token",
        "next_sequence": 1,
        "event_count": 5,
        "pending_intervals": [[start, end]],
    }


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.responses = []
        original = observer.post_json

        def fake_post(url, token, payload):
            self.sent.append(payload)
            outcome = self.responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        observer.post_json = fake_post
        self.addCleanup(setattr, observer, "post_json", original)

    def test_a_lost_response_is_replayed_byte_for_byte(self):
        state = state_with_activity()
        self.responses = [observer.ObserverError("timed out"), {"timing_v2": {"ok": True}}]
        self.assertFalse(observer.upload_pending(state, 2_000.0, force=True))
        state["event_count"] = 9  # the session moved on before the retry
        self.assertTrue(observer.upload_pending(state, 2_100.0, force=True))
        self.assertEqual(self.sent[0], self.sent[1])
        self.assertNotIn("outbox", state)
        self.assertEqual(state["pending_intervals"], [])
        self.assertEqual(state["next_sequence"], 1 + len(self.sent[0]["evidence"]))

    def test_a_conflicting_batch_is_settled_instead_of_retried_forever(self):
        state = state_with_activity()
        conflict = observer.ObserverError(
            "ScoreBench returned HTTP 400: timing evidence id 'obs-x' already exists with different content",
            status=400,
        )
        self.responses = [conflict]
        self.assertFalse(observer.upload_pending(state, 2_000.0, force=True))
        self.assertNotIn("outbox", state)
        self.assertEqual(state["pending_intervals"], [])
        self.assertEqual(state["conflicting_batches"], 1)
        state["pending_intervals"] = [[1_150.0, 1_200.0]]  # only newer activity is sent next
        self.responses = [{"timing_v2": None}]
        self.assertTrue(observer.upload_pending(state, 2_100.0, force=True))
        self.assertEqual(self.sent[1]["evidence"][0]["started_at"], observer.timestamp_iso(1_150.0))

    def test_revoked_credentials_stop_uploads(self):
        state = state_with_activity()
        self.responses = [observer.ObserverError("ScoreBench returned HTTP 401: revoked", status=401)]
        observer.upload_pending(state, 2_000.0, force=True)
        self.assertFalse(state["enabled"])

    def test_failures_back_off_and_honor_retry_after(self):
        state = state_with_activity()
        self.responses = [observer.ObserverError("busy", status=503, retry_after=900.0)]
        observer.upload_pending(state, 2_000.0, force=True)
        self.assertEqual(state["upload_retry_seconds"], 900.0)
        self.assertFalse(observer.upload_pending(state, 2_000.0 + 600.0))  # still backing off
        self.assertEqual(len(self.sent), 1)
        self.responses = [observer.ObserverError("timed out"), observer.ObserverError("timed out")]
        observer.upload_pending(state, 3_000.0, force=True)
        first = state["upload_retry_seconds"]
        observer.upload_pending(state, 4_000.0, force=True)
        self.assertGreater(state["upload_retry_seconds"], first)
        self.assertLessEqual(state["upload_retry_seconds"], observer.MAX_UPLOAD_BACKOFF_SECONDS)


if __name__ == "__main__":
    unittest.main()
