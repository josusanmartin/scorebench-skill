---
name: scorebench
description: "Use when solving an exercise through a Scorebench (Harness) middleware server such as https://scorebench.dev/, including Paradigm Puzzles runs, or when coordinating parallel Harness-scoped agent runs. Scorebench owns connector credentials and submissions; workers use scoped run tokens and the scorebench CLI, while coordinators create prompt-bound tokens, launch isolated workers, supervise tmux sessions, and report results."
---

# Scorebench

Use Scorebench as the control plane between an agent and an optimization venue.
Scorebench owns connector credentials, trusted run metadata, submissions, and
results. A scoped worker talks only to Scorebench, never directly to the venue.
This repository contains the skill, not the server.

## Route To The Required References

Read every selected reference completely before acting. Load only the workflows
triggered by the task:

| Task | Read |
| --- | --- |
| Install or upgrade the CLI; authenticate a coordinator | [CLI and authentication](references/cli-and-auth.md) |
| Solve or resume one scoped run | [Worker workflow](references/worker-workflow.md), then [exact token accounting](references/token-accounting.md) |
| Capture or upload the end-of-run agent trace | [End-of-run traces](references/run-traces.md), plus worker workflow |
| Create tokens or coordinate parallel runs | [Coordinator runs](references/coordinator-runs.md), plus CLI/authentication |
| Use connector-specific flags or interpret responses | [Connector guidance](references/connectors.md) |
| Solve a Paradigm Puzzles exercise | [Paradigm Puzzles](references/paradigm-puzzles.md), plus connector guidance |
| Launch persistent interactive workers in tmux | [tmux goal sessions](references/tmux-goal-sessions.md) |
| Supervise long-running tmux workers | [tmux watchers](references/tmux-watchers.md) |
| Build isolated workers without prior artifacts | [Clean-room Docker](references/clean-room-docker.md) |

## Required Installation Gate

Treat the skill, CLI, and run metadata as separate:

- Install this skill on every worker before giving it a run token.
- Installing the `scorebench` CLI does not install the skill.
- Passing `--skills scorebench` records metadata; it does not install or load
  the skill.

Verify the agent-specific payload:

```bash
# Codex
test -f "${CODEX_HOME:-$HOME/.codex}/skills/scorebench/SKILL.md"

# Claude Code
test -f "$HOME/.claude/skills/scorebench/SKILL.md"

# Grok
test -f "$HOME/.grok/skills/scorebench/SKILL.md"
```

If absent, install `skills/scorebench` from
`https://github.com/josusanmartin/scorebench-skill`, reload the agent, and only
then provide the scoped token.

## Universal Contract

Apply these hard gates to every worker:

1. Give it only `SCOREBENCH_URL` and its own `SCOREBENCH_RUN_TOKEN`. Never
   provide admin auth, connector credentials, cookies, another run token, or
   `run_state.json`.
2. Confirm `scorebench context` reports the intended run-token scope. The
   dedicated `vliw` connector is credentialless and appears as `ScoreBench
   main`, but still requires a scoped run token.
3. Use exactly one run. Keep a pre-bound run; otherwise start one with the
   actual model, coding harness, effort, autonomy, skills, and complete original
   `--prompt-file`.
4. Send a successful `scorebench run ping --event start` for a new worker
   session or `--event resume` for a resumed session before optimization and
   before the first submission. During active work, preserve server-side timing
   evidence with `--event activity` at least every five minutes. Use the bundled
   watcher for long autonomous runs; never emit activity while idle or complete.
5. Immediately after that trusted ping, record the trace source and byte
   offset. Do all sanitization, compression, and upload after final usage and
   the finish ping; never include private reasoning or secrets.
6. Every submission requires an exact, run-relative token total. Establish one
   run-scoped source and use the token helper so provider JSONL is deduplicated
   and normalized. Working tokens exclude cached-input reads; the separate
   cache-read counter remains available for API-equivalent cost. Snapshot before
   every submission and finish with `scorebench run usage`. Grok workers must
   bind the active session through `updates.jsonl` and let the helper read its
   exact unified inference log; Grok's aggregate `totalTokens` includes cached
   reads and is not a valid Scorebench working-token total.
   Never guess a token split or model cost.
7. Submit the simplest correct protective baseline in the first work cycle,
   then submit materially different, validated improvements promptly. Never
   create a new candidate for unchanged content; reuse the original idempotency
   key only to recover an uncertain response and refresh pending candidates.
8. Before each new candidate, use `scorebench run progress` to inspect the
   authoritative trusted timing/token read and any `submission` allowance.
   Honor `can_submit`, `retry_after_seconds`, and venue cooldowns while
   continuing local work. Never infer latest-run timing from dashboard HTML,
   ordinary elapsed time, or `scorebench best`.
9. Do not use an external venue CLI, API, cookie, or credential. Route every
   submission and venue-visible read through Scorebench.
10. Preserve exact harness errors, `trace_id`, `trust.warnings`, and immutable
    audit evidence.
11. Submit only legitimate general solutions. Honor the published exercise,
    pinned generator, and every stricter original-prompt condition.

Never hardcode benchmark instances, cache exact venue outputs, key off pointer
identity, reuse stale outputs, detect hidden tests, skip required work, or
bypass intended semantics. Specialization to an explicitly documented input
property is valid unless the original prompt is stricter.

If a submitted candidate is later invalid or exploitive:

```bash
scorebench invalidate <candidate_id> --reason "<specific evidence>"
```

Use `scorebench reinstate` only when concrete contract evidence proves an
invalidation wrong; reinstatement appends an audit decision and never erases
the original.

## Minimal Worker Sequence

Follow the detailed worker, accounting, and trace references:

```text
context -> exercise -> current/start -> ping -> trace boundary -> token baseline
-> correct baseline -> submit -> refresh -> iterate -> final usage -> finish ping
-> sanitized trace upload
```

Trace capture is best-effort observability. A trace failure never changes a
candidate score, validity, status, or run completion; preserve the local
artifact and exact error for a later retry.

Do not spend a full high-effort or max-effort turn designing an ideal solution
before creating, testing, and submitting a correct artifact.

## No-Solving-Skill Experiments

Distinguish lifecycle infrastructure from solving methodology:

- `scorebench` is the required lifecycle and submission skill.
- A “no skill” or “no solving skill” condition records
  `--skills scorebench` and must not load an optimization skill.
- Add `problem-agnostic-optimization` or another solving skill only when the
  worker genuinely uses it.

Keep model, coding harness, effort, autonomy, prompt, skills, GPU, strategy,
and notes aligned with the actual runtime and experimental condition. The
coding harness is the agent interface that ran the model, not the model
provider: normally Claude uses `Claude Code`, GPT/OpenAI uses `Codex`, DeepSeek
uses `Deep Code`, Grok uses `Grok Build`, Kimi (including model ID `k3`) uses
`Kimi Code`, GLM uses `ZCode`, and other models use their actual native coding
harness. Record `Codex` for a Claude model run
inside Codex, such as the historical `claude-codex-*` runs.

## Coordinator Invariants

Before launching a batch:

1. Upgrade and capability-check the CLI.
2. Render every lane's complete goal before creating its prompt-bound token.
3. Use unique timestamped run, container, volume, tmux, and session names.
4. Validate provider auth with a real bounded inference request.
5. Install the skill in the actual worker image/state.
6. Start every container before creating tmux attachment windows.
7. Verify the executable, live process arguments, and TUI headers match coding
   harness, model, effort, and permission/autonomy metadata before sending
   goals.
8. Monitor scoped progress/history, candidate states, auth, and process
   liveness with hard-bounded commands.
9. Tear down only exact batch-owned names; never use prefix globs, Docker
   prune, or `tmux kill-server` on a shared host.

Read [Coordinator runs](references/coordinator-runs.md) before issuing admin
commands. `scorebench admin launch --dry-run` still creates keys and prompt
files; it is not a side-effect-free validator. For strategy x model matrices,
prefer a YAML run plan: `scorebench admin plan plan.yaml` expands the cross
product into per-cell tokens and prompts, and its `--dry-run` validates
without creating anything.
