"""Versioned experiment exclusions, distinct from the provider's invoice."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import random
import re

POLICY = "openrouter-infrastructure-v1"
SOURCE = "openrouter_experiment_usage"
INFRASTRUCTURE_CODES = {408, 429, 500, 502, 503, 504}
RETRY_CODES = INFRASTRUCTURE_CODES
ERROR_TYPES = {
    "provider_overloaded": 503, "provider_unavailable": 502, "server": 500, "timeout": 408,
    "rate_limit_exceeded": 429, "authentication": 401, "permission_denied": 403, "payment_required": 402,
    "context_length_exceeded": 400, "max_tokens_exceeded": 400, "token_limit_exceeded": 400,
    "string_too_long": 400, "invalid_request": 400, "invalid_prompt": 400, "not_found": 404,
    "precondition_failed": 412, "payload_too_large": 413, "unprocessable": 422,
    "content_policy_violation": 403, "refusal": 403, "invalid_image": 400, "image_too_large": 400,
    "image_too_small": 400, "unsupported_image_format": 400, "image_not_found": 404,
    "image_download_failed": 400, "unmapped": 0,
}


def model_output(obj):
    """Do not discard delivered text, reasoning, tool calls, or native output."""
    if not isinstance(obj, dict):
        return False
    choices = obj.get("choices")
    return any(obj.get(key) for key in (
        "content", "text", "reasoning", "reasoning_content", "reasoning_details",
        "tool_calls", "function_call", "output",
    )) or (isinstance(obj.get("delta"), str) and bool(obj["delta"])) or any(
        model_output(obj.get(key)) for key in ("message", "response", "delta", "content_block")) or any(
        model_output(choice) for choice in (choices if isinstance(choices, list) else []) if isinstance(choice, dict))


def failure_status(status, code, error_type):
    if error_type is not None:
        return ERROR_TYPES.get(error_type, 0)
    return code if type(code) is int and 400 <= code < 600 else status


def classify_error(status, obj):
    response = obj.get("response", obj) if isinstance(obj, dict) else {}
    response = response if isinstance(response, dict) else {}
    error = response.get("error")
    if not isinstance(error, dict):
        choices = response.get("choices")
        error = next((choice["error"] for choice in (choices if isinstance(choices, list) else [])
                      if isinstance(choice, dict) and isinstance(choice.get("error"), dict)), {})
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and code.isdecimal():
        code = int(code)
    if type(code) is not int:
        code = None
    metadata = error.get("metadata")
    error_type = (metadata.get("error_type") if isinstance(metadata, dict) else None) or error.get("error_type") or response.get("error_type")
    if error_type is not None:
        error_type = error_type if isinstance(error_type, str) and error_type in ERROR_TYPES else "unrecognized"
    return failure_status(status, code, error_type), code, error_type


def response_evidence(status, content_type, body):
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        obj = None
    effective, code, error_type = classify_error(status, obj)
    evidence = {
        "http_status": status, "error_code": code, "error_type": error_type,
        "content_type": content_type.split(";", 1)[0].strip().lower()
        if content_type.split(";", 1)[0].strip().lower() in {
            "application/json", "text/plain", "text/html", "text/event-stream"} else "other",
        "response_bytes": len(body), "response_sha256": hashlib.sha256(body).hexdigest(),
        "model_output": bool(model_output(obj)),
    }
    return effective, evidence, obj


def retry_delay(status, headers, attempt):
    if status not in RETRY_CODES or attempt >= 3:
        return None
    value = headers.get("Retry-After")
    if value is not None:
        try:
            delay = float(value)
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                date = date if date.tzinfo else date.replace(tzinfo=timezone.utc)
                delay = (date - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return None
        # Longer waits are delegated to the native client, with the header intact.
        return max(0, delay) if math.isfinite(delay) and delay <= 60 else None
    return min(30, 2 ** attempt) + random.uniform(0, 1)


def exclusion_records(records):
    """Validate durable policy and exclusions before calculating experiment usage."""
    enabled, excluded = False, {}
    ids, receipted = set(), set()
    for _, _, record in records:
        event = record.get("event")
        if event == "accounting_policy":
            if enabled or record != {"event": event, "accounting_policy": POLICY, "accounting_basis": "experiment"}:
                raise ValueError("invalid OpenRouter experiment accounting policy")
            enabled = True
        elif event == "infrastructure_excluded":
            evidence = record.get("evidence") or {}
            status, code = evidence.get("http_status"), evidence.get("error_code")
            request_id = record.get("request_id")
            if (not enabled or record.get("accounting_policy") != POLICY
                    or not re.fullmatch(r"[a-f0-9]{32}", str(request_id))
                    or request_id in excluded or evidence.get("model_output") is not False
                    or type(status) is not int or failure_status(status, code, evidence.get("error_type")) not in INFRASTRUCTURE_CODES
                    or not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("response_sha256")))
                    or type(evidence.get("response_bytes")) is not int or evidence["response_bytes"] < 0
                    or record.get("accounting_error") or "usage" in record):
                raise ValueError("invalid OpenRouter infrastructure exclusion evidence")
            excluded[request_id] = record
            if record.get("generation_id"):
                ids.add(record["generation_id"])
        elif event:
            raise ValueError("unknown OpenRouter accounting event")
        elif not record.get("accounting_error"):
            receipted.add(record.get("request_id"))
            if record.get("id"):
                receipted.add(record["id"])
    if receipted & (set(excluded) | ids):
        raise ValueError("OpenRouter request cannot be both included and excluded")
    return enabled, excluded


def accounting_summary(records):
    enabled, excluded = exclusion_records(records)
    known, unknown = [], 0
    for record in excluded.values():
        cost = record.get("provider_usage", {}).get("cost")
        if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            known.append(cost)
        else:
            unknown += 1
    return {
        "accounting_basis": "experiment" if enabled else "provider_usage",
        "accounting_policy": POLICY if enabled else None,
        "excluded_infrastructure_requests": len(excluded),
        "excluded_provider_cost_usd": math.fsum(known),
        "excluded_provider_cost_unknown_requests": unknown,
    }
