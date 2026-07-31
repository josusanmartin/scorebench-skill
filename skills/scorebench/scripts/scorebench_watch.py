#!/usr/bin/env python3
"""Supervise isolated ScoreBench workers in separate tmux windows."""

import argparse
import hashlib
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


BUSY_PATTERNS = (
    "working (",
    "pursuing goal",
    "esc to interrupt",
    "recombobulating",
    "thinking",
    "responding",
    "tool running",
    "tool is running",
    "send now",
)
BUSY_STATUS_LINES = 16
CAPACITY_PATTERNS = (
    "capacity",
    "overloaded",
    "try again",
    "retry",
    "usage limit",
)
AUTH_PATTERNS = (
    "not logged in",
    "please run /login",
    "authentication failed",
    "authentication required",
)
KNOWN_TOP_LEVEL = {
    "tmux_session",
    "report_url",
    "docker_command",
    "recovery_poll_seconds",
    "active_poll_seconds",
    "target_active_seconds",
    "nudge_seconds",
    "resume_cooldown_seconds",
    "completion_marker",
    "active_marker",
    "enforce_active_gate",
    "workers",
}
KNOWN_WORKER_FIELDS = {
    "run_id",
    "window",
    "container",
    "client",
    "restart_command",
}
ALLOWED_CLIENTS = {"claude", "codex", "gemini", "grok", "other"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Worker:
    run_id: str
    window: str
    container: str
    client: str
    restart_command: Tuple[str, ...]


@dataclass(frozen=True)
class Config:
    tmux_session: str
    # Retained only so existing watcher configs continue to validate. Progress
    # is read from each worker's scoped CLI and never from this URL.
    report_url: Optional[str]
    docker_command: Tuple[str, ...]
    recovery_poll_seconds: float
    active_poll_seconds: float
    target_active_seconds: float
    nudge_seconds: float
    resume_cooldown_seconds: float
    completion_marker: str
    active_marker: str
    enforce_active_gate: bool
    workers: Tuple[Worker, ...]


@dataclass(frozen=True)
class RunProgress:
    run_id: str
    active_seconds: float
    elapsed_seconds: float
    tokens_total: float
    active_seconds_source: str
    elapsed_seconds_source: str
    tokens_total_source: Optional[str]
    measured_at: Optional[str]
    tokens_measured_at: Optional[str]
    candidate_count: int


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        raise ConfigError(f"{field} must be a non-empty single-line string")
    return value


def _positive_number(data: Dict[str, Any], field: str, default: float) -> float:
    value = data.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{field} must be a positive number")
    return float(value)


def _command(value: Any, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{field} must be a non-empty JSON array")
    result = tuple(_nonempty_string(item, field) for item in value)
    return result


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")

    unknown = set(raw) - KNOWN_TOP_LEVEL
    if unknown:
        raise ConfigError(f"unknown config fields: {', '.join(sorted(unknown))}")

    tmux_session = _nonempty_string(raw.get("tmux_session"), "tmux_session")
    report_url_value = raw.get("report_url")
    report_url = (
        _nonempty_string(report_url_value, "report_url")
        if report_url_value is not None
        else None
    )
    docker_command = _command(raw.get("docker_command", ["docker"]), "docker_command")
    completion_marker = _nonempty_string(
        raw.get("completion_marker", "/work/GOAL_COMPLETE"), "completion_marker"
    )
    active_marker = _nonempty_string(
        raw.get("active_marker", "/work/SCOREBENCH_ACTIVE_TARGET_REACHED"),
        "active_marker",
    )
    if not completion_marker.startswith("/") or not active_marker.startswith("/"):
        raise ConfigError("completion_marker and active_marker must be absolute paths")

    enforce_active_gate = raw.get("enforce_active_gate", True)
    if not isinstance(enforce_active_gate, bool):
        raise ConfigError("enforce_active_gate must be a boolean")

    worker_rows = raw.get("workers")
    if not isinstance(worker_rows, list) or not worker_rows:
        raise ConfigError("workers must be a non-empty JSON array")

    workers: List[Worker] = []
    seen_run_ids = set()
    seen_windows = set()
    seen_containers = set()
    for index, row in enumerate(worker_rows):
        prefix = f"workers[{index}]"
        if not isinstance(row, dict):
            raise ConfigError(f"{prefix} must be a JSON object")
        worker_unknown = set(row) - KNOWN_WORKER_FIELDS
        if worker_unknown:
            raise ConfigError(
                f"{prefix} has unknown fields: {', '.join(sorted(worker_unknown))}"
            )
        run_id = _nonempty_string(row.get("run_id"), f"{prefix}.run_id")
        window = _nonempty_string(row.get("window"), f"{prefix}.window")
        container = _nonempty_string(row.get("container"), f"{prefix}.container")
        client = _nonempty_string(row.get("client"), f"{prefix}.client").lower()
        if client not in ALLOWED_CLIENTS:
            raise ConfigError(
                f"{prefix}.client must be one of {', '.join(sorted(ALLOWED_CLIENTS))}"
            )
        restart_command = _command(
            row.get("restart_command"), f"{prefix}.restart_command"
        )
        for value, seen, field in (
            (run_id, seen_run_ids, "run_id"),
            (window, seen_windows, "window"),
            (container, seen_containers, "container"),
        ):
            if value in seen:
                raise ConfigError(f"duplicate worker {field}: {value}")
            seen.add(value)
        workers.append(Worker(run_id, window, container, client, restart_command))

    return Config(
        tmux_session=tmux_session,
        report_url=report_url,
        docker_command=docker_command,
        recovery_poll_seconds=_positive_number(raw, "recovery_poll_seconds", 30),
        active_poll_seconds=_positive_number(raw, "active_poll_seconds", 120),
        target_active_seconds=_positive_number(raw, "target_active_seconds", 14400),
        nudge_seconds=_positive_number(raw, "nudge_seconds", 300),
        resume_cooldown_seconds=_positive_number(
            raw, "resume_cooldown_seconds", 300
        ),
        completion_marker=completion_marker,
        active_marker=active_marker,
        enforce_active_gate=enforce_active_gate,
        workers=tuple(workers),
    )


def _nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be numeric") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return number


def _optional_single_line(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        raise ValueError(f"{field} must be a non-empty single-line string or null")
    return value


def parse_run_progress(body: str, expected_run_id: str) -> RunProgress:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"progress command did not return JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("progress response must be a JSON object")

    scope = data.get("scope")
    progress = data.get("progress")
    if not isinstance(scope, dict) or not isinstance(progress, dict):
        raise ValueError("progress response must contain scope and progress objects")
    if scope.get("kind") != "run_token":
        raise ValueError("progress response is not run-token scoped")
    if scope.get("run_id") != expected_run_id:
        raise ValueError(
            f"progress scope mismatch: expected {expected_run_id!r}, "
            f"got {scope.get('run_id')!r}"
        )
    if progress.get("run_id") != expected_run_id:
        raise ValueError(
            f"progress run mismatch: expected {expected_run_id!r}, "
            f"got {progress.get('run_id')!r}"
        )
    if progress.get("schema_version") != 1:
        raise ValueError(
            f"unsupported progress schema version: {progress.get('schema_version')!r}"
        )

    active = _nonnegative_number(progress.get("active_seconds"), "active_seconds")
    elapsed = _nonnegative_number(progress.get("elapsed_seconds"), "elapsed_seconds")
    if active > elapsed + 1e-6:
        raise ValueError("active_seconds cannot exceed elapsed_seconds")
    tokens_value = progress.get("tokens_total")
    tokens = (
        0.0
        if tokens_value is None
        else _nonnegative_number(tokens_value, "tokens_total")
    )
    candidate_count = progress.get("candidate_count")
    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int):
        raise ValueError("candidate_count must be a non-negative integer") from None
    if candidate_count < 0:
        raise ValueError("candidate_count must be a non-negative integer")

    active_source = _optional_single_line(
        progress.get("active_seconds_source"), "active_seconds_source"
    )
    elapsed_source = _optional_single_line(
        progress.get("elapsed_seconds_source"), "elapsed_seconds_source"
    )
    if active_source is None or elapsed_source is None:
        raise ValueError("active and elapsed sources are required")
    measured_at = _optional_single_line(progress.get("measured_at"), "measured_at")
    if candidate_count and measured_at is None:
        raise ValueError("measured_at is required when candidates exist")

    return RunProgress(
        run_id=expected_run_id,
        active_seconds=active,
        elapsed_seconds=elapsed,
        tokens_total=tokens,
        active_seconds_source=active_source,
        elapsed_seconds_source=elapsed_source,
        tokens_total_source=_optional_single_line(
            progress.get("tokens_total_source"), "tokens_total_source"
        ),
        measured_at=measured_at,
        tokens_measured_at=_optional_single_line(
            progress.get("tokens_measured_at"), "tokens_measured_at"
        ),
        candidate_count=candidate_count,
    )


def is_worker_busy(pane_text: str) -> bool:
    # Completed reports remain in scrollback and often describe tests or
    # monitors as "running". Only recent nonblank TUI status lines should
    # decide whether another turn is safe to enqueue.
    recent_lines = [line for line in pane_text.splitlines() if line.strip()]
    lowered = "\n".join(recent_lines[-BUSY_STATUS_LINES:]).lower()
    return any(pattern in lowered for pattern in BUSY_PATTERNS)


def nudge_text(
    worker: Worker,
    active: float,
    tokens: float,
    target: float,
    active_marker: str,
) -> str:
    return (
        f"Continue only the same existing ScoreBench run {worker.run_id}. "
        f"Its own latest submitted active time is {format_number(active)} seconds "
        f"and submitted token total is {format_number(tokens)}; the active-time "
        f"target is {format_number(target)} seconds. Keep independently optimizing "
        "in this isolated workspace, run `scorebench run ping --event resume`, "
        "and make verified legitimate submissions with exact tokens. Do not stop "
        "or create GOAL_COMPLETE while "
        f"{active_marker} is absent. Never use elapsed time or inspect any other run "
        "or solution."
    )


def finalize_text(
    worker: Worker,
    active: float,
    tokens: float,
    active_marker: str,
    completion_marker: str,
) -> str:
    return (
        f"Continue only the same existing ScoreBench run {worker.run_id}. "
        f"Its latest submitted active time is {format_number(active)} seconds "
        f"and submitted token total is {format_number(tokens)}. The target marker "
        f"{active_marker} exists. Run `scorebench run ping --event resume`, finish "
        "the current safe operation, refresh pending candidates, verify the best "
        "candidate is valid and terminal, and record exact final usage with "
        f"`scorebench run usage`. Then create {completion_marker} and report the "
        "final run result. Never inspect another run or solution."
    )


def format_number(value: float) -> str:
    # int.is_integer() only exists on Python 3.12+, and the progress API returns
    # integral values as ints. Without the coercion this raises AttributeError,
    # which the caller swallows as "active-time check failed" and silently stops
    # nudging and finalizing that worker.
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", flush=True)


def run_command(
    args: Sequence[str],
    timeout: float = 30,
) -> subprocess.CompletedProcess:
    command = list(args)
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        message = f"command timed out after {timeout:g} seconds"
        stderr = f"{stderr.rstrip()}\n{message}".lstrip()
        return subprocess.CompletedProcess(command, 124, stdout, stderr)


class Supervisor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.last_nudge = {
            worker.run_id: float("-inf") for worker in config.workers
        }
        self.last_resume = {
            worker.run_id: float("-inf") for worker in config.workers
        }
        self.high_water_active = {worker.run_id: 0.0 for worker in config.workers}
        self.high_water_elapsed = {worker.run_id: 0.0 for worker in config.workers}
        self.high_water_tokens = {worker.run_id: 0.0 for worker in config.workers}

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        return run_command(("tmux",) + args)

    def docker(self, *args: str) -> subprocess.CompletedProcess:
        return run_command(self.config.docker_command + args)

    def target(self, worker: Worker) -> str:
        return f"{self.config.tmux_session}:{worker.window}"

    def window_exists(self, worker: Worker) -> bool:
        result = self.tmux(
            "list-windows",
            "-t",
            self.config.tmux_session,
            "-F",
            "#{window_name}",
        )
        if result.returncode != 0:
            return False
        names = {line.strip() for line in result.stdout.splitlines()}
        return worker.window in names

    def capture_pane(self, worker: Worker, history: int = 120) -> str:
        if not self.window_exists(worker):
            return ""
        result = self.tmux(
            "capture-pane", "-t", self.target(worker), "-p", "-S", f"-{history}"
        )
        return result.stdout if result.returncode == 0 else ""

    def send_keys(self, worker: Worker, *keys: str) -> None:
        if not self.window_exists(worker):
            return
        self.tmux("send-keys", "-t", self.target(worker), *keys)

    def send_literal(self, worker: Worker, value: str) -> None:
        if not self.window_exists(worker):
            return
        digest = hashlib.sha256(worker.run_id.encode("utf-8")).hexdigest()[:12]
        buffer_name = f"scorebench-watch-{digest}"
        self.tmux("set-buffer", "-b", buffer_name, "--", value)
        self.tmux("paste-buffer", "-b", buffer_name, "-t", self.target(worker))

    def marker_exists(self, worker: Worker, marker: str) -> bool:
        return self.docker("exec", worker.container, "test", "-f", marker).returncode == 0

    def set_marker(self, worker: Worker, marker: str) -> bool:
        return self.docker("exec", worker.container, "touch", marker).returncode == 0

    def worker_progress(self, worker: Worker) -> RunProgress:
        result = self.docker(
            "exec",
            worker.container,
            "scorebench",
            "run",
            "progress",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if len(detail) > 500:
                detail = detail[-500:]
            raise RuntimeError(
                "scoped progress command failed"
                + (f": {detail}" if detail else "")
                + "; ensure the container has the current scorebench CLI and "
                "the ScoreBench deployment exposes /run/progress"
            )
        return parse_run_progress(result.stdout, worker.run_id)

    def container_running(self, worker: Worker) -> bool:
        result = self.docker(
            "inspect", "--format", "{{.State.Running}}", worker.container
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def pane_state(self, worker: Worker) -> Optional[str]:
        if not self.window_exists(worker):
            return None
        result = self.tmux(
            "display-message", "-p", "-t", self.target(worker), "#{pane_dead}"
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def respawn(self, worker: Worker, command: Sequence[str]) -> None:
        pane_command = "exec " + shlex.join(command)
        result = self.tmux(
            "respawn-pane", "-k", "-t", self.target(worker), pane_command
        )
        if result.returncode != 0:
            log(f"{worker.run_id} respawn failed: {result.stderr.strip()}")

    def create_window(self, worker: Worker, command: Sequence[str]) -> None:
        pane_command = "exec " + shlex.join(command)
        result = self.tmux(
            "new-window",
            "-d",
            "-t",
            self.config.tmux_session,
            "-n",
            worker.window,
            pane_command,
        )
        if result.returncode != 0:
            log(f"{worker.run_id} window creation failed: {result.stderr.strip()}")

    def recovery_once(self) -> None:
        now = time.monotonic()
        for worker in self.config.workers:
            try:
                pane = self.capture_pane(worker)
                status = "\n".join(pane.splitlines()[-16:])
                lowered_status = status.lower()
                lowered_pane = pane.lower()

                if any(
                    marker in lowered_status
                    for marker in (
                        "do you trust the contents",
                        "press enter to continue",
                        "trust this folder",
                    )
                ):
                    log(f"{worker.run_id} startup confirmation detected")
                    self.send_keys(worker, "Enter")

                capacity_block = (
                    "goal blocked" in lowered_status or "/goal resume" in lowered_status
                ) and any(pattern in lowered_pane for pattern in CAPACITY_PATTERNS)
                if (
                    capacity_block
                    and now - self.last_resume[worker.run_id]
                    >= self.config.resume_cooldown_seconds
                ):
                    log(f"{worker.run_id} capacity block detected; requesting resume")
                    self.send_keys(worker, "C-u")
                    self.send_literal(worker, "/goal resume")
                    self.send_keys(worker, "Enter")
                    time.sleep(5)
                    self.send_keys(worker, "Enter")
                    self.last_resume[worker.run_id] = time.monotonic()

                if any(pattern in lowered_status for pattern in AUTH_PATTERNS):
                    log(f"{worker.run_id} authentication failure detected")

                pane_state = self.pane_state(worker)
                complete = self.marker_exists(worker, self.config.completion_marker)
                if pane_state is None and not complete:
                    if self.container_running(worker):
                        log(f"{worker.run_id} tmux window missing; reattaching")
                        self.create_window(
                            worker,
                            self.config.docker_command + ("attach", worker.container),
                        )
                    else:
                        log(f"{worker.run_id} tmux window missing; restarting worker")
                        self.create_window(worker, worker.restart_command)
                elif pane_state == "1" and not complete:
                    if self.container_running(worker):
                        log(f"{worker.run_id} attachment ended; reattaching")
                        self.respawn(
                            worker, self.config.docker_command + ("attach", worker.container)
                        )
                    else:
                        log(f"{worker.run_id} stopped unexpectedly; restarting worker")
                        self.respawn(worker, worker.restart_command)
            except Exception as exc:  # Keep sibling supervisors alive.
                log(f"{worker.run_id} recovery check failed: {exc}")

    def nudge(self, worker: Worker, active: float, tokens: float) -> None:
        prompt = nudge_text(
            worker,
            active,
            tokens,
            self.config.target_active_seconds,
            self.config.active_marker,
        )
        self.send_keys(worker, "C-u")
        self.send_literal(worker, prompt)
        self.send_keys(worker, "Enter")
        if worker.client == "codex":
            time.sleep(2)
            self.send_keys(worker, "Enter")

    def finalize(self, worker: Worker, active: float, tokens: float) -> None:
        prompt = finalize_text(
            worker,
            active,
            tokens,
            self.config.active_marker,
            self.config.completion_marker,
        )
        self.send_keys(worker, "C-u")
        self.send_literal(worker, prompt)
        self.send_keys(worker, "Enter")
        if worker.client == "codex":
            time.sleep(2)
            self.send_keys(worker, "Enter")

    def active_once(self) -> None:
        now = time.monotonic()
        for worker in self.config.workers:
            try:
                progress = self.worker_progress(worker)
                observed_active = progress.active_seconds
                observed_elapsed = progress.elapsed_seconds
                observed_tokens = progress.tokens_total
                previous_active = self.high_water_active[worker.run_id]
                previous_elapsed = self.high_water_elapsed[worker.run_id]
                previous_tokens = self.high_water_tokens[worker.run_id]
                if observed_active + 1e-6 < previous_active:
                    log(
                        f"{worker.run_id} active-time regression observed "
                        f"current={format_number(observed_active)}s "
                        f"high_water={format_number(previous_active)}s; retaining high water"
                    )
                if observed_elapsed + 1e-6 < previous_elapsed:
                    log(
                        f"{worker.run_id} elapsed-time regression observed "
                        f"current={format_number(observed_elapsed)}s "
                        f"high_water={format_number(previous_elapsed)}s; retaining high water"
                    )
                if observed_tokens + 1e-6 < previous_tokens:
                    log(
                        f"{worker.run_id} token regression observed "
                        f"current={format_number(observed_tokens)} "
                        f"high_water={format_number(previous_tokens)}; retaining high water"
                    )
                active = max(observed_active, previous_active)
                elapsed = max(observed_elapsed, previous_elapsed)
                tokens = max(observed_tokens, previous_tokens)
                self.high_water_active[worker.run_id] = active
                self.high_water_elapsed[worker.run_id] = elapsed
                self.high_water_tokens[worker.run_id] = tokens

                active_text = format_number(active)
                tokens_text = format_number(tokens)
                active_marker_exists = self.marker_exists(
                    worker, self.config.active_marker
                )
                completion_marker_exists = self.marker_exists(
                    worker, self.config.completion_marker
                )

                if (
                    active_marker_exists
                    or active >= self.config.target_active_seconds
                ):
                    marker_ready = active_marker_exists
                    if not marker_ready:
                        marker_ready = self.set_marker(
                            worker, self.config.active_marker
                        )
                    if not marker_ready:
                        log(f"{worker.run_id} could not create active-target marker")
                        continue

                    evidence = "marker" if active_marker_exists else "progress"
                    if completion_marker_exists:
                        log(
                            f"{worker.run_id} active={active_text}s "
                            f"tokens={tokens_text} target=reached "
                            f"evidence={evidence} complete=1"
                        )
                        continue

                    recent = self.capture_pane(worker, history=80)
                    busy = is_worker_busy(recent)
                    if (
                        not busy
                        and now - self.last_nudge[worker.run_id]
                        >= self.config.nudge_seconds
                    ):
                        log(
                            f"{worker.run_id} active={active_text}s "
                            f"tokens={tokens_text} target=reached "
                            f"evidence={evidence} idle; requesting finalization"
                        )
                        self.finalize(worker, active, tokens)
                        self.last_nudge[worker.run_id] = time.monotonic()
                    else:
                        log(
                            f"{worker.run_id} active={active_text}s "
                            f"tokens={tokens_text} "
                            f"elapsed={format_number(elapsed)}s target=reached "
                            f"evidence={evidence} busy={int(busy)} "
                            f"source={progress.active_seconds_source} "
                            f"measured_at={progress.measured_at or 'none'}"
                        )
                    continue

                if completion_marker_exists:
                    log(
                        f"{worker.run_id} active={active_text}s tokens={tokens_text} "
                        f"target=pending premature_complete=1 action=preserved "
                        f"source={progress.active_seconds_source} "
                        f"measured_at={progress.measured_at or 'none'}"
                    )
                    continue

                recent = self.capture_pane(worker, history=80)
                busy = is_worker_busy(recent)
                if (
                    not busy
                    and now - self.last_nudge[worker.run_id]
                    >= self.config.nudge_seconds
                ):
                    log(
                        f"{worker.run_id} active={active_text}s tokens={tokens_text} "
                        "idle; nudging exact session"
                    )
                    self.nudge(worker, active, tokens)
                    self.last_nudge[worker.run_id] = time.monotonic()
                else:
                    log(
                        f"{worker.run_id} active={active_text}s tokens={tokens_text} "
                        f"target=pending busy={int(busy)}"
                    )
            except Exception as exc:  # Keep sibling monitors alive.
                log(f"{worker.run_id} active-time check failed: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recover tmux workers or monitor ScoreBench active time."
    )
    parser.add_argument("mode", choices=("validate", "recovery", "active"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--once", action="store_true", help="run one poll instead of looping"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.mode == "validate":
        print(f"valid: {len(config.workers)} workers")
        return 0

    supervisor = Supervisor(config)
    poll_seconds = (
        config.recovery_poll_seconds
        if args.mode == "recovery"
        else config.active_poll_seconds
    )
    log(
        f"{args.mode} watcher started; workers={len(config.workers)} "
        f"target={format_number(config.target_active_seconds)}s; "
        "no elapsed-time stop"
    )
    try:
        while True:
            if args.mode == "recovery":
                supervisor.recovery_once()
            else:
                supervisor.active_once()
            if args.once:
                return 0
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        log(f"{args.mode} watcher stopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
