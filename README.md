# Harness Agent Skill

Codex skill for solving challenges through the local Harness middleware.

The skill assumes the Harness web UI creates an exercise API key scoped to one
user, one credential profile, one venue, and one exercise. Agents receive only:

```bash
export HARNESS_URL=http://127.0.0.1:8718
export HARNESS_RUN_TOKEN=hrun_...
```

Agents then use `harness context`, `harness challenge`, `harness run start`,
and `harness submit`. Venue credentials stay in the Harness middleware.

## Install

From this repo:

```bash
mkdir -p ~/.codex/skills
rsync -a skills/harness-agent/ ~/.codex/skills/harness-agent/
```

Or ask Codex:

```text
Install the harness-agent skill from josusanmartin/harness-agent-skill, path skills/harness-agent.
```

Restart Codex after installing.

## Update

```bash
git pull
rsync -a skills/harness-agent/ ~/.codex/skills/harness-agent/
```

Restart Codex after updating.
