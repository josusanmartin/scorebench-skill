# Scorebench Skill

Agent skill for solving optimization exercises through a running
[Scorebench](https://scorebench.dev/) (Harness) server.

> [!IMPORTANT]
> Install this skill on every worker agent before giving it a ScoreBench run
> token. Installing the `scorebench` CLI does **not** install the skill, and
> passing `--skills scorebench` only records run metadata.

Scorebench is a middleware and dashboard for benchmarking coding agents on
competitive optimization venues (Tensara, HighLoad.fun, CPU.mode, GPU Mode /
Popcorn, Paradigm Puzzles, GitHub PRs). The server holds the connector credentials, records every
submission with trusted timestamps and token usage, and renders strategy
comparison dashboards. Agents never talk to the venues directly — they talk to
the harness, and the harness talks to the venue.

This repository contains **only the skill payload** (instructions plus helper
scripts) that teaches an agent how to work through the harness. The server, web
UI, connectors, database, and reports live in
[josusanmartin/scorebench](https://github.com/josusanmartin/scorebench).

> This repo was previously named `harness-agent-skill` and the skill was
> previously named `harness-agent`. GitHub redirects the old repo URL.

## How it fits together

| Piece | What it is | Where |
| --- | --- | --- |
| Scorebench server | Middleware, web UI, dashboards, credential store | <https://scorebench.dev/> ([source](https://github.com/josusanmartin/scorebench)) |
| `scorebench` CLI (legacy alias `harness`) | Command-line client used by agents and coordinators | `curl -fsSL https://scorebench.dev/install.sh \| bash` |
| `scorebench` skill | This repo: agent instructions + token-accounting helpers | `skills/scorebench/` |

A typical experiment: a coordinator creates one scoped run key per worker in
the web UI (or with `scorebench admin launch`), each worker agent loads this skill,
solves the exercise, and submits through the harness. Results appear live on
the dashboard at <https://scorebench.dev/>.

## Install the skill (required)

For Codex:

```bash
git clone https://github.com/josusanmartin/scorebench-skill.git "$HOME/scorebench-skill"
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/scorebench"
rsync -a --delete \
  "$HOME/scorebench-skill/skills/scorebench/" \
  "${CODEX_HOME:-$HOME/.codex}/skills/scorebench/"
test -f "${CODEX_HOME:-$HOME/.codex}/skills/scorebench/SKILL.md"
```

For Claude Code:

```bash
git clone https://github.com/josusanmartin/scorebench-skill.git "$HOME/scorebench-skill"
mkdir -p "$HOME/.claude/skills/scorebench"
rsync -a --delete \
  "$HOME/scorebench-skill/skills/scorebench/" \
  "$HOME/.claude/skills/scorebench/"
test -f "$HOME/.claude/skills/scorebench/SKILL.md"
```

For Grok:

```bash
git clone https://github.com/josusanmartin/scorebench-skill.git "$HOME/scorebench-skill"
mkdir -p "$HOME/.grok/skills/scorebench"
rsync -a --delete \
  "$HOME/scorebench-skill/skills/scorebench/" \
  "$HOME/.grok/skills/scorebench/"
test -f "$HOME/.grok/skills/scorebench/SKILL.md"
```

Restart the agent CLI after installing or updating the skill. To update an
existing checkout:

```bash
git -C "$HOME/scorebench-skill" pull --ff-only
rsync -a --delete \
  "$HOME/scorebench-skill/skills/scorebench/" \
  "${CODEX_HOME:-$HOME/.codex}/skills/scorebench/"
```

Or simply ask the agent:

```text
Install the scorebench skill from
https://github.com/josusanmartin/scorebench-skill using path
skills/scorebench. Confirm that SKILL.md is installed, then restart or reload
before using the ScoreBench run token. Installing the CLI is not enough.
```

## Worker quick start

A worker agent should receive only a scoped exercise API key — never admin
credentials or connector secrets:

```bash
export SCOREBENCH_URL=https://scorebench.dev/
export SCOREBENCH_RUN_TOKEN=hrun_...
export SCOREBENCH_TIMING_OBSERVER_TOKEN=hobs_... # supplied by pair/handoff/launcher
```

Then the whole loop goes through the `scorebench` CLI (legacy alias `harness`):

```bash
scorebench context                 # verify the scoped run token
scorebench exercise                # read the assigned problem
```

When context shows a pre-bound run, keep it:

```bash
scorebench run current
```

Only when context reports `needs_run_name: true`, start a run and preserve the
complete original assignment:

```bash
scorebench run start --id run001 \
  --skills scorebench \
  --model <actual-model> \
  --coding-harness <actual-coding-harness> \
  --effort <actual-effort> \
  --autonomy autonomous \
  --prompt-file /path/to/original-assignment.md
```

Then ping, establish exact token accounting, and submit:

```bash
scorebench run ping --event start  # mandatory before the first submission
scorebench run ping --event activity # temporary v1 clock; prefer host watcher
scorebench run progress            # top-level v1 authoritative; v2 is shadow
SCOREBENCH_TOKEN_HELPER="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/token_usage.py"
SCOREBENCH_TOKEN_STATE="/work/.scorebench-token-usage.json"
SCOREBENCH_SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/scorebench"
test -f "$SCOREBENCH_SKILL_DIR/scripts/run_trace.py" || \
  SCOREBENCH_SKILL_DIR="$HOME/.claude/skills/scorebench"
SCOREBENCH_TRACE_HELPER="$SCOREBENCH_SKILL_DIR/scripts/run_trace.py"
SCOREBENCH_OBSERVER="$SCOREBENCH_SKILL_DIR/scripts/scorebench_observer.py"
python3 "$SCOREBENCH_OBSERVER" register --provider auto --cwd "$PWD"
python3 "$SCOREBENCH_TRACE_HELPER" start # records one file offset; no background work
python3 "$SCOREBENCH_TOKEN_HELPER" start \
  --state "$SCOREBENCH_TOKEN_STATE" \
  --total-tokens <exact-session-total-at-run-start> \
  --source codex_goal
TOKEN_FLAGS="$(python3 "$SCOREBENCH_TOKEN_HELPER" flags \
  --state "$SCOREBENCH_TOKEN_STATE" \
  --total-tokens <exact-current-session-total> \
  --source codex_goal)"
scorebench submit submission.py $TOKEN_FLAGS
scorebench refresh                 # poll queued submissions to a terminal state
scorebench best
scorebench history
FINAL_TOKEN_FLAGS="$(python3 "$SCOREBENCH_TOKEN_HELPER" flags \
  --state "$SCOREBENCH_TOKEN_STATE" \
  --total-tokens <exact-final-session-total> \
  --source codex_goal)"
scorebench run usage $FINAL_TOKEN_FLAGS
scorebench run ping --event finish
python3 "$SCOREBENCH_OBSERVER" unregister --cwd "$PWD"
python3 "$SCOREBENCH_TRACE_HELPER" finish # sanitize, compress, and upload now
```

Seven hard rules for workers:

- **Ping before submitting.** `scorebench run ping --event start` (or
  `--event resume` for resumed sessions) is mandatory even when the token is
  already bound to a run. The dashboard uses that server timestamp as the
  trusted run-time origin; without it, reports fall back to first-submission
  time zero and cross-run timing comparisons become misleading. During active
  work, the v1 clock still needs `--event activity` at least every five minutes.
  Prefer the bundled host-side watcher so the model does not report on a timer.
  Do not ping while idle.
- **Register passive timing once.** The bundled observer reads only local
  structured event boundaries and uses no model prompts or tokens. Its nested
  `progress.timing_v2` bounds are shadow diagnostics; top-level
  `progress.active_seconds` remains authoritative until migration.
- **Use exact run-relative tokens.** Establish a baseline from the current
  session, then use the bundled helper to deduplicate and normalize provider
  counters before generating submission flags. Working tokens exclude cached
  reads; API-equivalent cost still includes them at the cached-input rate. Never
  use an estimate, account-wide usage, or an `agent_claim`.
- **Submit a protective baseline first.** Create, test, and submit the simplest
  correct candidate in the first work cycle, then optimize in bounded
  increments.
- **Keep real progress visible without replay spam.** Before each new candidate,
  read `scorebench run progress` and obey any submission allowance. Submit each
  materially different validated improvement promptly, refresh pending work,
  and reuse an idempotency key only to recover an uncertain response. When an
  older server exposes no limit metadata, keep routine attempts at least five
  minutes apart while still submitting a newly validated best immediately.
- **Never call the venue directly.** Connector credentials stay in the
  harness. Workers must not call Tensara, HighLoad, CPU.mode, GPU Mode /
  Popcorn, Paradigm Puzzles, or GitHub themselves — use `scorebench leaderboard`,
  `scorebench solutions`, `scorebench inspect-solution`, `scorebench solve-form`, and
  `scorebench challenge-page` for read-only venue context.
- **Upload traces only after the run.** The trace helper records one byte offset
  at startup, then performs all JSONL filtering, secret redaction, compression,
  and upload after final usage. It excludes private reasoning and never changes
  candidate status when trace upload fails.

For GPU Mode, the harness is the Popcorn proxy: `scorebench submit` and
`scorebench refresh` return the Popcorn payload under
`connector_response.raw.popcorn`. Public benchmark/test cases are normalized
under `connector_response.case_results` with a `case_summary`; benchmark timing
fields use nanoseconds. Secret case bodies are never returned. Use
`scorebench refresh <candidate_id>` to enrich an older candidate, and
`scorebench solution <submission_id> --no-code` for the human Popcorn report.

## CLI bootstrap

If `scorebench` is missing or lacks prompt-bound tokens or `run progress`,
install it straight from the deployment (no repository access needed):

```bash
curl -fsSL https://scorebench.dev/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
scorebench --help
```

The skill bundles a deployment-first helper for agents, with an explicit
fallback to a current local server/CLI checkout when the deployment is
unreachable:

```bash
SCOREBENCH_CLI_BOOTSTRAP="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/install_scorebench_cli.sh"
bash "$SCOREBENCH_CLI_BOOTSTRAP"
```

Set `SCOREBENCH_CLI_FORCE=1` to refresh an existing installation. For an
offline fallback, set `SCOREBENCH_CLI_CHECKOUT=/path/to/current/checkout`.

## Coordinator quick start

Coordinators log the local CLI into the scorebench admin API once, then create one
scoped key per worker:

```bash
scorebench admin login --url https://scorebench.dev/ --username <your-username>
scorebench admin whoami
scorebench admin launch \
  --connector local_tensara \
  --credential skill-research \
  --exercise leaky-relu \
  --count 4 \
  --run-prefix no-skill- \
  --skills scorebench \
  --model gpt-5-codex \
  --coding-harness Codex \
  --effort high \
  --autonomy autonomous \
  --goal 'Solve leaky-relu for 3 hours. Do not use exploits. Use the scorebench skill to submit.'
```

`scorebench admin login` opens or prints a browser authorization link. If already
signed in to the web UI, click `Authorize CLI`; otherwise log in first (new
users can register at <https://scorebench.dev/ui/register>) and then authorize
the CLI request.

Launching tmux windows is not success by itself. After launching, verify each
worker actually connected and submitted:

```bash
scorebench context
scorebench run current
scorebench run progress
scorebench history
scorebench best
scorebench refresh
```

Inspect each pane and confirm the worker ran `scorebench run ping --event start`
(or `--event resume`) before submitting, and keep checking until every worker
has terminal scored or failed submissions. The full follow-up checklist is in
`skills/scorebench/references/coordinator-runs.md`; long-running tmux `/goal`
sessions are covered in `skills/scorebench/references/tmux-goal-sessions.md`.

Long-running supervisors must use each container's scoped `scorebench run
progress` response. They must not parse dashboard HTML, use `scorebench best`
as latest timing, or delete completion markers. The durable watcher contract is
in `skills/scorebench/references/tmux-watchers.md`.

## Token accounting

Dashboards compare strategies by score per token and per API-equivalent cost,
so submitted usage must be honest. The skill ships `scripts/token_usage.py`, a
helper that baselines usage when the run starts and emits run-relative working
tokens, available input/output/cache categories, and provenance before each
submission. It parses Codex, Claude Code, and Grok session JSONL exactly. Cache
reads are excluded from working tokens but retained for the server's model-cost
calculation. Grok's native aggregate includes cached reads, so Grok workers
must use the helper's `--grok-jsonl` path rather than submit `totalTokens`
directly.

Run identity metadata (skills, model, coding harness, effort, autonomy) is
run-level: set it once with `scorebench run start`, not on every submission.
The harness copies the active run metadata onto each candidate automatically.
The model and coding harness are distinct: use `Claude Code` for Claude,
`Codex` for GPT/OpenAI, `Grok Build` for Grok, `Kimi Code` for Kimi, `ZCode`
for GLM, and the actual native coding harness for other models. If Claude was
run inside Codex, record `Codex`; the historical `claude-codex-*` runs are the
known crossed case.

## Repository layout

```text
skills/scorebench/SKILL.md                         # concise contract and workflow router
skills/scorebench/agents/openai.yaml               # Codex skill UI metadata
skills/scorebench/references/clean-room-docker.md  # isolated worker image recipe
skills/scorebench/references/cli-and-auth.md        # CLI compatibility and coordinator login
skills/scorebench/references/connectors.md         # connector-specific contracts
skills/scorebench/references/coordinator-runs.md   # admin and parallel-run operations
skills/scorebench/references/paradigm-puzzles.md   # Paradigm exercise file and status contracts
skills/scorebench/references/run-traces.md         # end-only sanitized trace capture
skills/scorebench/references/tmux-goal-sessions.md # long-running tmux /goal sessions
skills/scorebench/references/tmux-watchers.md      # recovery and active-time monitors
skills/scorebench/references/token-accounting.md   # exact run-relative usage
skills/scorebench/references/worker-workflow.md     # one scoped run from bootstrap to final usage
skills/scorebench/scripts/install_scorebench_cli.sh # CLI bootstrap (hosted installer + fallback)
skills/scorebench/scripts/scorebench_watch.py      # durable worker monitors
skills/scorebench/scripts/run_trace.py             # post-run trace sanitizer/uploader
skills/scorebench/scripts/token_usage.py           # run-relative token accounting
```

## Links

- Live dashboard: <https://scorebench.dev/>
- Server, web UI, and connector source: <https://github.com/josusanmartin/scorebench> (private)
- Register an account: <https://scorebench.dev/ui/register>
