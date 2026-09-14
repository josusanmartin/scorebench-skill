"""Native routing overrides for multi-provider coding agents.

Require an explicit model/provider so a saved default cannot silently select
an unmetered provider. Overrides live in the child environment or workspace,
never in the user's global provider configuration.
"""
from __future__ import annotations

import json
import os
import math
import subprocess
from pathlib import Path
from typing import Sequence
import urllib.parse
import urllib.request


def check_installation(command: Sequence[str], env: dict[str, str]) -> None:
    try:
        result = subprocess.run([command[0], "--version"], env=env, capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError("version command failed")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise SystemExit("selected coding CLI is not ready; repair its installation before starting the run") from exc


def model_metadata(model: str, upstream: str) -> dict:
    """Resolve new models without waiting for the agent's bundled catalog."""
    url = upstream.rstrip("/") + "/api/v1/models/" + urllib.parse.quote(model, safe="/") + "/endpoints"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.load(response)["data"]
        endpoints = [endpoint for endpoint in data["endpoints"]
                     if "tools" in endpoint.get("supported_parameters", [])]
        if data["id"] != model or not endpoints:
            raise ValueError("no matching tool-capable endpoint")
        contexts = [int(endpoint["context_length"]) for endpoint in endpoints]
        outputs = [int(endpoint["max_completion_tokens"]) for endpoint in endpoints
                   if endpoint.get("max_completion_tokens")]
        context = min(contexts)
        maximum = min(outputs) if outputs else min(context, 32768)
        if min(context, maximum) <= 0:
            raise ValueError("invalid model limits")
        pricing = endpoints[0]["pricing"]
        def price(key: str, fallback: str = "prompt") -> float:
            value = float(pricing.get(key, pricing.get(fallback, 0))) * 1_000_000
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid model price")
            return value
        return {"id": model, "name": data["name"],
                "reasoning": any("reasoning" in e.get("supported_parameters", []) for e in endpoints),
                "input": [mode for mode in data.get("architecture", {}).get("input_modalities", ["text"])
                          if mode in {"text", "image"}],
                "contextWindow": context, "maxTokens": min(maximum, context),
                "cost": {"input": price("prompt"), "output": price("completion"),
                         "cacheRead": price("input_cache_read"), "cacheWrite": price("input_cache_write")},
                "compat": {"thinkingFormat": "openrouter", "maxTokensField": "max_tokens",
                           "supportsDeveloperRole": False}}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit("OpenRouter model metadata unavailable or model does not support tools; check the exact model ID before launch") from exc


def agent_kind(harness: str) -> str:
    name = harness.strip().lower()
    return name if name in {"pi", "opencode"} else ""


def option(command: Sequence[str], *names: str) -> str:
    values = []
    index = 1
    while index < len(command):
        argument = command[index]
        if argument == "--":
            break
        if argument in names:
            index += 1
            if index >= len(command):
                raise SystemExit(f"{argument} requires a value")
            values.append(command[index])
        else:
            for name in names:
                if argument.startswith(name + "="):
                    values.append(argument.split("=", 1)[1])
        index += 1
    if len(set(values)) > 1:
        raise SystemExit(f"conflicting {names[0]} arguments; select one model/provider")
    return values[-1] if values else ""


def selected_route(harness: str, command: Sequence[str]) -> tuple[str, str]:
    kind = agent_kind(harness)
    if not kind:
        return "", ""
    model = option(command, "--model", "-m")
    if not model:
        raise SystemExit(f"{harness} accounting requires an explicit --model; do not rely on a saved default")
    if kind == "opencode":
        provider, separator, model_id = model.partition("/")
        if not separator or not model_id:
            raise SystemExit("OpenCode --model must be provider/model, for example openrouter/deepseek/deepseek-v4.1-flash")
        return provider, model_id
    provider = option(command, "--provider")
    if not provider:
        raise SystemExit("Pi accounting requires an explicit --provider and --model")
    return provider, model


def validate_route(harness: str, command: Sequence[str], expected_model: str = "", expected_effort: str = "") -> tuple[str, str]:
    provider, model = selected_route(harness, command)
    if not provider:
        return provider, model
    if expected_model and expected_model not in {model, f"{provider}/{model}"}:
        raise SystemExit("coding harness model does not match the assigned ScoreBench recipe")
    effort = option(command, "--variant" if agent_kind(harness) == "opencode" else "--thinking")
    if expected_effort and effort != expected_effort:
        raise SystemExit("coding harness reasoning effort does not match the assigned ScoreBench recipe")
    if provider == "openrouter" and effort and effort not in {"low", "medium", "high"}:
        raise SystemExit("ScoreBench Pi/OpenCode OpenRouter recipes support low, medium or high effort")
    if agent_kind(harness) == "opencode" and any(
        arg == "--attach" or arg.startswith("--attach=") for arg in command[1:]
    ):
        raise SystemExit("OpenCode --attach cannot use a per-worker proxy; start a local OpenCode worker")
    return provider, model


def route_agent(
    harness: str, command: list[str], env: dict[str, str], origin: str, workspace: Path,
    metadata: dict | None = None,
) -> list[str]:
    kind = agent_kind(harness)
    provider, model = selected_route(harness, command)
    if provider != "openrouter":
        raise SystemExit(f"{harness} selected a non-OpenRouter provider; refusing to claim OpenRouter accounting")
    if kind == "opencode":
        try:
            config = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
            if not isinstance(config, dict):
                raise ValueError("not an object")
            providers = config.setdefault("provider", {})
            router = providers.setdefault("openrouter", {})
            options = router.setdefault("options", {})
            options.update({"baseURL": origin + "/api/v1", "apiKey": "{env:OPENROUTER_API_KEY}"})
            if metadata:
                models = router.setdefault("models", {})
                selected = models.setdefault(model, {})
                selected.update({"name": metadata["name"], "reasoning": metadata["reasoning"],
                    "limit": {"context": metadata["contextWindow"], "output": metadata["maxTokens"]}})
                # Native model-name heuristics omit variants for some providers.
                # Explicit OpenRouter variants preserve the saved experiment effort.
                selected["variants"] = {effort: {"reasoning": {"effort": effort}}
                                        for effort in ("low", "medium", "high")}
        except (ValueError, TypeError, AttributeError) as exc:
            raise SystemExit("OPENCODE_CONFIG_CONTENT must be a JSON object with valid provider options") from exc
        # OpenCode otherwise chooses an automatic helper model that may use a
        # different provider. Pin its default to the selected metered model.
        small = config.get("small_model", f"openrouter/{model}")
        if not isinstance(small, str) or not small.startswith("openrouter/"):
            raise SystemExit("OpenCode small_model must also use OpenRouter for complete accounting")
        config.update({"model": f"openrouter/{model}", "small_model": small,
                       "enabled_providers": ["openrouter"]})
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        for variable, directory in (("XDG_DATA_HOME", "opencode-data"), ("XDG_STATE_HOME", "opencode-state")):
            env[variable] = str(workspace / ".scorebench/openrouter" / directory)
        return command
    path = workspace / ".scorebench" / "openrouter" / "pi-route.mjs"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Register the exact selected model even when Pi's bundled catalog is old.
    # Only public model metadata goes into the extension; credentials stay in env.
    model_config = ("    api: 'openai-completions',\n    models: " + json.dumps([metadata]) + ",\n") if metadata else ""
    content = (
        "export default function (pi) {\n"
        "  pi.registerProvider('openrouter', {\n"
        "    baseUrl: process.env.OPENAI_BASE_URL,\n"
        "    apiKey: process.env.OPENROUTER_API_KEY,\n"
        + model_config +
        "  });\n"
        "}\n"
    )
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        handle.write(content)
    env["OPENAI_BASE_URL"] = origin + "/api/v1"
    env["PI_CODING_AGENT_DIR"] = str(workspace / ".scorebench/openrouter/pi-agent")
    return [command[0], "--extension", str(path), *command[1:]]
