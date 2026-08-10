#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


# No built-in default: any fixed value is either relative (and silently follows
# the working directory) or hardcodes one environment's layout. Workers set
# SCOREBENCH_TOKEN_STATE once; everyone else passes --state explicitly.
STATE_ENV_VAR = "SCOREBENCH_TOKEN_STATE"
DEFAULT_STATE = os.environ.get(STATE_ENV_VAR, "")
ACCOUNTING_VERSION = 2
COMPONENT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "reasoning_output_tokens",
)


@dataclass(frozen=True)
class UsageSnapshot:
    total_tokens: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    def components(self) -> dict[str, int]:
        return {
            field: value
            for field in COMPONENT_FIELDS
            if isinstance((value := getattr(self, field)), int)
        }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"token usage baseline missing at {path}\n"
            "Run this first after the harness run is established, with the same "
            "--state path used for every later invocation:\n"
            f"  {Path(__file__).name} start --state {path} "
            "--total-tokens <current_exact_tokens> --source <source>"
        )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SystemExit(f"invalid token usage state: {path}")
    return loaded


def resolve_state_path(raw: str) -> Path:
    """Return an absolute state path, refusing ambiguous relative ones.

    The state file carries the run-relative token baseline. A relative path
    follows the current working directory, so a worker that runs ``start`` from
    one directory and ``flags`` from another silently loses its baseline and
    then cannot submit at all, because the middleware rejects submissions
    without a token snapshot. Fail loudly instead.
    """
    if not raw:
        raise SystemExit(
            "no token state path: pass --state /absolute/path.json or set "
            f"{STATE_ENV_VAR}.\n"
            "The state file carries this run's token baseline and must be the "
            "same absolute path for every invocation of the run."
        )
    path = Path(raw)
    if not path.is_absolute():
        # Deliberately not suggesting the cwd-resolved form: the current
        # directory is the variable that causes this bug, so echoing it back
        # would recommend a path that is only correct from here.
        raise SystemExit(
            f"--state must be an absolute path, got: {raw}\n"
            "A relative state path follows the current working directory, which "
            "silently loses the token baseline between invocations.\n"
            "Pass one absolute path and reuse it for start, status, and flags, "
            f"or set {STATE_ENV_VAR} once for the run."
        )
    return path


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def usage_int(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def usage_total(usage: dict[str, Any]) -> int | None:
    for key in ("total_tokens", "total", "tokens_total"):
        value = usage_int(usage, key)
        if value is not None:
            return value
    input_tokens = usage_int(usage, "input_tokens", "prompt_tokens")
    output_tokens = usage_int(usage, "output_tokens", "completion_tokens")
    if input_tokens is not None and output_tokens is not None:
        return input_tokens + output_tokens
    return None


def usage_snapshot(usage: dict[str, Any], *, claude: bool = False) -> UsageSnapshot | None:
    total = claude_usage_total(usage) if claude else usage_total(usage)
    if total is None:
        return None
    reasoning = usage_int(usage, "reasoning_output_tokens")
    output_details = usage.get("output_tokens_details")
    if reasoning is None and isinstance(output_details, dict):
        reasoning = usage_int(output_details, "reasoning_tokens")
    return UsageSnapshot(
        total_tokens=total,
        input_tokens=usage_int(usage, "input_tokens", "prompt_tokens"),
        output_tokens=usage_int(usage, "output_tokens", "completion_tokens"),
        cache_creation_tokens=usage_int(
            usage, "cache_creation_input_tokens", "cache_creation_tokens"
        ),
        cache_read_tokens=usage_int(
            usage,
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_read_tokens",
        ),
        reasoning_output_tokens=reasoning,
    )


def codex_usage_snapshot(
    usage: dict[str, Any], *, cache_aware: bool
) -> UsageSnapshot | None:
    if not cache_aware:
        return usage_snapshot(usage)

    input_tokens = usage_int(usage, "input_tokens", "prompt_tokens")
    output_tokens = usage_int(usage, "output_tokens", "completion_tokens")
    if input_tokens is None or output_tokens is None:
        return usage_snapshot(usage)

    cache_read_tokens = usage_int(usage, "cached_input_tokens")
    input_details = usage.get("input_tokens_details")
    if cache_read_tokens is None and isinstance(input_details, dict):
        cache_read_tokens = usage_int(input_details, "cached_tokens")
    cache_creation_tokens = usage_int(
        usage,
        "cache_write_input_tokens",
        "cache_creation_input_tokens",
        "cache_creation_tokens",
    )

    # Codex reports cached and cache-write input as subsets of input_tokens.
    # Canonical ScoreBench components are disjoint so cached reads can be left
    # out of working tokens while every category is still priced exactly once.
    if cache_read_tokens is None:
        return UsageSnapshot(
            total_tokens=input_tokens + output_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            reasoning_output_tokens=usage_int(usage, "reasoning_output_tokens"),
        )
    cache_creation_tokens = cache_creation_tokens or 0
    categorized_input = cache_read_tokens + cache_creation_tokens
    if categorized_input > input_tokens:
        raise SystemExit(
            "invalid Codex usage: cached plus cache-write input exceeds input_tokens"
        )
    fresh_input_tokens = input_tokens - categorized_input
    return UsageSnapshot(
        total_tokens=fresh_input_tokens + cache_creation_tokens + output_tokens,
        input_tokens=fresh_input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        reasoning_output_tokens=usage_int(usage, "reasoning_output_tokens"),
    )


def aggregate_snapshots(snapshots: list[UsageSnapshot], path: Path, label: str) -> UsageSnapshot:
    if not snapshots:
        raise SystemExit(f"no {label} usage records found in {path}")
    components: dict[str, int | None] = {}
    for field in COMPONENT_FIELDS:
        values = [getattr(snapshot, field) for snapshot in snapshots]
        components[field] = sum(values) if all(isinstance(value, int) for value in values) else None
    return UsageSnapshot(
        total_tokens=sum(snapshot.total_tokens for snapshot in snapshots),
        **components,
    )


def codex_jsonl_snapshot(path: Path, *, accounting_version: int = ACCOUNTING_VERSION) -> UsageSnapshot:
    snapshots: list[tuple[str | None, UsageSnapshot]] = []
    current_thread_id: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id.strip():
                    current_thread_id = thread_id.strip()
                continue
            if event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            snapshot = codex_usage_snapshot(
                usage, cache_aware=accounting_version >= ACCOUNTING_VERSION
            )
            if snapshot is not None:
                snapshots.append((current_thread_id, snapshot))
    if accounting_version < ACCOUNTING_VERSION:
        return aggregate_snapshots(
            [snapshot for _thread_id, snapshot in snapshots], path, "turn.completed"
        )
    if not snapshots:
        raise SystemExit(f"no turn.completed usage records found in {path}")

    thread_ids = {thread_id for thread_id, _snapshot in snapshots if thread_id}
    if len(thread_ids) > 1:
        raise SystemExit(
            f"multiple Codex threads found in {path}; use one JSONL file per run"
        )
    if thread_ids and any(thread_id is None for thread_id, _snapshot in snapshots):
        raise SystemExit(f"unscoped Codex usage mixed with a thread in {path}")
    if not thread_ids and len(snapshots) > 1:
        raise SystemExit(
            f"multiple unscoped turn.completed records found in {path}; "
            "cannot determine whether usage is cumulative"
        )

    # codex exec emits the thread's cumulative usage at turn completion. A
    # resumed thread can therefore appear more than once in an appended log;
    # the latest snapshot replaces earlier cumulative snapshots.
    return snapshots[-1][1]


def codex_jsonl_total(path: Path) -> int:
    return codex_jsonl_snapshot(path).total_tokens


def claude_usage_total(usage: dict[str, Any]) -> int | None:
    input_tokens = usage_int(usage, "input_tokens")
    output_tokens = usage_int(usage, "output_tokens")
    cache_creation_tokens = usage_int(
        usage, "cache_creation_input_tokens", "cache_creation_tokens"
    )
    cache_read_tokens = usage_int(usage, "cache_read_input_tokens", "cache_read_tokens")
    distinct_values = (input_tokens, output_tokens, cache_creation_tokens)
    if any(value is not None for value in (*distinct_values, cache_read_tokens)):
        return sum(value for value in distinct_values if value is not None)
    return usage_total(usage)


def claude_jsonl_snapshot(
    path: Path, *, accounting_version: int = ACCOUNTING_VERSION
) -> UsageSnapshot:
    snapshots: list[UsageSnapshot] = []
    identified: dict[str, UsageSnapshot] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            message = event.get("message")
            usage = None
            if isinstance(message, dict):
                usage = message.get("usage")
            if not isinstance(usage, dict):
                usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            snapshot = usage_snapshot(usage, claude=True)
            if snapshot is None:
                continue
            message_id = message.get("id") if isinstance(message, dict) else None
            if not isinstance(message_id, str) or not message_id.strip():
                message_id = event.get("requestId")
            if (
                accounting_version >= ACCOUNTING_VERSION
                and isinstance(message_id, str)
                and message_id.strip()
            ):
                stable_id = message_id.strip()
                previous = identified.get(stable_id)
                if previous is not None and previous != snapshot:
                    raise SystemExit(
                        "conflicting Claude usage records for message/request "
                        f"{stable_id} in {path}"
                    )
                identified[stable_id] = snapshot
                continue
            snapshots.append(snapshot)
    snapshots.extend(identified.values())
    return aggregate_snapshots(snapshots, path, "Claude Code message.usage")


def claude_jsonl_total(path: Path) -> int:
    return claude_jsonl_snapshot(path).total_tokens


def grok_inference_snapshot(ctx: dict[str, Any]) -> UsageSnapshot:
    inclusive_input = usage_int(ctx, "prompt_tokens")
    output_tokens = usage_int(ctx, "completion_tokens")
    cache_read_tokens = usage_int(ctx, "cached_prompt_tokens")
    if None in (inclusive_input, output_tokens, cache_read_tokens):
        raise SystemExit(
            "invalid Grok inference usage: prompt_tokens, completion_tokens, "
            "and cached_prompt_tokens are all required"
        )
    assert inclusive_input is not None
    assert output_tokens is not None
    assert cache_read_tokens is not None
    if cache_read_tokens > inclusive_input:
        raise SystemExit(
            "invalid Grok inference usage: cached_prompt_tokens exceeds "
            "prompt_tokens"
        )
    reasoning_tokens = usage_int(ctx, "reasoning_tokens")
    if reasoning_tokens is not None and reasoning_tokens > output_tokens:
        raise SystemExit(
            "invalid Grok inference usage: reasoning_tokens exceeds "
            "completion_tokens"
        )
    fresh_input_tokens = inclusive_input - cache_read_tokens
    return UsageSnapshot(
        total_tokens=fresh_input_tokens + output_tokens,
        input_tokens=fresh_input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=cache_read_tokens,
        reasoning_output_tokens=reasoning_tokens,
    )


def grok_unified_log_path(updates_path: Path) -> Path:
    for parent in updates_path.parents:
        if parent.name == "sessions":
            return parent.parent / "logs" / "unified.jsonl"
    raise SystemExit(
        f"cannot locate Grok unified log from {updates_path}; pass --grok-log"
    )


def grok_jsonl_snapshot(
    path: Path, *, unified_log_path: Path | None = None
) -> UsageSnapshot:
    session_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            params = event.get("params")
            if not isinstance(params, dict):
                continue
            session_id = params.get("sessionId")
            if isinstance(session_id, str) and session_id.strip():
                session_ids.add(session_id.strip())
    if not session_ids:
        raise SystemExit(f"no Grok sessionId found in {path}")
    if len(session_ids) > 1:
        raise SystemExit(
            f"multiple Grok sessions found in {path}; use one updates.jsonl per run"
        )
    session_id = next(iter(session_ids))
    log_path = unified_log_path or grok_unified_log_path(path)
    identified: dict[tuple[Any, ...], UsageSnapshot] = {}
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("msg") != "shell.turn.inference_done":
                continue
            if event.get("sid") != session_id:
                continue
            ctx = event.get("ctx")
            if not isinstance(ctx, dict):
                raise SystemExit(
                    f"Grok inference usage is missing ctx in {log_path}"
                )
            snapshot = grok_inference_snapshot(ctx)
            timestamp = event.get("ts")
            if not isinstance(timestamp, str) or not timestamp.strip():
                raise SystemExit(
                    f"Grok inference usage is missing a timestamp in {log_path}"
                )
            stable_id = (timestamp.strip(), ctx.get("loop_index"))
            previous = identified.get(stable_id)
            if previous is not None and previous != snapshot:
                raise SystemExit(
                    f"conflicting Grok inference records in {log_path}"
                )
            identified[stable_id] = snapshot
    if not identified:
        return UsageSnapshot(
            total_tokens=0,
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            reasoning_output_tokens=0,
        )
    return aggregate_snapshots(list(identified.values()), log_path, "Grok inference")


def read_snapshot(
    args: argparse.Namespace, *, accounting_version: int = ACCOUNTING_VERSION
) -> UsageSnapshot:
    jsonl_sources = [args.codex_jsonl, args.claude_jsonl, args.grok_jsonl]
    if sum(bool(source) for source in jsonl_sources) > 1:
        raise SystemExit(
            "provide only one of --codex-jsonl, --claude-jsonl, or --grok-jsonl"
        )
    if args.grok_log and not args.grok_jsonl:
        raise SystemExit("--grok-log requires --grok-jsonl")
    if args.codex_jsonl:
        return codex_jsonl_snapshot(
            Path(args.codex_jsonl), accounting_version=accounting_version
        )
    if args.claude_jsonl:
        return claude_jsonl_snapshot(
            Path(args.claude_jsonl), accounting_version=accounting_version
        )
    if args.grok_jsonl:
        return grok_jsonl_snapshot(
            Path(args.grok_jsonl),
            unified_log_path=Path(args.grok_log) if args.grok_log else None,
        )
    if args.total_tokens is None:
        raise SystemExit(
            "provide --total-tokens, --codex-jsonl, --claude-jsonl, or --grok-jsonl"
        )
    if args.total_tokens < 0:
        raise SystemExit("--total-tokens cannot be negative")
    return UsageSnapshot(total_tokens=args.total_tokens)


def read_total(args: argparse.Namespace) -> int:
    return read_snapshot(args).total_tokens


def tokens_source_from_args(args: argparse.Namespace) -> str | None:
    if args.source:
        return args.source
    if args.codex_jsonl:
        return "codex_exec_jsonl"
    if args.claude_jsonl:
        return "claude_code_jsonl"
    if args.grok_jsonl:
        return "grok_session_jsonl"
    return None


def usage_source_for(tokens_source: str) -> str:
    if tokens_source.startswith("codex_"):
        return "codex_usage"
    if tokens_source.startswith("claude_"):
        return "claude_code"
    if tokens_source.startswith("grok_"):
        return "grok_build"
    if tokens_source in {"provider_usage", "api_meter"}:
        return "api_meter"
    if tokens_source in {"runner_measured", "launcher"}:
        return "launcher"
    return tokens_source


def cmd_start(args: argparse.Namespace) -> int:
    snapshot = read_snapshot(args, accounting_version=ACCOUNTING_VERSION)
    tokens_source = tokens_source_from_args(args) or "codex_goal"
    confidence = args.confidence or "exact"
    state = {
        "accounting_version": ACCOUNTING_VERSION,
        "baseline_total_tokens": snapshot.total_tokens,
        "baseline_usage": snapshot.components(),
        "confidence": confidence,
        "tokens_total_source": tokens_source,
        "usage_source": usage_source_for(tokens_source),
    }
    write_state(resolve_state_path(args.state), state)
    print(json.dumps({"ok": True, **state}, indent=2, sort_keys=True))
    return 0


def current_run_usage(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], UsageSnapshot, int, dict[str, int]]:
    state = load_state(resolve_state_path(args.state))
    baseline = state.get("baseline_total_tokens")
    if not isinstance(baseline, int):
        raise SystemExit(f"invalid baseline_total_tokens in {args.state}")
    accounting_version = state.get("accounting_version", 1)
    if not isinstance(accounting_version, int) or accounting_version < 1:
        raise SystemExit(f"invalid accounting_version in {args.state}")
    snapshot = read_snapshot(args, accounting_version=accounting_version)
    run_total = snapshot.total_tokens - baseline
    if run_total < 0:
        raise SystemExit(
            "current token total is lower than the stored baseline; do not submit. "
            "Start a new harness run or recreate the token baseline."
        )
    baseline_usage = state.get("baseline_usage")
    if not isinstance(baseline_usage, dict):
        baseline_usage = {}
    run_components: dict[str, int] = {}
    for field, current in snapshot.components().items():
        component_baseline = baseline_usage.get(field)
        if not isinstance(component_baseline, int):
            continue
        delta = current - component_baseline
        if delta < 0:
            raise SystemExit(
                f"current {field} is lower than the stored baseline; do not submit. "
                "Start a new harness run or recreate the token baseline."
            )
        run_components[field] = delta

    required = (
        ("input_tokens", "output_tokens", "cache_creation_tokens")
        if args.claude_jsonl
        else ("input_tokens", "output_tokens", "cache_read_tokens")
        if args.grok_jsonl
        else ("input_tokens", "output_tokens")
        if args.codex_jsonl
        else ()
    )
    if required and not all(field in run_components for field in required):
        run_components = {}
    if "input_tokens" in run_components and "output_tokens" in run_components:
        run_total = (
            run_components["input_tokens"]
            + run_components["output_tokens"]
            + run_components.get("cache_creation_tokens", 0)
        )
    return state, snapshot, run_total, run_components


def current_run_total(args: argparse.Namespace) -> tuple[dict[str, Any], int, int]:
    state, snapshot, run_total, _run_components = current_run_usage(args)
    return state, snapshot.total_tokens, run_total


def current_provenance(
    args: argparse.Namespace, state: dict[str, Any]
) -> tuple[str, str, str]:
    stored_source = str(state.get("tokens_total_source") or "codex_goal")
    requested_source = tokens_source_from_args(args)
    if requested_source and requested_source != stored_source:
        raise SystemExit(
            "token usage source changed after the run baseline; do not submit. "
            f"Expected {stored_source}, received {requested_source}."
        )

    stored_confidence = str(state.get("confidence") or "exact")
    if args.confidence and args.confidence != stored_confidence:
        raise SystemExit(
            "token usage confidence changed after the run baseline; do not submit. "
            f"Expected {stored_confidence}, received {args.confidence}."
        )

    source = requested_source or stored_source
    usage_source = usage_source_for(source) or str(
        state.get("usage_source") or "codex_usage"
    )
    return source, usage_source, args.confidence or stored_confidence


def cmd_status(args: argparse.Namespace) -> int:
    state, snapshot, run_total, run_components = current_run_usage(args)
    tokens_source, usage_source, confidence = current_provenance(args, state)
    print(
        json.dumps(
            {
                "absolute_total_tokens": snapshot.total_tokens,
                "accounting_version": state.get("accounting_version", 1),
                "baseline_total_tokens": state["baseline_total_tokens"],
                "source_run_total_tokens": snapshot.total_tokens
                - state["baseline_total_tokens"],
                "run_total_tokens": run_total,
                "run_usage": run_components,
                "tokens_total_source": tokens_source,
                "usage_source": usage_source,
                "confidence": confidence,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_flags(args: argparse.Namespace) -> int:
    state, _snapshot, run_total, run_components = current_run_usage(args)
    tokens_source, usage_source, confidence = current_provenance(args, state)
    component_flags = {
        "input_tokens": "--input-tokens",
        "output_tokens": "--output-tokens",
        "cache_creation_tokens": "--cache-creation-tokens",
        "cache_read_tokens": "--cache-read-tokens",
        "reasoning_output_tokens": "--reasoning-output-tokens",
    }
    flags = [f"--total-tokens {run_total}"]
    flags.extend(
        f"{component_flags[field]} {run_components[field]}"
        for field in COMPONENT_FIELDS
        if field in run_components
    )
    flags.extend(
        [
            f"--usage-source {usage_source}",
            f"--usage-confidence {confidence}",
            f"--tokens-total-source {tokens_source}",
        ]
    )
    print(
        " ".join(flags)
    )
    return 0


def add_total_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state",
        default=DEFAULT_STATE,
        help=f"absolute state file path; defaults to ${STATE_ENV_VAR} ({DEFAULT_STATE or 'unset'})",
    )
    parser.add_argument("--total-tokens", type=int, help="current exact cumulative token count from the runner")
    parser.add_argument("--codex-jsonl", help="Codex exec --json event log to parse")
    parser.add_argument("--claude-jsonl", help="current Claude Code session JSONL transcript to parse")
    parser.add_argument("--grok-jsonl", help="current Grok session updates.jsonl to parse")
    parser.add_argument("--grok-log", help="Grok unified.jsonl override; normally discovered from --grok-jsonl")
    parser.add_argument("--source", help="usage source, for example codex_goal, codex_exec_jsonl, claude_code_jsonl, grok_session_jsonl, provider_usage")
    parser.add_argument("--confidence", choices=["exact", "parsed", "estimated"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare trustworthy harness token flags")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="store the token baseline immediately after the harness run is established")
    add_total_args(start)
    start.set_defaults(func=cmd_start)

    status = sub.add_parser("status", help="print current run-relative token usage")
    add_total_args(status)
    status.set_defaults(func=cmd_status)

    flags = sub.add_parser("flags", help="emit harness submit token flags")
    add_total_args(flags)
    flags.set_defaults(func=cmd_flags)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
