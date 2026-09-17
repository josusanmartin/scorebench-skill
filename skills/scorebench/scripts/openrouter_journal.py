"""Durable, secret-free request identities and append-only receipt repairs."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from openrouter_generations import STREAM_ERRORS, generation_usage, stream_gap
from openrouter_failures import exclusion_records

JOURNAL_FILE = "requests.sqlite3"
UNSETTLED = ("pending", "unresolved", "conflict")
REPAIRABLE_ERRORS = STREAM_ERRORS | {"retained request ended without a durable receipt", "generation response omitted usage"}
COUNTERS = {"prompt_tokens", "completion_tokens", "total_tokens", "cost", "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens", "cached_tokens", "cache_write_tokens",
            "reasoning_tokens"}
DETAILS = {"prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details"}


def valid_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def invalid_usage(usage):
    return isinstance(usage, dict) and any(
        (key in COUNTERS and not valid_number(value)) or (key in DETAILS and invalid_usage(value))
        for key, value in usage.items())


def safe_usage(usage):
    if not isinstance(usage, dict):
        return {}
    return {key: safe_usage(value) if key in DETAILS else value for key, value in usage.items()
            if (key in DETAILS and isinstance(value, dict)) or
            (key in COUNTERS and valid_number(value))}


class RequestJournal:
    def __init__(self, ledger: Path, *, readonly=False):
        self.ledger = ledger.resolve()
        self.path = ledger.with_name(JOURNAL_FILE)
        self.readonly = readonly
        start = ledger.parent.parent / "supervisor-run-start.json"
        self.run_id = json.loads(start.read_text())["run"]["run_id"] if start.exists() else None
        if not readonly:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
        with self.connect() as db:
            if not readonly:
                db.execute("CREATE TABLE IF NOT EXISTS binding (version INTEGER, ledger TEXT, run_id TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS requests ("
                           "request_id TEXT PRIMARY KEY, model TEXT, body_sha256 TEXT, attempt INTEGER, "
                           "created_at REAL, updated_at REAL, generation_id TEXT, observed TEXT, "
                           "state TEXT, reason TEXT, lookup_attempts INTEGER DEFAULT 0, next_lookup_at REAL DEFAULT 0)")
                if not db.execute("SELECT 1 FROM binding").fetchone():
                    db.execute("INSERT INTO binding VALUES (1, ?, ?)", (str(self.ledger), self.run_id))
            rows = db.execute("SELECT version, ledger, run_id FROM binding").fetchall()
            if len(rows) != 1 or tuple(rows[0]) != (1, str(self.ledger), self.run_id):
                raise ValueError("OpenRouter journal does not match the retained ledger and run")

    @contextmanager
    def connect(self):
        uri = self.path.resolve().as_uri() + ("?mode=ro" if self.readonly else "?mode=rw")
        db = sqlite3.connect(uri, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if not self.readonly:
                db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def begin(self, model, body: bytes | None, attempt: int) -> str:
        request_id, now = uuid4().hex, time.time()
        with self.connect() as db:
            db.execute("INSERT INTO requests (request_id, model, body_sha256, attempt, created_at, updated_at, observed, state) "
                       "VALUES (?, ?, ?, ?, ?, ?, '{}', 'pending')",
                       (request_id, model, hashlib.sha256(body or b"").hexdigest(), attempt, now, now))
        return request_id

    def observe(self, request_id: str, response: dict) -> None:
        # Persist only identity and counters, never deltas, prompts or headers.
        identity = {key: response.get(key) for key in ("id", "model") if response.get(key)}
        if identity.get("id") and not re.fullmatch(r"[A-Za-z0-9_-]{1,204}", str(identity["id"])):
            self.finish(request_id, "conflict", "invalid generation identity")
            raise ValueError("invalid OpenRouter generation identity")
        if identity.get("model") and (not isinstance(identity["model"], str) or len(identity["model"]) > 256):
            raise ValueError("invalid OpenRouter model identity")
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            previous = json.loads(row["observed"])
            observed = {**previous, **identity, "usage": {**previous.get("usage", {}), **safe_usage(response.get("usage"))}}
            conflict = response.get("identity_conflict") or response.get("usage_invalid") or invalid_usage(response.get("usage")) or any(
                previous.get(key) and identity.get(key) and previous[key] != identity[key] for key in ("id", "model"))
            if observed == previous and not conflict:
                return
            db.execute("UPDATE requests SET generation_id=COALESCE(generation_id, ?), observed=?, updated_at=?, "
                       "state=CASE WHEN ? THEN 'conflict' ELSE state END WHERE request_id=?",
                       (identity.get("id"), json.dumps(observed, sort_keys=True), time.time(), bool(conflict), request_id))

    def finish(self, request_id: str, state: str, reason: str = "") -> None:
        with self.connect() as db:
            db.execute("UPDATE requests SET state=?, reason=?, updated_at=? WHERE request_id=? AND state!='conflict'",
                       (state, reason, time.time(), request_id))

    def lookup_failed(self, request_id: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT lookup_attempts FROM requests WHERE request_id=?", (request_id,)).fetchone()
            count = row[0] + 1
            db.execute("UPDATE requests SET lookup_attempts=?, next_lookup_at=? WHERE request_id=?",
                       (count, time.time() + min(300, 30 * 2 ** min(count - 1, 4)), request_id))

    def rows(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY created_at, request_id")]

    def adopt(self, row):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO requests (request_id, model, attempt, created_at, updated_at, "
                       "generation_id, observed, state, reason) VALUES (?, ?, 0, ?, ?, ?, ?, 'unresolved', ?)",
                       (row["request_id"], row["model"], time.time(), time.time(), row["generation_id"],
                        row["observed"], row["reason"]))


def read_records(path: Path, *, allow_partial_tail=False, raw=None) -> list[tuple[int, str, dict]]:
    raw = path.read_text() if raw is None else raw
    lines = raw.splitlines()
    records = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if index == len(lines) and not raw.endswith("\n"):
            if allow_partial_tail:
                break
            raise ValueError("unfinished OpenRouter ledger; retain it without appending")
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("invalid OpenRouter ledger record")
        records.append((index, line, record))
    return records


def resolved_error_lines(records) -> set[int]:
    """A repair references exact original error bytes and provider evidence."""
    by_line = {index: (line, record) for index, line, record in records}
    resolved = set()
    for index, _, record in records:
        references = record.get("resolved_errors", [])
        if not references:
            continue
        lookup = record.get("reconciliation", {})
        if lookup.get("source") != "openrouter_generation_api" or not lookup.get("data", {}).get("finish_reason"):
            raise ValueError("receipt repair requires terminal provider evidence")
        expected = generation_usage(lookup["data"], record.get("id"), record.get("model"), model_alias=lookup.get("model_alias"))
        if record.get("usage") != expected:
            raise ValueError("receipt repair differs from provider usage")
        for ref in references:
            number = ref.get("line")
            if type(number) is not int or number >= index or number not in by_line:
                raise ValueError("receipt repair has an invalid error reference")
            line, error = by_line[number]
            if (error.get("accounting_error") not in REPAIRABLE_ERRORS or error.get("generation_id") != record.get("id")
                    or error.get("model") not in (None, record.get("model"), lookup["data"]["model"])
                    or error.get("request_id") not in (None, record.get("request_id"))
                    or hashlib.sha256(line.encode()).hexdigest() != ref.get("sha256")):
                raise ValueError("receipt repair does not match the original error")
            resolved.add(number)
    return resolved


def matching_receipt(records, generation_id):
    if not generation_id:
        return None
    matches = [record for _, _, record in records if record.get("id") == generation_id and record.get("usage")]
    if matches and any((row.get("model"), row["usage"]) != (matches[0].get("model"), matches[0]["usage"]) for row in matches):
        raise ValueError("conflicting OpenRouter receipts for one generation")
    return matches[0] if matches else None


def reconciled_receipt(row: dict, lookup: dict, records) -> dict:
    generation_id, model = row["generation_id"], row["model"]
    observed = json.loads(row["observed"])
    if not lookup.get("data", {}).get("finish_reason"):
        raise ValueError("generation termination is not confirmed")
    usage = generation_usage(lookup["data"], generation_id, model, model_alias=lookup.get("model_alias"))
    if observed.get("model") not in (None, model, lookup["data"]["model"]):
        raise ValueError("generation lookup differs from observed model")
    for field in ("prompt_tokens", "completion_tokens", "cost"):
        value = observed.get("usage", {}).get(field)
        if value is not None and (type(value) not in (int, float) or not 0 <= value <= usage[field]):
            raise ValueError("generation lookup contradicts observed usage")
    references = [{"line": number, "sha256": hashlib.sha256(line.encode()).hexdigest()}
                  for number, line, error in records if error.get("accounting_error")
                  and error.get("generation_id") == generation_id
                  and error.get("request_id") in (None, row["request_id"])]
    return {"id": generation_id, "model": model, "request_id": row["request_id"], "usage": usage,
            "reconciliation": {**lookup, "reason": row["reason"] or "retained in-flight request",
                               "observed_usage": observed.get("usage", {})},
            **({"resolved_errors": references} if references else {})}


def append_record(path, record):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def reconcile_requests(path: Path, lookup, *, check=False, abandoned=False, max_lookups=4) -> dict:
    """Caller holds the ledger writer lock; lookup performs metadata GETs only."""
    records = read_records(path)
    _, excluded = exclusion_records(records)
    resolved = resolved_error_lines(records)
    journal_path = path.with_name(JOURNAL_FILE)
    journal = RequestJournal(path, readonly=check) if journal_path.exists() or not check else None
    rows = journal.rows() if journal else []
    known = {row["request_id"] for row in rows}
    for number, line, error in records:
        if number in resolved or not stream_gap(error) or not error.get("model"):
            continue
        request_id = error.get("request_id") or "legacy-" + hashlib.sha256(line.encode()).hexdigest()
        if request_id in known:
            continue
        row = {"request_id": request_id, "model": error["model"], "generation_id": error["generation_id"],
               "observed": "{}", "state": "unresolved", "reason": error["accounting_error"], "next_lookup_at": 0}
        rows.append(row)
        known.add(request_id)
        if not check:
            journal.adopt(row)
    result = {"checked": check, "lookups": 0, "reconciled": 0, "unresolved": 0, "without_generation_id": 0}
    receipted_requests = {item.get("request_id") for _, _, item in records if item.get("usage")}
    for row in rows:
        if row["request_id"] in excluded and row["state"] != "conflict":
            if not check:
                journal.finish(row["request_id"], "excluded", excluded[row["request_id"]]["accounting_policy"])
            continue
        if row["state"] == "excluded":
            raise ValueError("excluded OpenRouter request has no durable policy evidence")
        if row["state"] == "accounted" and row["request_id"] not in receipted_requests:
            row["state"] = "pending"
        if row["state"] not in UNSETTLED or (row["state"] == "pending" and not abandoned):
            continue
        if row["state"] == "pending":
            error = {"accounting_error": "retained request ended without a durable receipt", "request_id": row["request_id"],
                     "generation_id": row["generation_id"], "model": row["model"]}
            if not check:
                journal.finish(row["request_id"], "unresolved", error["accounting_error"])
            # A receipt may have committed immediately before the journal state update.
            if not matching_receipt(records, row["generation_id"]) and not any(
                    item.get("accounting_error") and item.get("request_id") == row["request_id"] for _, _, item in records):
                if not check:
                    append_record(path, error)
                    records = read_records(path)
        remaining_errors = [number for number, _, item in records if item.get("accounting_error")
                            and item.get("generation_id") == row["generation_id"] and number not in resolved]
        receipt = matching_receipt(records, row["generation_id"])
        if receipt and not remaining_errors and row["state"] != "conflict":
            if row["model"] not in (None, receipt.get("model")):
                raise ValueError("retained request model conflicts with its receipt")
            if not check:
                journal.finish(row["request_id"], "accounted")
            continue
        if row["state"] == "conflict" or not row["generation_id"] or not re.fullmatch(r"gen-[A-Za-z0-9_-]{1,200}", row["generation_id"]):
            result["unresolved"] += 1
            result["without_generation_id"] += int(not row["generation_id"])
            continue
        if result["lookups"] >= max_lookups or time.time() < row["next_lookup_at"]:
            result["unresolved"] += 1
            continue
        result["lookups"] += 1
        try:
            repair = reconciled_receipt(row, lookup(row["generation_id"], row["model"]), records)
            candidate_records = [*records, ((records[-1][0] if records else 0) + 1, json.dumps(repair, sort_keys=True), repair)]
            matching_receipt(candidate_records, row["generation_id"])
            resolved = resolved_error_lines(candidate_records)
        except (ValueError, OSError, OverflowError, HTTPException):
            if not check:
                journal.lookup_failed(row["request_id"])
            result["unresolved"] += 1
            continue
        if not check:
            append_record(path, repair)
            journal.finish(row["request_id"], "accounted")
        records = candidate_records
        result["reconciled"] += 1
    # Old errors without any provider identity remain unknown, not zero-cost.
    errors = [item for number, _, item in records if item.get("accounting_error") and number not in resolved]
    from token_usage import openrouter_usage_snapshot
    seen = {}
    for _, _, record in records:
        if record.get("event") in {"accounting_policy", "infrastructure_excluded"}:
            continue
        if not record.get("accounting_error"):
            if openrouter_usage_snapshot(record.get("usage", record)) is None:
                raise ValueError("retained OpenRouter receipt has invalid usage")
            if record.get("id"):
                identity = (record.get("model"), record.get("usage"))
                if record["id"] in seen and seen[record["id"]] != identity:
                    raise ValueError("conflicting OpenRouter receipts for one generation")
                seen[record["id"]] = identity
    result["unresolved"] = max(result["unresolved"], len(errors))
    result["without_generation_id"] = max(result["without_generation_id"], sum(not item.get("generation_id") for item in errors))
    current_rows = journal.rows() if journal and not check else rows
    deadlines = [row.get("next_lookup_at", 0) for row in current_rows if row["state"] in UNSETTLED
                 and row.get("generation_id") and row.get("next_lookup_at", 0) > time.time()]
    result["next_lookup_at"] = min(deadlines) if deadlines else None
    result["accounting_complete"] = result["unresolved"] == 0
    return result
