# Clean-Room Docker Workers

Use this only when the operator explicitly requests isolated, reproducible
workers: a fresh filesystem per run, no prior attempts, no private code, and
agent CLIs plus the Scorebench skill preinstalled.

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

Verify both the CLI and skill:

```bash
docker run --rm scorebench-worker bash -lc '
  scorebench --help >/dev/null &&
  test -f "$HOME/.codex/skills/scorebench/SKILL.md" &&
  test -f "$HOME/.claude/skills/scorebench/SKILL.md"
'
```

## Prepare Model Authentication

```bash
claude setup-token
mkdir -p ~/.codex-worker
```

Place only a pre-authenticated Codex `auth.json` in `~/.codex-worker`, or inject
`OPENAI_API_KEY` instead. A Scorebench worker needs no admin login: it receives
one scoped `hrun_` token created in the Runs UI or with
`scorebench admin create-run-token`.

## Launch One Worker Per Run

```bash
docker run --rm -it \
  -e SCOREBENCH_URL=https://scorebench.dev/ \
  -e SCOREBENCH_RUN_TOKEN=hrun_... \
  -e CLAUDE_CODE_OAUTH_TOKEN=... \
  -v "$HOME/.codex-worker/auth.json:/home/worker/.codex/auth.json:ro" \
  scorebench-worker
```

Expose only the exact Codex auth file. Do not replace the preinstalled
`~/.codex` directory with a broad host mount because that would hide the skill.

Inside the container, start `claude` or `codex` and use the normal worker
workflow from `SKILL.md`. Use one container and one scoped token per run.

## Isolation Properties And Limits

- A new container filesystem makes prior attempts unreachable.
- No GitHub or SSH credentials are baked in.
- Connector credentials never enter the worker.
- The scoped token limits Scorebench reads and writes to that run.
- Host workspaces and dotfiles are not mounted.
- The skill and CLI are public build inputs, not private server source.

For hard egress controls, add a proxy/firewall allowlist for the Scorebench
deployment, model provider APIs, and explicitly required package indexes.
