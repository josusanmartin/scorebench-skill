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
command -v harness
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
GOAL_TEXT="Use the harness-agent skill. Solve the assigned Harness exercise. Submit only through Harness. Do not read run_state.json, connector credentials, .env, or unrelated transcripts."
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
harness admin login --url https://harness.example.com --username admin
harness admin whoami
```

The login command opens or prints a browser authorization link. If already
signed in to the Harness UI, authorize the CLI from that page.

## Create Run Keys First

Create scoped run keys and worker prompt files before launching agent TUIs:

```bash
harness admin launch \
  --connector local_tensara \
  --credential skill-research \
  --exercise leaky-relu \
  --count 4 \
  --run-prefix no-skill- \
  --goal 'Without using the problem agnostic skill, solve leaky-relu for 3 hours and target <100us. Do not use exploits. Use the harness skill to submit.' \
  --workspace-root "$PWD/.harness/agent-runs/leaky-relu-no-skill" \
  --dry-run \
  --json
```

Use the generated manifest to get each worker's `cwd`, `HARNESS_RUN_TOKEN`, and
prompt file. The worker must receive only its own run token.

## Codex Goal Window

Start an interactive Codex TUI in the worker directory with the worker token in
the environment:

```bash
WINDOW="codex-run001"
PROJECT_DIR="/path/to/worker/run001"
HARNESS_URL="https://harness.example.com"
HARNESS_RUN_TOKEN="hrun_..."
EFFORT="xhigh"
AGENT_CMD="export HARNESS_URL=$HARNESS_URL; export HARNESS_RUN_TOKEN=$HARNESS_RUN_TOKEN; exec $CODEX_BIN -c 'model_reasoning_effort=\"$EFFORT\"'"

tmux new-window -n "$WINDOW" -c "$PROJECT_DIR" "$AGENT_CMD"
```

Then send the goal into the TUI:

```bash
GOAL_TEXT="Use the harness-agent skill. Solve the assigned Harness exercise for 3 hours. Submit only through Harness. Do not use exploits."
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
HARNESS_URL="https://harness.example.com"
HARNESS_RUN_TOKEN="hrun_..."
EFFORT="max"
AGENT_CMD="export HARNESS_URL=$HARNESS_URL; export HARNESS_RUN_TOKEN=$HARNESS_RUN_TOKEN; exec $CLAUDE_BIN --effort $EFFORT --name $WINDOW"

tmux new-window -n "$WINDOW" -c "$PROJECT_DIR" "$AGENT_CMD"
```

If the workspace is trusted and the user explicitly wants no permission prompts,
add the bypass flag to the Claude command:

```bash
AGENT_CMD="export HARNESS_URL=$HARNESS_URL; export HARNESS_RUN_TOKEN=$HARNESS_RUN_TOKEN; exec $CLAUDE_BIN --dangerously-skip-permissions --effort $EFFORT --name $WINDOW"
```

Then send the goal:

```bash
GOAL_TEXT="Use the harness-agent skill. Solve the assigned Harness exercise for 3 hours. Submit only through Harness. Do not use exploits."
tmux send-keys -t "$WINDOW" "/goal $GOAL_TEXT" Enter
tmux send-keys -t "$WINDOW" Enter
```

Use `low`, `medium`, `high`, `xhigh`, or `max` for Claude Code effort when
supported. Confirm supported values with `claude --help`.

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
