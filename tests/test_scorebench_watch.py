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
    / "scorebench_watch.py"
)
SPEC = importlib.util.spec_from_file_location("scorebench_watch", SCRIPT)
WATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WATCH)


def valid_config():
    return {
        "tmux_session": "ant",
        "report_url": "https://scorebench.dev/report.html",
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


class ConfigTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return WATCH.load_config(path)

    def test_loads_defaults(self):
        config = self.load(valid_config())
        self.assertEqual(config.target_active_seconds, 14400)
        self.assertEqual(config.docker_command, ("docker",))
        self.assertTrue(config.enforce_active_gate)
        self.assertEqual(config.workers[0].run_id, "run-one")

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


class ReportTests(unittest.TestCase):
    def test_parses_embedded_report_data(self):
        payload = {"arms": [{"points": [{"run_id": "run-one"}]}]}
        html = (
            '<html><script id="report-data" type="application/json">'
            + json.dumps(payload)
            + "</script></html>"
        )
        self.assertEqual(WATCH.parse_report_html(html), payload)

    def test_selects_latest_active_point_and_ignores_elapsed(self):
        data = {
            "arms": [
                {
                    "points": [
                        {
                            "run_id": "run-one",
                            "wall_seconds": 120,
                            "run_elapsed_seconds": 999999,
                            "tokens_total": 1000,
                        },
                        {
                            "run_id": "run-one",
                            "wall_seconds": 480,
                            "run_elapsed_seconds": 500,
                            "tokens_total": 4200,
                        },
                    ]
                }
            ]
        }
        self.assertEqual(WATCH.latest_run_metrics(data, "run-one"), (480, 4200))

    def test_exact_run_id_only(self):
        data = {
            "arms": [
                {
                    "points": [
                        {
                            "run_id": "run-one-copy",
                            "wall_seconds": 900,
                            "tokens_total": 9000,
                        }
                    ]
                }
            ]
        }
        self.assertEqual(WATCH.latest_run_metrics(data, "run-one"), (0, 0))


class PromptTests(unittest.TestCase):
    def test_busy_detector_covers_supported_tuis(self):
        for status in ("Pursuing goal", "Thinking", "Responding", "send now"):
            with self.subTest(status=status):
                self.assertTrue(WATCH.is_worker_busy(status))
        self.assertFalse(WATCH.is_worker_busy("Ready for another prompt"))

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


class SupervisorTests(unittest.TestCase):
    def config(self):
        worker = WATCH.Worker(
            "run-one", "worker-one", "container-one", "codex", ("start",)
        )
        return WATCH.Config(
            tmux_session="ant",
            report_url="https://scorebench.dev/report.html",
            docker_command=("docker",),
            recovery_poll_seconds=30,
            active_poll_seconds=120,
            target_active_seconds=14400,
            nudge_seconds=300,
            resume_cooldown_seconds=300,
            completion_marker="/work/GOAL_COMPLETE",
            active_marker="/work/SCOREBENCH_4H_REACHED",
            enforce_active_gate=True,
            workers=(worker,),
        )

    def test_active_gate_removes_premature_completion_and_nudges_idle_worker(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.removed = []
                self.nudged = []

            def remove_markers(self, worker, *markers):
                self.removed.append((worker.run_id, markers))
                return True

            def capture_pane(self, worker, history=120):
                return "Ready for another prompt"

            def nudge(self, worker, active, tokens):
                self.nudged.append((worker.run_id, active, tokens))

        report = {
            "arms": [
                {
                    "points": [
                        {
                            "run_id": "run-one",
                            "wall_seconds": 600,
                            "tokens_total": 5000,
                        }
                    ]
                }
            ]
        }
        supervisor = FakeSupervisor(self.config())
        with mock.patch.object(WATCH, "fetch_report", return_value=report):
            supervisor.active_once()

        self.assertEqual(
            supervisor.removed,
            [
                (
                    "run-one",
                    ("/work/SCOREBENCH_4H_REACHED", "/work/GOAL_COMPLETE"),
                )
            ],
        )
        self.assertEqual(supervisor.nudged, [("run-one", 600, 5000)])

    def test_active_target_sets_marker_without_nudging(self):
        class FakeSupervisor(WATCH.Supervisor):
            def __init__(self, config):
                super().__init__(config)
                self.markers = []

            def set_marker(self, worker, marker):
                self.markers.append((worker.run_id, marker))
                return True

            def nudge(self, worker, active, tokens):
                raise AssertionError("target-reached worker must not be nudged")

        report = {
            "arms": [
                {
                    "points": [
                        {
                            "run_id": "run-one",
                            "wall_seconds": 14400,
                            "tokens_total": 9000,
                        }
                    ]
                }
            ]
        }
        supervisor = FakeSupervisor(self.config())
        with mock.patch.object(WATCH, "fetch_report", return_value=report):
            supervisor.active_once()

        self.assertEqual(
            supervisor.markers,
            [("run-one", "/work/SCOREBENCH_4H_REACHED")],
        )

    def test_busy_status_at_top_of_fullscreen_pane_prevents_nudge(self):
        class FakeSupervisor(WATCH.Supervisor):
            def remove_markers(self, worker, *markers):
                return True

            def capture_pane(self, worker, history=120):
                return "Responding\n" + ("\n" * 40)

            def nudge(self, worker, active, tokens):
                raise AssertionError("busy fullscreen worker must not be nudged")

        report = {
            "arms": [
                {
                    "points": [
                        {
                            "run_id": "run-one",
                            "wall_seconds": 600,
                            "tokens_total": 5000,
                        }
                    ]
                }
            ]
        }
        supervisor = FakeSupervisor(self.config())
        with mock.patch.object(WATCH, "fetch_report", return_value=report):
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


if __name__ == "__main__":
    unittest.main()
