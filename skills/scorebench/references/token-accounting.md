# Exact Token Accounting

Every submission requires an exact, run-relative `--total-tokens` value. Full
input, output, cache-write, and cache-read counters also let Scorebench derive a
more accurate API-equivalent cost curve. Never submit zero, an estimate, an
account-wide usage number, or a value copied from another session. If no exact
run-scoped source is available after one quick check, stop before submitting
and ask for a supervised runner that exposes usage.

Use the bundled helper:

```bash
SCOREBENCH_TOKEN_HELPER="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/token_usage.py"
SCOREBENCH_TOKEN_STATE="/work/.scorebench-token-usage.json"
```

Use a unique absolute state path per lane. The helper has no built-in state
path; set one absolute path once or pass it on every call. Never reuse another
run's state file.

## Contents

- [Establish the baseline](#establish-the-baseline)
- [Before every submission](#before-every-submission)
- [Final run usage](#final-run-usage)

## Establish The Baseline

Immediately after the Scorebench run is established, capture the first exact
source available:

Codex goal usage:

```bash
python3 "$SCOREBENCH_TOKEN_HELPER" start \
  --state "$SCOREBENCH_TOKEN_STATE" \
  --total-tokens <get_goal_total_tokens> \
  --source codex_goal
```

Codex JSONL from a supervised/noninteractive launcher:

```bash
python3 "$SCOREBENCH_TOKEN_HELPER" start \
  --state "$SCOREBENCH_TOKEN_STATE" \
  --codex-jsonl "$CODEX_EXEC_JSONL" \
  --source codex_exec_jsonl \
  --confidence parsed
```

The helper sums completed-turn usage while keeping input, output, cached input,
and reasoning-output counters separate. Cached input is excluded from the
working-token total but retained for API cost accounting. Do not launch a
nested `codex exec` from inside a solving agent.

For a Codex TUI without `get_goal`, an exact current-session value visibly
reported by `/status` is acceptable. Do not use `/usage`; it is account-wide.

For Claude Code, use the exact visible `/cost` total or the unambiguously
identified JSONL transcript for the current session:

```bash
export CLAUDE_CODE_JSONL=/home/.../.claude/projects/.../<session>.jsonl
python3 "$SCOREBENCH_TOKEN_HELPER" start \
  --state "$SCOREBENCH_TOKEN_STATE" \
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
same value for `start`, `status`, and `flags`.

There is no built-in default path. Either pass `--state` on every call or export
`SCOREBENCH_TOKEN_STATE` once for the run:

```bash
export SCOREBENCH_TOKEN_STATE=/work/.harness-token-usage.json
```

Omitting both fails immediately rather than writing a baseline somewhere the
next invocation will not look for it.

## Before Every Submission

Take another snapshot from the same source and generate flags:

```bash
TOKEN_FLAGS="$(python3 "$SCOREBENCH_TOKEN_HELPER" flags \
  --state "$SCOREBENCH_TOKEN_STATE" \
  --total-tokens <current_total_tokens> \
  --source codex_goal)"
```

Codex JSONL:

```bash
TOKEN_FLAGS="$(python3 "$SCOREBENCH_TOKEN_HELPER" flags \
  --state "$SCOREBENCH_TOKEN_STATE" \
  --codex-jsonl "$CODEX_EXEC_JSONL" \
  --source codex_exec_jsonl \
  --confidence parsed)"
```

Claude Code JSONL:

```bash
TOKEN_FLAGS="$(python3 "$SCOREBENCH_TOKEN_HELPER" flags \
  --state "$SCOREBENCH_TOKEN_STATE" \
  --claude-jsonl "$CLAUDE_CODE_JSONL" \
  --source claude_code_jsonl \
  --confidence parsed)"
```

The helper subtracts the run-start baseline and emits `--total-tokens`, the
available run-relative component flags, `--usage-source`,
`--usage-confidence`, and `--tokens-total-source`. Cache reads do not inflate
the working-token total, but the server prices them at the model's cache-hit
rate. Do not hand-write those flags unless the helper itself is unavailable.

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
  --state "$SCOREBENCH_TOKEN_STATE" \
  --total-tokens <current_total_tokens> \
  --source codex_goal)"
scorebench run usage $FINAL_TOKEN_FLAGS
```

The JSONL forms use the same arguments as the pre-submit examples. Reports
prefer this final run measurement over inference from the last candidate.

JSONL-backed helper invocations include exact provider breakdown fields
automatically. For another exact source, they may be included manually:

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

Scorebench hardcodes a dated public API price table on the server. The skill
never guesses a price or computes invoice spend locally. Dashboard values with
incomplete historical usage are marked as estimates; see
<https://scorebench.dev/ui/docs/cost-accounting/>.
