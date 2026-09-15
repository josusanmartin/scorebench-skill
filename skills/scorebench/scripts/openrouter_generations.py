"""Read-only generation lookups for retained missing-stream accounting receipts."""
from __future__ import annotations

from http.client import HTTPException
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

STREAM_ERRORS = {"generation stream omitted final usage", "generation stream interrupted before final usage"}


class GenerationPending(ValueError):
    """Usage exists, but OpenRouter has not recorded a terminal generation."""


def stream_gap(record) -> bool:
    return (isinstance(record, dict) and record.get("accounting_error") in STREAM_ERRORS
            and isinstance(record.get("generation_id"), str)
            and re.fullmatch(r"gen-[A-Za-z0-9_-]{1,200}", record["generation_id"]) is not None)


def generation_usage(data: dict, generation_id: str, model: str, *, model_alias: dict | None = None) -> dict:
    alias = model_alias.get("data", {}) if isinstance(model_alias, dict) else {}
    matches_alias = (isinstance(model_alias, dict) and model_alias.get("source") == "openrouter_models_api"
                     and isinstance(alias, dict) and alias.get("id") == model
                     and isinstance(alias.get("canonical_slug"), str) and bool(alias["canonical_slug"])
                     and isinstance(data, dict) and data.get("model") == alias["canonical_slug"])
    if (not isinstance(data, dict) or data.get("id") != generation_id
            or not model or (data.get("model") != model and not matches_alias)):
        raise ValueError("OpenRouter generation lookup does not match the retained generation and model")
    counts = {}
    for field in ("native_tokens_prompt", "native_tokens_completion", "native_tokens_cached", "native_tokens_reasoning"):
        value = data.get(field)
        if field == "native_tokens_reasoning" and value is None:
            continue
        if type(value) is not int or value < 0:
            raise ValueError(f"OpenRouter generation lookup has no valid {field}")
        counts[field] = value
    prompt, output = counts["native_tokens_prompt"], counts["native_tokens_completion"]
    if counts["native_tokens_cached"] > prompt or counts.get("native_tokens_reasoning", 0) > output:
        raise ValueError("OpenRouter generation lookup has inconsistent native token counts")
    cost = data.get("total_cost")
    try:
        valid_cost = type(cost) in (float, int) and math.isfinite(cost) and cost >= 0
    except OverflowError:
        valid_cost = False
    if not valid_cost:
        raise ValueError("OpenRouter generation lookup has no valid total_cost")
    for field in ("finish_reason", "native_finish_reason"):
        if data.get(field) is not None and not isinstance(data[field], str):
            raise ValueError("OpenRouter generation lookup has an invalid finish reason")
    usage = {"prompt_tokens": prompt, "completion_tokens": output, "cost": cost,
             "prompt_tokens_details": {"cached_tokens": counts["native_tokens_cached"]}}
    if "native_tokens_reasoning" in counts:
        usage["completion_tokens_details"] = {"reasoning_tokens": counts["native_tokens_reasoning"]}
    # Native completion already includes reasoning. Generation metadata does not
    # expose cache-write counts; neither infer them nor reprice the reported cost.
    return usage


def lookup_model_alias(model: str, canonical: str, *, opener, base, timeout: int) -> dict:
    if not isinstance(canonical, str) or not canonical:
        raise ValueError("OpenRouter generation has no model identity")
    url = urllib.parse.urlunsplit((base.scheme, base.netloc, "/api/v1/models", "", ""))
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(8388609)
        if len(raw) > 8388608:
            raise ValueError("OpenRouter model catalog response is too large")
        payload = json.loads(raw)
    models = payload.get("data") if isinstance(payload, dict) else None
    matches = [item for item in models if isinstance(item, dict) and item.get("id") == model] if isinstance(models, list) else []
    if len(matches) != 1 or matches[0].get("canonical_slug") != canonical:
        raise ValueError("OpenRouter generation model does not match the published recipe alias")
    return {"source": "openrouter_models_api", "fetched_at": time.time(),
            "data": {"id": model, "canonical_slug": canonical}}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def lookup_generation(generation_id: str, model: str, *, upstream: str, api_key: str,
                      require_final: bool = False) -> dict:
    base = urllib.parse.urlsplit(upstream)
    if (base.username or base.password or base.query or base.fragment
            or not (base.scheme == "https" or (base.scheme == "http" and base.hostname in ("127.0.0.1", "localhost", "::1")))
            or base.path.rstrip("/") not in ("", "/api/v1") or not api_key):
        raise ValueError("invalid OpenRouter generation lookup configuration")
    url = urllib.parse.urlunsplit((base.scheme, base.netloc, "/api/v1/generation",
                                  urllib.parse.urlencode({"id": generation_id}), ""))
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=3 if require_final else 10) as response:
                raw = response.read(131073)
                if len(raw) > 131072:
                    raise ValueError("OpenRouter generation lookup response is too large")
                payload = json.loads(raw)
                data = payload.get("data") if isinstance(payload, dict) else None
            model_alias = None
            if isinstance(data, dict) and data.get("id") == generation_id and data.get("model") != model:
                model_alias = lookup_model_alias(model, data.get("model"), opener=opener, base=base,
                                                timeout=3 if require_final else 5)
            generation_usage(data, generation_id, model, model_alias=model_alias)
            if require_final and not data.get("finish_reason"):
                raise GenerationPending("OpenRouter generation has no terminal finish reason")
            # Store usage metadata only, never prompt/completion content or headers.
            fields = ("id", "model", "total_cost", "native_tokens_prompt", "native_tokens_completion",
                      "native_tokens_cached", "native_tokens_reasoning", "finish_reason", "native_finish_reason",
                      "provider_name", "created_at")
            lookup = {"source": "openrouter_generation_api", "fetched_at": time.time(),
                      "data": {key: data[key] for key in fields if key in data}}
            if model_alias:
                lookup["model_alias"] = model_alias
            return lookup
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = exc.headers.get("Retry-After", "")
            exc.close()
            if status not in (404, 429, 500, 502, 503, 504):
                raise ValueError(f"OpenRouter generation lookup refused (HTTP {status}); no recovery attempted") from None
            # Long cooldowns require a later read-only check, not a tight retry.
            if attempt == 2 or retry_after:
                raise ValueError(f"OpenRouter generation lookup unavailable (HTTP {status}); evidence unchanged, retry check later") from None
        except (OSError, urllib.error.URLError, HTTPException):
            if attempt == 2:
                raise ValueError("OpenRouter generation lookup transport failed; evidence unchanged, retry check later") from None
        except GenerationPending:
            if attempt == 2:
                raise
        time.sleep(attempt + 1)
    raise AssertionError("unreachable")
