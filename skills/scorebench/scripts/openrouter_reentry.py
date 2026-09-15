"""Bounded OpenCode continuation; never create a replacement session."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from openrouter_agents import option, selected_route
from openrouter_accounting import AccountingError

MAX_REENTRIES = 3
MAX_EXPORT_BYTES = 64 * 1024 * 1024
CONTINUABLE_FINISHES = {"length", "stop"}
CONTINUE_PROMPT = (
    "The previous response reached its output limit. Continue the same assigned ScoreBench goal "
    "in this session and workspace. Keep the original run identity, model, effort, and token baseline. "
    "Prefer bounded reasoning and concrete tool work rather than another oversized response. "
    "The supervisor owns final usage and completion. Check progress and obey the remaining budget "
    "and explicit stops; do not restart the run or repeat unchanged submissions."
)
EARLY_STOP_PROMPT = (
    "Your previous turn ended before the assigned ScoreBench budget was completed. "
    "Continue the SAME goal, session and workspace with the original run, model, effort and token baseline. "
    "A conversation summary, compaction, empty answer, plateau or normal turn ending is not completion. "
    "Use the retained summary and your own work to take the next concrete optimization or validation step. "
    "Preserve the best correct candidate and submit materially improved validated work when allowed. "
    "Check scoped progress and obey the remaining budget, configured target and explicit stops. "
    "The supervisor owns final accounting and completion; do not restart the run, reset usage, "
    "repeat the original prompt or blindly resubmit a pending candidate."
)
GAP_PROMPT = (
    "The owner explicitly accepted partial accounting to recover this missing-receipt interruption. "
    "Continue the original assigned ScoreBench goal in the SAME session and workspace; preserve "
    "run identity, model, effort, receipts and zero baseline. Generation lookups, when available, "
    "are already included by the supervisor; do not add them again. Final charges remain uncertain: "
    "cost and tokens are lower bounds, and remaining budget is only an upper bound. "
    "Do not claim exact accounting or reset the ledger. The supervisor owns finalization. "
    "Obey budget and explicit stops. Inspect your own history before retrying an uncertain "
    "submission; never duplicate it blindly. Use concrete, bounded tool work."
)


def write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        json.dump(value, handle, indent=2)
    os.replace(temp, path)


def resume_command(command: list[str], session: str, *, accounting_gap=False, finish_reason="length") -> list[str]:
    if not accounting_gap and finish_reason not in CONTINUABLE_FINISHES:
        raise AccountingError("unsupported native finish reason for continuation")
    if not re.fullmatch(r"ses_[A-Za-z0-9]+", session):
        raise AccountingError("invalid retained OpenCode session ID")
    if command[1:2] != ["run"] or option(command, "--format") != "json":
        raise AccountingError("OpenCode recovery requires run --format json")
    # Only the documented headless invocation can be resumed automatically.
    # Unknown flags, --fork, --continue, --attach and alternate workspaces fail closed.
    valued = {"--model", "-m", "--variant", "--format", "--title", "--agent"}
    switches = {"--pure", "--auto", "--thinking"}
    base, messages = command[:2], []
    index = 2
    while index < len(command):
        arg = command[index]
        key = arg.split("=", 1)[0]
        if key in valued:
            base.append(arg)
            if "=" not in arg:
                index += 1
                if index >= len(command):
                    raise AccountingError("incomplete OpenCode launch option")
                base.append(command[index])
        elif arg in switches:
            base.append(arg)
        elif arg.startswith("-"):
            raise AccountingError("unsupported OpenCode option for same-session recovery")
        else:
            messages.append(arg)
        index += 1
    if len(messages) != 1:
        raise AccountingError("recovery requires one original worker prompt")
    prompt = GAP_PROMPT if accounting_gap else EARLY_STOP_PROMPT if finish_reason == "stop" else CONTINUE_PROMPT
    return [*base, "--session", session, prompt]


class SessionOutput:
    def __init__(self, directory: Path, session: str = ""):
        self.directory = directory
        self.session = session
        self.reason = ""
        self.error = False

    def observe(self, line: bytes) -> None:
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("type") not in {"step_start", "step_finish", "error"}:
                return
            session = event.get("sessionID", "")
            if not isinstance(session, str) or not re.fullmatch(r"ses_[A-Za-z0-9]+", session):
                return
            if self.session and session != self.session:
                self.error = True
                return
            self.session = session
            if event["type"] == "error":
                self.error = True
            if event["type"] == "step_start":
                self.reason = ""
            if event["type"] == "step_finish":
                self.reason = event.get("part", {}).get("reason", "")
        except (ValueError, TypeError, AttributeError):
            return

    def copy_stdout(self, stream) -> None:
        path = self.directory / "native-output.jsonl"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as log:
            partial = False
            while chunk := stream.readline(1024 * 1024):
                log.write(chunk)
                log.flush()
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except BrokenPipeError:
                    pass
                if not partial and chunk.endswith(b"\n"):
                    self.observe(chunk)
                partial = not chunk.endswith(b"\n")


def inspect_session(command: list[str], session: str, workspace: Path, env: dict, *, accounting_gap=False) -> str | None:
    # OpenCode may exit before piped stdout drains. A private regular file
    # receives the full export without repairing or guessing truncated JSON.
    with tempfile.TemporaryFile() as exported:
        result = subprocess.run([command[0], "--pure", "export", session], cwd=workspace,
                                env=env, stdout=exported, stderr=subprocess.PIPE, timeout=30)
        if result.returncode:
            raise AccountingError("retained OpenCode session cannot be read")
        if exported.tell() > MAX_EXPORT_BYTES:
            raise AccountingError("retained OpenCode session export exceeds validation size limit")
        exported.seek(0)
        try:
            data = json.load(exported)
        except (ValueError, UnicodeError) as exc:
            raise AccountingError("retained OpenCode session export is not complete valid JSON") from exc
    try:
        info = data["info"]
        if info["id"] != session or info.get("parentID") or Path(info["directory"]).resolve() != workspace:
            raise ValueError("session belongs to a different workspace")
        assistants = [m["info"] for m in data["messages"] if m["info"].get("role") == "assistant"]
        last = assistants[-1]
        provider, model = selected_route("OpenCode", command)
        if ((not accounting_gap and (last.get("finish") not in CONTINUABLE_FINISHES or last.get("error")))
                or any(m.get("modelID") != model or m.get("providerID") != provider for m in assistants)):
            raise ValueError("session is not a continuable run of the assigned model")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AccountingError("retained OpenCode session failed identity/terminal-state validation") from exc
    return last.get("finish")


def admit_reentry(publisher, metadata: dict, command: list[str], session: str, *, manual=False, check=False, accounting_gap=False, expected_finish=None) -> dict:
    resume_command(command, session)
    if not check:
        publisher.publish()
    progress = publisher.read("progress")
    current = publisher.read("current")
    start = json.loads((publisher.workspace / ".scorebench/supervisor-run-start.json").read_text())
    run_id = start["run"]["run_id"]
    model = selected_route("OpenCode", command)[1]
    for run in (start["run"], current["run"]):
        assigned = run["metadata"]
        if (run["run_id"] != run_id or assigned.get("coding_harness") != "OpenCode"
                or assigned.get("model") not in {model, "openrouter/" + model}
                or assigned.get("effort") != option(command, "--variant")
                or assigned.get("completion_policy") != "budget_or_target"):
            raise AccountingError("reentry does not match the original supervised run assignment")
    if progress.get("scope", {}).get("kind") != "run_token" or progress["run"]["run_id"] != run_id:
        raise AccountingError("reentry requires the original scoped run credential")
    if progress["run"].get("status") in {"finished", "stopped", "revoked"}:
        raise AccountingError("cannot recover a finished or stopped run")
    budget = progress["progress"]["budget"]
    remaining = budget.get("remaining")
    if (budget.get("available") is not True or budget.get("reached") is not False
            or budget.get("type") not in {"cost", "time", "tokens"}
            or type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining <= 0):
        raise AccountingError("reentry requires a conclusive remaining fixed budget")
    estimate = metadata.get("resumeCostUpperBound")
    if budget["type"] == "cost" and (
        type(estimate) not in (int, float) or not math.isfinite(estimate) or estimate <= 0 or remaining < estimate
    ):
        raise AccountingError("insufficient resume budget for a cold-context request; retain the worker")
    if manual and publisher.read("gate").get("ready") is not True:
        raise AccountingError("retained worker execution gate is not ready")
    finish_reason = inspect_session(command, session, publisher.workspace, publisher.env, accounting_gap=accounting_gap)
    if expected_finish is not None and finish_reason != expected_finish:
        raise AccountingError("native session finish disagrees with captured output")
    reason = "accounting_gap" if accounting_gap else "early_stop" if finish_reason == "stop" else "length"
    path = publisher.log.parent / "reentries.json"
    record = json.loads(path.read_text()) if path.exists() else {"run_id": run_id, "session_id": session, "attempts": []}
    if record["run_id"] != run_id or record["session_id"] != session or len(record["attempts"]) >= MAX_REENTRIES:
        raise AccountingError("OpenCode same-session recovery limit or identity mismatch")
    if check:
        return {"recoverable": True, "run_id": run_id, "session_id": session,
                "reason": reason, "native_finish_reason": finish_reason,
                "attempts_remaining": MAX_REENTRIES - len(record["attempts"]),
                "remaining": remaining, "resume_cost_estimate": estimate,
                "accounting_complete": not accounting_gap,
                "remaining_is_upper_bound": accounting_gap,
                "context_window": metadata.get("contextWindow"), "max_output_tokens": metadata.get("maxTokens")}
    record["attempts"].append({"reason": reason, "native_finish_reason": finish_reason, "manual": manual, "remaining": remaining,
                               "resume_cost_estimate": estimate, "at": time.time(),
                               "context_window": metadata.get("contextWindow"), "max_output_tokens": metadata.get("maxTokens")})
    # Reserve the attempt before reopening lifecycle or launching another model.
    write_json(path, record)
    if not publisher.ping("resume", "OpenCode same-session recovery; owner accepted incomplete accounting" if accounting_gap
                          else "OpenCode same-session continuation after early stop" if finish_reason == "stop"
                          else "OpenCode same-session continuation after output limit"):
        raise AccountingError("could not confirm the resume heartbeat")
    return record
