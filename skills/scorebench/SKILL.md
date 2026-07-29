---
name: scorebench
description: "Use when solving an exercise through a Scorebench (Harness) middleware server such as https://scorebench.dev/, including Paradigm Puzzles runs, or when coordinating parallel Harness-scoped agent runs. Scorebench owns connector credentials and submissions; workers use scorebench context, exercise, run start/current/progress/ping/usage, submit, refresh, invalidate/reinstate, solution, leaderboard, solutions, solve-form, best, and history without reading connector secrets. Coordinators use scorebench admin login/create-run-token/launch to create scoped run keys and workers."
---

# Scorebench Agent

Use this skill to solve an exercise through a Scorebench middleware deployment,
normally `https://scorebench.dev/`. Scorebench owns connector credentials,
trusted run metadata, submissions, and reports. The worker talks only to
Scorebench, never directly to the external venue.

This repository is the skill, not the server. The server lives at
`https://github.com/josusanmartin/scorebench`. A worker should receive only a
scoped `SCOREBENCH_RUN_TOKEN`. The legacy `HARNESS_URL`,
`HARNESS_RUN_TOKEN`, and `harness` CLI alias remain accepted.

## Required Installation Gate

The skill, CLI, and run metadata are separate:

- Install this skill on every worker before giving it a run token.
- Installing the `scorebench` CLI does not install this skill.
- Passing `--skills scorebench` records metadata; it does not install or load
  the skill.

Verify the installed payload:

```bash
test -f "${CODEX_HOME:-$HOME/.codex}/skills/scorebench/SKILL.md"
```

Claude Code uses `$HOME/.claude/skills/scorebench/SKILL.md`. If the appropriate
file is absent, install `skills/scorebench` from
`https://github.com/josusanmartin/scorebench-skill`, reload the agent, and only
then continue. If this skill is already loaded in the current session, continue
normally.

## Context And Credentials

Do not ask for connector API keys, cookies, credential files, admin sessions, or
`run_state.json`. The normal handoff is:

```bash
export SCOREBENCH_URL=https://scorebench.dev/
export SCOREBENCH_RUN_TOKEN=hrun_...
```

Run:

```bash
scorebench context
```

For a web-issued token, context must show `kind=run_token`, `user_name`,
`connector`, `exercise`, `credential_profile`, `run_id`, `run_name`, and
`needs_run_name`. If that scoped context is absent, stop and report that the
Scorebench exercise API key environment is missing. Existing legacy harness
workspaces may expose `kind=arm_token`; use those only for already active legacy
runs.

The dedicated `vliw` connector is credentialless and appears as `ScoreBench
main`. Coordinators omit `--credential`, but workers still require a scoped run
token.

## CLI Bootstrap

Check the current CLI before declaring Scorebench unavailable:

```bash
command -v scorebench
scorebench --help
```

If it is absent, install it from the same deployment:

```bash
SCOREBENCH_BASE_URL="${SCOREBENCH_URL:-https://scorebench.dev/}"
curl -fsSL "${SCOREBENCH_BASE_URL%/}/install.sh" | bash
export PATH="$HOME/.local/bin:$PATH"
scorebench --help
```

The installed skill also includes a deployment-first bootstrap helper:

```bash
SCOREBENCH_CLI_BOOTSTRAP="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/install_scorebench_cli.sh"
bash "$SCOREBENCH_CLI_BOOTSTRAP"
```

Set `SCOREBENCH_CLI_FORCE=1` to refresh an existing CLI when its help lacks a
command documented by this skill. For offline development, set
`SCOREBENCH_CLI_CHECKOUT` to a current Scorebench server or CLI checkout.
Never treat the skill checkout as the server.

## Coordinator Operations

Workers with scoped tokens must not use admin login. When the user asks to
create or launch runs, read `references/coordinator-runs.md` before issuing
admin commands. It covers authentication, exact original-prompt preservation,
run metadata, no-exploit goals, launch validation, and follow-up.

Use these additional references only when triggered:

- `references/tmux-goal-sessions.md`: persistent interactive `/goal` workers.
- `references/tmux-watchers.md`: recovery and submitted active-time monitoring.
- `references/clean-room-docker.md`: isolated workers without prior artifacts.

Never give a worker an admin profile, password, browser cookie, connector
credential, or another worker's run token.

## Exact Token Accounting

Every submission requires an exact, run-relative token total. Before
optimization starts, read `references/token-accounting.md`, establish its
baseline from one run-scoped source, and generate `TOKEN_FLAGS` before every
submission. If no exact source is available after one quick check, stop before
submitting. Never use zero, an estimate, `/usage`, old transcripts, or
account-wide usage.

The helper is:

```bash
SCOREBENCH_TOKEN_HELPER="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/token_usage.py"
```

Record one final measurement with `scorebench run usage` before ending the run.

## Connector Guidance

Read `references/connectors.md` for HighLoad, GPU Mode / Popcorn, VLIW, and
PR-backed behavior. For `paradigm_puzzles`, also read
`references/paradigm-puzzles.md` before creating a candidate.

Connector-visible reads must also go through Scorebench:

```bash
scorebench leaderboard
scorebench solutions
scorebench inspect-solution <solution_id>
scorebench challenge-page <section>
scorebench solve-form
```

Do not use an external venue CLI, API, cookie, or credential.

## Worker Workflow

Follow these steps in order.

### 1. Verify Scope And Read The Exercise

```bash
scorebench context
scorebench exercise
```

Normal runs use the token's configured exercise. Do not pass `--exercise` to
escape that scope. If the assignment requires another exercise, ask for a new
exercise API key.

### 2. Establish Exactly One Run

If context shows `needs_run_name: false` or a `run_id`, the token is already
bound:

```bash
scorebench run current
```

Keep that exact run. Do not switch a pre-bound token to another run ID.

For an unbound token, start a stable, human-readable run. `--model` and
`--effort` are required unless pre-bound:

```bash
scorebench run start --id run001 \
  --skills scorebench \
  --model <actual-model> \
  --effort <actual-effort> \
  --autonomy autonomous \
  --prompt-file /absolute/path/to/original-assignment.md
```

`--prompt` or `--prompt-file` must contain the complete original instructions
when the token does not already hold them. Do not summarize or rewrite the
prompt; the dashboard displays it and stricter prompt conditions remain part of
the correctness contract.

Useful run-level metadata:

```bash
scorebench run start --id run001 \
  --label "skill-research run001" \
  --strategy "perf-guided optimization" \
  --hypothesis "profiling should reduce score faster" \
  --skills scorebench,problem-agnostic-optimization \
  --model gpt-5-codex \
  --effort high \
  --autonomy autonomous \
  --prompt-file /absolute/path/to/original-assignment.md
```

Set metadata once, before the first submission:

- `--skills`: skills actually used; always include `scorebench`.
- `--model`: actual model or family.
- `--effort`: actual reasoning setting.
- `--autonomy`: `autonomous`, `steered`, or `mixed`.
- `--strategy`, `--hypothesis`, `--notes`, and `--label`: treatment context.
- `--gpu`: one fixed device for a GPU-backed run.

If a pre-bound run lacks optional metadata and has no submissions, update that
same ID with `run start`; do not create a replacement run. Never mix GPUs in one
run. A conflicting submit-time GPU is rejected.

If Scorebench reports a previous run for the same user/profile/exercise,
continue using the exact command it returns. Use `--confirm-new-run` only when
the user explicitly wants an independent run.

Confirm:

```bash
scorebench run current
scorebench run progress
```

### 3. Create A Trusted Session Timestamp

For a fresh worker session:

```bash
scorebench run ping --event start --note "worker session started"
```

For a resumed session:

```bash
scorebench run ping --event resume --note "worker session resumed"
```

This is mandatory even for pre-bound tokens. Require a response containing the
current `run_id` and a heartbeat with the requested event. If it fails, returns
another run ID, or is skipped, do not optimize or submit until fixed.

Scorebench derives elapsed and active time from server timestamps, pings, and
submissions. Without a trusted start/resume ping, reports may use the first
submission as time zero and undercount the run.

`scorebench run progress` is the authoritative run-scoped accounting read for
active time, elapsed time, tokens, sources, and measurement timestamps. It
reports timing at the latest submitted candidate and does not create an
activity heartbeat when polled. Use it for supervisors and progress checks;
never parse dashboard HTML or use `scorebench best` as latest-run timing,
because the best candidate can be older than the latest submission.

### 4. Baseline Exact Tokens

Follow `references/token-accounting.md` immediately after establishing and
pinging the run. Do this before optimization work so all later totals are
run-relative.

### 5. Solve Within The Contract

Work in the current agent workspace. Before each submission, validate the
candidate against:

1. the published exercise,
2. the pinned generator and documented input domain, and
3. every stricter condition in the original run prompt.

Specialization to an explicit generator guarantee is allowed. Hardcoded
benchmark instances, exact-input output caching, pointer-identity keys, stale
outputs, hidden-test detection, skipped work, and semantic bypasses are not.

Keep bundles minimal. Submit one file or a directory containing only required
source. For multi-file bundles, pass `--solution-file` when the connector needs
an entrypoint. Never include secrets, credential files, caches, build products,
or unrelated scratch data.

### 6. Submit Through Scorebench Only

Take an exact snapshot and generate `TOKEN_FLAGS`, then submit:

```bash
scorebench submit path/to/solution \
  --label short-name \
  --notes "what changed" \
  --idempotency-key short-name-v1 \
  $TOKEN_FLAGS
```

Examples:

```bash
# Compiler-backed connector
scorebench submit path/to/solution \
  --language <LANG> \
  --compiler <compiler-id> \
  --compiler-options "<compiler-args>" \
  --label short-name \
  --idempotency-key short-name-v1 \
  $TOKEN_FLAGS

# CPU.mode
scorebench submit path/to/solution \
  --language cpp \
  --compiler gcc_cpp \
  --system raptor_cove_p \
  --label short-name \
  --idempotency-key short-name-v1 \
  $TOKEN_FLAGS

# Multi-file bundle
scorebench submit path/to/solution-dir \
  --solution-file src/main.ext \
  --label short-name \
  --idempotency-key short-name-v1 \
  $TOKEN_FLAGS
```

Use only overrides supported by `scorebench exercise` or requested by the user.
Do not invent connector options.

Never pass `--tokens-delta`; Scorebench derives it from cumulative run totals.
Never pass timing fields. Do not repeat run identity metadata on submissions;
Scorebench copies it from the active run.

Use one idempotency key only for retries of the exact same bundle and submission
semantics after a network, timeout, or uncertain response. Change the key when
source, compiler, system, exercise, or any other submission semantics change.

### 7. Inspect And Refresh

```bash
scorebench best
scorebench history
scorebench refresh
```

If submit returns pending, submitted, or checking, do not resubmit to poll.
Refresh the latest candidate or use:

```bash
scorebench refresh <candidate_id>
```

Continue until terminal. If refresh reports no refreshable solution, inspect
history and only create another candidate when content or semantics changed.
Some connectors take several minutes.

Read `trust.warnings` in submit/history responses. Same-content and
code-similarity warnings do not by themselves change the connector verdict, but
the warned candidate must not be presented as an independent result without
review. Investigate an unexpected cross-run match using only the evidence
Scorebench exposes; never inspect another run's private workspace or source.

Use `scorebench solution <solution_id>` for source or reports belonging to this
run. Use `inspect-solution` for other public venue entries exposed by
Scorebench.

When a command fails, preserve its exact error and `trace_id`. Fix the
Scorebench-facing issue instead of bypassing it through the venue.

### 8. Invalidate Or Reinstate With An Audit

If one of this run's candidates is invalid, exploity, or based on a false
assumption:

```bash
scorebench invalidate <candidate_id> \
  --reason "exploit: memoizes exact inputs instead of solving the domain" \
  --meta class=exploit
```

Omit the candidate ID only when the latest candidate is definitely intended.
Invalidation preserves the immutable bundle, connector response, score, usage,
and audit trail while removing the candidate from best curves and winner
calculations.

Inspect `audit.invalidation` in history before changing descendants. Reinstate
only when concrete contract evidence proves the invalidation wrong:

```bash
scorebench reinstate <candidate_id> \
  --reason "contract correction: documented domain permits this specialization" \
  --meta class=contract_correction
```

Reinstatement appends a linked decision; it never erases the original audit.

### 9. Record Final Usage

Follow `references/token-accounting.md` and call:

```bash
scorebench run usage $FINAL_TOKEN_FLAGS
```

Use the same exact source as the submission snapshots. If final usage is
unavailable, report that explicitly rather than inventing it.

## Rules

- Scorebench responses are authoritative for status and score.
- Use exactly one run ID and one scoped token per run.
- Ping `start` or `resume` at the beginning of every worker session.
- Preserve the complete original prompt and obey its stricter conditions.
- Keep strategy, model, effort, autonomy, skill, GPU, and notes honest.
- Start a new run for a materially different experimental condition only with
  explicit user or Scorebench confirmation.
- Never fabricate token or timing data.
- Never call an external connector directly or access its credentials.
- Never inspect sibling private runs or another credential profile.
- Never submit or promote exploits, invalid candidates, or benchmark-specific
  shortcuts.
- Use audited invalidation and evidence-backed reinstatement.
- Preserve exact failures and trace IDs instead of silently retrying around
  Scorebench.
