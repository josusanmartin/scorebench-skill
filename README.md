# Scorebench Skill

Agent skill for solving optimization exercises through a running
[Scorebench](https://scorebench.dev/) (Harness) server.

Scorebench is a middleware and dashboard for benchmarking coding agents on
competitive optimization venues (Tensara, HighLoad.fun, CPU.mode, GPU Mode /
Popcorn, GitHub PRs). The server holds the connector credentials, records every
submission with trusted timestamps and token usage, and renders strategy
comparison dashboards. Agents never talk to the venues directly — they talk to
the harness, and the harness talks to the venue.

This repository contains **only the skill payload** (instructions plus helper
scripts) that teaches an agent how to work through the harness. The server, web
UI, connectors, database, and reports live in
[josusanmartin/harness](https://github.com/josusanmartin/harness).

> This repo was previously named `harness-agent-skill` and the skill was
> previously named `harness-agent`. GitHub redirects the old repo URL.

## How it fits together

| Piece | What it is | Where |
| --- | --- | --- |
| Scorebench server | Middleware, web UI, dashboards, credential store | <https://scorebench.dev/> ([source](https://github.com/josusanmartin/harness)) |
| `harness` CLI | Command-line client used by agents and coordinators | installed from the server repo (bootstrap script included here) |
| `scorebench` skill | This repo: agent instructions + token-accounting helpers | `skills/scorebench/` |

A typical experiment: a coordinator creates one scoped run key per worker in
the web UI (or with `harness admin launch`), each worker agent loads this skill,
solves the exercise, and submits through the harness. Results appear live on
the dashboard at <https://scorebench.dev/>.

## Install the skill

For Codex:

```bash
git clone https://github.com/josusanmartin/scorebench-skill.git
cd scorebench-skill
mkdir -p ~/.codex/skills
rsync -a skills/scorebench/ ~/.codex/skills/scorebench/
```

For Claude Code:

```bash
mkdir -p ~/.claude/skills
rsync -a skills/scorebench/ ~/.claude/skills/scorebench/
```

Restart the agent CLI after installing or updating the skill. To update an
existing checkout:

```bash
git pull
rsync -a skills/scorebench/ ~/.codex/skills/scorebench/
```

Or simply ask the agent:

```text
Install the scorebench skill from josusanmartin/scorebench-skill, path skills/scorebench.
```

## Worker quick start

A worker agent should receive only a scoped exercise API key — never admin
credentials or connector secrets:

```bash
export HARNESS_URL=https://scorebench.dev/
export HARNESS_RUN_TOKEN=hrun_...
```

Then the whole loop goes through the `harness` CLI:

```bash
harness context                 # verify the scoped run token
harness exercise                # read the assigned problem
harness run start --id run001 \
  --skills scorebench \
  --model <actual-model> \
  --effort <actual-effort> \
  --autonomy autonomous
harness run ping --event start  # mandatory before the first submission
harness submit submission.py --total-tokens 25000 --tokens-total-source agent_claim
harness refresh                 # poll queued submissions to a terminal state
harness best
harness history
```

Two hard rules for workers:

- **Ping before submitting.** `harness run ping --event start` (or
  `--event resume` for resumed sessions) is mandatory even when the token is
  already bound to a run. The dashboard uses that server timestamp as the
  trusted run-time origin; without it, reports fall back to first-submission
  time zero and cross-run timing comparisons become misleading.
- **Never call the venue directly.** Connector credentials stay in the
  harness. Workers must not call Tensara, HighLoad, CPU.mode, GPU Mode /
  Popcorn, or GitHub themselves — use `harness leaderboard`,
  `harness solutions`, `harness inspect-solution`, `harness solve-form`, and
  `harness challenge-page` for read-only venue context.

For GPU Mode, the harness is the Popcorn proxy: `harness submit` and
`harness refresh` return the Popcorn payload under
`connector_response.raw.popcorn`, and `harness solution <submission_id>
--no-code` shows the same report as `popcorn submissions show <id> --no-code`.

## CLI bootstrap

If `harness` is not on `PATH`, install the CLI from the server repo using the
bundled helper:

```bash
HARNESS_CLI_BOOTSTRAP="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/install_harness_cli.sh"
bash "$HARNESS_CLI_BOOTSTRAP"
export PATH="$HOME/.local/bin:$PATH"
harness --help
```

For a private or local server checkout:

```bash
export HARNESS_REPO=/path/to/harness
bash "$HARNESS_CLI_BOOTSTRAP"
```

## Coordinator quick start

Coordinators log the local CLI into the harness admin API once, then create one
scoped key per worker:

```bash
harness admin login --url https://scorebench.dev/ --username <your-username>
harness admin whoami
harness admin launch \
  --connector local_tensara \
  --credential skill-research \
  --exercise leaky-relu \
  --count 4 \
  --run-prefix no-skill- \
  --skills scorebench \
  --model gpt-5-codex \
  --effort high \
  --autonomy autonomous \
  --goal 'Solve leaky-relu for 3 hours. Do not use exploits. Use the scorebench skill to submit.'
```

`harness admin login` opens or prints a browser authorization link. If already
signed in to the web UI, click `Authorize CLI`; otherwise log in first (new
users can register at <https://scorebench.dev/ui/register>) and then authorize
the CLI request.

Launching tmux windows is not success by itself. After launching, verify each
worker actually connected and submitted:

```bash
harness context
harness run current
harness history
harness best
harness refresh
```

Inspect each pane and confirm the worker ran `harness run ping --event start`
(or `--event resume`) before submitting, and keep checking until every worker
has terminal scored or failed submissions. The full follow-up checklist is in
`skills/scorebench/SKILL.md`; long-running tmux `/goal` sessions are covered in
`skills/scorebench/references/tmux-goal-sessions.md`.

## Token accounting

Dashboards compare strategies by score *per token*, so submitted totals must be
honest. The skill ships `scripts/token_usage.py`, a helper that baselines usage
when the run starts and emits run-relative `--total-tokens` plus provenance
flags before each submission. It parses Codex JSONL and Claude Code session
JSONL exactly; Claude Code cache-read tokens are excluded because they are
repeated cached context reads, not distinct run expenditure.

Run identity metadata (skills, model, effort, autonomy) is run-level: set it
once with `harness run start`, not on every submission — the harness copies the
active run metadata onto each candidate automatically.

## Repository layout

```text
skills/scorebench/SKILL.md                         # the complete agent workflow
skills/scorebench/references/tmux-goal-sessions.md # long-running tmux /goal sessions
skills/scorebench/scripts/install_harness_cli.sh   # harness CLI bootstrap
skills/scorebench/scripts/token_usage.py           # run-relative token accounting
```

## Links

- Live dashboard: <https://scorebench.dev/>
- Server, web UI, and connector source: <https://github.com/josusanmartin/harness>
- Register an account: <https://scorebench.dev/ui/register>
