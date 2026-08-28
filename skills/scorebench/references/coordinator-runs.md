# Coordinator Runs

Use this reference when creating scoped tokens or supervising multiple workers.
A coordinator orchestrates and reports; it does not solve for a lane, copy
candidates between lanes, or submit on a worker's behalf.

## Contents

- [Preflight](#preflight)
- [Define identities and conditions](#define-identities-and-conditions)
- [Render goals before tokens](#render-goals-before-tokens)
- [Mint scoped tokens safely](#mint-scoped-tokens-safely)
- [Validate provider authentication](#validate-provider-authentication)
- [Launch in dependency order](#launch-in-dependency-order)
- [Verify the actual runtime](#verify-the-actual-runtime)
- [Monitor authoritative state](#monitor-authoritative-state)
- [Repair safely](#repair-safely)
- [Report and tear down](#report-and-tear-down)

## Preflight

Before creating server state:

1. Read [CLI and authentication](cli-and-auth.md), upgrade the CLI, and verify
   prompt-bound token creation plus `scorebench run progress`.
2. Run `scorebench admin whoami` and verify the intended account/profile.
3. Verify `docker`, `tmux`, the selected agent CLI, and required GPU.
4. Verify the Scorebench skill exists inside the actual worker image or state
   volume. `--skills scorebench` is metadata, not an installer.
5. Verify the installed skill includes `scripts/scorebench_observer.py`; read
   [passive timing observer](timing-observer.md). The launcher supplies a
   lane-scoped observer credential separately from the run token.
6. Verify target run IDs, containers, volumes, tmux session, and windows do not
   exist.
7. Resolve the actual coding harness, provider model identifier, and supported
   effort value.

Treat `scorebench admin launch --dry-run` as mutating: it creates run keys and
prompt files but skips tmux. Use local help and file/identifier validation
before the first token-creating command. Do not repeat a dry run with the same
run IDs merely to test shape. The exception is `scorebench admin plan
--dry-run`, which only validates and prints the expanded matrix and creates
nothing.

## Define Identities And Conditions

Use a batch timestamp and lane suffix across every resource:

```text
<condition>-<model>-<effort>-<UTC timestamp>-l<N>
```

Use the same exact identity for the Scorebench run, container, volumes, tmux
window, logs, and lane files, adding only safe type prefixes. Timestamping
prevents concurrent same-model/effort batches from colliding.

Record:

- connector, credential profile, and exercise;
- model, coding harness, and effort;
- autonomous, steered, or mixed operation;
- solving skills actually used;
- strategy, hypothesis, lane, and batch timestamp;
- one GPU, when applicable;
- requested `progress.active_seconds` target.

For a no-solving-skill condition use `--skills scorebench`. The worker still
uses Scorebench for lifecycle and submission, but must not load an optimization
skill.

## Render Goals Before Tokens

Render complete lane-specific goals first because every token must store exact
`original_prompt` text. Keep immutable rendered files in a coordinator-owned
directory and use one per lane.

Every goal must:

- name exact run ID, lane, model, coding harness, effort, autonomy, and solving
  skills;
- require context, exercise, current run, start/resume ping, and exact token
  baseline before optimization;
- require one passive timing-observer registration after the trusted ping;
- require a correct protective baseline in the first work cycle;
- require bounded iterations, official correctness checks, periodic
  submissions, and refresh of pending candidates;
- forbid direct venue access, sibling inspection, credential access, prior
  artifacts, and exploit classes;
- define the active/completion marker contract when watchers enforce a target;
- require final usage and best-candidate verification before completion.

Use explicit baseline language:

```text
Create, test, and submit the simplest correct protective baseline before
extended design. Keep responses bounded. Then optimize in small verified
increments and submit after meaningful improvements. Before each new candidate,
check scorebench run progress and obey its submission allowance. Never create a
new candidate for unchanged content; refresh pending candidates and reuse an
idempotency key only to recover an uncertain response.
```

The published exercise, pinned generator, and original prompt together define
the correctness contract. Allow specialization to documented generator
properties unless the prompt is stricter. Add this boundary when absent:

```text
Do not use exploits. Do not hardcode exact benchmark instances, memoize or
cache outputs for repeated venue inputs, key off pointer identity, reuse stale
outputs, detect hidden tests, skip required work, or bypass intended semantics.
Invalidate any submitted candidate later found invalid or exploitive.
```

For long instructions, stage the full text at a worker-local path such as
`/work/TMUX_GOAL` and send a short `/goal` command to read it. Never put tokens
in a goal.

## Mint Scoped Tokens Safely

Create one distinct token per lane after all goals exist:

```bash
scorebench admin create-run-token \
  --connector <connector> \
  --credential <credential-profile> \
  --exercise <exercise> \
  --run-id "<unique-lane-id>" \
  --run-name "<unique-lane-id>" \
  --prompt-file "<absolute-rendered-goal-path>" \
  --strategy "<condition>" \
  --hypothesis "<hypothesis>" \
  --notes "<batch and lane>" \
  --skills scorebench \
  --model <actual-model> \
  --coding-harness <actual-coding-harness> \
  --effort <actual-effort> \
  --autonomy autonomous \
  --json > "<coordinator-secret-dir>/lane<N>.json"
```

Omit `--credential` for credentialless connectors such as `vliw`. Add solving
skills and GPU only when used. Treat server-returned fields, including a
normalized credential profile, as authoritative.

The coding harness is the executable/interface, independent of model. Use
`Claude Code` for Claude, `Codex` for GPT/OpenAI, `Deep Code` for DeepSeek,
`Grok Build` for Grok, `Kimi Code` for Kimi (including model ID `k3`), `ZCode`
for GLM, and the actual native harness for other models.
A Claude model launched inside Codex records `Codex`; do not relabel it
`Claude Code` merely because of the model name.

Keep token JSON/manifests outside worker workspaces. Use a mode-700 directory
and mode-600 files. Never print raw manifests or prompt files; redact tokens
before summaries.

The CLI/server may normalize one terminal newline from `--prompt-file`. When
verifying stored `original_prompt`, normalize only that terminal line ending on
both sides. Do not ignore internal whitespace or compare a different template.

If token creation stops mid-batch, validate every existing token against exact
run ID and normalized prompt, preserve valid tokens, and create only missing
lanes.

`scorebench admin launch` can create multiple tokens and prompt files. Use its
JSON manifest as secret material. Persistent `/goal` or custom-container runs
must also follow [tmux goal sessions](tmux-goal-sessions.md).

For a strategy x model matrix, prefer one YAML run plan over repeated
`create-run-token`/`launch` invocations:

```yaml
plan: <batch-name>
connector: <connector>          # plus credential: unless credentialless
exercise: <exercise>            # or exercises: [...]
count: <lanes per cell>
defaults:
  effort: <effort>
  autonomy: autonomous
  skills: [scorebench]
  goal_file: goals/shared.md    # paths resolve relative to the plan file
models:
  - <model-id>
  - name: <model-id>
    coding_harness: <harness>
    effort: <override>
strategies:
  - name: <condition>
    hypothesis: <hypothesis>
    goal_file: goals/<condition>.md
```

`scorebench admin plan plan.yaml --dry-run` validates every cell and prints the
matrix without creating anything. Without `--dry-run` it mints one token per
cell (run ID `<strategy>-<model>`, exercise-prefixed for multi-exercise plans,
`-NN` suffixes when `count > 1`), writes one rendered `prompt.md` per run, and
saves a manifest containing every token — treat the workspace root as secret
material like a launch manifest. Overrides merge defaults, then the model
entry, then the strategy entry; the whole plan is rejected before any minting
on unknown keys, missing goals, missing credentials, or run-ID collisions.
Re-running an identical plan reuses the same run IDs, so rename the plan or
strategies for a fresh batch. A `launch:` section starts tmux windows exactly
like `admin launch`; omit it (or pass `--no-launch`) to hand prompt files to
workers yourself. Goal rendering rules above apply unchanged: every cell's
resolved goal becomes that token's stored `original_prompt`.

## Validate Provider Authentication

An auth-status command proves credentials exist, not that inference works.
Before launch, make one noninteractive bounded inference request with the exact
model and auth mechanism. Require the expected small response.

Run probes in a fresh process group. On timeout, terminate and then SIGKILL the
entire group so wedged descendants cannot survive.

For parallel Claude Code lanes, prefer:

```bash
claude setup-token
```

Inject the long-lived `CLAUDE_CODE_OAUTH_TOKEN` through each lane's mode-600
environment file. Do not put it in command arguments, goals, or logs.

Copying one refreshable `.credentials.json` into several lanes creates a
refresh-token rotation race. Avoid shared refreshable OAuth state. If
unavoidable, use lane-private auth volumes and a guarded repair monitor:

1. Validate the host credential with a real bounded inference request.
2. If it fails, stop repairs and report `OPERATOR ACTION REQUIRED`.
3. Otherwise copy only the allowlisted credential into the exact lane.
4. Restart only that lane and enforce a cooldown.

Repeatedly copying an expired host credential cannot recover a worker.

## Launch In Dependency Order

For isolated batches, read [Clean-room Docker](clean-room-docker.md).

1. Create and verify fresh lane-specific work and agent-state volumes.
2. Seed only allowlisted files, installed skills, bootstrap, token helper, and
   staged goal.
3. Create/start every container.
4. Verify each container is running and bootstrap-ready.
5. Create the tmux session and one exact attachment window per running
   container.
6. Verify model, effort, session identity, and permission/autonomy flags.
7. Send the short staged-goal command.
8. Verify `/goal`, context, run, ping, passive observer, token baseline, and first
   candidate.
9. Start recovery and progress watchers in separate windows.

Attaching before a container runs can close the tmux window or newly created
session when `docker attach` fails.

## Verify The Actual Runtime

Run metadata describes intent; it does not configure the provider. Verify:

- exact process arguments in each container or host process;
- TUI header model and effort;
- requested permission/autonomy mode;
- fixed lane-specific provider session ID across restarts;
- exactly one scoped Scorebench token per lane.

Do not report a model/effort condition merely because metadata names it. The
live process must match.

## Monitor Authoritative State

Launching windows is not success. Query each lane through only its scoped
token:

```bash
SCOREBENCH_URL=<url> SCOREBENCH_RUN_TOKEN=<lane-token> scorebench context
SCOREBENCH_URL=<url> SCOREBENCH_RUN_TOKEN=<lane-token> scorebench run current
SCOREBENCH_URL=<url> SCOREBENCH_RUN_TOKEN=<lane-token> scorebench run progress
SCOREBENCH_URL=<url> SCOREBENCH_RUN_TOKEN=<lane-token> scorebench history
SCOREBENCH_URL=<url> SCOREBENCH_RUN_TOKEN=<lane-token> scorebench best
```

Use top-level `run progress` fields for authoritative v1 active time/tokens,
current submission allowance when exposed, and history for candidate state.
Log nested timing-v2 bounds and coverage for shadow review only. Refresh every
pending candidate until terminal.

Watch for:

- no protective baseline after the expected bootstrap interval;
- no artifact/test activity during a long high/max-effort response;
- pending candidates that stop changing;
- repeated same-content warnings or submission-rate 429s;
- failures, rejections, `trust.warnings`, or trace IDs;
- authentication/capacity errors;
- missing windows, stopped containers, dead provider processes, or frozen
  retry counters;
- mismatched run, prompt, model, effort, skills, or token provenance.

If a lane spends its first long work cycle without an artifact, queue:

```text
Submit a working candidate now. Create the simplest correct baseline, run the
official tests, submit it, then optimize in small bounded increments.
```

Use [tmux watchers](tmux-watchers.md) for durable recovery. Hard-bound every
`docker`, `tmux`, provider, and Scorebench subprocess so one wedged command
cannot disable sibling monitoring.

## Repair Safely

Resolve exact targets read-only before repair:

- list tmux windows and exact-match the name; `tmux display-message -t` can
  silently resolve an unknown target to another pane;
- inspect the exact container name/state;
- preserve monotonic active/completion markers;
- preserve provider session ID, run token, model, and effort;
- send `scorebench run ping --event resume` after recovery.

Never copy source, transcripts, tokens, or metrics between lanes. A coordinator
repairs infrastructure, not solutions.

## Report And Tear Down

Report each lane's verified runtime condition, best terminal candidate/score,
pending/failing candidates, exact tokens/source, latest trusted
`progress.active_seconds`, and auth/restart caveats.

Before cleanup, archive only permitted final artifacts and record final usage.
Then remove exact validated names only:

- kill only the batch's tmux session, never `tmux kill-server`;
- remove exact containers/volumes, never prefix globs;
- never run Docker prune on a shared host;
- verify unrelated batches remain healthy.
