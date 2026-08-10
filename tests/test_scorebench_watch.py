import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "scorebench"
    / "scripts"
    / "scorebench_watch.py"
)
SPEC = importlib.util.spec_from_file_location("scorebench_watch", SCRIPT)
WATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WATCH)


def valid_config():
    return {
        "tmux_session": "ant",
        "workers": [
            {
                "run_id": "run-one",
                "window": "worker-one",
                "container": "container-one",
                "client": "codex",
                "restart_command": ["/tmp/start-one.sh"],
            }
        ],
    }


def subprocess_result(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class ConfigTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return WATCH.load_config(path)

    def test_loads_defaults(self):
        config = self.load(valid_config())
        self.assertEqual(config.target_active_seconds, 14400)
        self.assertEqual(config.activity_heartbeat_seconds, 300)
        self.assertEqual(config.docker_command, ("docker",))
        self.assertTrue(config.enforce_active_gate)
        self.assertIsNone(config.report_url)
        self.assertEqual(config.workers[0].run_id, "run-one")

    def test_accepts_legacy_report_url_without_using_it(self):
        data = valid_config()
        data["report_url"] = "https://scorebench.dev/report.html"
        config = self.load(data)
        self.assertEqual(config.report_url, data["report_url"])

    def test_rejects_duplicate_worker_identity(self):
        data = valid_config()
        duplicate = dict(data["workers"][0])
        duplicate["window"] = "worker-two"
        duplicate["container"] = "container-two"
        data["workers"].append(duplicate)
        with self.assertRaisesRegex(WATCH.ConfigError, "duplicate worker run_id"):
            self.load(data)

    def test_rejects_unknown_fields(self):
        data = valid_config()
        data["elapsed_stop_seconds"] = 14400
        with self.assertRaisesRegex(WATCH.ConfigError, "unknown config fields"):
            self.load(data)


class ProgressTests(unittest.TestCase):
    @staticmethod
    def payload(**updates):
        progress = {
            "schema_version": 1,
            "run_id": "run-one",
            "active_seconds": 480,
            "elapsed_seconds": 500,
            "tokens_total": 4200,
            "active_seconds_source": "server_timestamps_with_run_pings",
            "elapsed_seconds_source": "server_timestamps",
            "tokens_total_source": "claude_code_jsonl",
            "measured_at": "2026-07-29T10:00:00Z",
            "tokens_measured_at": "2026-07-29T10:01:00Z",
            "candidate_count": 2,
        }
        progress.update(updates)
        return {
            "scope": {"kind": "run_token", "run_id": "run-one"},
            "progress": progress,
        }

    def test_parses_scoped_authoritative_progress(self):
        progress = WATCH.parse_run_progress(
            json.dumps(self.payload()), "run-one"
        )
        self.assertEqual(progress.run_id, "run-one")
        self.assertEqual(progress.active_seconds, 480)
        self.assertEqual(progress.elapsed_seconds, 500)
        self.assertEqual(progress.tokens_total, 4200)
        self.assertEqual(
            progress.active_seconds_source, "server_timestamps_with_run_pings"
        )
        self.assertEqual(progress.measured_at, "2026-07-29T10:00:00Z")

    def test_rejects_scope_mismatch(self):
        payload = self.payload()
        payload["scope"]["run_id"] = "run-two"
        with self.assertRaisesRegex(ValueError, "progress scope mismatch"):
            WATCH.parse_run_progress(json.dumps(payload), "run-one")

    def test_rejects_active_time_above_elapsed_time(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            WATCH.parse_run_progress(
                json.dumps(self.payload(active_seconds=501)), "run-one"
            )

    def test_requires_measurement_timestamp_for_candidates(self):
        with self.assertRaisesRegex(ValueError, "measured_at is required"):
            WATCH.parse_run_progress(
                json.dumps(self.payload(measured_at=None)), "run-one"
            )

    def test_accepts_empty_run_with_null_measurements(self):
        progress = WATCH.parse_run_progress(
            json.dumps(
                self.payload(
                    active_seconds=0,
                    elapsed_seconds=0,
                    tokens_total=None,
                    active_seconds_source="no_submitted_candidates",
                    elapsed_seconds_source="no_submitted_candidates",
                    tokens_total_source=None,
                    measured_at=None,
                    tokens_measured_at=None,
                    candidate_count=0,
                )
            ),
            "run-one",
        )
        self.assertEqual(progress.tokens_total, 0)
        self.assertIsNone(progress.measured_at)


class PromptTests(unittest.TestCase):
    def test_busy_detector_covers_supported_tuis(self):
        for status in ("Pursuing goal", "Thinking", "Responding", "send now"):
            with self.subTest(status=status):
                self.assertTrue(WATCH.is_worker_busy(status))
        self.assertFalse(WATCH.is_worker_busy("Ready for another prompt"))

    def test_completed_summary_that_says_running_is_idle(self):
        pane = (
            "Verification and search both running in parallel.\n"
            "All checks passed.\n"
            "Ready for another prompt"
        )
        self.assertFalse(WATCH.is_worker_busy(pane))

    def test_live_recent_status_remains_busy(self):
        pane = ("old completed output\n" * 40) + "Working (42s · esc to interrupt)"
        self.assertTrue(WATCH.is_worker_busy(pane))

    def test_nudge_contains_only_assigned_identity_and_metrics(self):
        worker = WATCH.Worker(
            "run-one", "worker-one", "container-one", "grok", ("start",)
        )
        prompt = WATCH.nudge_text(
            worker, 120.0, 4200.0, 14400.0, "/work/SCOREBENCH_4H_REACHED"
        )
        self.assertIn("run-one", prompt)
        self.assertIn("120 seconds", prompt)
        self.assertIn("4200", prompt)
        self.assertNotIn("run-two", prompt)
        self.assertIn("/work/SCOREBENCH_4H_REACHED", prompt)
        self.assertIn("Never use elapsed time", prompt)
        self.assertIn("scorebench run ping --event resume", prompt)

    def test_finalization_prompt_requires_usage_and_completion(self):
        worker = WATCH.Worker(
            "run-one", "worker-one", "container-one", "claude", ("start",)
        )
        prompt = WATCH.finalize_text(
            worker,
            14400.0,
            9000.0,
            "/work/SCOREBENCH_4H_REACHED",
            "/work/GOAL_COMPLETE",
        )
        self.assertIn("run-one", prompt)
        self.assertIn("scorebench run usage", prompt)
        self.assertIn("/work/SCOREBENCH_4H_REACHED", prompt)
        self.assertIn("/work/GOAL_COMPLETE", prompt)
        self.assertIn("scorebench run ping --event resume", prompt)


class CommandTests(unittest.TestCase):
    def test_run_command_hard_bounds_a_wedged_process(self):
        started = time.monotonic()
        result = WATCH.run_command(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            timeout=0.05,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 124)
        self.assertIn("command timed out", result.stderr)
        self.assertLess(elapsed, 3)


class SupervisorTests(unittest.TestCase):
    def config(self, workers=None):
        if workers is None:
            workers = (
                WATCH.Worker(
                    "run-one",
                    "worker-one",
                    "container-one",
                    "codex",
                    ("start",),
                ),
            )
        return WATCH.Config(
            tmux_session="ant",
            report_url=None,
            docker_command=("docker",),
            recovery_poll_seconds=30,
            active_poll_seconds=120,
            activity_heartbeat_seconds=300,
            target_active_seconds=14400,
            nudge_seconds=300,
            resume_cooldown_seconds=300,
            completion_marker="/work/GOAL_COMPLETE",
            active_marker="/work/SCOREBENCH_4H_REACHED",
            enforce_active_gate=True,
            workers=workers,
        )

    @staticmethod
    def progress(
        run_id="run-one", active=600, elapsed=700, tokens=5000
    ):
        return WATCH.RunProgress(
            run_id=run_id,
            active_seconds=float(active),
            elapsed_seconds=float(elapsed),
            tokens_total=float(tokens),
            active_seconds_source="server_timestamps_with_run_pings",
            elapsed_seconds_source="server_timestamps",
            tokens_total_source="claude_code_jsonl",
            measured_at="2026-07-29T10:00:00Z",
            tokens_measured_at="2026-07-29T10:01:00Z",
            candidate_count=2,
        )

    def test_worker_progress_uses_scoped_cli_inside_exact_container(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.commands = []

            def docker(self, *args):
                self.commands.append(args)
                payload = ProgressTests.payload()
                return subprocess_result(stdout=json.dumps(payload))

        supervisor = FakeSupervisor(self.config())
        progress = supervisor.worker_progress(supervisor.config.workers[0])
        self.assertEqual(progress.run_id, "run-one")
        self.assertEqual(
            supervisor.commands,
            [
                (
                    "exec",
                    "container-one",
                    "scorebench",
                    "run",
                    "progress",
                )
            ],
        )

    def test_activity_heartbeat_uses_scoped_cli_inside_exact_container(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.commands = []

            def docker(self, *args):
                self.commands.append(args)
                return subprocess_result(
                    stdout=json.dumps(
                        {
                            "run_id": "run-one",
                            "heartbeat": {"event": "activity"},
                        }
                    )
                )

        supervisor = FakeSupervisor(self.config())
        supervisor.record_activity_heartbeat(supervisor.config.workers[0])
        self.assertEqual(
            supervisor.commands,
            [
                (
                    "exec",
                    "container-one",
                    "scorebench",
                    "run",
                    "ping",
                    "--event",
                    "activity",
                    "--note",
                    "watcher observed exact worker pane busy",
                    "--meta",
                    "activity_evidence=watcher_busy",
                )
            ],
        )

    def test_busy_worker_heartbeats_before_reading_progress(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.calls = []

            def marker_exists(self, worker, marker):
                return False

            def capture_pane(self, worker, history=120):
                return "Working (42s - esc to interrupt)"

            def record_activity_heartbeat(self, worker):
                self.calls.append(("heartbeat", worker.run_id))

            def worker_progress(self, worker):
                self.calls.append(("progress", worker.run_id))
                return SupervisorTests.progress()

        supervisor = FakeSupervisor(self.config())
        supervisor.active_once()
        self.assertEqual(
            supervisor.calls[:2],
            [("heartbeat", "run-one"), ("progress", "run-one")],
        )

    def test_idle_and_completed_workers_do_not_emit_activity_heartbeats(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config, *, completed):
                super().__init__(config)
                self.completed = completed
                self.heartbeats = []

            def marker_exists(self, worker, marker):
                return self.completed and marker == self.config.completion_marker

            def capture_pane(self, worker, history=120):
                return "Ready for another prompt"

            def record_activity_heartbeat(self, worker):
                self.heartbeats.append(worker.run_id)

            def worker_progress(self, worker):
                return SupervisorTests.progress()

            def nudge(self, worker, active, tokens):
                pass

        for completed in (False, True):
            with self.subTest(completed=completed):
                supervisor = FakeSupervisor(self.config(), completed=completed)
                supervisor.active_once()
                self.assertEqual(supervisor.heartbeats, [])

    def test_busy_activity_heartbeat_is_throttled(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.heartbeats = []

            def marker_exists(self, worker, marker):
                return False

            def capture_pane(self, worker, history=120):
                return "Thinking"

            def record_activity_heartbeat(self, worker):
                self.heartbeats.append(worker.run_id)

            def worker_progress(self, worker):
                return SupervisorTests.progress()

        supervisor = FakeSupervisor(self.config())
        with mock.patch.object(WATCH.time, "monotonic", return_value=1_000):
            supervisor.active_once()
            supervisor.active_once()
        self.assertEqual(supervisor.heartbeats, ["run-one"])

    def test_unchanged_busy_evidence_does_not_extend_activity(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.heartbeats = []

            def marker_exists(self, worker, marker):
                return False

            def capture_pane(self, worker, history=120):
                return "Thinking"

            def record_activity_heartbeat(self, worker):
                self.heartbeats.append(worker.run_id)

            def worker_progress(self, worker):
                return SupervisorTests.progress()

        supervisor = FakeSupervisor(self.config())
        supervisor.last_activity_heartbeat["run-one"] = 0
        supervisor.last_activity_evidence["run-one"] = WATCH.busy_evidence_fingerprint(
            "Thinking"
        )
        with mock.patch.object(WATCH.time, "monotonic", return_value=1_000):
            with mock.patch.object(WATCH, "log") as watcher_log:
                supervisor.active_once()
        self.assertEqual(supervisor.heartbeats, [])
        messages = "\n".join(call.args[0] for call in watcher_log.call_args_list)
        self.assertIn("activity heartbeat withheld", messages)

    def test_premature_completion_is_preserved_and_not_nudged(self):
        class FakeSupervisor(WATCH.Supervisor):
            def worker_progress(self, worker):
                return SupervisorTests.progress()

            def marker_exists(self, worker, marker):
                return marker == self.config.completion_marker

            def set_marker(self, worker, marker):
                raise AssertionError("below-target worker must not set target marker")

            def capture_pane(self, worker, history=120):
                raise AssertionError("completed worker must not be nudged")

            def nudge(self, worker, active, tokens):
                raise AssertionError("completed worker must not be nudged")

        supervisor = FakeSupervisor(self.config())
        with mock.patch.object(WATCH, "log") as watcher_log:
            supervisor.active_once()
        messages = "\n".join(call.args[0] for call in watcher_log.call_args_list)
        self.assertIn("premature_complete=1", messages)
        self.assertIn("action=preserved", messages)

    def test_below_target_idle_worker_is_nudged_without_deleting_markers(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.nudged = []

            def worker_progress(self, worker):
                return SupervisorTests.progress()

            def marker_exists(self, worker, marker):
                return False

            def capture_pane(self, worker, history=120):
                return "Ready for another prompt"

            def nudge(self, worker, active, tokens):
                self.nudged.append((worker.run_id, active, tokens))

        supervisor = FakeSupervisor(self.config())
        supervisor.active_once()
        self.assertEqual(supervisor.nudged, [("run-one", 600, 5000)])

    def test_active_target_sets_marker_and_finalizes_idle_worker(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.markers = []
                self.finalized = []

            def worker_progress(self, worker):
                return SupervisorTests.progress(
                    active=14400, elapsed=15000, tokens=9000
                )

            def marker_exists(self, worker, marker):
                return False

            def set_marker(self, worker, marker):
                self.markers.append((worker.run_id, marker))
                return True

            def capture_pane(self, worker, history=120):
                return "Ready for another prompt"

            def finalize(self, worker, active, tokens):
                self.finalized.append((worker.run_id, active, tokens))

        supervisor = FakeSupervisor(self.config())
        supervisor.active_once()

        self.assertEqual(
            supervisor.markers,
            [("run-one", "/work/SCOREBENCH_4H_REACHED")],
        )
        self.assertEqual(supervisor.finalized, [("run-one", 14400, 9000)])

    def test_active_target_does_not_interrupt_busy_worker(self):
        class FakeSupervisor(WATCH.Supervisor):
            def worker_progress(self, worker):
                return SupervisorTests.progress(
                    active=14400, elapsed=15000, tokens=9000
                )

            def marker_exists(self, worker, marker):
                return False

            def set_marker(self, worker, marker):
                return True

            def capture_pane(self, worker, history=120):
                return "Working (42s · esc to interrupt)"

            def finalize(self, worker, active, tokens):
                raise AssertionError("busy worker must not be interrupted")

        supervisor = FakeSupervisor(self.config())
        supervisor.active_once()

    def test_existing_target_marker_survives_metric_regression(self):
        class FakeSupervisor(WATCH.Supervisor):
            def worker_progress(self, worker):
                return SupervisorTests.progress(active=100, elapsed=200, tokens=50)

            def marker_exists(self, worker, marker):
                return marker in (
                    self.config.active_marker,
                    self.config.completion_marker,
                )

            def nudge(self, worker, active, tokens):
                raise AssertionError("marked worker must not be nudged")

        supervisor = FakeSupervisor(self.config())
        supervisor.high_water_active["run-one"] = 14400
        supervisor.high_water_elapsed["run-one"] = 15000
        supervisor.high_water_tokens["run-one"] = 9000
        supervisor.active_once()
        self.assertEqual(supervisor.high_water_active["run-one"], 14400)
        self.assertEqual(supervisor.high_water_tokens["run-one"], 9000)

    def test_progress_failure_for_one_worker_does_not_block_siblings(self):
        workers = (
            WATCH.Worker(
                "run-one", "worker-one", "container-one", "codex", ("start",)
            ),
            WATCH.Worker(
                "run-two", "worker-two", "container-two", "claude", ("start",)
            ),
        )

        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.markers = []

            def worker_progress(self, worker):
                if worker.run_id == "run-one":
                    raise RuntimeError("endpoint unavailable")
                return SupervisorTests.progress(
                    run_id="run-two", active=14400, elapsed=14500
                )

            def marker_exists(self, worker, marker):
                return False

            def set_marker(self, worker, marker):
                self.markers.append((worker.run_id, marker))
                return True

        supervisor = FakeSupervisor(self.config(workers))
        with mock.patch.object(WATCH, "log") as watcher_log:
            supervisor.active_once()
        self.assertEqual(
            supervisor.markers,
            [("run-two", "/work/SCOREBENCH_4H_REACHED")],
        )
        messages = "\n".join(call.args[0] for call in watcher_log.call_args_list)
        self.assertIn("run-one active-time check failed", messages)

    def test_busy_status_at_top_of_fullscreen_pane_prevents_nudge(self):
        class FakeSupervisor(WATCH.Supervisor):
            def worker_progress(self, worker):
                return SupervisorTests.progress()

            def marker_exists(self, worker, marker):
                return False

            def capture_pane(self, worker, history=120):
                return "Responding\n" + ("\n" * 40)

            def nudge(self, worker, active, tokens):
                raise AssertionError("busy fullscreen worker must not be nudged")

        supervisor = FakeSupervisor(self.config())
        supervisor.active_once()

    def test_missing_window_reattaches_running_container(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.created = []

            def capture_pane(self, worker, history=120):
                return ""

            def pane_state(self, worker):
                return None

            def marker_exists(self, worker, marker):
                return False

            def container_running(self, worker):
                return True

            def create_window(self, worker, command):
                self.created.append((worker.run_id, command))

        supervisor = FakeSupervisor(self.config())
        supervisor.recovery_once()
        self.assertEqual(
            supervisor.created,
            [("run-one", ("docker", "attach", "container-one"))],
        )

    def test_pane_state_requires_exact_window_membership(self):
        class FakeSupervisor(WATCH.Supervisor):
            def tmux(self, *args):
                if args[0] == "list-windows":
                    return subprocess.CompletedProcess(
                        args, 0, "worker-one-copy\nother\n", ""
                    )
                raise AssertionError("display-message must not use a missing target")

        supervisor = FakeSupervisor(self.config())
        self.assertIsNone(supervisor.pane_state(self.config().workers[0]))

    def test_pane_state_reads_an_exact_existing_window(self):
        class FakeSupervisor(WATCH.Supervisor):
            def tmux(self, *args):
                if args[0] == "list-windows":
                    return subprocess.CompletedProcess(
                        args, 0, "worker-one\nother\n", ""
                    )
                if args[0] == "display-message":
                    return subprocess.CompletedProcess(args, 0, "0\n", "")
                raise AssertionError(f"unexpected tmux command: {args}")

        supervisor = FakeSupervisor(self.config())
        self.assertEqual(supervisor.pane_state(self.config().workers[0]), "0")


if __name__ == "__main__":
    unittest.main()
