import json
import os
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
        thread_id = "019fc3d8-e800-7f62-be5f-78bfdbeea6ba"
        log.write_text(
            json.dumps({"type": "thread.started", "thread_id": thread_id})
            + "\n"
            + json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 110,
                        "output_tokens": 5,
                        "cached_input_tokens": 100,
                        "cache_write_input_tokens": 0,
                        "reasoning_output_tokens": 2,
                    },
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
                        "usage": {
                            # Codex emits usage for this turn. input_tokens
                            # includes the cached subset.
                            "input_tokens": 180,
                            "output_tokens": 8,
                            "cached_input_tokens": 150,
                            "cache_write_input_tokens": 0,
                            "reasoning_output_tokens": 3,
                        },
                    }
                )
                + "\n"
            )

        result = self.run_helper("flags", "--codex-jsonl", str(log))

        self.assertIn("--total-tokens 38", result.stdout)
        self.assertIn("--input-tokens 30", result.stdout)
        self.assertIn("--output-tokens 8", result.stdout)
        self.assertIn("--cache-creation-tokens 0", result.stdout)
        self.assertIn("--cache-read-tokens 150", result.stdout)
        self.assertIn("--reasoning-output-tokens 3", result.stdout)
        self.assertIn("--usage-confidence exact", result.stdout)
        self.assertIn("--tokens-total-source codex_exec_jsonl", result.stdout)

    def test_codex_resumed_live_turns_are_summed(self):
        cases = (
            (
                "gpt-5.6-sol",
                (
                    (17_125, 11_008, 7, 0),
                    (17_145, 16_128, 7, 0),
                    (17_166, 16_128, 8, 0),
                ),
                6_124,
                {
                    "input_tokens": 2_055,
                    "output_tokens": 15,
                    "cache_read_tokens": 32_256,
                    "reasoning_output_tokens": 0,
                },
                2_070,
            ),
            (
                "gpt-5.4-mini",
                (
                    (14_501, 4_352, 21, 12),
                    (16_311, 14_080, 20, 11),
                    (20_280, 16_128, 23, 13),
                ),
                10_170,
                {
                    "input_tokens": 6_383,
                    "output_tokens": 43,
                    "cache_read_tokens": 30_208,
                    "reasoning_output_tokens": 24,
                },
                6_426,
            ),
        )
        for model, turns, baseline, expected_usage, expected_total in cases:
            with self.subTest(model=model):
                log = self.root / f"{model}.jsonl"
                thread_id = f"thread-{model}"

                def event(turn):
                    inclusive_input, cached_input, output, reasoning = turn
                    return {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": inclusive_input,
                            "cached_input_tokens": cached_input,
                            "cache_write_input_tokens": 0,
                            "output_tokens": output,
                            "reasoning_output_tokens": reasoning,
                        },
                    }

                log.write_text(
                    json.dumps({"type": "thread.started", "thread_id": thread_id})
                    + "\n"
                    + json.dumps(event(turns[0]))
                    + "\n",
                    encoding="utf-8",
                )
                started = json.loads(
                    self.run_helper("start", "--codex-jsonl", str(log)).stdout
                )
                self.assertEqual(started["baseline_total_tokens"], baseline)
                with log.open("a", encoding="utf-8") as handle:
                    for turn in turns[1:]:
                        handle.write(
                            json.dumps(
                                {"type": "thread.started", "thread_id": thread_id}
                            )
                            + "\n"
                            + json.dumps(event(turn))
                            + "\n"
                        )

                status = json.loads(
                    self.run_helper("status", "--codex-jsonl", str(log)).stdout
                )

                self.assertEqual(status["run_total_tokens"], expected_total)
                for field, value in expected_usage.items():
                    self.assertEqual(status["run_usage"][field], value)

    def test_claude_jsonl_excludes_cache_reads(self):
        log = self.root / "claude.jsonl"
        first = {
            "message": {
                "id": "msg_first",
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
                "id": "msg_second",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 2000,
                }
            }
        }
        # Claude Code can serialize the same assistant message several times.
        # Stable message IDs make those records one billable provider response.
        log.write_text(json.dumps(first) + "\n" + json.dumps(first) + "\n", encoding="utf-8")
        self.run_helper("start", "--claude-jsonl", str(log))
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(second) + "\n" + json.dumps(second) + "\n")

        result = self.run_helper("flags", "--claude-jsonl", str(log))

        self.assertIn("--total-tokens 7", result.stdout)
        self.assertIn("--input-tokens 3", result.stdout)
        self.assertIn("--output-tokens 4", result.stdout)
        self.assertIn("--cache-creation-tokens 0", result.stdout)
        self.assertIn("--cache-read-tokens 2000", result.stdout)
        self.assertIn("--usage-source claude_code", result.stdout)
        self.assertIn("--usage-confidence exact", result.stdout)

    def test_claude_stream_json_output_is_rejected(self):
        log = self.root / "claude-stream.jsonl"
        log.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "msg_partial",
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 1,
                            "cache_creation_input_tokens": 76,
                            "cache_read_input_tokens": 2865,
                        },
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "result",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 11,
                        "cache_creation_input_tokens": 76,
                        "cache_read_input_tokens": 2865,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.run_helper(
            "start", "--claude-jsonl", str(log), check=False
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stream-json output is not an exact session transcript", result.stderr)

    def test_grok_jsonl_sums_inferences_and_excludes_cache_reads(self):
        grok_root = self.root / ".grok"
        updates = grok_root / "sessions" / "%2Fwork" / "session-1" / "updates.jsonl"
        unified = grok_root / "logs" / "unified.jsonl"
        updates.parent.mkdir(parents=True)
        unified.parent.mkdir(parents=True)
        updates.write_text(
            json.dumps(
                {
                    "method": "session/update",
                    "params": {"sessionId": "session-1", "update": {}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        def inference(ts, prompt, cached, completion, reasoning, loop_index):
            return {
                "ts": ts,
                "msg": "shell.turn.inference_done",
                "sid": "session-1",
                "ctx": {
                    "prompt_tokens": prompt,
                    "cached_prompt_tokens": cached,
                    "completion_tokens": completion,
                    "reasoning_tokens": reasoning,
                    "loop_index": loop_index,
                },
            }

        first = inference("2026-08-10T00:00:00Z", 17_038, 128, 50, 47, 0)
        second = inference("2026-08-10T00:01:00Z", 26_253, 17_024, 22, 15, 1)
        # An exact duplicate log line is a replay, not another provider call.
        unified.write_text(
            json.dumps(first) + "\n" + json.dumps(first) + "\n",
            encoding="utf-8",
        )
        started = json.loads(
            self.run_helper("start", "--grok-jsonl", str(updates)).stdout
        )
        self.assertEqual(started["baseline_total_tokens"], 16_960)
        self.assertEqual(started["baseline_usage"]["input_tokens"], 16_910)
        self.assertEqual(started["baseline_usage"]["cache_read_tokens"], 128)
        with unified.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(second) + "\n")

        result = self.run_helper("flags", "--grok-jsonl", str(updates))

        self.assertIn("--total-tokens 9251", result.stdout)
        self.assertIn("--input-tokens 9229", result.stdout)
        self.assertIn("--output-tokens 22", result.stdout)
        self.assertIn("--cache-creation-tokens 0", result.stdout)
        self.assertIn("--cache-read-tokens 17024", result.stdout)
        self.assertIn("--reasoning-output-tokens 15", result.stdout)
        self.assertIn("--usage-source grok_build", result.stdout)
        self.assertIn("--usage-confidence exact", result.stdout)
        self.assertIn("--tokens-total-source grok_session_jsonl", result.stdout)

    def test_grok_conflicting_duplicate_inference_fails_loudly(self):
        updates = self.root / "grok-conflict-updates.jsonl"
        unified = self.root / "grok-conflict-unified.jsonl"
        updates.write_text(
            json.dumps({"params": {"sessionId": "session-1"}}) + "\n",
            encoding="utf-8",
        )
        base = {
            "ts": "2026-08-10T00:00:00Z",
            "msg": "shell.turn.inference_done",
            "sid": "session-1",
            "ctx": {
                "prompt_tokens": 100,
                "cached_prompt_tokens": 90,
                "completion_tokens": 3,
                "loop_index": 0,
            },
        }
        first = json.loads(json.dumps(base))
        first["ctx"]["reasoning_tokens"] = 1
        second = json.loads(json.dumps(base))
        second["ctx"]["reasoning_tokens"] = 2
        unified.write_text(
            json.dumps(first) + "\n" + json.dumps(second) + "\n",
            encoding="utf-8",
        )

        result = self.run_helper(
            "start",
            "--grok-jsonl",
            str(updates),
            "--grok-log",
            str(unified),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting Grok inference records", result.stderr)

    def test_grok_jsonl_rejects_multiple_sessions_and_schema_drift(self):
        multiple = self.root / "grok-multiple.jsonl"
        multiple.write_text(
            json.dumps({"params": {"sessionId": "session-1"}})
            + "\n"
            + json.dumps({"params": {"sessionId": "session-2"}})
            + "\n",
            encoding="utf-8",
        )
        result = self.run_helper(
            "start", "--grok-jsonl", str(multiple), check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiple Grok sessions", result.stderr)

        changed = self.root / "grok-schema-changed.jsonl"
        changed.write_text(
            json.dumps({"params": {"sessionId": "session-1"}}) + "\n",
            encoding="utf-8",
        )
        changed_log = self.root / "grok-schema-changed-unified.jsonl"
        changed_log.write_text(
            json.dumps(
                {
                    "ts": "2026-08-10T00:00:00Z",
                    "msg": "shell.turn.inference_done",
                    "sid": "session-1",
                    "ctx": {
                        "prompt_tokens": 10,
                        "cached_prompt_tokens": 11,
                        "completion_tokens": 2,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = self.run_helper(
            "start",
            "--grok-jsonl",
            str(changed),
            "--grok-log",
            str(changed_log),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cached_prompt_tokens exceeds", result.stderr)

    def test_grok_empty_session_has_an_exact_zero_baseline(self):
        updates = self.root / "grok-empty-updates.jsonl"
        unified = self.root / "grok-empty-unified.jsonl"
        updates.write_text(
            json.dumps({"params": {"sessionId": "session-1"}}) + "\n",
            encoding="utf-8",
        )
        unified.write_text("", encoding="utf-8")

        started = json.loads(
            self.run_helper(
                "start",
                "--grok-jsonl",
                str(updates),
                "--grok-log",
                str(unified),
            ).stdout
        )

        self.assertEqual(started["baseline_total_tokens"], 0)
        self.assertEqual(started["baseline_usage"]["cache_read_tokens"], 0)

    def test_jsonl_breakdown_defines_scorebench_working_total(self):
        log = self.root / "codex-total.jsonl"
        thread_id = "019fc3d8-e800-7f62-be5f-78bfdbeea6ba"
        first = {
            "type": "turn.completed",
            "usage": {
                "total_tokens": 117,
                "input_tokens": 115,
                "output_tokens": 2,
                "cached_input_tokens": 100,
                "cache_write_input_tokens": 5,
            },
        }
        second = {
            "type": "turn.completed",
            "usage": {
                "total_tokens": 149,
                "input_tokens": 143,
                "output_tokens": 6,
                "cached_input_tokens": 120,
                "cache_write_input_tokens": 8,
            },
        }
        log.write_text(
            json.dumps({"type": "thread.started", "thread_id": thread_id})
            + "\n"
            + json.dumps(first)
            + "\n",
            encoding="utf-8",
        )
        self.run_helper("start", "--codex-jsonl", str(log))
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(second) + "\n")

        result = self.run_helper("flags", "--codex-jsonl", str(log))

        self.assertIn("--total-tokens 29", result.stdout)
        self.assertIn("--input-tokens 15", result.stdout)
        self.assertIn("--output-tokens 6", result.stdout)
        self.assertIn("--cache-creation-tokens 8", result.stdout)
        self.assertIn("--cache-read-tokens 120", result.stdout)

    def test_codex_real_schema_excludes_cached_input_from_working_total(self):
        log = self.root / "codex-real.jsonl"
        log.write_text(
            json.dumps({"type": "thread.started", "thread_id": "thread-1"})
            + "\n"
            + json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10_739,
                        "cached_input_tokens": 7_936,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 221,
                        "reasoning_output_tokens": 201,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        started = json.loads(
            self.run_helper("start", "--codex-jsonl", str(log)).stdout
        )
        self.assertEqual(started["baseline_total_tokens"], 3_024)
        self.assertEqual(started["baseline_usage"]["input_tokens"], 2_803)
        self.assertEqual(started["baseline_usage"]["cache_read_tokens"], 7_936)
        status = json.loads(
            self.run_helper("status", "--codex-jsonl", str(log)).stdout
        )
        self.assertEqual(status["absolute_total_tokens"], 3_024)
        self.assertEqual(status["accounting_version"], 3)
        self.assertEqual(status["run_usage"]["input_tokens"], 0)

    def test_codex_v2_state_keeps_cumulative_snapshot_semantics(self):
        log = self.root / "codex-v2.jsonl"
        first = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 110,
                "cached_input_tokens": 100,
                "output_tokens": 5,
            },
        }
        second = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 180,
                "cached_input_tokens": 150,
                "output_tokens": 8,
            },
        }
        log.write_text(
            json.dumps({"type": "thread.started", "thread_id": "thread-v2"})
            + "\n"
            + json.dumps(first)
            + "\n"
            + json.dumps(second)
            + "\n",
            encoding="utf-8",
        )
        self.state.write_text(
            json.dumps(
                {
                    "accounting_version": 2,
                    "baseline_total_tokens": 15,
                    "baseline_usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_creation_tokens": 0,
                        "cache_read_tokens": 100,
                    },
                    "confidence": "exact",
                    "tokens_total_source": "codex_exec_jsonl",
                    "usage_source": "codex_usage",
                }
            ),
            encoding="utf-8",
        )

        result = self.run_helper("flags", "--codex-jsonl", str(log))

        self.assertIn("--total-tokens 23", result.stdout)
        self.assertIn("--input-tokens 20", result.stdout)
        self.assertIn("--output-tokens 3", result.stdout)
        self.assertIn("--cache-read-tokens 50", result.stdout)

    def test_claude_conflicting_duplicate_usage_fails_loudly(self):
        log = self.root / "claude-conflict.jsonl"
        records = [
            {"message": {"id": "msg_same", "usage": {"input_tokens": 10, "output_tokens": 2}}},
            {"message": {"id": "msg_same", "usage": {"input_tokens": 10, "output_tokens": 3}}},
        ]
        log.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

        result = self.run_helper("start", "--claude-jsonl", str(log), check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting Claude usage records", result.stderr)

    def test_codex_multiple_threads_fail_loudly(self):
        log = self.root / "codex-multiple.jsonl"
        records = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1}},
            {"type": "thread.started", "thread_id": "thread-2"},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1}},
        ]
        log.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

        result = self.run_helper("start", "--codex-jsonl", str(log), check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiple Codex threads", result.stderr)

    def test_old_state_without_component_baselines_remains_compatible(self):
        self.state.write_text(
            json.dumps(
                {
                    "baseline_total_tokens": 100,
                    "confidence": "parsed",
                    "tokens_total_source": "codex_exec_jsonl",
                    "usage_source": "codex_usage",
                }
            ),
            encoding="utf-8",
        )
        log = self.root / "codex-old-state.jsonl"
        log.write_text(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"total_tokens": 145, "input_tokens": 120, "output_tokens": 25},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.run_helper("flags", "--codex-jsonl", str(log))

        self.assertIn("--total-tokens 45", result.stdout)
        self.assertNotIn("--input-tokens", result.stdout)

    def test_old_claude_state_keeps_legacy_parser_for_active_run(self):
        first = {
            "message": {
                "id": "msg_first",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": 0,
                },
            }
        }
        second = {
            "message": {
                "id": "msg_second",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "cache_creation_input_tokens": 0,
                },
            }
        }
        log = self.root / "claude-legacy.jsonl"
        log.write_text(
            json.dumps(first) + "\n" + json.dumps(first) + "\n",
            encoding="utf-8",
        )
        self.state.write_text(
            json.dumps(
                {
                    "baseline_total_tokens": 24,
                    "baseline_usage": {
                        "input_tokens": 20,
                        "output_tokens": 4,
                        "cache_creation_tokens": 0,
                    },
                    "confidence": "parsed",
                    "tokens_total_source": "claude_code_jsonl",
                    "usage_source": "claude_code",
                }
            ),
            encoding="utf-8",
        )
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(second) + "\n" + json.dumps(second) + "\n")

        result = self.run_helper("flags", "--claude-jsonl", str(log))

        self.assertIn("--total-tokens 14", result.stdout)
        self.assertIn("--input-tokens 6", result.stdout)
        self.assertIn("--output-tokens 8", result.stdout)
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

    def test_flags_also_rejects_a_relative_state_path(self):
        """The read path must reject a relative --state, not just start.

        cmd_start and current_run_total resolve the path separately, so a guard
        on only one of them still lets a lane read a different baseline than it
        wrote.
        """
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), "flags",
                "--state", "relative-usage.json",
                "--total-tokens", "250", "--source", "codex_goal",
            ],
            text=True, capture_output=True, cwd=str(self.root),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--state must be an absolute path", result.stderr)

    def test_requires_a_state_path_when_env_var_is_unset(self):
        """With no --state and no SCOREBENCH_TOKEN_STATE, fail with guidance.

        The previous default resolved against the working directory, and a
        hardcoded container path raised an unhandled PermissionError anywhere
        that path was not writable.
        """
        env = dict(os.environ)
        env.pop("SCOREBENCH_TOKEN_STATE", None)
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), "start",
                "--total-tokens", "100", "--source", "codex_goal",
            ],
            text=True, capture_output=True, cwd=str(self.root), env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no token state path", result.stderr)
        self.assertIn("SCOREBENCH_TOKEN_STATE", result.stderr)

    def test_state_env_var_supplies_the_default(self):
        """SCOREBENCH_TOKEN_STATE lets a run set the path once."""
        env = dict(os.environ)
        env["SCOREBENCH_TOKEN_STATE"] = str(self.state)
        subprocess.run(
            [
                sys.executable, str(SCRIPT), "start",
                "--total-tokens", "100", "--source", "codex_goal",
            ],
            text=True, capture_output=True, check=True, cwd=str(self.root), env=env,
        )
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), "flags",
                "--total-tokens", "250", "--source", "codex_goal",
            ],
            text=True, capture_output=True, check=True,
            cwd=str(self.root / ".."), env=env,
        )
        self.assertIn("--total-tokens 150", result.stdout)

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
