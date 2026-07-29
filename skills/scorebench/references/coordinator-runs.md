# Coordinator Runs

Read this reference only when acting as a human operator or coordinator. A
worker that already has `SCOREBENCH_RUN_TOKEN` must not use admin credentials.

## Admin Session

Browser login and account/session management live at:

```text
https://scorebench.dev/ui/login
https://scorebench.dev/ui/account
```

For the CLI:

```bash
scorebench admin login --url https://scorebench.dev/ --username <username>
scorebench admin whoami
```

The login command opens or prints a browser authorization URL. Over SSH, add
`--no-browser` and open the URL locally. For supervised automation only, pass a
password through stdin:

```bash
printf '%s\n' "$SCOREBENCH_ADMIN_PASSWORD" | scorebench admin login \
  --url https://scorebench.dev/ \
  --username <username> \
  --password-stdin
```

Use `--profile <name>` on login, whoami, and logout when managing more than one
deployment. The local admin profile is stored under
`~/.config/harness/cli.json`.

Never give a worker the admin profile, password, browser cookie, or that config
file. Give each worker only its own scoped run token.

## Before Creating Runs

Install and load the Scorebench skill in every worker environment first,
including clean Docker environments. Installing the CLI is separate, and
`--skills scorebench` records metadata rather than installing the skill.

Preserve the complete original assignment. Prefer files for nontrivial text:

```bash
scorebench admin create-run-token \
  --connector <connector> \
  --credential <profile> \
  --exercise <exercise> \
  --run-id <run-id> \
  --prompt-file /absolute/path/to/original-prompt.md \
  --skills scorebench \
  --model <model> \
  --effort <effort> \
  --autonomy autonomous
```

`--prompt` or `--prompt-file` is required by `create-run-token`; the exact text
is stored on the run and shown in the dashboard. Do not replace it with a
summary. Omit `--credential` for credentialless connectors such as `vliw`.

Every worker assignment must include an explicit no-exploit boundary. Append
this text when the user has not already supplied an equivalent requirement:

```text
Do not use exploits. Solve the full input domain documented by the exercise and
pinned generator. Specialization to explicitly guaranteed domain properties is
allowed unless this run's prompt adds a stricter requirement. Do not hardcode
exact benchmark instances, memoize or cache outputs for repeated venue inputs,
key off pointer identity, reuse stale outputs, detect hidden tests, skip required
work, or bypass the intended problem semantics. If any submitted candidate is
later found exploity or invalid, immediately run `scorebench invalidate
<candidate_id> --reason "..."`
```

The published exercise, pinned generator, and original prompt together define
the contract. A documented fixed property is eligible for specialization; an
extra restriction in the original prompt remains binding.

## Launching Workers

Use `scorebench admin launch` when creating parallel scoped runs:

```bash
scorebench admin launch \
  --connector local_tensara \
  --credential skill-research \
  --exercise leaky-relu \
  --count 4 \
  --run-prefix no-skill- \
  --skills scorebench \
  --model gpt-5-codex \
  --effort high \
  --autonomy autonomous \
  --gpu RTX5090 \
  --goal-file /absolute/path/to/goal.md \
  --agent-command codex
```

Use `--dry-run --json` first for a new launch shape. `admin launch` stores the
complete assembled goal and additional prompt as the run's original prompt.
Use `--goal-file` and `--prompt-file` for long text so shell quoting cannot
truncate or alter it.

Run metadata describes the experimental condition:

- `--skills` lists skills actually used; always include `scorebench`.
- `--model` and `--effort` name the actual runtime configuration.
- `--autonomy` is `autonomous`, `steered`, or `mixed`.
- `--gpu` pins a GPU-backed run to one device. Omit it for CPU-only connectors.
- `--strategy`, `--hypothesis`, and `--notes` describe the treatment.

For the credentialless `vliw` connector, omit `--credential`; Scorebench binds
the run to its `main` profile. Match the model and effort in both run metadata
and the launched agent command.

Passing a prompt to `codex` or `claude` is not equivalent to a persistent
`/goal` session. For long-running tmux goals, read
`tmux-goal-sessions.md` and send `/goal ...` to the interactive TUI. When
workers must recover from capacity/process interruptions or continue to an
active-time target, also read `tmux-watchers.md`.

For clean-room Docker workers, read `clean-room-docker.md`. Keep independent
workspaces and tokens; never mount prior solution artifacts into a clean run.

## Required Follow-up

A created token or tmux window is not evidence that a worker is operating.
Keep the JSON launch output or generated manifest, then verify every job:

1. Inspect the pane and confirm the agent loaded this skill.
2. Confirm it ran `scorebench context`, `scorebench exercise`, and
   `scorebench run current` or `scorebench run start`.
3. Confirm a successful `scorebench run ping --event start` or `--event resume`
   happened before any submission.
4. Query each job with only that job's token:

   ```bash
   SCOREBENCH_URL=<url> SCOREBENCH_RUN_TOKEN=<token> scorebench context
   SCOREBENCH_URL=<url> SCOREBENCH_RUN_TOKEN=<token> scorebench run current
   SCOREBENCH_URL=<url> SCOREBENCH_RUN_TOKEN=<token> scorebench history
   SCOREBENCH_URL=<url> SCOREBENCH_RUN_TOKEN=<token> scorebench best
   ```

5. If history stays empty past bootstrap, inspect and nudge the matching pane.
6. Refresh pending candidates with that worker's token until terminal.
7. Preserve exact errors and `trace_id` values for failed or rejected runs.

Use read-only Scorebench commands for venue-visible context:
`leaderboard`, `solutions`, `inspect-solution`, `challenge-page`, and
`solve-form`. Never give workers connector credentials or let them call a venue
directly.

For long experiments, repeat these checks during the run and near its deadline.
The final coordinator report must distinguish runs with scored submissions,
pending submissions, terminal failures, and workers that never connected.
