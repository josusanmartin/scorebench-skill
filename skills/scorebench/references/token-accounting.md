# Exact Token Accounting

Every submission requires an exact, run-relative `--total-tokens` value. Never
submit zero, an estimate, an account-wide usage number, or a value copied from
another session. If no exact run-scoped source is available after one quick
check, stop before submitting and ask for a supervised runner that exposes
usage.

Use the bundled helper:

```bash
SCOREBENCH_TOKEN_HELPER="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/token_usage.py"
```

## Establish The Baseline

Immediately after the Scorebench run is established, capture the first exact
source available:

Codex goal usage:

```bash
python3 "$SCOREBENCH_TOKEN_HELPER" start \
  --total-tokens <get_goal_total_tokens> \
  --source codex_goal
```

Codex JSONL from a supervised/noninteractive launcher:

```bash
python3 "$SCOREBENCH_TOKEN_HELPER" start \
  --codex-jsonl "$CODEX_EXEC_JSONL" \
  --source codex_exec_jsonl \
  --confidence parsed
```

The helper sums `input_tokens + output_tokens` from completed turns. Do not
launch a nested `codex exec` from inside a solving agent.

For a Codex TUI without `get_goal`, an exact current-session value visibly
reported by `/status` is acceptable. Do not use `/usage`; it is account-wide.

For Claude Code, use the exact visible `/cost` total or the unambiguously
identified JSONL transcript for the current session:

```bash
export CLAUDE_CODE_JSONL=/home/.../.claude/projects/.../<session>.jsonl
python3 "$SCOREBENCH_TOKEN_HELPER" start \
  --claude-jsonl "$CLAUDE_CODE_JSONL" \
  --source claude_code_jsonl \
  --confidence parsed
```

Only inspect the current project's transcript directory, and choose a file only
when the active session is unambiguous. Otherwise ask for its path. The helper
sums distinct `input_tokens`, `output_tokens`, and
`cache_creation_input_tokens`. It deliberately excludes
`cache_read_input_tokens`, which can count the same cached context repeatedly.

For Gemini, use an exact current-session `/stats` total. For provider/API
runners, sum the provider's usage fields for only this run and use
`provider_usage` or `runner_measured` provenance.

Never broadly search `~/.codex`, `~/.claude`, browser profiles, shell snapshots,
or old transcripts to infer usage.

### Use An Absolute State Path

`--state` must be an absolute path. The default and any relative value are
resolved against the current working directory, so a worker that runs `start`
from one directory and `flags` from another silently loses its baseline and
then reports:

```text
token usage baseline missing: run this first after the harness run is established
```

Because the middleware rejects submissions without a token snapshot, that lane
cannot submit at all until the baseline is restored. Pass an absolute path
explicitly, for example `--state /work/.harness-token-usage.json`, and keep the
same value for `start` and `flags`.

## Before Every Submission

Take another snapshot from the same source and generate flags:

```bash
TOKEN_FLAGS="$(python3 "$SCOREBENCH_TOKEN_HELPER" flags \
  --total-tokens <current_total_tokens> \
  --source codex_goal)"
```

Codex JSONL:

```bash
TOKEN_FLAGS="$(python3 "$SCOREBENCH_TOKEN_HELPER" flags \
  --codex-jsonl "$CODEX_EXEC_JSONL" \
  --source codex_exec_jsonl \
  --confidence parsed)"
```

Claude Code JSONL:

```bash
TOKEN_FLAGS="$(python3 "$SCOREBENCH_TOKEN_HELPER" flags \
  --claude-jsonl "$CLAUDE_CODE_JSONL" \
  --source claude_code_jsonl \
  --confidence parsed)"
```

The helper subtracts the run-start baseline and emits `--total-tokens`,
`--usage-source`, `--usage-confidence`, and `--tokens-total-source`. Do not
hand-write those flags unless the helper itself is unavailable.

Use the resulting flags in the same submission call:

```bash
scorebench submit path/to/solution \
  --label short-name \
  --idempotency-key short-name-v1 \
  $TOKEN_FLAGS
```

Scorebench computes candidate deltas from cumulative run-relative totals. Do
not pass `--tokens-delta` manually.

## Final Run Usage

Before ending or handing control back, take one final snapshot from the same
source and record it:

```bash
FINAL_TOKEN_FLAGS="$(python3 "$SCOREBENCH_TOKEN_HELPER" flags \
  --total-tokens <current_total_tokens> \
  --source codex_goal)"
scorebench run usage $FINAL_TOKEN_FLAGS
```

The JSONL forms use the same arguments as the pre-submit examples. Reports
prefer this final run measurement over inference from the last candidate.

If exact provider breakdown fields are available, they may be included:

```bash
scorebench run usage \
  --total-tokens <final_total> \
  --input-tokens <exact_input> \
  --cached-input-tokens <exact_cached_input> \
  --output-tokens <exact_output> \
  --reasoning-output-tokens <exact_reasoning_output> \
  --usage-source codex_usage \
  --usage-confidence exact \
  --tokens-total-source final_goal_usage
```

Omit any unavailable breakdown. Never invent component counts.
