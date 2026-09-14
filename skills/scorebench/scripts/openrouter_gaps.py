"""Explicit acknowledgement of retained transport gaps, never receipt replacement."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

ACK_FILE = "accepted-gaps.json"
PARTIAL_SOURCE = "openrouter_usage_partial"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def transport_gap(record) -> bool:
    return (isinstance(record, dict)
            and record.get("accounting_error") == "upstream transport failure; generation acceptance and cost are unknown"
            and record.get("phase") == "request_or_headers"
            and isinstance(record.get("request_id"), str)
            and re.fullmatch(r"[a-f0-9]{32}", record["request_id"]) is not None)


def load_ack(log: Path, state: Path, *, preview: str = "") -> dict | None:
    path = log.with_name(ACK_FILE)
    ack = json.loads(preview) if preview else json.loads(path.read_text()) if path.exists() else None
    if ack is None:
        return None
    raw = log.read_bytes()
    size = ack.get("ledger_prefix_bytes") if isinstance(ack, dict) else None
    if (not isinstance(ack, dict) or ack.get("version") != 1 or ack.get("accepted_partial_accounting") is not True
            or type(size) is not int or size <= 0 or size > len(raw)
            or not raw[:size].endswith(b"\n") or digest(raw[:size]) != ack.get("ledger_prefix_sha256")
            or digest(state.read_bytes()) != ack.get("baseline_sha256")
            or str(log.resolve()) != ack.get("ledger_path")):
        raise ValueError("OpenRouter accounting-gap acknowledgement does not match retained evidence")
    gaps = {str(i): digest(line) for i, line in enumerate(raw[:size].splitlines(), 1)
            if line.strip() and transport_gap(json.loads(line))}
    if not gaps or ack.get("gaps") != gaps:
        raise ValueError("invalid acknowledged OpenRouter transport gaps")
    return ack


def prepare_ack(log: Path, state: Path, *, run_id: str, session_id: str, result: dict, control: dict) -> dict:
    previous = load_ack(log, state)
    if previous and (previous.get("run_id") != run_id or previous.get("session_id") != session_id):
        raise ValueError("accounting-gap acknowledgement belongs to another run or session")
    raw = log.read_bytes()
    if not raw.endswith(b"\n"):
        raise ValueError("unfinished OpenRouter ledger cannot be acknowledged")
    gaps = {}
    for i, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if isinstance(record, dict) and record.get("accounting_error"):
            if not transport_gap(record):
                raise ValueError("only retained request/header transport gaps support partial recovery")
            gaps[str(i)] = digest(line)
    if not gaps:
        raise ValueError("no retained transport gap to acknowledge")
    return {"version": 1, "accepted_partial_accounting": True, "created_at": time.time(),
            "run_id": run_id, "session_id": session_id, "ledger_path": str(log.resolve()),
            "ledger_prefix_bytes": len(raw), "ledger_prefix_sha256": digest(raw),
            "baseline_sha256": digest(state.read_bytes()), "gaps": gaps,
            "previous_result": result, "previous_runtime_control": control, "previous_ack": previous}
