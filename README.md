# Harness Agent Skill

Codex skill for solving exercises through the local Harness middleware.

This is the **agent skill repository only**. It does not contain the Harness
server, web UI, connector implementations, database, or deployment scripts. Use
the server repository for that:

<https://github.com/josusanmartin/harness>

Keep the split clear:

- `josusanmartin/harness`: Harness server, admin UI, CLI, connectors,
  credential storage, submissions, logging, and dashboards.
- `josusanmartin/harness-agent-skill`: Codex skill payload that teaches an
  agent how to use an already-running Harness server through scoped run tokens.

The skill assumes the Harness web UI creates an exercise API key scoped to one
user, one credential profile, one connector, and one exercise. Agents receive only:

```bash
export HARNESS_URL=http://127.0.0.1:8718
export HARNESS_RUN_TOKEN=hrun_...
```

Agents then use the `harness` CLI provided by the server repository:
`harness context`, `harness exercise`, `harness run start`, `harness run ping`,
and `harness submit`. Connector credentials stay in the Harness middleware.

The skill includes token-accounting helpers for Codex JSONL and Claude Code
session JSONL. For Claude Code, agents should parse only the current session
transcript and baseline it after the harness run is established; they should
not scrape unrelated `~/.claude` history.

If the solving environment does not already have `harness` on `PATH`, the skill
includes a bootstrap helper that installs the CLI from the server repository:

```bash
HARNESS_CLI_BOOTSTRAP="${CODEX_HOME:-$HOME/.codex}/skills/harness-agent/scripts/install_harness_cli.sh"
bash "$HARNESS_CLI_BOOTSTRAP"
export PATH="$HOME/.local/bin:$PATH"
harness --help
```

For private server checkouts or machines without GitHub access, provide the
server repo explicitly:

```bash
export HARNESS_REPO=/path/to/harness-server-checkout
bash "$HARNESS_CLI_BOOTSTRAP"
```

## Install

Clone this skill repo and install only the skill folder:

```bash
git clone https://github.com/josusanmartin/harness-agent-skill.git
cd harness-agent-skill
mkdir -p ~/.codex/skills
rsync -a skills/harness-agent/ ~/.codex/skills/harness-agent/
```

Or, from an existing checkout:

```bash
mkdir -p ~/.codex/skills
rsync -a skills/harness-agent/ ~/.codex/skills/harness-agent/
```

Or ask Codex:

```text
Install the harness-agent skill from josusanmartin/harness-agent-skill, path skills/harness-agent.
```

Restart Codex after installing.

## Server Setup

Do not use this repository to run the Harness server. Install and run the server
from:

<https://github.com/josusanmartin/harness>

The normal flow is:

1. Start the Harness server from the `harness` repository.
2. Log in to the Harness web UI.
3. Save connector credentials in the web UI.
4. Create an exercise API key in the web UI.
5. Give the generated environment block to the solving agent.
6. Tell the agent to use the `harness-agent` skill.

## Update

```bash
git pull
rsync -a skills/harness-agent/ ~/.codex/skills/harness-agent/
```

Restart Codex after updating.
