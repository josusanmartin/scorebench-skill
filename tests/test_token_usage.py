import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "scorebench"
    / "scripts"
    / "token_usage.py"
)


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state = self.root / "usage.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def run_helper(self, *args, check=True):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--state", str(self.state)],
            text=True,
            capture_output=True,
            check=check,
        )

    def test_flags_preserve_baseline_provenance_and_confidence(self):
        self.run_helper(
            "start",
            "--total-tokens",
            "100",
            "--source",
            "provider_usage",
            "--confidence",
            "parsed",
        )

        result = self.run_helper("flags", "--total-tokens", "145")

        self.assertEqual(
            result.stdout.strip(),
            "--total-tokens 45 --usage-source api_meter "
            "--usage-confidence parsed --tokens-total-source provider_usage",
        )

    def test_codex_jsonl_is_run_relative(self):
        log = self.root / "codex.jsonl"
        log.write_text(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.run_helper("start", "--codex-jsonl", str(log))
        with log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 20, "output_tokens": 3},
                    }
                )
                + "\n"
            )

        result = self.run_helper("flags", "--codex-jsonl", str(log))

        self.assertIn("--total-tokens 23", result.stdout)
        self.assertIn("--usage-confidence parsed", result.stdout)
        self.assertIn("--tokens-total-source codex_exec_jsonl", result.stdout)

    def test_claude_jsonl_excludes_cache_reads(self):
        log = self.root / "claude.jsonl"
        first = {
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 1000,
                }
            }
        }
        second = {
            "message": {
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 2000,
                }
            }
        }
        log.write_text(json.dumps(first) + "\n", encoding="utf-8")
        self.run_helper("start", "--claude-jsonl", str(log))
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(second) + "\n")

        result = self.run_helper("flags", "--claude-jsonl", str(log))

        self.assertIn("--total-tokens 7", result.stdout)
        self.assertIn("--usage-source claude_code", result.stdout)
        self.assertIn("--usage-confidence parsed", result.stdout)

    def test_rejects_relative_state_path(self):
        """A relative --state follows cwd and silently loses the baseline.

        Because the middleware rejects submissions without a token snapshot,
        that loss blocks every submission for the affected run.
        """
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "start",
                "--state",
                "relative-usage.json",
                "--total-tokens",
                "100",
                "--source",
                "codex_goal",
            ],
            text=True,
            capture_output=True,
            cwd=str(self.root),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--state must be an absolute path", result.stderr)

    def test_absolute_state_survives_a_cwd_change(self):
        """start and flags must agree even when run from different directories."""
        other = self.root / "elsewhere"
        other.mkdir()
        subprocess.run(
            [
                sys.executable, str(SCRIPT), "start",
                "--state", str(self.state),
                "--total-tokens", "100", "--source", "codex_goal",
            ],
            text=True, capture_output=True, check=True, cwd=str(self.root),
        )
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), "flags",
                "--state", str(self.state),
                "--total-tokens", "250", "--source", "codex_goal",
            ],
            text=True, capture_output=True, check=True, cwd=str(other),
        )
        self.assertIn("--total-tokens 150", result.stdout)

    def test_rejects_total_below_baseline(self):
        self.run_helper("start", "--total-tokens", "100")

        result = self.run_helper("flags", "--total-tokens", "99", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lower than the stored baseline", result.stderr)

    def test_rejects_source_or_confidence_change_after_baseline(self):
        self.run_helper(
            "start",
            "--total-tokens",
            "100",
            "--source",
            "provider_usage",
            "--confidence",
            "parsed",
        )

        wrong_source = self.run_helper(
            "flags",
            "--total-tokens",
            "120",
            "--source",
            "codex_goal",
            check=False,
        )
        wrong_confidence = self.run_helper(
            "flags",
            "--total-tokens",
            "120",
            "--confidence",
            "exact",
            check=False,
        )

        self.assertIn("source changed after the run baseline", wrong_source.stderr)
        self.assertIn(
            "confidence changed after the run baseline", wrong_confidence.stderr
        )


if __name__ == "__main__":
    unittest.main()
