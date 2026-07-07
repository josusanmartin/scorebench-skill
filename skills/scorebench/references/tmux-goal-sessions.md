# tmux `/goal` Sessions

Use this reference when launching persistent `/goal` sessions in separate tmux
windows for Codex or Claude Code. It covers both generic project sessions and
Harness-scoped worker sessions with per-run tokens.

## Core Rule

Passing a prompt as a CLI argument starts a normal one-turn request. It does not
activate persistent `/goal` mode.

For persistent goal mode:

1. Open the interactive agent TUI inside tmux.
2. Send a `/goal ...` slash command into that TUI.
3. Verify the TUI confirms an active goal.

Expected Codex signs:

```text
Goal active Objective: ...
Pursuing goal
```

Expected Claude Code signs:

```text
Goal set: ...
◎ /goal active
```

## Prerequisites

```bash
command -v tmux
command -v codex
command -v claude
```

For Harness-scoped runs, also verify:

```bash
command -v scorebench
```

If you are not already inside tmux:

```bash
tmux new -s harness-goals
```

Generic project setup:

```bash
PROJECT_DIR="$(pwd)"
CODEX_BIN="$(command -v codex)"
CLAUDE_BIN="$(command -v claude)"
GOAL_TEXT="Read this repository, summarize the current state, and propose one safe next step. Do not modify files or read secrets."
```

For a Harness worker, use a Harness-specific goal:

```bash
GOAL_TEXT="Use the scorebench skill. Solve the assigned Harness exercise. Submit only through Harness. Do not read run_state.json, connector credentials, .env, or unrelated transcripts."
```

## Generic Goal Windows

Use this when the user wants a standalone Codex or Claude Code goal session
without Harness run tokens.

Codex medium:

```bash
WINDOW="codex-goal-medium"
EFFORT="medium"

tmux new-window -n "$WINDOW" -c "$PROJECT_DIR" \
  "$CODEX_BIN -c 'model_reasoning_effort=\"'$EFFORT'\"'"

tmux send-keys -t "$WINDOW" "/goal $GOAL_TEXT" Enter
tmux send-keys -t "$WINDOW" Enter
```

Codex with an explicit model:

```bash
WINDOW="codex-goal-xhigh"
EFFORT="xhigh"
MODEL="<your-codex-model>"

tmux new-window -n "$WINDOW" -c "$PROJECT_DIR" \
  "$CODEX_BIN -m $MODEL -c 'model_reasoning_effort=\"'$EFFORT'\"'"

tmux send-keys -t "$WINDOW" "/goal $GOAL_TEXT" Enter
tmux send-keys -t "$WINDOW" Enter
```

Claude Code medium:

```bash
WINDOW="claude-goal-medium"
EFFORT="medium"

tmux new-window -n "$WINDOW" -c "$PROJECT_DIR" \
  "$CLAUDE_BIN --effort $EFFORT --name $WINDOW"

tmux send-keys -t "$WINDOW" "/goal $GOAL_TEXT" Enter
tmux send-keys -t "$WINDOW" Enter
```

Claude Code max with permission prompts bypassed in a trusted workspace:

```bash
WINDOW="claude-goal-max"
EFFORT="max"

tmux new-window -n "$WINDOW" -c "$PROJECT_DIR" \
  "$CLAUDE_BIN --dangerously-skip-permissions --effort $EFFORT --name $WINDOW"

tmux send-keys -t "$WINDOW" "/goal $GOAL_TEXT" Enter
tmux send-keys -t "$WINDOW" Enter
```

Codex commonly supports `low`, `medium`, `high`, and `xhigh` through
`model_reasoning_effort`. Claude Code commonly supports `low`, `medium`,
`high`, `xhigh`, and `max` through `--effort`. Confirm current support with
Codex `/model` or `claude --help`.

## Harness Coordinator Setup

Log the coordinator CLI into Harness first:

```bash
scorebench admin login --url https://scorebench.dev/ --username <your-username>
scorebench admin whoami
```

The login command opens or prints a browser authorization link. If already
signed in to the Harness UI, authorize the CLI from that page.

## Create Run Keys First

Create scoped run keys and worker prompt files before launching agent TUIs:

```bash
MANIFEST="$PWD/.harness/agent-runs/leaky-relu-no-skill-manifest.json"
WORKROOT="$PWD/.harness/agent-runs/leaky-relu-no-skill"

scorebench admin launch \
  --connector local_tensara \
  --credential skill-research \
  --exercise leaky-relu \
  --count 4 \
  --run-prefix no-skill- \
  --skills scorebench \
  --model gpt-5-codex \
  --effort high \
  --autonomy autonomous \
  --goal 'Without using the problem agnostic skill, solve leaky-relu for 3 hours and target <100us. Do not use exploits. Use the harness skill to submit.' \
  --workspace-root "$WORKROOT" \
  --dry-run \
  --json > "$MANIFEST"
```

Use the generated manifest to get each worker's `cwd`, `HARNESS_RUN_TOKEN`, and
prompt file. The worker must receive only its own run token. Do not print raw
manifests in user-visible output; they contain scoped run tokens. For summaries,
redact the token:

```bash
jq '{harness_url, connector, credential_name, exercise,
     jobs: [.jobs[] | {run_id, cwd, prompt_file, window}]}' "$MANIFEST"
```

For local Tensara URLs, pass the exercise slug, not the URL path. For example,
`http://host/problems/matrix-multiplication` becomes
`--exercise matrix-multiplication`, not `--exercise /problems/matrix-multiplication`.

## Batch Launcher From Manifest

Use this pattern when launching several long-lived goal sessions. It starts
interactive TUIs with per-worker Harness tokens and keeps the raw token inside
the process environment.

Claude Code, with optional model and effort:

```bash
MANIFEST="$PWD/.harness/agent-runs/matmul-fable-manifest.json"
CLAUDE_BIN="$(command -v claude)"
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
HARNESS_URL="$(jq -r '.harness_url' "$MANIFEST")"
MODEL_ARG="--model fable"       # leave empty for the default model
EFFORT="max"                    # low, medium, high, xhigh, or max
BYPASS="--dangerously-skip-permissions"  # only in a trusted workspace

jq -r '.jobs[] | @base64' "$MANIFEST" | while read -r job; do
  field() { printf '%s' "$job" | base64 -d | jq -r "$1"; }
  run_id="$(field '.run_id')"
  cwd="$(field '.cwd')"
  token="$(field '.token')"

  tmux new-window -n "$run_id" -c "$cwd" \
    "HARNESS_URL='$HARNESS_URL' HARNESS_RUN_TOKEN='$token' exec '$CLAUDE_BIN' $BYPASS $MODEL_ARG --effort '$EFFORT' --name '$run_id' --add-dir '$SKILL_DIR'"
done
```

Codex, with optional model and effort:

```bash
MANIFEST="$PWD/.harness/agent-runs/matmul-codex-manifest.json"
CODEX_BIN="$(command -v codex)"
HARNESS_URL="$(jq -r '.harness_url' "$MANIFEST")"
MODEL_ARG="-m gpt-5.5"          # leave empty for the default model
EFFORT="xhigh"

jq -r '.jobs[] | @base64' "$MANIFEST" | while read -r job; do
  field() { printf '%s' "$job" | base64 -d | jq -r "$1"; }
  run_id="$(field '.run_id')"
  cwd="$(field '.cwd')"
  token="$(field '.token')"

  tmux new-window -n "$run_id" -c "$cwd" \
    "HARNESS_URL='$HARNESS_URL' HARNESS_RUN_TOKEN='$token' exec '$CODEX_BIN' $MODEL_ARG -c 'model_reasoning_effort=\"'$EFFORT'\"'"
done
```

Pass run identity metadata to `scorebench admin launch` once, matching the actual
worker command you start from the manifest. The worker should not repeat
skills/model/effort/autonomy on every submission.

Name the run prefix, tmux window, strategy, and notes after the experimental
condition. Examples:

- `claude-pao-med-mm` with `problem-agnostic-optimization-claude-medium`
- `claude-pao-mm` with `problem-agnostic-optimization-claude-max`
- `fable-pao-mm` with `problem-agnostic-optimization-claude-fable-max`
- `codex-pao-xhigh-mm` with `problem-agnostic-optimization-codex-xhigh`

After launch, capture each pane before sending the goal. Claude should show the
requested model and effort, for example `Fable 5 with max effort` or
`medium · /effort`.

## Send Goals To A Batch

For long goals, prefer tmux buffers over `send-keys -l`; this preserves
newlines and avoids shell history issues. Do not paste token-bearing manifest
contents into the TUI.

```bash
GOAL_TEXT='/goal Objective: Solve the assigned Harness exercise.

Use the scorebench skill. Submit only through Harness.
Use problem-agnostic-optimization when the experiment calls for it.
Progress chart: off when Harness is handling progress.
If uncertain, keep iterating with best judgment toward a better score.'

jq -r '.jobs[] | @base64' "$MANIFEST" | while read -r job; do
  field() { printf '%s' "$job" | base64 -d | jq -r "$1"; }
  run_id="$(field '.run_id')"
  tmux set-buffer -b "goal_$run_id" "$GOAL_TEXT"
  tmux paste-buffer -t "$run_id" -b "goal_$run_id"
  tmux send-keys -t "$run_id" Enter
done
```

If each worker needs run-specific text, construct `GOAL_TEXT` inside the loop
from non-secret fields such as `run_id`, `exercise`, and the strategy name. Keep
these constraints in every worker goal:

- submit only through Harness,
- read only the assigned exercise and current run,
- do not read `run_state.json`, connector credentials, `.env`, sibling tokens,
  or unrelated transcripts,
- send `scorebench run ping --event start` or `scorebench run ping --event resume`
  before optimization work and before the first submission,
- initialize exact token accounting before the first submission,
- run autonomously for the requested wall-clock budget.

## Codex Goal Window

Start an interactive Codex TUI in the worker directory with the worker token in
the environment:

```bash
WINDOW="codex-run001"
PROJECT_DIR="/path/to/worker/run001"
HARNESS_URL="https://scorebench.dev/"
HARNESS_RUN_TOKEN="hrun_..."
EFFORT="xhigh"
AGENT_CMD="export SCOREBENCH_URL=$HARNESS_URL; export SCOREBENCH_RUN_TOKEN=$HARNESS_RUN_TOKEN; exec $CODEX_BIN -c 'model_reasoning_effort=\"$EFFORT\"'"

tmux new-window -n "$WINDOW" -c "$PROJECT_DIR" "$AGENT_CMD"
```

Then send the goal into the TUI:

```bash
GOAL_TEXT="Use the scorebench skill. Solve the assigned Harness exercise for 3 hours. Submit only through Harness. Do not use exploits."
tmux send-keys -t "$WINDOW" "/goal $GOAL_TEXT" Enter
tmux send-keys -t "$WINDOW" Enter
```

Use `low`, `medium`, `high`, or `xhigh` for Codex effort when supported. Verify
the active model and effort in the TUI with `/model` if uncertain.

## Claude Code Goal Window

Start an interactive Claude Code TUI in the worker directory with the worker
token in the environment:

```bash
WINDOW="claude-run001"
PROJECT_DIR="/path/to/worker/run001"
HARNESS_URL="https://scorebench.dev/"
HARNESS_RUN_TOKEN="hrun_..."
EFFORT="max"
MODEL_ARG=""  # for example: --model fable
AGENT_CMD="export SCOREBENCH_URL=$HARNESS_URL; export SCOREBENCH_RUN_TOKEN=$HARNESS_RUN_TOKEN; exec $CLAUDE_BIN $MODEL_ARG --effort $EFFORT --name $WINDOW"

tmux new-window -n "$WINDOW" -c "$PROJECT_DIR" "$AGENT_CMD"
```

If the workspace is trusted and the user explicitly wants no permission prompts,
add the bypass flag to the Claude command:

```bash
AGENT_CMD="export SCOREBENCH_URL=$HARNESS_URL; export SCOREBENCH_RUN_TOKEN=$HARNESS_RUN_TOKEN; exec $CLAUDE_BIN --dangerously-skip-permissions $MODEL_ARG --effort $EFFORT --name $WINDOW"
```

Then send the goal:

```bash
GOAL_TEXT="Use the scorebench skill. Solve the assigned Harness exercise for 3 hours. Submit only through Harness. Do not use exploits."
tmux send-keys -t "$WINDOW" "/goal $GOAL_TEXT" Enter
tmux send-keys -t "$WINDOW" Enter
```

Use `low`, `medium`, `high`, `xhigh`, or `max` for Claude Code effort when
supported. Use `--model fable` for Claude Fable when requested. Confirm current
model aliases and effort values with `claude --help`.

## Why Send Enter Twice

Long `/goal ...` text can land in the TUI multiline editor without executing.
If the full command is visible but the UI does not show an active goal, send one
more Enter:

```bash
tmux send-keys -t "$WINDOW" Enter
```

## Verification

Inspect recent pane output:

```bash
tmux capture-pane -t "$WINDOW" -p -S -120
```

List windows:

```bash
tmux list-windows
```

Inspect the active command and cwd:

```bash
tmux list-panes -t "$WINDOW" -F '#{pane_id} #{pane_current_command} #{pane_current_path} #{pane_active}'
```

Every worker should show:

- the intended agent binary and effort level,
- active `/goal` confirmation,
- `HARNESS_URL` and its own `HARNESS_RUN_TOKEN` in the process environment,
- no access to sibling run tokens or connector credentials.

## Post-launch Harness Follow-up

Do not stop after tmux windows are created. The coordinator is responsible for
making sure each worker is actually submitting through Harness and not silently
erroring.

Keep the manifest from `scorebench admin launch --json` or from the generated
`manifest.json`. For each job, use only that job's scoped token when checking
its run:

```bash
HARNESS_URL="<manifest harness_url>"
HARNESS_RUN_TOKEN="<job token>"

env HARNESS_URL="$HARNESS_URL" HARNESS_RUN_TOKEN="$HARNESS_RUN_TOKEN" scorebench context
env HARNESS_URL="$HARNESS_URL" HARNESS_RUN_TOKEN="$HARNESS_RUN_TOKEN" scorebench run current
env HARNESS_URL="$HARNESS_URL" HARNESS_RUN_TOKEN="$HARNESS_RUN_TOKEN" scorebench history
env HARNESS_URL="$HARNESS_URL" HARNESS_RUN_TOKEN="$HARNESS_RUN_TOKEN" scorebench best
```

Expected healthy signs:

- `scorebench context` shows the intended connector, exercise, credential profile,
  and run id.
- the worker pane shows a successful `scorebench run ping --event start` or
  `scorebench run ping --event resume` before the first `scorebench submit`.
- `scorebench run current` shows the active run.
- `scorebench history` eventually shows candidate rows for that run.
- `scorebench best` updates after scored submissions.

If a candidate is pending/submitted/checking, refresh it with the same scoped
token instead of resubmitting:

```bash
env HARNESS_URL="$HARNESS_URL" HARNESS_RUN_TOKEN="$HARNESS_RUN_TOKEN" scorebench refresh
```

Repeat refresh until the candidate reaches a terminal scored or failed state.
Some connectors take 5-10 minutes; this is a reason to keep following up, not a
reason to drop the run.

Investigate immediately when:

- a worker has no `history` rows after the expected bootstrap period,
- `history` shows failed, errored, or rejected submissions,
- `refresh` keeps returning missing connector ids, stale pending state, or
  harness errors,
- the pane output shows token-accounting, CLI bootstrap, login, scope, or
  connector errors.

For each issue, capture both sides:

```bash
tmux capture-pane -t "$WINDOW" -p -S -240
env HARNESS_URL="$HARNESS_URL" HARNESS_RUN_TOKEN="$HARNESS_RUN_TOKEN" scorebench history
env HARNESS_URL="$HARNESS_URL" HARNESS_RUN_TOKEN="$HARNESS_RUN_TOKEN" scorebench refresh
```

Preserve exact error text and any `trace_id`. A coordinator report should say
which runs submitted, which scored, which are pending, and which are blocked or
erroring. Never report a parallel launch as successful based only on tmux window
creation.

## Safety

Put safety constraints directly in the goal:

```text
Do not read secrets or .env files.
Do not submit outside Harness.
Do not deploy anything.
Do not use exploits.
Only make changes needed for the assigned exercise.
```

If the agent should make changes, say what changes are allowed. If a change or
submission requires confirmation, put that in the goal text too.
