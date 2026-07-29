# Connector Guidance

The scoped run token fixes the connector, exercise, credential profile, and
optional GPU. Use Scorebench for both submissions and connector-visible reads;
never call the venue API or CLI directly.

## Shared Read Paths

Use these commands when supported by the connector:

```bash
scorebench leaderboard
scorebench solutions
scorebench inspect-solution <solution_id>
scorebench challenge-page <section>
scorebench solve-form
```

`scorebench solution <solution_id>` is for source or reports belonging to the
current scoped run. Use `inspect-solution` for other website-visible entries.

## HighLoad

Compiler selection and flags are part of the optimization surface. Inspect the
redacted solve form and public solution metadata before assuming a toolchain:

```bash
scorebench solve-form --language <LANG>
scorebench solutions --lang <LANG>
scorebench leaderboard
scorebench inspect-solution <solution_id>
scorebench challenge-page generators
```

Use the candidate's actual language, such as `CPP`, `RUST`, `GO`, `CSHARP`, or
`ZIG`. A compiler-only change is still a new candidate: use a new idempotency
key, record the flags in notes, and confirm the returned connector request.

## GPU Mode / Popcorn

Scorebench is the Popcorn proxy. `submit` and `refresh` expose the visible CLI
payload under `connector_response.raw.popcorn` and normalized public
benchmarks/tests under `connector_response.case_results` and
`connector_response.case_summary`.

When the scoped exercise exposes them, GPU Mode submit overrides include
`--submission-mode`, `--leaderboard`, `--profile-brev`, and
`--benchmark-index`. A run-level `--gpu` remains fixed; do not change it on a
submission.

Benchmark `mean_ns`, `error_ns`, `best_ns`, and `worst_ns` values are
nanoseconds. Semicolon-delimited case specs are also exposed as typed
`parameters`. Prefer these fields over parsing rendered text. Secret case bodies
are never returned.

Use `scorebench refresh <candidate_id>` to enrich an older candidate while the
venue retains it. `scorebench solution <submission_id> --no-code` returns the
human report; omit `--no-code` only when source is needed.

## VLIW

Submit one Python file, normally `perf_takehome.py`, defining `KernelBuilder`.
The connector is credentialless. Scorebench runs
`KernelBuilder().build_kernel(...)` beside the pinned problem module and sends
only the built instruction list to a private sequential judge; candidate Python
does not execute on the judge.

The run token selects `without-indices` (values checked) or `with-indices`
(values and tree indices checked). The pinned `Input.generate` contract starts
every lane's tree index at zero, so specialization to that documented base
domain is valid. If the original prompt explicitly requires arbitrary initial
indices, that stricter condition remains binding. Exact-input output caching is
never valid.

`scorebench exercise` provides the pinned `problem_url`, `build_kernel_args`,
and module/class names. Download the module once and iterate locally against its
simulator and tests. The judge is queued, so refresh a pending candidate rather
than resubmitting it. A rejected result contains its correctness failure. Keep
the candidate importable and free of import-time side effects.

## Paradigm Puzzles

Read `paradigm-puzzles.md` before creating or submitting a candidate. The
exercise determines whether the bundle is Solidity, Rust, Python, plain text,
packing JSON, or ONNX. Never request or use a Paradigm `pp_...` key; Scorebench
owns it.

Use Scorebench read commands for public context. Fix validation failures.
Respect `nextSubmissionAt` on cooldowns rather than changing idempotency keys.
Lean Semantics is asynchronous, so refresh the existing candidate until
terminal.

## PR-backed Connectors

Use the same workflow. The middleware decides whether a candidate becomes a
local score, API submission, or pull request. Treat the returned Scorebench
status and trace information as authoritative.
