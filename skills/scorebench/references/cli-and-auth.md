# CLI And Authentication

Use this reference to install or verify the Scorebench CLI and authenticate a
coordinator. A worker with a scoped run token must not use admin login.

## Contents

- [Install or upgrade the CLI](#install-or-upgrade-the-cli)
- [Verify required capabilities](#verify-required-capabilities)
- [Validate a worker token](#validate-a-worker-token)
- [Authenticate a coordinator](#authenticate-a-coordinator)
- [Protect admin credentials](#protect-admin-credentials)

## Install Or Upgrade The CLI

Do not assume an existing executable is current. The server requires
`original_prompt` on token creation, and durable watchers require scoped
`scorebench run progress`. An old CLI can fail with
`HTTP 400: original_prompt is required` or lack the progress command.

Install or refresh from the same deployment:

```bash
SCOREBENCH_BASE_URL="${SCOREBENCH_URL:-https://scorebench.dev/}"
curl -fsSL "${SCOREBENCH_BASE_URL%/}/install.sh" | bash
export PATH="$HOME/.local/bin:$PATH"
command -v scorebench
```

The installed skill includes a deployment-first helper. It upgrades an existing
CLI when required commands are absent:

```bash
SCOREBENCH_CLI_BOOTSTRAP="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/install_scorebench_cli.sh"
bash "$SCOREBENCH_CLI_BOOTSTRAP"
```

Set `SCOREBENCH_CLI_FORCE=1` to refresh an otherwise compatible installation.
For offline development, provide a current server or CLI checkout:

```bash
SCOREBENCH_CLI_CHECKOUT=/absolute/path/to/current-checkout \
  bash "$SCOREBENCH_CLI_BOOTSTRAP"
```

Never use the skill checkout as the server or CLI implementation.

## Verify Required Capabilities

Before coordinating a batch:

```bash
scorebench admin create-run-token --help |
  grep -E -- '--prompt|--prompt-file'
scorebench admin create-run-token --help | grep -F -- '--coding-harness'
scorebench run start --help | grep -F -- '--coding-harness'
scorebench run progress --help
```

The token command must expose a required `--prompt` or `--prompt-file` choice,
and token/run-start commands must expose `--coding-harness`. Stop and upgrade
if any capability is absent.

Inspect launch semantics:

```bash
scorebench admin launch --help
```

`--dry-run` creates scoped keys and prompt files but skips tmux windows. It is a
mutating operation, not a harmless schema validator. Do not repeat it with the
same run IDs merely to test command shape.

## Validate A Worker Token

A worker receives only:

```bash
export SCOREBENCH_URL=https://scorebench.dev/
export SCOREBENCH_RUN_TOKEN=hrun_...
scorebench context
```

Legacy `HARNESS_URL`, `HARNESS_RUN_TOKEN`, and the `harness` alias remain
accepted, but prefer current names.

A web-issued token should report `kind=run_token`, user, connector, exercise,
credential profile, run ID/name, and whether a run name is needed. A legacy
workspace can report `kind=arm_token`; use it only for an existing legacy run.

If the CLI is available but the scoped context is absent, stop and report that
the Scorebench exercise API key environment is missing. Do not ask for venue
credentials or search dotfiles for them.

The dedicated `vliw` connector has no external credential and resolves to
`ScoreBench main`; it still uses a scoped run token.

## Authenticate A Coordinator

Use admin auth only outside worker environments:

```bash
scorebench admin login \
  --url https://scorebench.dev/ \
  --username <username>
scorebench admin whoami
```

The command opens or prints a browser authorization link. Over SSH:

```bash
scorebench admin login \
  --url https://scorebench.dev/ \
  --username <username> \
  --no-browser
```

For supervised automation, pass a password through standard input rather than
shell arguments:

```bash
printf '%s\n' "$SCOREBENCH_ADMIN_PASSWORD" |
  scorebench admin login \
    --url https://scorebench.dev/ \
    --username <username> \
    --password-stdin
```

Use `--profile <name>` consistently on login, `whoami`, and logout when
managing multiple deployments. Browser session management is available at:

```text
https://scorebench.dev/ui/login
https://scorebench.dev/ui/account
```

## Protect Admin Credentials

The local admin profile is stored under `~/.config/harness/cli.json`. Never
copy, mount, print, or expose it to a worker. Never provide a worker an admin
password, browser cookie, connector credential, or another lane's token.

Create one prompt-bound scoped token per worker with
`scorebench admin create-run-token` or `scorebench admin launch`.
