# Harness Agent Skill

Codex skill for solving exercises through an already-running Harness server.
This repo contains only the skill payload; the server, web UI, connectors,
database, and dashboards live in:

<https://github.com/josusanmartin/harness>

## Install

```bash
git clone https://github.com/josusanmartin/harness-agent-skill.git
cd harness-agent-skill
mkdir -p ~/.codex/skills
rsync -a skills/harness-agent/ ~/.codex/skills/harness-agent/
```

Restart Codex after installing or updating the skill.

From an existing checkout:

```bash
git pull
rsync -a skills/harness-agent/ ~/.codex/skills/harness-agent/
```

Or ask Codex:

```text
Install the harness-agent skill from josusanmartin/harness-agent-skill, path skills/harness-agent.
```

## Worker Usage

A worker agent should receive only a scoped run token:

```bash
export HARNESS_URL=https://harness.194.233.95.225.sslip.io/
export HARNESS_RUN_TOKEN=hrun_...
```

Then use the Harness CLI:

```bash
harness context
harness exercise
harness run start --id run001
harness run ping --event start
harness submit submission.py --total-tokens 25000 --tokens-total-source agent_claim
harness refresh
harness best
harness history
```

Connector credentials stay in Harness. Workers must not call Tensara,
HighLoad, CPU.mode, GPU Mode/Popcorn, or GitHub directly.

For GPU Mode, Harness is the Popcorn proxy. Use:

```bash
harness solution <submission_id> --no-code
```

to inspect the same report as `popcorn submissions show <id> --no-code`.
Popcorn stdout/stderr/text/parsed payloads are returned under
`connector_response.raw.popcorn` from `harness submit` and `harness refresh`.

## CLI Bootstrap

If `harness` is not on `PATH`, install the CLI from the server repo:

```bash
HARNESS_CLI_BOOTSTRAP="${CODEX_HOME:-$HOME/.codex}/skills/harness-agent/scripts/install_harness_cli.sh"
bash "$HARNESS_CLI_BOOTSTRAP"
export PATH="$HOME/.local/bin:$PATH"
harness --help
```

For a private or local server checkout:

```bash
export HARNESS_REPO=/path/to/harness
bash "$HARNESS_CLI_BOOTSTRAP"
```

## Coordinator Usage

Coordinators can log the local CLI into the Harness admin API and create one
scoped key per worker:

```bash
harness admin login --url https://harness.194.233.95.225.sslip.io/ --username admin
harness admin whoami
harness admin launch \
  --connector local_tensara \
  --credential skill-research \
  --exercise leaky-relu \
  --count 4 \
  --run-prefix no-skill- \
  --goal 'Solve leaky-relu for 3 hours. Do not use exploits. Use the harness skill to submit.'
```

`harness admin login` opens or prints a browser authorization link. If already
signed in to the Harness UI, click `Authorize CLI`; otherwise log in first and
then authorize the CLI request.

After launching workers, verify actual submissions:

```bash
harness context
harness run current
harness history
harness best
harness refresh
```

A tmux window or launched agent is not success by itself. Keep checking until
each worker has terminal scored or failed submissions.

## Token Accounting

The skill includes helpers for Codex JSONL and Claude Code session JSONL.
Agents should baseline usage after the Harness run is established and submit
honest cumulative totals. Claude Code cache-read tokens are excluded from the
submitted total because they are repeated cached context reads, not distinct run
expenditure.

## More Detail

The complete agent workflow is in:

```text
skills/harness-agent/SKILL.md
skills/harness-agent/references/tmux-goal-sessions.md
```
