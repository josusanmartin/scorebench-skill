#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_STATE = ".harness-token-usage.json"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"token usage baseline missing: run this first after the harness run is established:\n"
            f"  {Path(__file__).name} start --total-tokens <current_exact_tokens> --source <source>"
        )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SystemExit(f"invalid token usage state: {path}")
    return loaded


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def usage_total(usage: dict[str, Any]) -> int | None:
    for key in ("total_tokens", "total", "tokens_total"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        return input_tokens + output_tokens
    return None


def codex_jsonl_total(path: Path) -> int:
    total = 0
    seen = 0
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
            if event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            value = usage_total(usage)
            if value is None:
                continue
            total += value
            seen += 1
    if seen == 0:
        raise SystemExit(f"no turn.completed usage events found in {path}")
    return total


def claude_usage_total(usage: dict[str, Any]) -> int | None:
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
    )
    values = [usage.get(field) for field in fields]
    if any(isinstance(value, int) for value in values):
        return sum(value for value in values if isinstance(value, int))
    return usage_total(usage)


def claude_jsonl_total(path: Path) -> int:
    total = 0
    seen = 0
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
            value = claude_usage_total(usage)
            if value is None:
                continue
            total += value
            seen += 1
    if seen == 0:
        raise SystemExit(f"no Claude Code message.usage records found in {path}")
    return total


def read_total(args: argparse.Namespace) -> int:
    if args.codex_jsonl and args.claude_jsonl:
        raise SystemExit("provide only one of --codex-jsonl or --claude-jsonl")
    if args.codex_jsonl:
        return codex_jsonl_total(Path(args.codex_jsonl))
    if args.claude_jsonl:
        return claude_jsonl_total(Path(args.claude_jsonl))
    if args.total_tokens is None:
        raise SystemExit("provide --total-tokens, --codex-jsonl, or --claude-jsonl")
    if args.total_tokens < 0:
        raise SystemExit("--total-tokens cannot be negative")
    return args.total_tokens


def tokens_source_from_args(args: argparse.Namespace) -> str:
    if args.source:
        return args.source
    if args.codex_jsonl:
        return "codex_exec_jsonl"
    if args.claude_jsonl:
        return "claude_code_jsonl"
    return "codex_goal"


def usage_source_for(tokens_source: str) -> str:
    if tokens_source.startswith("codex_"):
        return "codex_usage"
    if tokens_source.startswith("claude_"):
        return "claude_code"
    if tokens_source in {"provider_usage", "api_meter"}:
        return "api_meter"
    if tokens_source in {"runner_measured", "launcher"}:
        return "launcher"
    return tokens_source


def cmd_start(args: argparse.Namespace) -> int:
    total = read_total(args)
    tokens_source = tokens_source_from_args(args)
    state = {
        "baseline_total_tokens": total,
        "confidence": args.confidence,
        "tokens_total_source": tokens_source,
        "usage_source": usage_source_for(tokens_source),
    }
    write_state(Path(args.state), state)
    print(json.dumps({"ok": True, **state}, indent=2, sort_keys=True))
    return 0


def current_run_total(args: argparse.Namespace) -> tuple[dict[str, Any], int, int]:
    state = load_state(Path(args.state))
    baseline = state.get("baseline_total_tokens")
    if not isinstance(baseline, int):
        raise SystemExit(f"invalid baseline_total_tokens in {args.state}")
    absolute_total = read_total(args)
    run_total = absolute_total - baseline
    if run_total < 0:
        raise SystemExit(
            "current token total is lower than the stored baseline; do not submit. "
            "Start a new harness run or recreate the token baseline."
        )
    return state, absolute_total, run_total


def cmd_status(args: argparse.Namespace) -> int:
    state, absolute_total, run_total = current_run_total(args)
    print(
        json.dumps(
            {
                "absolute_total_tokens": absolute_total,
                "baseline_total_tokens": state["baseline_total_tokens"],
                "run_total_tokens": run_total,
                "tokens_total_source": tokens_source_from_args(args) or state.get("tokens_total_source"),
                "usage_source": usage_source_for(tokens_source_from_args(args))
                or state.get("usage_source"),
                "confidence": args.confidence or state.get("confidence"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_flags(args: argparse.Namespace) -> int:
    state, _absolute_total, run_total = current_run_total(args)
    tokens_source = tokens_source_from_args(args) or str(state.get("tokens_total_source") or "codex_goal")
    usage_source = usage_source_for(tokens_source) or str(state.get("usage_source") or "codex_usage")
    confidence = args.confidence or str(state.get("confidence") or "exact")
    print(
        " ".join(
            [
                f"--total-tokens {run_total}",
                f"--usage-source {usage_source}",
                f"--usage-confidence {confidence}",
                f"--tokens-total-source {tokens_source}",
            ]
        )
    )
    return 0


def add_total_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", default=DEFAULT_STATE, help=f"state file; default {DEFAULT_STATE}")
    parser.add_argument("--total-tokens", type=int, help="current exact cumulative token count from the runner")
    parser.add_argument("--codex-jsonl", help="Codex exec --json event log to parse")
    parser.add_argument("--claude-jsonl", help="current Claude Code session JSONL transcript to parse")
    parser.add_argument("--source", help="usage source, for example codex_goal, codex_exec_jsonl, claude_code_jsonl, provider_usage")
    parser.add_argument("--confidence", default="exact", choices=["exact", "parsed", "estimated"])


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
