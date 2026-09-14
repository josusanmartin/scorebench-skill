"""Low-frequency publication of the proxy ledger, independent of agent tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import token_usage
from openrouter_gaps import PARTIAL_SOURCE


class AccountingError(RuntimeError):
    pass


class Publisher:
    def __init__(self, workspace: Path, env: dict[str, str]):
        self.workspace = workspace
        self.env = env
        self.log = Path(env["SCOREBENCH_OPENROUTER_LOG"])
        self.state = Path(env["SCOREBENCH_TOKEN_STATE"])
        self.last_flags: list[str] | None = None
        self.lifecycle_attempts: list[dict] = []
        self.gap_preview: dict | None = None

    def read(self, name: str) -> dict:
        result = subprocess.run(["scorebench", "run", name], cwd=self.workspace, env=self.env,
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise AccountingError("ScoreBench scoped state is unavailable; do not resume")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise AccountingError("invalid scoped state response")
        return data

    def ping(self, event: str, note: str) -> bool:
        for attempt in range(3):
            try:
                result = subprocess.run(["scorebench", "run", "ping", "--event", event, "--note", note],
                                        cwd=self.workspace, env=self.env, capture_output=True, text=True, timeout=15)
                status = "accepted" if result.returncode == 0 else "rejected"
                output = result.stdout + result.stderr
                terminal = any(f"HTTP {code}" in output for code in (400, 401, 403, 404, 409))
            except (OSError, subprocess.TimeoutExpired) as exc:
                status, terminal = type(exc).__name__, False
            # Never retain raw CLI errors: they can contain credential material.
            self.lifecycle_attempts.append({"event": event, "attempt": attempt + 1, "status": status})
            if status == "accepted":
                return True
            if terminal:
                break
            if attempt < 2:
                time.sleep(1 + attempt)
        return False

    def helper(self, command: str) -> str:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("token_usage.py")), command,
             "--state", str(self.state), "--openrouter-jsonl", str(self.log),
             *(["--openrouter-gap-ack", json.dumps(self.gap_preview)] if self.gap_preview else [])],
            cwd=self.workspace, env=self.env, capture_output=True, text=True, timeout=15,
        )
        if result.returncode:
            raise AccountingError("OpenRouter token helper rejected the ledger or baseline")
        return result.stdout

    def initialize(self) -> None:
        if not self.state.exists():
            if self.log.stat().st_size:
                raise AccountingError("nonempty OpenRouter ledger has no baseline; refusing to discard previous usage")
            self.helper("start")
            state = json.loads(self.state.read_text())
            state["supervisor_managed"] = True
            temp = self.state.with_suffix(".tmp")
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
                json.dump(state, handle)
            os.replace(temp, self.state)
        self.env[token_usage.SUPERVISED_ENV_VAR] = "1"
        self.publish()

    def current_flags(self) -> list[str]:
        flags = shlex.split(self.helper("flags"))
        if "--cost-usd" not in flags:
            raise AccountingError("OpenRouter omitted billed cost; refusing to publish a partial total")
        return flags

    def publish(self) -> None:
        flags = self.current_flags()
        if flags == self.last_flags:
            return
        result = subprocess.run(
            ["scorebench", "run", "usage", *flags], cwd=self.workspace, env=self.env,
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode:
            raise AccountingError("ScoreBench rejected the OpenRouter usage snapshot")
        self.last_flags = flags

    def finalize(self, returncode: int, *, accounting_ok: bool) -> int:
        control_path = self.workspace / ".scorebench/runtime-control.json"
        try:
            control = json.loads(control_path.read_text()) if control_path.exists() else {}
        except (OSError, ValueError):
            control = {"reason": "invalid_runtime_control"}
        eligible = accounting_ok and (returncode == 0 or control.get("reason") == "budget_reached")
        event = "finish" if eligible else "failed"
        completed = False
        confirmed = self.ping(event, "OpenRouter wrapper final accounting; native logs retained locally")
        # ScoreBench, not a rounded local estimate, certifies completion.
        completed = eligible and confirmed
        if eligible and not completed:
            # A timed-out finish may have committed. Read before sending failure.
            try:
                completed = self.read("progress")["run"]["status"] == "finished"
            except (OSError, subprocess.TimeoutExpired, AccountingError, ValueError, KeyError):
                pass
            if not completed:
                confirmed = self.ping("failed", "OpenRouter worker exited; server did not certify completion")
            else:
                confirmed = True
        partial = PARTIAL_SOURCE in (self.last_flags or [])
        report = {"harness_returncode": returncode, "accounting_ok": accounting_ok,
                  "accounting_complete": accounting_ok and not partial,
                  "accounting_quality": "partial" if partial else "exact" if accounting_ok else "unavailable",
                  "accounting_warnings": ["Missing OpenRouter receipt; tokens and cost are confirmed lower bounds, not complete totals"] if partial else [],
                  "completion_confirmed": completed, "runtime_control": control,
                  "trace_uploaded": False, "lifecycle_confirmed": confirmed,
                  "lifecycle_attempts": self.lifecycle_attempts}
        path = self.log.parent / "result.json"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            json.dump(report, handle, indent=2)
        if not completed:
            print("ScoreBench did not certify worker completion; retain the workspace, session and logs", file=sys.stderr)
        if not confirmed:
            print("ScoreBench final lifecycle is unconfirmed; report the local exit and server-state mismatch", file=sys.stderr)
        return 0 if completed else 1
