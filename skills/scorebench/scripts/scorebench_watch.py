#!/usr/bin/env python3
"""Supervise isolated ScoreBench workers in separate tmux windows."""

import argparse
import hashlib
import json
import math
import shlex
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


BUSY_PATTERNS = (
    "working (",
    "pursuing goal",
    "esc to interrupt",
    "recombobulating",
    "thinking",
    "responding",
    "running ",
    "tool running",
    "tool is running",
    "send now",
)
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


class ReportDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside = False
        self.parts: List[str] = []

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        if tag.lower() == "script" and dict(attrs).get("id") == "report-data":
            self._inside = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside:
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.parts.append(data)


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
    report_url: str
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
    report_url = _nonempty_string(raw.get("report_url"), "report_url")
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


def parse_report_html(html: str) -> Dict[str, Any]:
    parser = ReportDataParser()
    parser.feed(html)
    payload = "".join(parser.parts).strip()
    if not payload:
        raise ValueError("report-data script was not found")
    data = json.loads(payload)
    if not isinstance(data, dict) or not isinstance(data.get("arms"), list):
        raise ValueError("report-data does not contain an arms array")
    return data


def fetch_report(url: str, timeout: float = 30) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "scorebench-watch/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        html = response.read().decode("utf-8")
    return parse_report_html(html)


def _numeric(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def latest_run_metrics(data: Dict[str, Any], run_id: str) -> Tuple[float, float]:
    latest: Optional[Dict[str, Any]] = None
    latest_active = -1.0
    for arm in data.get("arms", []):
        if not isinstance(arm, dict):
            continue
        points = arm.get("points", [])
        if not isinstance(points, list):
            continue
        for point in points:
            if not isinstance(point, dict) or point.get("run_id") != run_id:
                continue
            active = _numeric(point.get("wall_seconds"))
            if active >= latest_active:
                latest = point
                latest_active = active
    if latest is None:
        return 0.0, 0.0
    return max(latest_active, 0.0), _numeric(latest.get("tokens_total"))


def is_worker_busy(pane_text: str) -> bool:
    lowered = pane_text.lower()
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
        "in this isolated workspace, send a heartbeat, and make verified legitimate "
        "submissions with exact tokens. Do not stop or create GOAL_COMPLETE while "
        f"{active_marker} is absent. Never use elapsed time or inspect any other run "
        "or solution."
    )


def format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}"


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", flush=True)


def run_command(
    args: Sequence[str],
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )


class Supervisor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.last_nudge = {
            worker.run_id: float("-inf") for worker in config.workers
        }
        self.last_resume = {
            worker.run_id: float("-inf") for worker in config.workers
        }

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        return run_command(("tmux",) + args)

    def docker(self, *args: str) -> subprocess.CompletedProcess:
        return run_command(self.config.docker_command + args)

    def target(self, worker: Worker) -> str:
        return f"{self.config.tmux_session}:{worker.window}"

    def capture_pane(self, worker: Worker, history: int = 120) -> str:
        result = self.tmux(
            "capture-pane", "-t", self.target(worker), "-p", "-S", f"-{history}"
        )
        return result.stdout if result.returncode == 0 else ""

    def send_keys(self, worker: Worker, *keys: str) -> None:
        self.tmux("send-keys", "-t", self.target(worker), *keys)

    def send_literal(self, worker: Worker, value: str) -> None:
        digest = hashlib.sha256(worker.run_id.encode("utf-8")).hexdigest()[:12]
        buffer_name = f"scorebench-watch-{digest}"
        self.tmux("set-buffer", "-b", buffer_name, "--", value)
        self.tmux("paste-buffer", "-b", buffer_name, "-t", self.target(worker))

    def marker_exists(self, worker: Worker, marker: str) -> bool:
        return self.docker("exec", worker.container, "test", "-f", marker).returncode == 0

    def set_marker(self, worker: Worker, marker: str) -> bool:
        return self.docker("exec", worker.container, "touch", marker).returncode == 0

    def remove_markers(self, worker: Worker, *markers: str) -> bool:
        result = self.docker("exec", worker.container, "rm", "-f", *markers)
        return result.returncode == 0

    def container_running(self, worker: Worker) -> bool:
        result = self.docker(
            "inspect", "--format", "{{.State.Running}}", worker.container
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def pane_state(self, worker: Worker) -> Optional[str]:
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

    def active_once(self) -> None:
        try:
            report = fetch_report(self.config.report_url)
        except Exception as exc:
            log(f"report fetch or parse failed; workers unchanged: {exc}")
            return

        now = time.monotonic()
        for worker in self.config.workers:
            try:
                active, tokens = latest_run_metrics(report, worker.run_id)
                active_text = format_number(active)
                tokens_text = format_number(tokens)
                if active >= self.config.target_active_seconds:
                    if not self.set_marker(worker, self.config.active_marker):
                        log(f"{worker.run_id} could not create active-target marker")
                    log(
                        f"{worker.run_id} active={active_text}s tokens={tokens_text} "
                        "target=reached"
                    )
                    continue

                if self.config.enforce_active_gate:
                    if not self.remove_markers(
                        worker,
                        self.config.active_marker,
                        self.config.completion_marker,
                    ):
                        log(f"{worker.run_id} could not enforce active-time markers")

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
