# Clean-Room Docker Workers

Use this only when the operator explicitly requests isolated, reproducible
workers: a fresh filesystem per run, no prior attempts, no private code, and
agent CLIs plus the Scorebench skill preinstalled.

## Contents

- [Build the image](#build-the-image)
- [Verify image cleanliness](#verify-image-cleanliness)
- [Prepare model authentication](#prepare-model-authentication)
- [Create fresh lane state](#create-fresh-lane-state)
- [Launch one worker per run](#launch-one-worker-per-run)
- [Verify isolation](#verify-isolation)
- [Recover and tear down](#recover-and-tear-down)

## Build The Image

Build from a fresh checkout of this public skill repository. The Dockerfile
below downloads the current CLI from the deployment rather than baking a
possibly stale standalone client.

```bash
git clone https://github.com/josusanmartin/scorebench-skill.git

docker build \
  -t scorebench-worker \
  -f - \
  scorebench-skill <<'DOCKERFILE'
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git ripgrep rsync tmux \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code @openai/codex \
    && curl -fsSL https://scorebench.dev/install.sh -o /tmp/install-scorebench.sh \
    && SCOREBENCH_INSTALL_HOME=/opt/scorebench/cli \
       SCOREBENCH_INSTALL_BIN=/usr/local/bin \
       bash /tmp/install-scorebench.sh \
    && rm /tmp/install-scorebench.sh

RUN useradd --create-home --shell /bin/bash worker
COPY skills/scorebench /opt/scorebench-skill
RUN mkdir -p /home/worker/.codex/skills/scorebench \
             /home/worker/.claude/skills/scorebench \
    && rsync -a /opt/scorebench-skill/ /home/worker/.codex/skills/scorebench/ \
    && rsync -a /opt/scorebench-skill/ /home/worker/.claude/skills/scorebench/ \
    && chown -R worker:worker /home/worker/.codex /home/worker/.claude

USER worker
WORKDIR /work
CMD ["/bin/bash"]
DOCKERFILE
```

For another deployment, replace `https://scorebench.dev` with its origin.
Rebuild when the server CLI or skill changes. Do not copy host solution
workspaces into the image.

## Verify Image Cleanliness

Verify the CLI/skill and confirm the image contains no prior work, credentials,
or transcripts:

```bash
docker run --rm scorebench-worker bash -lc '
  scorebench --help >/dev/null &&
  test -f "$HOME/.codex/skills/scorebench/SKILL.md" &&
  test -f "$HOME/.claude/skills/scorebench/SKILL.md" &&
  test -z "$(ls -A /work)" &&
  ! find "$HOME" -name "*.jsonl" -o -name auth.json -o -name ".credentials.json" |
    grep -q .
'
```

Inspect the actual image layers when clean-room provenance matters. A failed
check blocks any claim that the run started clean.

## Prepare Model Authentication

```bash
claude setup-token
```

Prefer the resulting long-lived `CLAUDE_CODE_OAUTH_TOKEN` for parallel Claude
lanes. Avoid copying one refreshable `.credentials.json` into multiple
containers because refresh-token rotation can invalidate sibling lanes.

For Codex, inject `OPENAI_API_KEY` or mount only one explicit pre-authenticated
`auth.json`. Never mount a full host `.codex` or `.claude` directory.

A worker needs no Scorebench admin login. It receives one scoped `hrun_` token.

## Create Fresh Lane State

Use unique timestamped exact names. Create one empty work volume and one empty
provider-session volume per lane. Never share or reuse them:

```text
container:      sb-<unique-lane-id>
work volume:    sbw-<unique-lane-id>
session volume: sbsession-<unique-lane-id>
```

Mount provider session state at the narrow history path so the preinstalled
skill remains visible, for example `/home/worker/.claude/projects` or
`/home/worker/.codex/sessions`.

Stage only the rendered goal, bootstrap, exact token helper wrapper, and an
official public starter repository. Never seed old projects, transcripts,
history, candidates, sibling volumes, or host workspaces.

## Launch One Worker Per Run

Create one mode-600 environment file outside worker workspaces:

```text
SCOREBENCH_URL=https://scorebench.dev/
SCOREBENCH_RUN_TOKEN=hrun_...
SCOREBENCH_RUN_ID=<exact-run-id>
CLAUDE_CODE_OAUTH_TOKEN=...
```

Create a named recoverable container; do not use `--rm` for a long-running
lane:

```bash
docker create \
  --name "sb-<unique-lane-id>" \
  --restart on-failure:10 \
  --env-file "/coordinator/secrets/lane.env" \
  --mount source="sbw-<unique-lane-id>",target=/work \
  --mount source="sbsession-<unique-lane-id>",target=/home/worker/.claude/projects \
  scorebench-worker \
  /work/start-worker.sh
```

Use the corresponding narrow Codex session path for Codex workers. An explicit
Codex `auth.json` may be mounted read-only as one file.

Create all lane containers, then start all of them. Verify each bootstrap before
creating tmux `docker attach` windows. An attachment to a stopped container can
close the new window or tmux session.

## Verify Isolation

Before sending goals, verify:

- `/work` contains only allowlisted bootstrap/goal/starter files;
- session storage contains no old transcript;
- `docker inspect` shows no host workspace, home, or sibling mounts;
- the worker has one scoped token;
- live process arguments/TUI show the requested model and effort;
- both CLI and agent-specific Scorebench skill are readable;
- no GitHub/SSH or connector credential is present.

For hard egress controls, add a proxy/firewall allowlist for the Scorebench
deployment, model provider APIs, and explicitly required package indexes.

## Recover And Tear Down

Keep the exact work/session volumes and fixed provider session ID across a lane
restart. Require `scorebench run ping --event resume` afterward.

Archive only explicitly allowed final artifacts and record final run usage.
Then remove only exact validated batch-owned tmux session, containers, work
volumes, and session volumes. Never use prefix globs, `docker system prune`, or
`tmux kill-server` on a shared host.
