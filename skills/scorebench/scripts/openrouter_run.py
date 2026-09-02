#!/usr/bin/env python3
"""Run a coding harness through per-run OpenRouter usage accounting.

In auto mode this launcher is inert unless the harness is already configured
to use OpenRouter. When active it starts a loopback-only proxy, routes the child
through the correct OpenRouter protocol skin, and exports the usage-log and
token-state paths consumed by ``token_usage.py``.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from typing import Mapping, Sequence

from openrouter_proxy import DEFAULT_UPSTREAM, Handler, UsageLog


OPENROUTER_HOST = "openrouter.ai"
TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}
ENDPOINT_ENV = (
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "XAI_BASE_URL",
    "GROK_BASE_URL",
    "OPENROUTER_BASE_URL",
)


def _relevant_endpoint_env(harness: str) -> tuple[str, ...]:
    normalized = harness.lower()
    if "claude" in normalized:
        return ("ANTHROPIC_BASE_URL",)
    if "codex" in normalized:
        return ("OPENAI_BASE_URL", "OPENROUTER_BASE_URL")
    if "grok" in normalized:
        return ("XAI_BASE_URL", "GROK_BASE_URL", "OPENAI_BASE_URL")
    return ENDPOINT_ENV


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _codex_overrides(command: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    index = 1
    while index < len(command):
        value = command[index]
        if value in {"-c", "--config"} and index + 1 < len(command):
            assignment = command[index + 1]
            index += 2
        elif value.startswith("--config="):
            assignment = value.split("=", 1)[1]
            index += 1
        else:
            index += 1
            continue
        if "=" in assignment:
            key, raw = assignment.split("=", 1)
            result[key.strip()] = _unquote(raw)
    return result


def _replace_codex_override(command: Sequence[str], key: str, value: str) -> tuple[list[str], bool]:
    """Replace every command-line Codex override for ``key`` in place."""
    result = list(command)
    replacement = f'{key}="{value}"'
    replaced = False
    index = 1
    while index < len(result):
        argument = result[index]
        if argument in {"-c", "--config"} and index + 1 < len(result):
            assignment = result[index + 1]
            if assignment.split("=", 1)[0].strip() == key:
                result[index + 1] = replacement
                replaced = True
            index += 2
            continue
        if argument.startswith("--config="):
            assignment = argument.split("=", 1)[1]
            if assignment.split("=", 1)[0].strip() == key:
                result[index] = f"--config={replacement}"
                replaced = True
        index += 1
    return result, replaced


def _codex_config(env: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    config_path = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "", {}
    provider = ""
    bases: dict[str, str] = {}
    section = ""
    section_pattern = re.compile(r"^\[model_providers\.([A-Za-z0-9_-]+)\]$")
    assignment_pattern = re.compile(r"^([A-Za-z0-9_.-]+)\s*=\s*(.+?)\s*$")
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = section_pattern.match(line)
        if match:
            section = match.group(1)
            continue
        if line.startswith("["):
            section = ""
            continue
        match = assignment_pattern.match(line)
        if not match:
            continue
        key, value = match.groups()
        value = _unquote(value.split(" #", 1)[0])
        if not section and key == "model_provider":
            provider = value
        elif section and key == "base_url":
            bases[section] = value
    return provider, bases


def _codex_openrouter_provider(command: Sequence[str], env: Mapping[str, str]) -> str:
    overrides = _codex_overrides(command)
    configured_provider, bases = _codex_config(env)
    provider = overrides.get("model_provider", configured_provider)
    if not provider:
        return ""
    base = overrides.get(f"model_providers.{provider}.base_url", bases.get(provider, ""))
    if provider.lower() == "openrouter" or OPENROUTER_HOST in base.lower():
        return provider
    return ""


def detect_openrouter(
    harness: str,
    command: Sequence[str],
    env: Mapping[str, str],
    mode: str,
) -> tuple[bool, str, str]:
    normalized_mode = mode.strip().lower() or "auto"
    protocol = "anthropic" if "claude" in harness.lower() else "openai"
    if normalized_mode in FALSY:
        return False, protocol, "disabled explicitly"
    if normalized_mode not in TRUTHY | {"auto"}:
        raise SystemExit("--mode must be auto, on, or off")
    if normalized_mode in TRUTHY:
        return True, protocol, "enabled explicitly"

    for name in _relevant_endpoint_env(harness):
        if OPENROUTER_HOST in env.get(name, "").lower():
            detected_protocol = "anthropic" if name == "ANTHROPIC_BASE_URL" else protocol
            return True, detected_protocol, f"{name} routes to OpenRouter"
    if any(OPENROUTER_HOST in value.lower() for value in command):
        return True, protocol, "command routes to OpenRouter"
    if "codex" in harness.lower():
        provider = _codex_openrouter_provider(command, env)
        if provider:
            return True, "openai", f"Codex provider {provider} routes to OpenRouter"
    return False, protocol, "no OpenRouter route detected"


def _route_child(
    harness: str,
    command: list[str],
    env: dict[str, str],
    protocol: str,
    origin: str,
) -> list[str]:
    if protocol == "anthropic":
        env["ANTHROPIC_BASE_URL"] = origin
        env["ANTHROPIC_AUTH_TOKEN"] = env["OPENROUTER_API_KEY"]
        env["ANTHROPIC_API_KEY"] = ""
        return command

    openai_base = f"{origin}/api/v1"
    replaced = False
    for name in ENDPOINT_ENV:
        if name != "ANTHROPIC_BASE_URL" and OPENROUTER_HOST in env.get(name, "").lower():
            env[name] = openai_base
            replaced = True
    env["OPENAI_BASE_URL"] = openai_base

    if "codex" in harness.lower():
        provider = _codex_openrouter_provider(command, env)
        if provider:
            provider_base_key = f"model_providers.{provider}.base_url"
            routed_command, replaced_override = _replace_codex_override(
                command, provider_base_key, openai_base
            )
            if replaced_override:
                return routed_command
            return [
                command[0],
                "-c",
                f'{provider_base_key}="{openai_base}"',
                *command[1:],
            ]
        if not replaced:
            return [
                command[0],
                "-c", 'model_provider="openrouter"',
                "-c", 'model_providers.openrouter.name="openrouter"',
                "-c", f'model_providers.openrouter.base_url="{openai_base}"',
                "-c", 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
                *command[1:],
            ]
    return command


def _upstream_for(protocol: str, env: Mapping[str, str]) -> str:
    upstream = env.get("OPENROUTER_BASE", DEFAULT_UPSTREAM).rstrip("/")
    if protocol == "anthropic" and not upstream.endswith("/api"):
        upstream += "/api"
    return upstream


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch a coding harness with automatic OpenRouter accounting")
    parser.add_argument("--harness", required=True, help="coding harness name, for example Codex or Claude Code")
    parser.add_argument("--workspace", default=os.getcwd(), help="isolated worker workspace")
    parser.add_argument("--mode", default=os.environ.get("SCOREBENCH_OPENROUTER", "auto"), help="auto, on, or off")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="coding harness command after --")
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("provide the coding harness command after --")

    env = dict(os.environ)
    enabled, protocol, reason = detect_openrouter(args.harness, command, env, args.mode)
    if not enabled:
        print(f"ScoreBench OpenRouter accounting inactive: {reason}", file=sys.stderr)
        return subprocess.run(command, env=env, cwd=args.workspace, check=False).returncode

    api_key = env.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "OpenRouter routing was detected but OPENROUTER_API_KEY is not set; "
            "refusing to launch without authoritative cost accounting"
        )

    workspace = Path(args.workspace).expanduser().resolve()
    accounting_dir = workspace / ".scorebench" / "openrouter"
    accounting_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        accounting_dir.chmod(0o700)
    except OSError:
        pass
    log_path = Path(env.get("SCOREBENCH_OPENROUTER_LOG") or accounting_dir / "usage.jsonl").expanduser().resolve()
    state_path = Path(env.get("SCOREBENCH_TOKEN_STATE") or accounting_dir / "token-state.json").expanduser().resolve()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.upstream = _upstream_for(protocol, env)  # type: ignore[attr-defined]
    server.api_key = api_key  # type: ignore[attr-defined]
    server.usage_log = UsageLog(log_path)  # type: ignore[attr-defined]
    try:
        log_path.chmod(0o600)
    except OSError:
        pass
    host, port = server.server_address
    origin = f"http://{host}:{port}"
    env["SCOREBENCH_OPENROUTER_LOG"] = str(log_path)
    env["SCOREBENCH_TOKEN_STATE"] = str(state_path)
    env["SCOREBENCH_OPENROUTER_ACTIVE"] = "1"
    routed_command = _route_child(args.harness, command, env, protocol, origin)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(
        f"ScoreBench OpenRouter accounting active ({reason}); usage and authoritative cost -> {log_path}",
        file=sys.stderr,
    )
    try:
        return subprocess.run(routed_command, env=env, cwd=workspace, check=False).returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
