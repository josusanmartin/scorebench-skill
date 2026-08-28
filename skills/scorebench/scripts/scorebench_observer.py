#!/usr/bin/env python3
"""Low-overhead host observer for ScoreBench timing-v2 shadow evidence.

The observer reads only local structured session events, derives operation
boundaries, and uploads timestamps plus categorical metadata. It never uploads
prompt text, messages, reasoning, source code, tool arguments, or tool output.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCHEMA_VERSION = 1
PARSER_VERSION = "timing-observer.1"
LEASE_SECONDS = 60.0
FALLBACK_POLL_SECONDS = 5.0
MAX_SOURCE_LINE_BYTES = 4 * 1024 * 1024
MAX_READ_BYTES_PER_CYCLE = 8 * 1024 * 1024
MAX_PENDING_INTERVALS = 10_000
MAX_UPLOAD_SPANS = 200
MAX_RESPONSE_BYTES = 1024 * 1024


class ObserverError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat().replace("+00:00", "Z")


def timestamp_seconds(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def timestamp_iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def state_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return root / "scorebench" / "timing-observer"


def registrations_dir() -> Path:
    return state_root() / "registrations"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ObserverError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ObserverError(f"expected an object in {path}")
    return value


def config_path() -> Path:
    explicit = os.environ.get("SCOREBENCH_CLI_CONFIG") or os.environ.get(
        "HARNESS_CLI_CONFIG"
    )
    if explicit:
        return Path(explicit).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "harness" / "cli.json"


def paired_context(cwd: Path) -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    config = load_json(path)
    records = config.get("paired_workspaces")
    if not isinstance(records, dict):
        return {}
    resolved = cwd.resolve()
    for candidate in (resolved, *resolved.parents):
        record = records.get(str(candidate))
        if isinstance(record, dict):
            return record
    return {}


def credentials(cwd: Path, url: str, token: str) -> tuple[str, str]:
    context = paired_context(cwd)
    resolved_url = (
        url
        or os.environ.get("SCOREBENCH_URL")
        or os.environ.get("HARNESS_URL")
        or str(context.get("url") or "")
    ).strip()
    resolved_token = (
        token
        or os.environ.get("SCOREBENCH_TIMING_OBSERVER_TOKEN")
        or os.environ.get("HARNESS_TIMING_OBSERVER_TOKEN")
        or str(context.get("timing_observer_token") or "")
    ).strip()
    if not resolved_url:
        raise ObserverError("ScoreBench URL is unavailable")
    if not resolved_token.startswith("hobs_"):
        raise ObserverError(
            "timing observer credential is unavailable; refresh the ScoreBench CLI "
            "and pair or launch this run again"
        )
    return resolved_url.rstrip("/"), resolved_token


def head_events(path: Path, limit: int = 80) -> Iterable[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            for _ in range(limit):
                raw = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
                if not raw:
                    return
                if len(raw) > MAX_SOURCE_LINE_BYTES:
                    return
                try:
                    event = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def event_cwd(event: dict[str, Any], provider: str) -> str:
    if provider == "codex" and event.get("type") == "session_meta":
        payload = event.get("payload")
        if isinstance(payload, dict):
            return str(payload.get("cwd") or "")
    return str(event.get("cwd") or "")


def path_matches_cwd(path: Path, provider: str, cwd: Path) -> bool:
    expected = os.path.realpath(cwd)
    return any(
        value and os.path.realpath(value) == expected
        for value in (event_cwd(event, provider) for event in head_events(path))
    )


def recent_jsonl(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    paths = list(root.rglob("*.jsonl"))
    return sorted(
        paths,
        key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
        reverse=True,
    )


def discover_source(provider: str, cwd: Path) -> tuple[str, Path]:
    candidates: list[tuple[str, Path]] = []
    pinned: list[tuple[str, Path]] = []
    if provider in {"auto", "codex"}:
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
        thread_id = os.environ.get("CODEX_THREAD_ID", "")
        paths = recent_jsonl(root)
        if thread_id:
            pinned.extend(("codex", path) for path in paths if thread_id in path.name)
        candidates.extend(
            ("codex", path)
            for path in paths[:200]
            if path_matches_cwd(path, "codex", cwd)
        )
    if provider in {"auto", "claude"}:
        root = Path.home() / ".claude" / "projects"
        session_id = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get(
            "CLAUDE_CODE_SESSION_ID", ""
        )
        paths = recent_jsonl(root)
        if session_id:
            pinned.extend(("claude", path) for path in paths if session_id in path.name)
        candidates.extend(
            ("claude", path)
            for path in paths[:200]
            if path_matches_cwd(path, "claude", cwd)
        )
    if pinned:
        unique_pinned = {(kind, str(path)): (kind, path) for kind, path in pinned}
        return max(
            unique_pinned.values(), key=lambda item: item[1].stat().st_mtime_ns
        )
    if not candidates:
        raise ObserverError(f"no {provider} session JSONL found for {cwd}")
    unique = {(kind, str(path)): (kind, path) for kind, path in candidates}
    return max(unique.values(), key=lambda item: item[1].stat().st_mtime_ns)


def process_start_ticks(pid: int) -> str:
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()
    except OSError:
        return ""
    return fields[21] if len(fields) > 21 else ""


def process_identity(pid: int) -> tuple[int, str]:
    return pid, process_start_ticks(pid)


def process_alive(pid: int, start_ticks: str) -> bool:
    return pid > 0 and bool(start_ticks) and process_start_ticks(pid) == start_ticks


def ancestor_agent_details() -> tuple[str, int, str]:
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            argv = [
                value.decode("utf-8", errors="replace")
                for value in (proc / "cmdline").read_bytes().split(b"\0")
                if value
            ]
            status = (proc / "status").read_text(encoding="utf-8")
        except OSError:
            break
        executable = Path(argv[0]).name.lower() if argv else ""
        launchers = [Path(value).name.lower() for value in argv[1:4]]
        provider = ""
        if "codex" in executable:
            provider = "codex"
        elif "claude" in executable:
            provider = "claude"
        elif any("codex" in value for value in launchers):
            provider = "codex"
        elif any("claude" in value for value in launchers):
            provider = "claude"
        if provider:
            identity = process_identity(pid)
            return provider, identity[0], identity[1]
        parent = 0
        for line in status.splitlines():
            if line.startswith("PPid:"):
                try:
                    parent = int(line.split(":", 1)[1].strip())
                except ValueError:
                    parent = 0
                break
        pid = parent
    return "", 0, ""


def source_writer_process(path: Path) -> tuple[int, str]:
    try:
        target = path.stat()
    except OSError:
        return 0, ""
    proc = Path("/proc")
    try:
        pids = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return 0, ""
    for entry in pids:
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                stat = fd.stat()
            except OSError:
                continue
            if stat.st_dev == target.st_dev and stat.st_ino == target.st_ino:
                return process_identity(int(entry.name))
    return 0, ""


def operation_key(provider: str, kind: str, raw_id: Any) -> str:
    digest = hashlib.sha256(
        f"{provider}\0{kind}\0{str(raw_id or kind)}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{kind}:{digest}"


Transition = tuple[float, str, str, str]


def codex_transitions(event: dict[str, Any]) -> list[Transition]:
    timestamp = timestamp_seconds(event.get("timestamp"))
    if timestamp is None:
        return []
    top_type = str(event.get("type") or "")
    payload = event.get("payload")
    transitions: list[Transition] = []
    if top_type == "event_msg" and isinstance(payload, dict):
        event_type = str(payload.get("type") or "")
        if event_type == "task_started":
            transitions.append((timestamp, "start", "turn", "agent_turn"))
        elif event_type in {"task_complete", "turn_aborted"}:
            transitions.append((timestamp, "finish_all", "turn", "agent_turn"))
        elif event_type == "user_message":
            transitions.append((timestamp, "start", "model", "model_request"))
        return transitions
    if top_type == "response_item" and isinstance(payload, dict):
        item_type = str(payload.get("type") or "")
        call_id = payload.get("call_id") or payload.get("id")
        if item_type in {"function_call", "custom_tool_call"}:
            transitions.extend(
                [
                    (timestamp, "end", "model", "model_request"),
                    (
                        timestamp,
                        "start",
                        operation_key("codex", "tool", call_id),
                        "tool_call",
                    ),
                ]
            )
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            transitions.extend(
                [
                    (
                        timestamp,
                        "end",
                        operation_key("codex", "tool", call_id),
                        "tool_call",
                    ),
                    (timestamp, "start", "model", "model_request"),
                ]
            )
        return transitions
    if top_type in {"turn.started", "thread.started"}:
        transitions.append((timestamp, "start", "turn", "agent_turn"))
    elif top_type == "turn.completed":
        transitions.append((timestamp, "finish_all", "turn", "agent_turn"))
    elif top_type == "item.started":
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type not in {"reasoning", "analysis"}:
                transitions.append(
                    (
                        timestamp,
                        "start",
                        operation_key("codex", "tool", item.get("id")),
                        "tool_call",
                    )
                )
    elif top_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type not in {"reasoning", "analysis"}:
                transitions.append(
                    (
                        timestamp,
                        "end",
                        operation_key("codex", "tool", item.get("id")),
                        "tool_call",
                    )
                )
    return transitions


def claude_transitions(event: dict[str, Any]) -> list[Transition]:
    timestamp = timestamp_seconds(event.get("timestamp"))
    top_type = str(event.get("type") or "")
    message = event.get("message")
    if timestamp is None or top_type not in {"assistant", "user"} or not isinstance(message, dict):
        return []
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    transitions: list[Transition] = []
    if top_type == "assistant":
        transitions.append((timestamp, "end", "model", "model_request"))
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                transitions.append(
                    (
                        timestamp,
                        "start",
                        operation_key("claude", "tool", block.get("id")),
                        "tool_call",
                    )
                )
        return transitions
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            transitions.append(
                (
                    timestamp,
                    "end",
                    operation_key("claude", "tool", block.get("tool_use_id")),
                    "tool_call",
                )
            )
    transitions.append((timestamp, "start", "model", "model_request"))
    return transitions


def generic_transitions(event: dict[str, Any]) -> list[Transition]:
    timestamp = timestamp_seconds(event.get("timestamp"))
    kind = str(event.get("kind") or "").lower()
    operation_id = operation_key(
        "generic", str(event.get("operation_kind") or "operation"), event.get("operation_id")
    )
    if timestamp is None:
        return []
    if kind in {"model_start", "tool_start", "operation_start"}:
        return [(timestamp, "start", operation_id, kind.rsplit("_", 1)[0])]
    if kind in {"model_end", "tool_end", "operation_end"}:
        return [(timestamp, "end", operation_id, kind.rsplit("_", 1)[0])]
    if kind in {"pause", "finish"}:
        return [(timestamp, "finish_all", operation_id, kind)]
    return []


def transitions(provider: str, event: dict[str, Any]) -> list[Transition]:
    if provider == "codex":
        return codex_transitions(event)
    if provider == "claude":
        return claude_transitions(event)
    return generic_transitions(event)


def merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[list[float]]:
    ordered = sorted((float(start), float(end)) for start, end in intervals if end > start)
    merged: list[list[float]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def append_interval(state: dict[str, Any], start: float, end: float) -> None:
    if end <= start:
        return
    pending = [
        item
        for item in state.setdefault("pending_intervals", [])
        if isinstance(item, list) and len(item) == 2
    ]
    interval = [float(start), float(end)]
    if not pending or interval[0] > float(pending[-1][1]):
        pending.append(interval)
    elif interval[0] >= float(pending[-1][0]):
        pending[-1][1] = max(float(pending[-1][1]), interval[1])
    else:
        pending = merge_intervals(
            [
                *((float(item[0]), float(item[1])) for item in pending),
                (interval[0], interval[1]),
            ]
        )
    if len(pending) > MAX_PENDING_INTERVALS:
        raise ObserverError("timing observer retry queue is full")
    state["pending_intervals"] = pending


def apply_transition(state: dict[str, Any], transition: Transition) -> None:
    timestamp, action, key, operation_kind = transition
    open_operations = state.setdefault("open_operations", {})
    if action == "start":
        if key in open_operations:
            return
        if not open_operations:
            state["active_since"] = timestamp
        open_operations[key] = {
            "started_at": timestamp,
            "operation_kind": operation_kind,
        }
        return
    if action == "finish_all":
        if open_operations:
            start = float(state.get("active_since") or timestamp)
            append_interval(state, start, max(start, timestamp))
        open_operations.clear()
        state["active_since"] = None
        return
    if key not in open_operations:
        return
    open_operations.pop(key, None)
    if not open_operations:
        start = float(state.get("active_since") or timestamp)
        append_interval(state, start, max(start, timestamp))
        state["active_since"] = None


def read_new_events(state: dict[str, Any]) -> int:
    path = Path(str(state["source_path"]))
    stat = path.stat()
    if stat.st_dev != int(state["source_device"]) or stat.st_ino != int(
        state["source_inode"]
    ):
        raise ObserverError("session source was replaced; register it again")
    offset = int(state.get("source_offset") or 0)
    if stat.st_size < offset:
        raise ObserverError("session source was truncated; register it again")
    end = min(stat.st_size, offset + MAX_READ_BYTES_PER_CYCLE)
    if end <= offset:
        return 0
    processed = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        while handle.tell() < end:
            line_start = handle.tell()
            raw = handle.readline(MAX_SOURCE_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_SOURCE_LINE_BYTES:
                state["source_offset"] = handle.tell()
                if not state.get("discarding_oversized_line"):
                    state["dropped_oversized_lines"] = int(
                        state.get("dropped_oversized_lines") or 0
                    ) + 1
                    # The skipped record may contain an operation completion.
                    # Clear open state so a missing boundary can only become
                    # unknown; it must never mint an indefinite active lease.
                    state["open_operations"] = {}
                    state["active_since"] = None
                state["discarding_oversized_line"] = not raw.endswith(b"\n")
                continue
            if state.get("discarding_oversized_line"):
                state["source_offset"] = handle.tell()
                if raw.endswith(b"\n"):
                    state["discarding_oversized_line"] = False
                continue
            if not raw.endswith(b"\n"):
                handle.seek(line_start)
                break
            state["source_offset"] = handle.tell()
            try:
                event = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                state["dropped_invalid_lines"] = int(
                    state.get("dropped_invalid_lines") or 0
                ) + 1
                continue
            if not isinstance(event, dict):
                continue
            for transition in transitions(str(state["provider"]), event):
                apply_transition(state, transition)
            processed += 1
    state["event_count"] = int(state.get("event_count") or 0) + processed
    return processed


def lease_open_activity(state: dict[str, Any], now: float) -> None:
    if not state.get("open_operations"):
        return
    pid = int(state.get("agent_pid") or 0)
    ticks = str(state.get("agent_start_ticks") or "")
    if pid and not process_alive(pid, ticks):
        state["open_operations"] = {}
        state["active_since"] = None
        state["agent_exit_observed_at"] = timestamp_iso(now)
        return
    # Without a stable process identity, completed events are still credited,
    # but an open operation cannot mint an unbounded active lease.
    if not pid:
        return
    start = float(state.get("active_since") or now)
    if now > start:
        append_interval(state, start, now)
        state["active_since"] = now


def interval_payload(state: dict[str, Any]) -> tuple[list[dict[str, Any]], float | None]:
    evidence: list[dict[str, Any]] = []
    cutoff: float | None = None
    sequence = int(state.get("next_sequence") or 1)
    for start, end in merge_intervals(
        (float(item[0]), float(item[1]))
        for item in state.get("pending_intervals") or []
        if isinstance(item, list) and len(item) == 2
    ):
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + LEASE_SECONDS)
            identity = hashlib.sha256(
                f"{state['registration_id']}\0{cursor:.6f}\0{chunk_end:.6f}".encode(
                    "utf-8"
                )
            ).hexdigest()[:24]
            evidence.append(
                {
                    "id": f"obs-{identity}",
                    "started_at": timestamp_iso(cursor),
                    "ended_at": timestamp_iso(chunk_end),
                    "classification": "confirmed_active",
                    "source": f"{state['provider']}_session",
                    "sequence": sequence,
                    "metadata": {
                        "provider": state["provider"],
                        "operation_kind": "model_or_tool",
                        "event_count": int(state.get("event_count") or 0),
                        "parser_version": PARSER_VERSION,
                        "lease_seconds": round(chunk_end - cursor, 3),
                    },
                }
            )
            cutoff = chunk_end
            sequence += 1
            cursor = chunk_end
            if len(evidence) >= MAX_UPLOAD_SPANS:
                return evidence, cutoff
    return evidence, cutoff


def consume_pending_through(state: dict[str, Any], cutoff: float) -> None:
    remaining: list[list[float]] = []
    for start, end in merge_intervals(
        (float(item[0]), float(item[1]))
        for item in state.get("pending_intervals") or []
        if isinstance(item, list) and len(item) == 2
    ):
        if end <= cutoff:
            continue
        remaining.append([max(start, cutoff), end])
    state["pending_intervals"] = remaining


def post_json(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    request = Request(
        url.rstrip("/") + "/run/timing-evidence",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"scorebench-timing-observer/{PARSER_VERSION}",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise ObserverError(f"ScoreBench returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ObserverError(f"ScoreBench timing upload failed: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ObserverError("ScoreBench timing response is too large")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ObserverError("ScoreBench timing response is not JSON") from exc
    if not isinstance(decoded, dict):
        raise ObserverError("ScoreBench timing response is invalid")
    return decoded


def upload_pending(state: dict[str, Any], now: float, *, force: bool = False) -> bool:
    evidence, cutoff = interval_payload(state)
    if not evidence or cutoff is None:
        return False
    last_attempt = float(state.get("last_upload_attempt_seconds") or 0)
    if not force and now - last_attempt < LEASE_SECONDS:
        return False
    state["last_upload_attempt_seconds"] = now
    payload = {
        "observer_id": state["observer_id"],
        "observer_boot_id": state["observer_boot_id"],
        "evidence": evidence,
    }
    try:
        response = post_json(str(state["scorebench_url"]), str(state["token"]), payload)
    except ObserverError as exc:
        state["last_error"] = str(exc)[:500]
        state["last_error_at"] = timestamp_iso(now)
        return False
    consume_pending_through(state, cutoff)
    state["next_sequence"] = int(state.get("next_sequence") or 1) + len(evidence)
    state["last_upload_at"] = timestamp_iso(now)
    state["last_error"] = ""
    state["last_projection"] = response.get("timing_v2")
    state["uploaded_span_count"] = int(state.get("uploaded_span_count") or 0) + len(
        evidence
    )
    return True


def registration_paths() -> list[Path]:
    root = registrations_dir()
    if not root.is_dir():
        return []
    return sorted(root.glob("*.json"))


def process_registration(path: Path, *, force_upload: bool = False) -> dict[str, Any]:
    state = load_json(path)
    if state.get("schema_version") != SCHEMA_VERSION or not state.get("enabled", True):
        return state
    now = time.time()
    try:
        read_new_events(state)
        lease_open_activity(state, now)
        upload_pending(state, now, force=force_upload)
        pid = int(state.get("agent_pid") or 0)
        ticks = str(state.get("agent_start_ticks") or "")
        if (
            pid
            and not process_alive(pid, ticks)
            and not state.get("pending_intervals")
        ):
            state["enabled"] = False
            state.setdefault("agent_exit_observed_at", timestamp_iso(now))
        state["last_checked_at"] = timestamp_iso(now)
    except (ObserverError, OSError) as exc:
        state["last_error"] = str(exc)[:500]
        state["last_error_at"] = timestamp_iso(now)
    atomic_write_json(path, state)
    return state


class Wakeup:
    IN_MODIFY = 0x00000002
    IN_CLOSE_WRITE = 0x00000008
    IN_MOVED_TO = 0x00000080
    IN_CREATE = 0x00000100
    IN_DELETE = 0x00000200

    def __init__(self, paths: Iterable[Path]):
        self.fd = -1
        if not sys.platform.startswith("linux"):
            return
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            init = libc.inotify_init1
            init.argtypes = [ctypes.c_int]
            init.restype = ctypes.c_int
            fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                return
            add = libc.inotify_add_watch
            add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            add.restype = ctypes.c_int
            mask = (
                self.IN_MODIFY
                | self.IN_CLOSE_WRITE
                | self.IN_MOVED_TO
                | self.IN_CREATE
                | self.IN_DELETE
            )
            watched = 0
            for path in {Path(item) for item in paths}:
                target = path if path.is_dir() else path.parent
                if not target.exists():
                    continue
                if add(fd, os.fsencode(target), mask) >= 0:
                    watched += 1
            if watched:
                self.fd = fd
            else:
                os.close(fd)
        except (AttributeError, OSError):
            self.fd = -1

    def wait(self, timeout: float) -> None:
        if self.fd < 0:
            time.sleep(min(timeout, FALLBACK_POLL_SECONDS))
            return
        ready, _write, _error = select.select([self.fd], [], [], timeout)
        if ready:
            try:
                os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                pass

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def daemon(*, once: bool = False) -> int:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "observer.lock"
    lock_handle = lock_path.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return 0
        raise
    (root / "observer.pid").write_text(f"{os.getpid()}\n", encoding="ascii")
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            paths = registration_paths()
            for path in paths:
                process_registration(path, force_upload=once)
            if once:
                break
            sources = [registrations_dir()]
            for path in paths:
                try:
                    sources.append(Path(str(load_json(path).get("source_path") or "")))
                except ObserverError:
                    continue
            wakeup = Wakeup(sources)
            try:
                wakeup.wait(min(LEASE_SECONDS, FALLBACK_POLL_SECONDS))
            finally:
                wakeup.close()
    finally:
        (root / "observer.pid").unlink(missing_ok=True)
        lock_handle.close()
    return 0


def ensure_daemon() -> int:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "observer.log"
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    time.sleep(0.05)
    return process.pid


def register(args: argparse.Namespace) -> dict[str, Any]:
    cwd = Path(args.cwd or Path.cwd()).expanduser().resolve()
    scorebench_url, token = credentials(cwd, args.url or "", args.token or "")
    ancestor_provider, ancestor_pid, ancestor_ticks = ancestor_agent_details()
    requested_provider = args.provider
    if requested_provider == "auto":
        if ancestor_provider:
            requested_provider = ancestor_provider
        elif os.environ.get("CODEX_THREAD_ID"):
            requested_provider = "codex"
        elif os.environ.get("CLAUDE_SESSION_ID") or os.environ.get(
            "CLAUDE_CODE_SESSION_ID"
        ):
            requested_provider = "claude"
    if args.source:
        source = Path(args.source).expanduser().resolve()
        if not source.is_file():
            raise ObserverError(f"session source does not exist: {source}")
        provider = requested_provider
        if provider == "auto":
            provider = "claude" if ".claude" in source.parts else "codex"
    else:
        provider, source = discover_source(requested_provider, cwd)
    if provider not in {"codex", "claude", "generic"}:
        raise ObserverError("provider must be codex, claude, or generic")
    stat = source.stat()
    identity = hashlib.sha256(
        f"{scorebench_url}\0{hashlib.sha256(token.encode()).hexdigest()}\0{source}".encode(
            "utf-8"
        )
    ).hexdigest()[:24]
    registration_id = f"reg-{identity}"
    path = registrations_dir() / f"{registration_id}.json"
    agent_pid, agent_ticks = 0, ""
    if ancestor_provider == provider:
        agent_pid, agent_ticks = ancestor_pid, ancestor_ticks
    if not agent_pid:
        agent_pid, agent_ticks = source_writer_process(source)
    if path.exists():
        state = load_json(path)
        if state.get("source_device") != stat.st_dev or state.get("source_inode") != stat.st_ino:
            raise ObserverError("existing registration source was replaced; unregister it first")
        state["enabled"] = True
        state["updated_at"] = utcnow_iso()
        if agent_pid:
            state["agent_pid"] = agent_pid
            state["agent_start_ticks"] = agent_ticks
            state.pop("agent_exit_observed_at", None)
        atomic_write_json(path, state)
        daemon_pid = 0 if args.no_start else ensure_daemon()
        return {
            "ok": True,
            "registration_id": registration_id,
            "provider": provider,
            "source": str(source),
            "idempotent": True,
            "daemon_pid": daemon_pid,
        }
    state = {
        "schema_version": SCHEMA_VERSION,
        "registration_id": registration_id,
        "observer_id": f"host-{hashlib.sha256(os.uname().nodename.encode()).hexdigest()[:16]}",
        "observer_boot_id": f"boot-{hashlib.sha256(f'{os.getpid()}-{time.time_ns()}'.encode()).hexdigest()[:16]}",
        "created_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
        "enabled": True,
        "cwd": str(cwd),
        "provider": provider,
        "source_path": str(source),
        "source_device": stat.st_dev,
        "source_inode": stat.st_ino,
        "source_offset": 0 if args.from_start else stat.st_size,
        "scorebench_url": scorebench_url,
        "token": token,
        "agent_pid": agent_pid,
        "agent_start_ticks": agent_ticks,
        "open_operations": {},
        "active_since": None,
        "pending_intervals": [],
        "next_sequence": 1,
        "event_count": 0,
        "uploaded_span_count": 0,
        "last_error": "",
    }
    atomic_write_json(path, state)
    daemon_pid = 0 if args.no_start else ensure_daemon()
    return {
        "ok": True,
        "registration_id": registration_id,
        "provider": provider,
        "source": str(source),
        "source_offset": state["source_offset"],
        "agent_process_verified": bool(agent_pid),
        "idempotent": False,
        "daemon_pid": daemon_pid,
    }


def status() -> dict[str, Any]:
    rows = []
    for path in registration_paths():
        try:
            state = load_json(path)
        except ObserverError as exc:
            rows.append({"path": str(path), "error": str(exc)})
            continue
        rows.append(
            {
                "registration_id": state.get("registration_id"),
                "provider": state.get("provider"),
                "cwd": state.get("cwd"),
                "enabled": bool(state.get("enabled", True)),
                "last_checked_at": state.get("last_checked_at"),
                "last_upload_at": state.get("last_upload_at"),
                "uploaded_span_count": int(state.get("uploaded_span_count") or 0),
                "pending_interval_count": len(state.get("pending_intervals") or []),
                "agent_process_verified": bool(state.get("agent_pid")),
                "last_error": state.get("last_error") or "",
            }
        )
    pid = 0
    try:
        pid = int((state_root() / "observer.pid").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pass
    return {"ok": True, "daemon_pid": pid, "registrations": rows}


def unregister(registration_id: str = "", cwd: str = "") -> dict[str, Any]:
    paths: list[Path] = []
    if registration_id:
        if not registration_id.startswith("reg-"):
            raise ObserverError("registration id is invalid")
        path = registrations_dir() / f"{registration_id}.json"
        if not path.exists():
            raise ObserverError(f"unknown registration: {registration_id}")
        paths = [path]
    elif cwd:
        expected = str(Path(cwd).expanduser().resolve())
        paths = [
            path
            for path in registration_paths()
            if str(load_json(path).get("cwd") or "") == expected
        ]
        if not paths:
            raise ObserverError(f"no timing observer registration found for {expected}")
    else:
        raise ObserverError("unregister requires a registration id or --cwd")

    disabled: list[str] = []
    for path in paths:
        state = process_registration(path, force_upload=True)
        if state.get("pending_intervals"):
            raise ObserverError(
                f"cannot disable {state.get('registration_id') or path.stem}: "
                "timing evidence is still queued; retry after connectivity recovers"
            )
        state["enabled"] = False
        state["updated_at"] = utcnow_iso()
        atomic_write_json(path, state)
        disabled.append(str(state.get("registration_id") or path.stem))
    return {"ok": True, "registration_ids": disabled, "enabled": False}


def benchmark(count: int) -> dict[str, Any]:
    start = time.perf_counter()
    state: dict[str, Any] = {
        "provider": "codex",
        "open_operations": {},
        "pending_intervals": [],
    }
    origin = time.time() - count
    for index in range(count):
        event = {
            "type": "event_msg",
            "timestamp": timestamp_iso(origin + index),
            "payload": {
                "type": (
                    "task_started"
                    if index == 0
                    else "task_complete" if index == count - 1 else "agent_message"
                ),
                "message": "not inspected",
            },
        }
        for transition in codex_transitions(event):
            apply_transition(state, transition)
    elapsed = time.perf_counter() - start
    return {
        "events": count,
        "elapsed_seconds": elapsed,
        "events_per_second": count / elapsed if elapsed else None,
        "intervals": len(state.get("pending_intervals") or []),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    register_parser = commands.add_parser("register", help="register this local agent session")
    register_parser.add_argument("--provider", choices=("auto", "codex", "claude", "generic"), default="auto")
    register_parser.add_argument("--source")
    register_parser.add_argument("--cwd")
    register_parser.add_argument("--url")
    register_parser.add_argument("--token")
    register_parser.add_argument("--from-start", action="store_true")
    register_parser.add_argument("--no-start", action="store_true")
    daemon_parser = commands.add_parser("daemon", help="run the singleton host observer")
    daemon_parser.add_argument("--once", action="store_true")
    commands.add_parser("status", help="show observer health without secrets")
    unregister_parser = commands.add_parser("unregister", help="disable a registration")
    unregister_parser.add_argument("registration_id", nargs="?")
    unregister_parser.add_argument("--cwd")
    benchmark_parser = commands.add_parser("benchmark", help="benchmark local event parsing")
    benchmark_parser.add_argument("--events", type=int, default=100_000)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "register":
            result = register(args)
        elif args.command == "daemon":
            return daemon(once=args.once)
        elif args.command == "status":
            result = status()
        elif args.command == "unregister":
            result = unregister(args.registration_id or "", args.cwd or "")
        elif args.command == "benchmark":
            if args.events <= 0:
                raise ObserverError("--events must be positive")
            result = benchmark(args.events)
        else:
            raise ObserverError(f"unsupported command: {args.command}")
    except ObserverError as exc:
        print(f"scorebench timing observer: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
