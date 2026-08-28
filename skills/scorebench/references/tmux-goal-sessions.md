# tmux `/goal` Sessions

Use this reference to launch persistent Codex or Claude Code goal sessions.
Passing a prompt as an agent CLI argument creates a one-turn request; it does
not activate persistent `/goal` mode.

## Contents

- [Core sequence](#core-sequence)
- [Create prompt-bound run keys](#create-prompt-bound-run-keys)
- [Launch host TUI workers](#launch-host-tui-workers)
- [Attach container workers](#attach-container-workers)
- [Stage and activate goals](#stage-and-activate-goals)
- [Goal size limit](#goal-size-limit)
- [Tell workers to submit a baseline early](#tell-workers-to-submit-a-baseline-early)
- [Verify every lane](#verify-every-lane)
- [Recover without changing identity](#recover-without-changing-identity)

## Core Sequence

For each worker:

1. Render its complete goal.
2. Create a token bound to that exact original prompt.
3. Start the interactive TUI with intended model/effort.
4. Send a short `/goal` command into the TUI.
5. Verify active goal, Scorebench scope/run/ping, exact token baseline, and a
   protective first candidate.

Expected active signs include:

```text
Goal active
Pursuing goal
```

or:

```text
Goal set
◎ /goal active
```

## Create Prompt-Bound Run Keys

Read [Coordinator runs](coordinator-runs.md) first. Render goals before
creating tokens. A batch manifest is secret material:

```bash
umask 077
MANIFEST="$PWD/.harness/agent-runs/condition-timestamp-manifest.json"
WORKROOT="$PWD/.harness/agent-runs/condition-timestamp"

scorebench admin launch \
  --connector <connector> \
  --credential <credential-profile> \
  --exercise <exercise> \
  --count 3 \
  --run-prefix <condition-timestamp-lane-prefix> \
  --skills scorebench \
  --model <actual-model> \
  --coding-harness <actual-coding-harness> \
  --effort <actual-effort> \
  --autonomy autonomous \
  --goal-file /absolute/path/to/rendered-goal.md \
  --workspace-root "$WORKROOT" \
  --dry-run \
  --json > "$MANIFEST"
```

Omit `--credential` for credentialless connectors such as `vliw`.
`--dry-run` still creates keys and prompt files; it only skips tmux. Do not
repeat it as a pure syntax check.

Keep manifest and generated prompts mode 0600. Never print them. A redacted
summary may include only non-secret job fields:

```bash
jq '{harness_url, connector, credential_name, exercise,
     jobs: [.jobs[] | {run_id, cwd, prompt_file, window}]}' "$MANIFEST"
```

## Launch Host TUI Workers

Verify installed skills and CLI first:

```bash
test -f "$HOME/.claude/skills/scorebench/SKILL.md"
test -f "${CODEX_HOME:-$HOME/.codex}/skills/scorebench/SKILL.md"
command -v scorebench tmux
```

Claude Code:

```bash
CLAUDE_BIN="$(command -v claude)"
SCOREBENCH_URL="$(jq -r '.harness_url' "$MANIFEST")"
MODEL="<actual-model>"
EFFORT="<actual-effort>"
SECRET_DIR="$PWD/.harness/agent-runs/condition-timestamp-secrets"
install -d -m 0700 "$SECRET_DIR"

jq -r '.jobs[] | @base64' "$MANIFEST" | while read -r job; do
  field() { printf '%s' "$job" | base64 -d | jq -r "$1"; }
  run_id="$(field '.run_id')"
  cwd="$(field '.cwd')"
  token="$(field '.token.token')"
  env_file="$SECRET_DIR/$run_id.env"
  (umask 077; printf 'SCOREBENCH_URL=%s\nSCOREBENCH_RUN_TOKEN=%s\n' \
    "$SCOREBENCH_URL" "$token" > "$env_file")

  tmux new-window -d -t "$SESSION" -n "$run_id" -c "$cwd" \
    "set -a; . '$env_file'; set +a; exec '$CLAUDE_BIN' --name '$run_id' --model '$MODEL' --effort '$EFFORT'"
done
```

Add `--dangerously-skip-permissions` only when explicitly authorized and the
workspace is isolated or trusted.

Codex:

```bash
CODEX_BIN="$(command -v codex)"
SCOREBENCH_URL="$(jq -r '.harness_url' "$MANIFEST")"
MODEL="<actual-model>"
EFFORT="<actual-effort>"
SECRET_DIR="$PWD/.harness/agent-runs/condition-timestamp-secrets"
install -d -m 0700 "$SECRET_DIR"

jq -r '.jobs[] | @base64' "$MANIFEST" | while read -r job; do
  field() { printf '%s' "$job" | base64 -d | jq -r "$1"; }
  run_id="$(field '.run_id')"
  cwd="$(field '.cwd')"
  token="$(field '.token.token')"
  env_file="$SECRET_DIR/$run_id.env"
  (umask 077; printf 'SCOREBENCH_URL=%s\nSCOREBENCH_RUN_TOKEN=%s\n' \
    "$SCOREBENCH_URL" "$token" > "$env_file")

  tmux new-window -d -t "$SESSION" -n "$run_id" -c "$cwd" \
    "set -a; . '$env_file'; set +a; exec '$CODEX_BIN' -m '$MODEL' -c 'model_reasoning_effort=\"'$EFFORT'\"'"
done
```

Scorebench metadata does not configure the provider. Pass and later verify the
same coding harness, model, and effort in both layers. `Claude Code`, `Codex`,
`Deep Code`, `Grok Build`, `Kimi Code`, and `ZCode` identify the
executable/interface, not the model family. A Claude model launched by Codex
records `Codex`. Keep lane
environment files mode 0600 until recovery is no longer needed, then remove
only their exact paths.

## Attach Container Workers

For isolated lanes:

1. Create every container with its lane-specific environment file, volumes,
   model, effort, and fixed provider session ID.
2. Start every container.
3. Verify each remains running and bootstrap-ready.
4. Only then create tmux windows that run `docker attach <exact-container>`.

A failed attachment can immediately close a newly created window or session.

List and exact-match windows before assuming one exists:

```bash
tmux list-windows -t "$SESSION" -F '#{window_name}'
```

Do not use `tmux display-message -t <target>` as an existence check; an unknown
target can resolve to another current pane.

## Stage And Activate Goals

For long goals, stage the complete prompt at `/work/TMUX_GOAL` or use the
manifest's generated `prompt_file`. This preserves the same original text and
avoids tmux quoting/input limits.

Send a short instruction through a named buffer:

```bash
GOAL_COMMAND='/goal Read /work/TMUX_GOAL completely and execute it as the persistent goal.'
BUFFER="goal-${WINDOW}"

tmux set-buffer -b "$BUFFER" -- "$GOAL_COMMAND"
tmux paste-buffer -b "$BUFFER" -t "${SESSION}:${WINDOW}"
tmux send-keys -t "${SESSION}:${WINDOW}" Enter
```

If the command is visible but unexecuted in a multiline editor, inspect first
and send one more Enter. Never paste a raw manifest, environment file, or token
into the TUI.

The goal must require:

- installed Scorebench skill and assigned scope only;
- start/resume ping and exact token baseline before optimization;
- a tested protective baseline before extended design;
- Scorebench-only submissions and reads;
- no secrets, siblings, prior artifacts, or exploits;
- final exact usage and terminal candidate verification.

## Goal Size Limit

Claude Code refuses a `/goal` whose condition exceeds **4,000 characters**:

```text
Goal condition is limited to 4000 characters (got 5114)
```

The whole goal is rejected, so every lane in a batch fails at once. Measure the
rendered text excluding the leading `/goal ` before sending, and move long
command sequences into a helper script seeded into the worker's workspace
instead of inlining them.

## Tell Workers To Submit A Baseline Early

Long-horizon optimization goals reproducibly trigger this failure: the worker
spends 60-80 minutes designing a complete solution inside one response, writes
no file, submits nothing, and then loses the whole turn to the model's
output-token cap:

```text
API Error: Claude's response exceeded the 64000 output token maximum
```

Raising the cap does not help, because the worker expands to fill it. Workers
recover completely once told to ship something first, so put it in the goal:

```text
Write the simplest correct solution that passes the official tests and submit it
as a protective baseline even if its score is poor. Then optimize in small
increments, submitting after each validated material improvement. Before each
new candidate, check scorebench run progress and obey its submission allowance.
Refresh pending candidates and never create a new candidate for unchanged
content. Keep every response small and bounded.
```

Coordinator detection signal: the worker's repository has no modified files and
the run has no candidates after ~75 minutes. Deliver the correction by pasting
it into the pane as a queued message rather than interrupting; a queued message
is consumed at the next turn boundary and preserves in-flight reasoning.

## Verify Every Lane

```bash
tmux capture-pane -t "${SESSION}:${WINDOW}" -p -S -160
tmux list-panes -t "${SESSION}:${WINDOW}" \
  -F '#{pane_id} #{pane_current_command} #{pane_current_path} #{pane_dead}'
```

For containers, inspect the exact process with a bounded `docker top` or
equivalent command.

Require evidence of:

- intended model, effort, autonomy/permission mode, and fixed session identity;
- active `/goal`;
- installed skill;
- `scorebench context`, exercise, and exact current run;
- successful start/resume ping;
- registered passive timing observer with no unresolved setup error;
- exact token baseline;
- tested protective baseline progressing to submission.

Do not declare success because windows exist. Query each lane's scoped
`scorebench run progress` and history until candidates are recorded.

## Recover Without Changing Identity

On provider or attachment failure:

1. Exact-match tmux window and container.
2. Preserve work/session volumes, session ID, run token, model, and effort.
3. Restart/resume only that lane.
4. Recreate its exact attachment if missing.
5. Send `scorebench run ping --event resume`.
6. Resume the same persistent goal.

Use [tmux watchers](tmux-watchers.md) for durable recovery and progress
monitoring.
