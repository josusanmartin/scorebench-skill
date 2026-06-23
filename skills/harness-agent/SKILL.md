---
name: harness-agent
description: "Use when solving a challenge through the local A/B harness middleware. The harness owns venue credentials and submissions; agents use harness context, challenge, run start/current, submit, refresh, solution, best, history, and budget without reading run_state.json or venue secrets."
---

# Harness Agent

Use this skill when solving a challenge through the local A/B harness middleware.
The harness owns venue credentials and submissions; the agent only talks to
`harnessd`.

## Harness Context

Do not ask the user for venue API keys, cookies, credential paths, or
`run_state.json`. The normal workflow starts in the Harness web UI: the user
creates an exercise API key for exactly one user, credential profile, venue, and
exercise, then provides the agent with:

```bash
export HARNESS_URL=http://127.0.0.1:8718
export HARNESS_RUN_TOKEN=hrun_...
```

The `harness` CLI also supports auto-discovery from a harness workspace for
legacy runs, but a web-issued `HARNESS_RUN_TOKEN` is the preferred path:

```bash
harness context
```

The token is never printed. For the preferred web-issued workflow,
`harness context` must show a server scope with `kind=run_token`, `user_name`,
`venue`, `exercise`, and `credential_profile`. If a user expected a web-issued
exercise API key but that scoped context is missing, stop and report that the
harness exercise API key is required. Legacy workspace contexts may show
`kind=arm_token`; use them only for existing harness workspaces that already
have an active arm context.

Venue credentials are stored on the middleware side.
The Harness UI can store many named credential profiles per venue, but the
exercise API key binds exactly one of them. Do not try to list, infer, or switch
credential profiles from the agent side. If the selected profile, venue, or
exercise is wrong, ask the user to create a new exercise API key in the web UI.

## Workflow

1. Read the configured challenge:

```bash
harness challenge
```

If the user explicitly asks for a different problem than the run default, pass
that problem with `--challenge`, but normal runs should not need this.

2. Choose and start a run before submitting:

```bash
harness run start --id run001
```

Use a stable, human-readable run id such as `run001`,
`harness-run-20260622-001`, or a short strategy name plus timestamp. Optional
metadata can be supplied when it is useful:

```bash
harness run start --id run001 \
  --label "skill-research run001" \
  --strategy "progress logging skill with perf access" \
  --hypothesis "perf-guided changes should reduce score faster"
```

If the harness says a previous run already exists for this user/profile/exercise,
continue it with the exact `harness run start --id <previous-run>` command from
the error. Only use `--confirm-new-run` when the user explicitly wants a new
independent run rather than a continuation.

After starting, confirm the active run:

```bash
harness run current
```

3. Work in the current agent workspace. Keep candidate files small and scoped to
the actual solution.

4. Capture a token snapshot before every submission. Use the current runner or
   tool-provided usage counter when available. In Codex sessions with an active
   goal, call `get_goal` and use the current cumulative token count. Keep the
   provenance honest with `--tokens-total-source`.

5. Submit through the harness only, and include `--total-tokens` in the same
   call. The CLI automatically includes the token-bound run id from
   `harness run current`; the middleware rejects submissions without both an
   active run and a token snapshot:

```bash
harness submit path/to/solution \
  --label short-name \
  --notes "what changed" \
  --idempotency-key short-name-v1 \
  --total-tokens 45678 \
  --tokens-total-source agent_claim
```

6. Include active time and token delta when they are available:

```bash
harness submit path/to/solution \
  --label short-name \
  --notes "what changed" \
  --idempotency-key short-name-v1 \
  --active-seconds 123 \
  --total-tokens 45678 \
  --tokens-delta 1234 \
  --usage-source agent_reported \
  --usage-confidence estimated \
  --tokens-total-source agent_claim \
  --active-seconds-source agent_claim
```

Do not fabricate usage fields. If no token counter is available, do not submit;
report that the harness requires a token snapshot and the current environment
does not expose one.

7. Inspect feedback and refresh queued submissions through the harness:

```bash
harness best
harness history --limit 10
harness budget
harness refresh
```

Use `harness refresh <candidate_id>` when you need to refresh a specific
submitted candidate. If the venue exposes source retrieval through the harness,
use `harness solution <solution_id>` rather than calling the venue directly.

For PR-backed venues, this is still the full workflow. The middleware decides
whether the candidate becomes a local harness run, an API submission, or a
GitHub pull request.

## Rules

- Do not call the external venue CLI or API directly.
- Do not read or write credential files or `run_state.json`.
- Treat `harnessd` responses as the source of truth for scores and statuses.
- Do not choose users, credential profiles, or exercises yourself; those are
  bound into the web-issued exercise API key.
- Do not compare against or inspect other credential profiles or runs unless
  the harness response for the current token exposes that data.
- Do choose exactly one run id before the first submission, then keep using it
  for that run.
- If the strategy, model, tool access, prompt, or experimental condition
  materially changes, ask whether this is a continuation or start a new run only
  with explicit confirmation from the harness/user.
- The token-bound run metadata must be honest and specific: strategy,
  hypothesis, and notes are used for deterministic strategy comparison reports.
- Keep `--notes` factual: hypothesis, result, important failure, or venue id.
- Use `--label` to make deterministic progress logs readable.
- Use `--idempotency-key` for retries of the same candidate.
- If a submit error is unclear, read `docs/middleware-protocol.md` before retrying.
