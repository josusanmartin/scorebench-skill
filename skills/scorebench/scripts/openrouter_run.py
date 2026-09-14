#!/usr/bin/env python3
"""Run a coding harness through per-run OpenRouter usage accounting.

In auto mode this launcher is inert unless the harness is already configured
to use OpenRouter. When active it starts a loopback-only proxy, routes the child
through the correct OpenRouter protocol skin, and exports the usage-log and
token-state paths consumed by ``token_usage.py``.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from typing import Mapping, Sequence

from openrouter_proxy import DEFAULT_UPSTREAM, Handler, UsageLog
from openrouter_agents import agent_kind, check_installation, model_metadata, route_agent, validate_route
from openrouter_accounting import Publisher
from openrouter_reentry import SessionOutput, admit_reentry, resume_command, write_json
from openrouter_gaps import ACK_FILE, prepare_ack


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
RUNTIME_CONTROL_POLL_SECONDS = 30.0
RUNTIME_CONTROL_REQUEST_TIMEOUT_SECONDS = 10.0


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
    if normalized_mode not in TRUTHY | FALSY | {"auto"}:
        raise SystemExit("--mode must be auto, on, or off")
    protocol = "anthropic" if "claude" in harness.lower() else "openai"
    if agent_kind(harness):
        provider, _model = validate_route(harness, command)
        if provider == "openrouter":
            if normalized_mode in FALSY:
                raise SystemExit("cannot disable accounting for an explicitly selected OpenRouter model")
            return True, "openai", f"{harness} selected OpenRouter"
        if normalized_mode in TRUTHY:
            raise SystemExit("selected provider is not OpenRouter")
        if normalized_mode == "auto":
            return False, "openai", f"{harness} selected a different provider"
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


def _runtime_control_reason(
    *, workspace: Path, env: Mapping[str, str]
) -> str | None:
    try:
        completed = subprocess.run(
            ["scorebench", "run", "progress"],
            cwd=workspace,
            env=dict(env),
            text=True,
            capture_output=True,
            check=False,
            timeout=RUNTIME_CONTROL_REQUEST_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        response = f"{completed.stdout}\n{completed.stderr}".lower()
        if "http 401" in response or "status 401" in response:
            return "credential_revoked"
        return None
    try:
        response = json.loads(completed.stdout)
    except (TypeError, ValueError):
        return None
    progress = response.get("progress") if isinstance(response, dict) else None
    budget = progress.get("budget") if isinstance(progress, dict) else None
    if isinstance(budget, dict) and budget.get("reached") is True:
        return "budget_reached"
    return None


def _write_runtime_control(workspace: Path, reason: str) -> None:
    target = workspace / ".scorebench" / "runtime-control.json"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps({"reason": reason}) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, target)


def _terminate_process_group(
    process: subprocess.Popen[bytes], *, grace_seconds: float = 10.0
) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_supervised(
    command: Sequence[str], *, workspace: Path, env: Mapping[str, str], publisher: Publisher | None = None,
    session_output: SessionOutput | None = None,
) -> int:
    process = subprocess.Popen(
        list(command),
        cwd=workspace,
        env=dict(env),
        start_new_session=True,
        stdout=subprocess.PIPE if session_output else None,
    )
    output_thread = None
    if session_output:
        def copy_output() -> None:
            try:
                session_output.copy_stdout(process.stdout)
            except Exception:
                session_output.error = True
                print("ScoreBench native output capture failed; automatic reentry disabled", file=sys.stderr)
        output_thread = threading.Thread(target=copy_output, daemon=True)
        output_thread.start()
    stop_event = threading.Event()
    try:
        poll_seconds = max(
            0.05,
            float(
                env.get(
                    "SCOREBENCH_RUNTIME_CONTROL_POLL_SECONDS",
                    RUNTIME_CONTROL_POLL_SECONDS,
                )
            ),
        )
    except (TypeError, ValueError):
        poll_seconds = RUNTIME_CONTROL_POLL_SECONDS

    def monitor() -> None:
        failures = 0
        while process.poll() is None and not stop_event.is_set():
            reason = None
            try:
                if publisher:
                    publisher.publish()
                failures = 0
            except Exception:
                failures += 1
                if failures == 1:
                    print("ScoreBench usage publication delayed; retrying without resetting accounting", file=sys.stderr)
                if failures >= 3:
                    reason = "accounting_unavailable"
            try:
                reason = _runtime_control_reason(workspace=workspace, env=env) or reason
            except Exception:
                # A malformed/transient response must not kill the watchdog.
                print("ScoreBench runtime check failed; retrying", file=sys.stderr)
            if reason:
                _write_runtime_control(workspace, reason)
                print(
                    f"ScoreBench stopped this worker: {reason.replace('_', ' ')}",
                    file=sys.stderr,
                )
                _terminate_process_group(process)
                return
            stop_event.wait(poll_seconds)

    monitor_thread = threading.Thread(
        target=monitor,
        name="scorebench-runtime-control",
        daemon=True,
    )
    monitor_thread.start()
    try:
        return process.wait()
    except KeyboardInterrupt:
        _terminate_process_group(process)
        raise
    finally:
        stop_event.set()
        monitor_thread.join(timeout=45)
        if output_thread:
            output_thread.join(timeout=10)
            if output_thread.is_alive():
                session_output.error = True


def _run_child(
    command: Sequence[str], *, workspace: Path, env: Mapping[str, str], publisher: Publisher | None = None
) -> int:
    runtime_control = env.get("SCOREBENCH_RUNTIME_CONTROL", "").strip().lower()
    if runtime_control in TRUTHY:
        return _run_supervised(command, workspace=workspace, env=env, publisher=publisher)
    return subprocess.run(
        list(command), env=dict(env), cwd=workspace, check=False
    ).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch a coding harness with automatic OpenRouter accounting")
    parser.add_argument("--harness", required=True, help="coding harness name, for example Codex or Claude Code")
    parser.add_argument("--workspace", default=os.getcwd(), help="isolated worker workspace")
    parser.add_argument("--mode", default=os.environ.get("SCOREBENCH_OPENROUTER", "auto"), help="auto, on, or off")
    parser.add_argument("--expected-model", default="", help="immutable model identifier from the assigned recipe")
    parser.add_argument("--expected-effort", default="", help="immutable reasoning effort from the assigned recipe")
    parser.add_argument("--check", action="store_true", help="validate launch prerequisites without starting a run or model")
    parser.add_argument("--recover-session", default="", help="explicitly resume a retained, length-limited OpenCode session")
    parser.add_argument("--accept-accounting-gap", action="store_true",
                        help="owner acknowledgement: resume a retained transport failure with incomplete cost/token totals")
    parser.add_argument("--no-auto-reentry", action="store_true", help="retain length-limited OpenCode exits for explicit recovery")
    parser.add_argument(
        "--runtime-control",
        action="store_true",
        help="stop the harness when its scoped credential is revoked or budget is reached",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="coding harness command after --")
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("provide the coding harness command after --")

    env = dict(os.environ)
    if args.runtime_control:
        env["SCOREBENCH_RUNTIME_CONTROL"] = "1"
    workspace = Path(args.workspace).expanduser().resolve()
    if args.recover_session and (agent_kind(args.harness) != "opencode" or not args.runtime_control):
        parser.error("--recover-session requires supervised OpenCode")
    if args.accept_accounting_gap and not args.recover_session:
        parser.error("--accept-accounting-gap requires --recover-session; never enables automatic gap recovery")
    if (agent_kind(args.harness) == "opencode" and not args.recover_session
            and (workspace / ".scorebench/openrouter/result.json").exists()):
        raise SystemExit("OpenCode worker already exited; use documented --recover-session checks, not a new launch")
    validate_route(args.harness, command, args.expected_model, args.expected_effort)
    enabled, protocol, reason = detect_openrouter(args.harness, command, env, args.mode)
    if not enabled:
        if args.runtime_control and agent_kind(args.harness):
            raise SystemExit("ScoreBench Pi/OpenCode workers currently require an explicit OpenRouter route; native-provider accounting is not supported")
        if args.check:
            return 0
        print(f"ScoreBench OpenRouter accounting inactive: {reason}", file=sys.stderr)
        return _run_child(command, env=env, workspace=workspace)

    api_key = env.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "OpenRouter routing was detected but OPENROUTER_API_KEY is not set; "
            "refusing to launch without authoritative cost accounting"
        )

    metadata = None
    if agent_kind(args.harness):
        for name, filename in (("SCOREBENCH_OPENROUTER_LOG", "usage.jsonl"),
                               ("SCOREBENCH_TOKEN_STATE", "token-state.json")):
            expected = workspace / ".scorebench/openrouter" / filename
            if env.get(name) and Path(env[name]).expanduser().resolve() != expected.resolve():
                raise SystemExit(f"{name} must be this worker's private workspace path; clear inherited accounting paths")
        check_installation(command, env)
        _provider, selected_model = validate_route(args.harness, command, args.expected_model)
        metadata = model_metadata(selected_model, _upstream_for(protocol, env),
                                  output_target=128000 if agent_kind(args.harness) == "opencode" else 0)
        if args.expected_effort and not metadata["reasoning"]:
            raise SystemExit("selected OpenRouter model does not expose reasoning support for this recipe")
    if args.check and not args.recover_session:
        # Validate inline OpenCode routing without writing a Pi extension or
        # touching the run's ledger. A stale helper rejects --check itself.
        if agent_kind(args.harness) == "opencode":
            route_agent(args.harness, command, env, "http://127.0.0.1:1", workspace, metadata)
        print("ScoreBench OpenRouter preflight passed", file=sys.stderr)
        return 0

    accounting_dir = workspace / ".scorebench" / "openrouter"
    accounting_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        accounting_dir.chmod(0o700)
    except OSError:
        pass
    log_path = Path(env.get("SCOREBENCH_OPENROUTER_LOG") or accounting_dir / "usage.jsonl").expanduser().resolve()
    state_path = Path(env.get("SCOREBENCH_TOKEN_STATE") or accounting_dir / "token-state.json").expanduser().resolve()

    # A second launcher must never interleave generations in the same ledger.
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(str(log_path) + ".lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit("OpenRouter ledger already belongs to a running worker")

    gap_ack = None
    if args.recover_session:
        # Recovery may neither initialize an absent baseline nor replace a live
        # writer. Validate everything under the same ledger lock used for launch.
        try:
            if not state_path.is_file() or not log_path.is_file():
                raise RuntimeError("retained ledger and original token baseline are required")
            result = json.loads((accounting_dir / "result.json").read_text())
            control_path = workspace / ".scorebench/runtime-control.json"
            control = json.loads(control_path.read_text()) if control_path.exists() else {}
            if args.accept_accounting_gap:
                retry_partial = result.get("accounting_quality") == "partial" and result.get("accounting_complete") is False
                if ((result.get("accounting_ok") is not False and not retry_partial) or result.get("completion_confirmed") is not False
                        or result.get("runtime_control") not in ({}, {"reason": "accounting_unavailable"})
                        or control != result["runtime_control"]):
                    raise RuntimeError("partial recovery requires a retained accounting-unavailable transport stop")
                start = json.loads((workspace / ".scorebench/supervisor-run-start.json").read_text())
                gap_ack = prepare_ack(log_path, state_path, run_id=start["run"]["run_id"],
                                      session_id=args.recover_session, result=result, control=control)
            elif result.get("accounting_ok") is not True or result.get("completion_confirmed") is not False:
                if result.get("runtime_control", {}).get("reason") == "accounting_unavailable":
                    raise RuntimeError("missing OpenRouter receipt blocks exact recovery; owner may explicitly use --accept-accounting-gap with --check to assess partial recovery")
                raise RuntimeError("recovery requires an accounted, incomplete retained worker")
            if result.get("runtime_control") and not args.accept_accounting_gap:
                raise RuntimeError("controlled stops cannot be recovered as length-limit exits")
            recovery_env = dict(env)
            recovery_env["SCOREBENCH_OPENROUTER_LOG"] = str(log_path)
            recovery_env["SCOREBENCH_TOKEN_STATE"] = str(state_path)
            route_agent(args.harness, command, recovery_env, "http://127.0.0.1:1", workspace, metadata)
            preview = Publisher(workspace, recovery_env)
            preview.gap_preview = gap_ack
            recovery_env["SCOREBENCH_ACCOUNTING_SUPERVISED"] = "1"
            preview_flags = preview.current_flags()  # Validate binding and all retained receipts without writes.
            assessment = admit_reentry(preview, metadata, command, args.recover_session, manual=True, check=True,
                                       accounting_gap=args.accept_accounting_gap)
            if gap_ack:
                # The last server snapshot can predate several complete receipts.
                known_cost = float(preview_flags[preview_flags.index("--cost-usd") + 1])
                budget = preview.read("progress")["progress"]["budget"]
                if budget.get("type") != "cost":
                    raise RuntimeError("partial transport recovery currently requires a fixed cost budget")
                remaining = min(assessment["remaining"], max(0.0, float(budget["target"]) - known_cost))
                if remaining < metadata["resumeCostUpperBound"]:
                    raise RuntimeError("insufficient confirmed remaining budget for partial recovery")
                assessment.update(confirmed_cost_usd=known_cost, unknown_cost_usd=None,
                                  acknowledged_gaps=len(gap_ack["gaps"]), remaining=remaining)
            if args.check:
                print(json.dumps(assessment))
                lock.close()
                return 0
        except Exception:
            lock.close()
            raise

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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(
        f"ScoreBench OpenRouter accounting active ({reason}); usage and authoritative cost -> {log_path}",
        file=sys.stderr,
    )
    publisher = None
    accounting_ok = False
    returncode = 1
    try:
        routed_command = (
            route_agent(args.harness, command, env, origin, workspace, metadata)
            if agent_kind(args.harness)
            else _route_child(args.harness, command, env, protocol, origin)
        )
        if agent_kind(args.harness) == "opencode":
            print(f"ScoreBench OpenCode limits: context {metadata['contextWindow']}, "
                  f"output including reasoning {metadata['maxTokens']}", file=sys.stderr)
        if agent_kind(args.harness):
            _provider, selected_model = validate_route(args.harness, command, args.expected_model)
            server.allowed_models = {selected_model}
            if agent_kind(args.harness) == "opencode":
                small_model = json.loads(env["OPENCODE_CONFIG_CONTENT"])["small_model"]
                server.allowed_models.add(small_model.removeprefix("openrouter/"))
        if env.get("SCOREBENCH_RUNTIME_CONTROL", "").lower() in TRUTHY:
            publisher = Publisher(workspace, env)
            if gap_ack:
                publisher.gap_preview = gap_ack
                # Do not persist an acknowledgement or clear the old stop until
                # the server accepts the partial provenance and exposes it back.
                publisher.publish()
                progress = publisher.read("progress")["progress"]
                if progress.get("accounting_quality") != "partial":
                    raise RuntimeError("server does not support partial OpenRouter accounting; recovery refused")
                write_json(accounting_dir / ACK_FILE, gap_ack)
                publisher.gap_preview = None
                control_path.unlink(missing_ok=True)
            publisher.initialize()
        if publisher and agent_kind(args.harness) == "opencode":
            session = args.recover_session
            if session:
                admit_reentry(publisher, metadata, command, session, manual=True, accounting_gap=args.accept_accounting_gap)
                routed_command = resume_command(routed_command, session, accounting_gap=args.accept_accounting_gap)
            while True:
                output = SessionOutput(accounting_dir, session)
                returncode = _run_supervised(routed_command, env=env, workspace=workspace,
                                             publisher=publisher, session_output=output)
                if (returncode != 0 or output.error or output.reason != "length"
                        or args.no_auto_reentry or (workspace / ".scorebench/runtime-control.json").exists()):
                    if output.error:
                        returncode = 1
                    break
                if not server.usage_log.wait_idle(30):
                    raise RuntimeError("unfinished OpenRouter request prevents reentry")
                publisher.publish()
                budget = publisher.read("progress")["progress"]["budget"]
                if budget.get("reached") is True or (budget.get("type") == "cost"
                    and budget.get("accounting_complete") is not False
                    and budget.get("available") is True and budget.get("used", 0) >= budget.get("target", float("inf")) * 0.95):
                    break
                admit_reentry(publisher, metadata, command, output.session)
                session = output.session
                print("ScoreBench resuming the same OpenCode session after its output limit", file=sys.stderr)
                # Rebuild from the original command, not a prior --session command.
                routed_command = resume_command(command, session)
        else:
            returncode = _run_child(routed_command, env=env, workspace=workspace, publisher=publisher)
    except KeyboardInterrupt:
        returncode = 130
    except Exception as exc:
        print(f"ScoreBench OpenRouter worker error ({type(exc).__name__}); retain its workspace", file=sys.stderr)
        if isinstance(exc, RuntimeError):
            print(str(exc), file=sys.stderr)
        returncode = 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if not server.usage_log.wait_idle(30):
            server.usage_log.error("worker exited with an unfinished upstream request")
        if publisher:
            try:
                publisher.publish()
                accounting_ok = True
            except Exception:
                print("ScoreBench final OpenRouter accounting failed; retain the workspace and usage ledger", file=sys.stderr)
                returncode = 1
    try:
        if publisher and agent_kind(args.harness):
            returncode = publisher.finalize(returncode, accounting_ok=accounting_ok)
    finally:
        lock.close()
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
