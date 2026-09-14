"""Low-frequency publication of the proxy ledger, independent of agent tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import token_usage


class AccountingError(RuntimeError):
    pass


class Publisher:
    def __init__(self, workspace: Path, env: dict[str, str]):
        self.workspace = workspace
        self.env = env
        self.log = Path(env["SCOREBENCH_OPENROUTER_LOG"])
        self.state = Path(env["SCOREBENCH_TOKEN_STATE"])
        self.last_flags: list[str] | None = None

    def helper(self, command: str) -> str:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("token_usage.py")), command,
             "--state", str(self.state), "--openrouter-jsonl", str(self.log)],
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

    def publish(self) -> None:
        flags = shlex.split(self.helper("flags"))
        if "--cost-usd" not in flags:
            raise AccountingError("OpenRouter omitted billed cost; refusing to publish a partial total")
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
        control = json.loads(control_path.read_text()) if control_path.exists() else {}
        eligible = accounting_ok and (returncode == 0 or control.get("reason") == "budget_reached")
        event = "finish" if eligible else "failed"
        completed = False
        try:
            result = subprocess.run(
                ["scorebench", "run", "ping", "--event", event,
                 "--note", "OpenRouter wrapper final accounting; native logs retained locally"],
                cwd=self.workspace, env=self.env, capture_output=True, text=True, timeout=15,
            )
            # The assigned run carries completion_policy=budget_or_target;
            # ScoreBench, not a rounded local estimate, certifies completion.
            completed = eligible and result.returncode == 0
            if eligible and not completed:
                subprocess.run(["scorebench", "run", "ping", "--event", "failed",
                                "--note", "OpenRouter worker exited; server did not certify completion"],
                               cwd=self.workspace, env=self.env, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
        report = {"harness_returncode": returncode, "accounting_ok": accounting_ok,
                  "completion_confirmed": completed, "runtime_control": control,
                  "trace_uploaded": False}
        path = self.log.parent / "result.json"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
            json.dump(report, handle, indent=2)
        if not completed:
            print("ScoreBench did not certify worker completion; retain the workspace, session and logs", file=sys.stderr)
        return 0 if completed else 1
