# Worker Workflow

Use this workflow inside one worker with exactly one scoped Scorebench token.
Do not use coordinator admin credentials from a worker.

## Contents

- [Verify scope and establish the run](#verify-scope-and-establish-the-run)
- [Create a trusted session timestamp](#create-a-trusted-session-timestamp)
- [Initialize exact token accounting](#initialize-exact-token-accounting)
- [Submit a protective baseline](#submit-a-protective-baseline)
- [Iterate and refresh](#iterate-and-refresh)
- [Audit invalidation and reinstatement](#audit-invalidation-and-reinstatement)
- [Finalize the run](#finalize-the-run)

## Verify Scope And Establish The Run

```bash
scorebench context
scorebench exercise
```

Confirm the intended connector, exercise, credential profile, original prompt,
and run identity. Do not pass `--exercise` to escape the token scope.

If context shows `needs_run_name: false` or a run ID, inspect and keep it:

```bash
scorebench run current
```

For an unbound token, start one stable run with the actual runtime metadata and
complete original instructions:

```bash
scorebench run start --id <unique-run-id> \
  --skills scorebench \
  --model <actual-model> \
  --effort <actual-effort> \
  --autonomy autonomous \
  --prompt-file /absolute/path/to/original-assignment.md
```

Do not summarize or rewrite the prompt. Add optional label, strategy,
hypothesis, notes, and one fixed GPU when known. Include another skill only if
the worker genuinely uses it. A no-solving-skill run records
`--skills scorebench`.

If a pre-bound run lacks optional metadata and has no submissions, update that
same ID with `run start`; do not create a replacement. If Scorebench reports a
prior run for the scope, continue with its exact command. Use
`--confirm-new-run` only when the user explicitly requested a new independent
run.

Confirm:

```bash
scorebench run current
scorebench run progress
```

## Create A Trusted Session Timestamp

For a new worker session:

```bash
scorebench run ping --event start --note "worker session started"
```

For a resumed worker:

```bash
scorebench run ping --event resume --note "worker session resumed"
```

Require the current run ID and a heartbeat with the requested event. A
pre-bound token is not enough. Do not optimize or submit until the ping
succeeds.

Use `scorebench run progress` for authoritative run-scoped active time, elapsed
time, tokens, sources, and measurement timestamps. It measures at the latest
submitted candidate and does not create a heartbeat when polled. Never parse
dashboard HTML or treat `scorebench best` as the latest timing point.

## Initialize Exact Token Accounting

Read [exact token accounting](token-accounting.md) immediately after the run
and ping are established. Baseline one exact current-session source before
optimization. Use one absolute state path per lane and the same source for
every submission and final usage.

If no exact run-scoped counter is available after one quick supported check,
stop before submitting.

## Submit A Protective Baseline

Create, validate, and submit the simplest legitimate correct candidate in the
first work cycle. Do this before attempting a complete redesign, especially at
high or max effort where one long design turn can exhaust an output cap without
producing an artifact.

First-cycle discipline:

1. Read the exercise, pinned generator, local simulator/tests, and stricter
   original-prompt conditions.
2. Create the smallest correct candidate.
3. Run official correctness checks.
4. Snapshot exact tokens.
5. Submit the baseline.
6. Refresh it to a terminal state.

Keep bundles minimal and free of secrets, caches, build outputs, transcripts,
and unrelated scratch files. Use `--solution-file` for a required entrypoint
inside a directory.

```bash
scorebench submit path/to/solution \
  --label baseline-v1 \
  --notes "correct protective baseline" \
  --idempotency-key baseline-v1 \
  $TOKEN_FLAGS
```

Use connector overrides only after reading [connector
guidance](connectors.md). Never invent options or change the scoped exercise,
credential, or GPU.

An idempotency key is a retry key for the exact same bundle and semantics after
an uncertain response. Change it whenever source, compiler, system, or other
submission semantics change.

## Iterate And Refresh

```bash
scorebench history
scorebench best
scorebench refresh
```

Refresh pending, submitted, or checking candidates instead of resubmitting:

```bash
scorebench refresh <candidate_id>
```

Continue until terminal. Preserve exact errors and `trace_id`.

Optimize in small falsifiable increments:

1. Protect the best valid artifact.
2. Change one mechanism or tightly coupled group.
3. Run official correctness tests.
4. Measure locally when supported.
5. Snapshot exact tokens and submit.
6. Refresh and compare with Scorebench's authoritative result.

Before submission, validate against the published exercise, pinned generator
and documented input domain, and every stricter original-prompt condition.
Specialization to an explicit generator guarantee is allowed. Hardcoded
instances, exact-input output caching, pointer-identity keys, stale outputs,
hidden-test detection, skipped work, and semantic bypasses are not.

Read `trust.warnings` in submit/history responses. Same-content or
code-similarity warnings do not alone change the connector verdict, but a
warned result must not be presented as independent without review. Use only
evidence exposed by Scorebench; never inspect a sibling workspace or private
source.

Connector-visible context also goes through Scorebench:

```bash
scorebench leaderboard
scorebench solutions
scorebench inspect-solution <solution_id>
scorebench challenge-page <section>
scorebench solve-form
scorebench solution <own_solution_id>
```

## Audit Invalidation And Reinstatement

Invalidate an incorrect, exploitive, or falsely assumed candidate:

```bash
scorebench invalidate <candidate_id> \
  --reason "specific correctness or exploit evidence" \
  --meta class=exploit
```

Omit the ID only when the latest candidate is definitely intended.
Invalidation preserves the immutable bundle, response, score, usage, and audit
trail while removing it from best curves.

Inspect `audit.invalidation` before changing descendants. Reinstate only when
concrete contract evidence proves the invalidation wrong:

```bash
scorebench reinstate <candidate_id> \
  --reason "contract correction: documented domain permits this specialization" \
  --meta class=contract_correction
```

Reinstatement appends a linked decision; it never erases the original audit.

## Finalize The Run

Before exit:

1. Finish the current safe operation.
2. Refresh pending candidates to terminal states.
3. Confirm the best candidate is valid.
4. Snapshot the original exact token source.
5. Run `scorebench run usage $FINAL_TOKEN_FLAGS`.
6. Only then create a coordinator-defined completion marker.

Report run ID, best terminal candidate and score, exact tokens/source,
submitted Scorebench active time, and unresolved failures. Never substitute
ordinary elapsed time for `progress.active_seconds`.
