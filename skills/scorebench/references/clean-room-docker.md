# Clean-Room Docker Workers

Use this only when the operator explicitly asks for isolated, reproducible
worker environments: fresh filesystem per run, no prior attempts on disk, no
access to non-public code, agent CLIs preinstalled.

## Build the image (once)

The public CLI repo ships the worker image definition:

```bash
git clone https://github.com/josusanmartin/scorebench-cli
docker build -t scorebench-worker scorebench-cli/docker/
```

The image contains Python 3.12, Node 22, Claude Code, Codex, and the
`scorebench` CLI (legacy `harness` alias), running as a non-root `worker`
user with an empty `/work`. No credentials, checkouts, or history are baked in.

## Prepare auth (once per operator machine)

```bash
claude setup-token          # prints CLAUDE_CODE_OAUTH_TOKEN for headless use
mkdir -p ~/.codex-worker    # place a pre-authenticated Codex auth.json here,
                            # or skip and inject OPENAI_API_KEY instead
```

Harness auth needs no login: each worker gets one scoped `hrun_` exercise API
key, minted on the Runs page or with `scorebench admin create-run-token`.

## Launch one worker per run

```bash
docker run --rm -it \
  -e HARNESS_URL=https://scorebench.dev/ \
  -e HARNESS_RUN_TOKEN=hrun_... \
  -e CLAUDE_CODE_OAUTH_TOKEN=... \
  -v "$HOME/.codex-worker:/home/worker/.codex:ro" \
  scorebench-worker
```

Inside the container, start the agent TUI (`claude` or `codex`) and hand it the
normal worker goal with the no-exploit contract; the scorebench workflow from
SKILL.md applies unchanged. For parallel experiments, run one container per
exercise API key (a tmux window per `docker run` composes with
`references/tmux-goal-sessions.md`).

## Isolation properties and limits

- Fresh container filesystem per run: prior attempts are unreachable.
- No GitHub/SSH credentials: private repositories cannot be fetched.
- Only the three injected secrets exist; mount the Codex dir read-only.
- Run tokens already scope harness reads to the worker's own run.
- For hard egress guarantees, add a network allowlist (proxy or firewall
  permitting only the harness URL, the two model APIs, and package indexes).
- Do not mount host workspaces or dotfiles into the container.
