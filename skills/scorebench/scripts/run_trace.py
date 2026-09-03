#!/usr/bin/env python3
"""Capture and upload a sanitized ScoreBench agent trace after a run."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit


TRACE_FORMAT = "scorebench-run-trace"
TRACE_SCHEMA_VERSION = 1
DEFAULT_MAX_EVENT_BYTES = 64 * 1024
DEFAULT_MAX_TRACE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_LINE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024

SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|secret|token|api[_-]?key)",
    re.IGNORECASE,
)
OMITTED_KEYS = {
    "encrypted_content",
    "signature",
    "thinking",
    "redacted_thinking",
    "image_url",
    "audio_url",
    "local_images",
    "local_audio",
}
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer)\s+\S+"),
    re.compile(
        r"(?i)\b((?:scorebench|harness)_run_token|api[_-]?key|password|cookie|secret)"
        r"(\s*[=:]\s*)[^\s,;]+"
    ),
    re.compile(r"\bhrun_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bpp_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:sk-ant-|sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
)


class TraceError(RuntimeError):
    pass


class UploadError(TraceError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def state_root() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return root / "scorebench" / "run-traces"


def paired_workspace_context(cwd: Path) -> dict[str, Any]:
    explicit = os.environ.get("SCOREBENCH_CLI_CONFIG") or os.environ.get(
        "HARNESS_CLI_CONFIG"
    )
    config_path = (
        Path(explicit).expanduser()
        if explicit
        else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "harness"
        / "cli.json"
    )
    if not config_path.is_file():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records = config.get("paired_workspaces") if isinstance(config, dict) else None
    if not isinstance(records, dict):
        return {}
    resolved = cwd.resolve()
    for candidate in (resolved, *resolved.parents):
        record = records.get(str(candidate))
        if isinstance(record, dict):
            return record
    return {}


def session_hint() -> str:
    for name in (
        "CODEX_THREAD_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
    ):
        value = os.environ.get(name)
        if value:
            return value
    return ""


def default_state_path(cwd: Path) -> Path:
    token = os.environ.get("SCOREBENCH_RUN_TOKEN") or os.environ.get(
        "HARNESS_RUN_TOKEN", ""
    )
    identity = "\0".join((str(cwd.resolve()), session_hint(), token))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return state_root() / f"state-{digest}.json"


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


def load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TraceError(
            f"trace state is missing: run {Path(__file__).name} start first ({path})"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TraceError(f"cannot read trace state {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TraceError(f"invalid trace state: {path}")
    return payload


def _event_cwd(event: dict[str, Any], provider: str) -> str:
    if provider == "codex" and event.get("type") == "session_meta":
        payload = event.get("payload")
        if isinstance(payload, dict):
            return str(payload.get("cwd") or "")
    return str(event.get("cwd") or "")


def _session_id(event: dict[str, Any], provider: str) -> str:
    if provider == "codex" and event.get("type") == "session_meta":
        payload = event.get("payload")
        if isinstance(payload, dict):
            return str(payload.get("session_id") or payload.get("id") or "")
    return str(event.get("sessionId") or event.get("session_id") or "")


def head_events(path: Path, limit: int = 80) -> Iterator[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            for _ in range(limit):
                raw = handle.readline(256 * 1024)
                if not raw:
                    return
                if not raw.endswith(b"\n") and len(raw) >= 256 * 1024:
                    return
                try:
                    event = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def detect_provider(path: Path) -> str:
    if ".claude" in path.parts:
        return "claude"
    for event in head_events(path):
        event_type = event.get("type")
        if event_type in {
            "session_meta",
            "event_msg",
            "response_item",
            "thread.started",
            "turn.completed",
        }:
            return "codex"
        if event_type in {"assistant", "user"} and isinstance(event.get("message"), dict):
            return "claude"
    raise TraceError(f"cannot identify Codex or Claude JSONL format: {path}")


def path_matches_cwd(path: Path, provider: str, cwd: Path) -> bool:
    expected = os.path.realpath(cwd)
    for event in head_events(path):
        event_cwd = _event_cwd(event, provider)
        if event_cwd and os.path.realpath(event_cwd) == expected:
            return True
    return False


def _recent_jsonl(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    candidates = list(root.rglob("*.jsonl"))
    return sorted(
        candidates,
        key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
        reverse=True,
    )


def discover_codex_source(cwd: Path) -> Path | None:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    candidates = _recent_jsonl(root)
    if thread_id:
        exact = [path for path in candidates if thread_id in path.name]
        if exact:
            return exact[0]
    for path in candidates[:200]:
        if path_matches_cwd(path, "codex", cwd):
            return path
    return None


def discover_claude_source(cwd: Path) -> Path | None:
    root = Path.home() / ".claude" / "projects"
    session_id = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get(
        "CLAUDE_CODE_SESSION_ID", ""
    )
    candidates = _recent_jsonl(root)
    if session_id:
        exact = [path for path in candidates if session_id in path.name]
        if exact:
            return exact[0]
    encoded = "-" + str(cwd.resolve()).strip("/").replace("/", "-")
    preferred = [path for path in candidates if path.parent.name == encoded]
    for path in [*preferred, *candidates[:200]]:
        if path_matches_cwd(path, "claude", cwd):
            return path
    return None


def discover_source(provider: str, cwd: Path) -> tuple[str, Path]:
    if provider == "codex":
        path = discover_codex_source(cwd)
        if path is None:
            raise TraceError(f"no Codex session JSONL found for {cwd}")
        return provider, path
    if provider == "claude":
        path = discover_claude_source(cwd)
        if path is None:
            raise TraceError(f"no Claude session JSONL found for {cwd}")
        return provider, path

    codex = discover_codex_source(cwd)
    claude = discover_claude_source(cwd)
    candidates = [path for path in (codex, claude) if path is not None]
    if not candidates:
        raise TraceError(f"no Codex or Claude session JSONL found for {cwd}")
    selected = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    return detect_provider(selected), selected


def source_session_id(path: Path, provider: str) -> str:
    if provider == "codex":
        hint = os.environ.get("CODEX_THREAD_ID", "")
    else:
        hint = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get(
            "CLAUDE_CODE_SESSION_ID", ""
        )
    if hint:
        return hint
    for event in head_events(path):
        value = _session_id(event, provider)
        if value:
            return value
    return path.stem


def trace_start(
    *,
    provider: str,
    source: Path | None,
    cwd: Path,
    state_path: Path,
) -> dict[str, Any]:
    if source is None:
        provider, source = discover_source(provider, cwd)
    else:
        source = source.expanduser().resolve()
        if not source.is_file():
            raise TraceError(f"trace source does not exist: {source}")
        if provider == "auto":
            provider = detect_provider(source)
    stat = source.stat()
    resolved_session_id = source_session_id(source, provider)
    if state_path.exists():
        existing = load_state(state_path)
        same_source = (
            str(existing.get("source_path") or "") == str(source)
            and str(existing.get("provider") or "") == provider
            and str(existing.get("session_id") or "") == resolved_session_id
        )
        if not same_source:
            raise TraceError(
                "trace state already belongs to another session; "
                "use an explicit --state path for the new session"
            )
        offset = int(existing.get("source_offset") or 0)
        if offset < 0 or offset > stat.st_size:
            raise TraceError("trace source is smaller than its saved start boundary")
        return {
            "ok": True,
            "action": "start",
            "provider": provider,
            "session_id": resolved_session_id,
            "source": source.name,
            "source_offset": offset,
            "state": str(state_path),
            "idempotent": True,
        }
    payload = {
        "schema_version": 1,
        "provider": provider,
        "session_id": resolved_session_id,
        "source_path": str(source),
        "source_offset": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_device": stat.st_dev,
        "source_inode": stat.st_ino,
        "cwd": str(cwd.resolve()),
        "started_at": utcnow_iso(),
    }
    atomic_write_json(state_path, payload)
    return {
        "ok": True,
        "action": "start",
        "provider": provider,
        "session_id": payload["session_id"],
        "source": source.name,
        "source_offset": stat.st_size,
        "state": str(state_path),
        "idempotent": False,
    }


class Sanitizer:
    def __init__(self) -> None:
        self.redactions = 0
        self.omitted_fields = 0
        values = []
        for name, value in os.environ.items():
            if SENSITIVE_KEY.search(name) and len(value) >= 8:
                values.append(value)
        self.secret_values = sorted(set(values), key=len, reverse=True)

    def text(self, value: str) -> str:
        cleaned = value
        for secret in self.secret_values:
            count = cleaned.count(secret)
            if count:
                cleaned = cleaned.replace(secret, "[REDACTED_ENV]")
                self.redactions += count
        if "data:" in cleaned and ";base64," in cleaned:
            digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
            self.redactions += 1
            return f"[BINARY_DATA_OMITTED sha256={digest}]"
        for pattern in SECRET_PATTERNS:
            if pattern.pattern.startswith("(?i)(authorization"):
                cleaned, count = pattern.subn(r"\1 [REDACTED]", cleaned)
            elif "scorebench" in pattern.pattern:
                cleaned, count = pattern.subn(r"\1\2[REDACTED]", cleaned)
            else:
                cleaned, count = pattern.subn("[REDACTED]", cleaned)
            self.redactions += count
        return cleaned

    def value(self, value: Any, *, key: str = "") -> Any:
        if key in OMITTED_KEYS:
            self.omitted_fields += 1
            return "[OMITTED]"
        if key and SENSITIVE_KEY.search(key):
            self.redactions += 1
            return "[REDACTED]"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                str(child_key): self.value(child_value, key=str(child_key))
                for child_key, child_value in value.items()
                if str(child_key) not in {"internal_chat_message_metadata_passthrough"}
            }
        if isinstance(value, list):
            return [self.value(item) for item in value[:500]]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.text(str(value))


def content_text(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content
    parts: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(block)
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"thinking", "redacted_thinking"}:
            continue
        if block_type in {"text", "input_text", "output_text"}:
            parts.append(block.get("text") or "")
        else:
            parts.append(block)
    if all(isinstance(part, str) for part in parts):
        return "\n".join(part for part in parts if part)
    return parts


def normalize_codex(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    timestamp = event.get("timestamp")
    top_type = str(event.get("type") or "")
    payload = event.get("payload")

    if top_type == "event_msg" and isinstance(payload, dict):
        event_type = str(payload.get("type") or "")
        if event_type == "agent_message":
            yield {
                "timestamp": timestamp,
                "kind": "assistant_message",
                "phase": payload.get("phase"),
                "content": payload.get("message") or "",
            }
        elif event_type == "user_message":
            yield {
                "timestamp": timestamp,
                "kind": "user_message",
                "content": payload.get("message") or "",
            }
        elif event_type in {
            "task_started",
            "task_complete",
            "turn_aborted",
            "web_search_end",
            "context_compacted",
        }:
            yield {
                "timestamp": timestamp,
                "kind": "lifecycle",
                "name": event_type,
                "details": {
                    key: value
                    for key, value in payload.items()
                    if key not in {"type", "message", "memory_citation"}
                },
            }
        return

    if top_type == "response_item" and isinstance(payload, dict):
        item_type = str(payload.get("type") or "")
        if item_type == "reasoning":
            return
        if item_type in {"function_call", "custom_tool_call"}:
            yield {
                "timestamp": timestamp,
                "kind": "tool_call",
                "name": payload.get("name") or item_type,
                "call_id": payload.get("call_id") or payload.get("id"),
                "input": payload.get("arguments", payload.get("input")),
            }
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            yield {
                "timestamp": timestamp,
                "kind": "tool_result",
                "call_id": payload.get("call_id"),
                "output": payload.get("output"),
            }
        elif item_type in {"message", "agent_message"}:
            role = str(payload.get("role") or "assistant")
            if role not in {"assistant", "user"}:
                return
            yield {
                "timestamp": timestamp,
                "kind": f"{role}_message",
                "content": content_text(
                    payload.get("content", payload.get("message", ""))
                ),
            }
        return

    if top_type in {"thread.started", "turn.started", "turn.completed"}:
        yield {
            "timestamp": timestamp,
            "kind": "lifecycle",
            "name": top_type,
            "details": {
                key: value
                for key, value in event.items()
                if key not in {"type", "timestamp", "usage"}
            },
        }
    elif top_type in {"item.started", "item.completed"}:
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type in {"reasoning", "analysis"}:
                return
            yield {
                "timestamp": timestamp,
                "kind": "tool_result" if top_type == "item.completed" else "tool_call",
                "name": item_type or top_type,
                "call_id": item.get("id"),
                "details": item,
            }


def normalize_claude(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    top_type = str(event.get("type") or "")
    if top_type not in {"assistant", "user"}:
        return
    message = event.get("message")
    if not isinstance(message, dict):
        return
    role = str(message.get("role") or top_type)
    timestamp = event.get("timestamp")
    content = message.get("content")
    if isinstance(content, str):
        if content:
            yield {
                "timestamp": timestamp,
                "kind": f"{role}_message",
                "content": content,
            }
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"thinking", "redacted_thinking"}:
            continue
        if block_type == "text":
            text = block.get("text")
            if text:
                yield {
                    "timestamp": timestamp,
                    "kind": f"{role}_message",
                    "content": text,
                }
        elif block_type == "tool_use":
            yield {
                "timestamp": timestamp,
                "kind": "tool_call",
                "name": block.get("name") or "tool",
                "call_id": block.get("id"),
                "input": block.get("input"),
            }
        elif block_type == "tool_result":
            yield {
                "timestamp": timestamp,
                "kind": "tool_result",
                "call_id": block.get("tool_use_id"),
                "is_error": bool(block.get("is_error")),
                "output": block.get("content"),
            }


def normalized_events(
    provider: str, event: dict[str, Any], sanitizer: Sanitizer
) -> Iterable[dict[str, Any]]:
    normalizer = normalize_codex if provider == "codex" else normalize_claude
    for normalized in normalizer(event):
        cleaned = sanitizer.value(normalized)
        if isinstance(cleaned, dict):
            cleaned["provider"] = provider
            yield cleaned


def compact_event(event: dict[str, Any], encoded: bytes) -> dict[str, Any]:
    compact = {
        "timestamp": event.get("timestamp"),
        "provider": event.get("provider"),
        "kind": event.get("kind"),
        "compacted": True,
        "content_bytes": len(encoded),
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    for key in ("name", "call_id", "role", "phase", "is_error"):
        if event.get(key) is not None:
            compact[key] = event[key]
    return compact


def fit_event(event: dict[str, Any], max_event_bytes: int) -> tuple[dict[str, Any], bool]:
    encoded = canonical_json(event)
    if len(encoded) + 1 <= max_event_bytes:
        return event, False
    compact = compact_event(event, encoded)
    preview_budget = max(0, max_event_bytes - len(canonical_json(compact)) - 128)
    if preview_budget:
        compact["preview"] = encoded[:preview_budget].decode("utf-8", errors="replace")
    compact["truncated"] = True
    return compact, True


def dedupe_key(event: dict[str, Any]) -> str:
    comparable = {
        key: value
        for key, value in event.items()
        if key not in {"timestamp", "phase", "provider"}
    }
    return hashlib.sha256(canonical_json(comparable)).hexdigest()


def read_segment_lines(
    handle: Any,
    *,
    start: int,
    end: int,
    raw_digest: Any,
    dropped: Counter[str],
) -> Iterator[bytes]:
    handle.seek(start)
    remaining = end - start
    while remaining > 0:
        limit = min(MAX_SOURCE_LINE_BYTES + 1, remaining)
        raw = handle.readline(limit)
        if not raw:
            break
        remaining -= len(raw)
        raw_digest.update(raw)
        if raw.endswith(b"\n") or remaining == 0:
            if not raw.endswith(b"\n") and remaining == 0:
                dropped["incomplete_trailing_line"] += 1
                continue
            yield raw
            continue

        oversized_bytes = len(raw)
        while remaining > 0:
            chunk = handle.readline(min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            oversized_bytes += len(chunk)
            raw_digest.update(chunk)
            if chunk.endswith(b"\n"):
                break
        dropped["oversized_source_line"] += 1
        dropped["oversized_source_bytes"] += oversized_bytes


def build_artifact(
    state: dict[str, Any],
    *,
    state_path: Path,
    output: Path | None,
    max_event_bytes: int,
    max_trace_bytes: int,
) -> tuple[dict[str, Any], Path]:
    cached = state.get("artifact")
    if isinstance(cached, dict):
        cached_path = Path(str(cached.get("path") or ""))
        expected_sha = str(cached.get("sha256") or "")
        if cached_path.is_file() and expected_sha and sha256_file(cached_path) == expected_sha:
            return cached, cached_path

    source = Path(str(state.get("source_path") or ""))
    provider = str(state.get("provider") or "")
    if provider not in {"codex", "claude"} or not source.is_file():
        raise TraceError("trace state has an invalid provider or source path")
    start = int(state.get("source_offset") or 0)
    source_stat = source.stat()
    source_size = source_stat.st_size
    expected_device = state.get("source_device")
    expected_inode = state.get("source_inode")
    if expected_device is not None and int(expected_device) != source_stat.st_dev:
        raise TraceError("trace source file was replaced after start")
    if expected_inode is not None and int(expected_inode) != source_stat.st_ino:
        raise TraceError("trace source file was replaced after start")
    end = int(state.get("end_offset") or source_size)
    if start < 0 or end < start or end > source_size:
        raise TraceError(
            f"trace source changed unexpectedly: start={start}, end={end}, size={source_size}"
        )
    state["end_offset"] = end
    atomic_write_json(state_path, state)

    if max_event_bytes < 1024:
        raise TraceError("--max-event-bytes must be at least 1024")
    if max_trace_bytes < 64 * 1024:
        raise TraceError("--max-trace-bytes must be at least 65536")

    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    fd, raw_events_path = tempfile.mkstemp(prefix=".events-", dir=root)
    raw_events = Path(raw_events_path)
    sanitizer = Sanitizer()
    dropped: Counter[str] = Counter()
    raw_digest = hashlib.sha256()
    recent_keys: deque[str] = deque(maxlen=128)
    recent_set: set[str] = set()
    detailed_budget = max_trace_bytes * 3 // 4
    written_bytes = 0
    event_count = 0
    detailed_count = 0
    compact_count = 0
    truncated_count = 0
    compact_mode = False

    try:
        with os.fdopen(fd, "wb") as event_output, source.open("rb") as handle:
            for raw in read_segment_lines(
                handle,
                start=start,
                end=end,
                raw_digest=raw_digest,
                dropped=dropped,
            ):
                try:
                    event = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    dropped["invalid_json"] += 1
                    continue
                if not isinstance(event, dict):
                    dropped["non_object_event"] += 1
                    continue
                produced = list(normalized_events(provider, event, sanitizer))
                if not produced:
                    dropped[f"source_type:{event.get('type') or 'unknown'}"] += 1
                for normalized in produced:
                    key = dedupe_key(normalized)
                    if key in recent_set:
                        dropped["duplicate_event"] += 1
                        continue
                    if len(recent_keys) == recent_keys.maxlen:
                        recent_set.discard(recent_keys[0])
                    recent_keys.append(key)
                    recent_set.add(key)

                    fitted, was_truncated = fit_event(
                        normalized, max_event_bytes=max_event_bytes
                    )
                    encoded = canonical_json(fitted) + b"\n"
                    if was_truncated:
                        truncated_count += 1
                    if not compact_mode and written_bytes + len(encoded) > detailed_budget:
                        compact_mode = True
                    if compact_mode and not fitted.get("compacted"):
                        encoded = canonical_json(
                            compact_event(normalized, canonical_json(normalized))
                        ) + b"\n"
                    if written_bytes + len(encoded) > max_trace_bytes:
                        dropped["global_budget_event"] += 1
                        dropped["global_budget_bytes"] += len(encoded)
                        continue
                    event_output.write(encoded)
                    written_bytes += len(encoded)
                    event_count += 1
                    if compact_mode:
                        compact_count += 1
                    else:
                        detailed_count += 1

        manifest = {
            "format": TRACE_FORMAT,
            "schema_version": TRACE_SCHEMA_VERSION,
            "provider": provider,
            "session_id": str(state.get("session_id") or source.stem),
            "captured_from": state.get("started_at"),
            "captured_at": utcnow_iso(),
            "event_count": event_count,
            "detailed_event_count": detailed_count,
            "compacted_event_count": compact_count,
            "truncated_event_count": truncated_count,
            "source": {
                "filename": source.name,
                "segment_bytes": end - start,
                "segment_sha256": raw_digest.hexdigest(),
            },
            "normalization": {
                "max_event_bytes": max_event_bytes,
                "max_trace_bytes": max_trace_bytes,
                "redactions": sanitizer.redactions,
                "omitted_fields": sanitizer.omitted_fields,
                "dropped": dict(sorted(dropped.items())),
                "private_reasoning_included": False,
            },
        }

        fd, raw_gzip_path = tempfile.mkstemp(prefix=".trace-", suffix=".jsonl.gz", dir=root)
        gzip_tmp = Path(raw_gzip_path)
        try:
            with os.fdopen(fd, "wb") as raw_output:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=1,
                    fileobj=raw_output,
                    mtime=0,
                ) as compressed:
                    compressed.write(canonical_json(manifest) + b"\n")
                    with raw_events.open("rb") as event_input:
                        shutil.copyfileobj(event_input, compressed, READ_CHUNK_BYTES)
                raw_output.flush()
                os.fsync(raw_output.fileno())
            os.chmod(gzip_tmp, 0o600)
            artifact_sha = sha256_file(gzip_tmp)
            trace_id = f"trace_{artifact_sha[:32]}"
            destination = (
                output.expanduser().resolve()
                if output is not None
                else root / "artifacts" / f"{trace_id}.jsonl.gz"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(gzip_tmp, destination)
        finally:
            gzip_tmp.unlink(missing_ok=True)

        artifact = {
            "trace_id": trace_id,
            "path": str(destination),
            "sha256": artifact_sha,
            "compressed_bytes": destination.stat().st_size,
            "uncompressed_event_bytes": written_bytes,
            "event_count": event_count,
            "provider": provider,
            "manifest": manifest,
        }
        state["artifact"] = artifact
        atomic_write_json(state_path, state)
        return artifact, destination
    finally:
        raw_events.unlink(missing_ok=True)


def read_response(response: http.client.HTTPResponse) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise UploadError("trace upload response exceeded 1 MiB")
    return body


def upload_once(
    *,
    artifact: dict[str, Any],
    path: Path,
    base_url: str,
    token: str,
    timeout: float,
) -> dict[str, Any]:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UploadError(f"invalid ScoreBench URL: {base_url}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UploadError("ScoreBench URL must not contain credentials, query, or fragment")
    endpoint = f"{parsed.path.rstrip('/')}/run/trace" or "/run/trace"
    connection_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.putrequest("POST", endpoint)
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Type", "application/gzip")
        connection.putheader("Content-Length", str(path.stat().st_size))
        connection.putheader("X-Scorebench-Trace-Id", str(artifact["trace_id"]))
        connection.putheader("X-Scorebench-Trace-Sha256", str(artifact["sha256"]))
        connection.putheader("User-Agent", "scorebench-skill-run-trace/1")
        connection.endheaders()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                connection.send(chunk)
        response = connection.getresponse()
        body = read_response(response)
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"error": body[:500].decode("utf-8", errors="replace")}
        if 200 <= response.status < 300:
            if not isinstance(payload, dict):
                raise UploadError("trace upload returned a non-object response")
            return payload
        error = payload.get("error") if isinstance(payload, dict) else payload
        raise UploadError(
            f"HTTP {response.status}: {error or response.reason}",
            retryable=response.status == 429 or response.status >= 500,
        )
    except (OSError, http.client.HTTPException) as exc:
        raise UploadError(f"trace upload failed: {exc}", retryable=True) from exc
    finally:
        connection.close()


def upload_trace(
    *,
    artifact: dict[str, Any],
    path: Path,
    base_url: str,
    token: str,
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    if not token:
        raise UploadError("SCOREBENCH_RUN_TOKEN is required to upload the trace")
    last_error: UploadError | None = None
    for attempt in range(retries + 1):
        try:
            return upload_once(
                artifact=artifact,
                path=path,
                base_url=base_url,
                token=token,
                timeout=timeout,
            )
        except UploadError as exc:
            last_error = exc
            if not exc.retryable or attempt >= retries:
                raise
            time.sleep(min(2**attempt, 8))
    assert last_error is not None
    raise last_error


def trace_finish(args: argparse.Namespace) -> dict[str, Any]:
    cwd = Path(args.cwd or Path.cwd()).expanduser().resolve()
    state_path = (
        Path(args.state).expanduser().resolve()
        if args.state
        else default_state_path(cwd)
    )
    state = load_state(state_path)
    artifact, artifact_path = build_artifact(
        state,
        state_path=state_path,
        output=Path(args.output) if args.output else None,
        max_event_bytes=args.max_event_bytes,
        max_trace_bytes=args.max_trace_bytes,
    )
    result: dict[str, Any] = {
        "ok": True,
        "action": "finish",
        "trace_id": artifact["trace_id"],
        "provider": artifact["provider"],
        "event_count": artifact["event_count"],
        "compressed_bytes": artifact["compressed_bytes"],
        "artifact": str(artifact_path),
        "uploaded": False,
    }
    if args.no_upload:
        return result

    paired = paired_workspace_context(cwd)
    base_url = (
        args.url
        or os.environ.get("SCOREBENCH_URL")
        or os.environ.get("HARNESS_URL")
        or str(paired.get("url") or "")
        or "https://scorebench.dev/"
    )
    token = os.environ.get("SCOREBENCH_RUN_TOKEN") or os.environ.get(
        "HARNESS_RUN_TOKEN"
    ) or str(
        paired.get("run_token") or ""
    )
    response = upload_trace(
        artifact=artifact,
        path=artifact_path,
        base_url=base_url,
        token=token,
        timeout=args.timeout,
        retries=args.retries,
    )
    result["uploaded"] = True
    result["server"] = response
    state = load_state(state_path)
    state["upload"] = {
        "uploaded_at": utcnow_iso(),
        "response": response,
    }
    atomic_write_json(state_path, state)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture a sanitized agent trace with no background runtime work."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser(
        "start",
        help="record the current session JSONL and byte offset; does not parse or upload",
    )
    start.add_argument("--provider", choices=("auto", "codex", "claude"), default="auto")
    start.add_argument("--source", type=Path, help="explicit Codex or Claude JSONL")
    start.add_argument("--state", type=Path, help="state path; normally auto-selected")
    start.add_argument("--cwd", type=Path, help="agent workspace; defaults to current directory")

    finish = sub.add_parser(
        "finish",
        help="sanitize, compress, and upload the trace segment recorded by start",
    )
    finish.add_argument("--state", type=Path, help="state path; normally auto-selected")
    finish.add_argument("--cwd", type=Path, help="agent workspace; defaults to current directory")
    finish.add_argument("--output", type=Path, help="override local gzip artifact path")
    finish.add_argument("--url", help="ScoreBench base URL")
    finish.add_argument("--no-upload", action="store_true", help="build the artifact without uploading")
    finish.add_argument("--timeout", type=float, default=120.0)
    finish.add_argument("--retries", type=int, default=2)
    finish.add_argument("--max-event-bytes", type=int, default=DEFAULT_MAX_EVENT_BYTES)
    finish.add_argument("--max-trace-bytes", type=int, default=DEFAULT_MAX_TRACE_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            cwd = Path(args.cwd or Path.cwd()).expanduser().resolve()
            state_path = (
                Path(args.state).expanduser().resolve()
                if args.state
                else default_state_path(cwd)
            )
            result = trace_start(
                provider=args.provider,
                source=args.source,
                cwd=cwd,
                state_path=state_path,
            )
        else:
            if args.retries < 0:
                raise TraceError("--retries cannot be negative")
            if args.timeout <= 0:
                raise TraceError("--timeout must be positive")
            result = trace_finish(args)
    except TraceError as exc:
        print(f"trace error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
