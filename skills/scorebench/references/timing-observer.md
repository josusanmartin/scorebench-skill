# Passive Timing Observer

Use the bundled observer to collect ScoreBench timing-v2 shadow evidence without
asking the model for periodic status reports. It is local, event driven, and
best effort. It does not replace the authoritative v1 activity clock yet.

## Contents

- [Shadow contract](#shadow-contract)
- [Register once](#register-once)
- [What it measures](#what-it-measures)
- [Privacy boundary](#privacy-boundary)
- [Finish and disable](#finish-and-disable)
- [Overhead check](#overhead-check)

## Shadow Contract

`scorebench run progress` keeps its existing schema and top-level fields. During
the shadow rollout it also returns `progress.timing_v2` with:

- `authoritative: false`
- confirmed and credited active seconds
- verified blocked, explicit pause, and unknown seconds
- active lower and upper bounds
- evidence coverage and target state
- policy and measurement timestamps

Continue to use top-level `progress.active_seconds` for run completion and tmux
markers. Never let v2 create, delete, or override a completion marker until an
explicit migration changes that contract.

## Register Once

Register immediately after the successful run start/resume ping and before the
main optimization loop:

```bash
SCOREBENCH_SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/scorebench"
test -f "$SCOREBENCH_SKILL_DIR/scripts/scorebench_observer.py" || \
  SCOREBENCH_SKILL_DIR="$HOME/.claude/skills/scorebench"
SCOREBENCH_OBSERVER="$SCOREBENCH_SKILL_DIR/scripts/scorebench_observer.py"

python3 "$SCOREBENCH_OBSERVER" register --provider auto --cwd "$PWD"
python3 "$SCOREBENCH_OBSERVER" status
```

Pairing stores the dedicated credential in the workspace's owner-only CLI
config. Web handoffs and admin launchers set
`SCOREBENCH_TIMING_OBSERVER_TOKEN`. The observer fails closed if it cannot find
an `hobs_...` credential.

Automatic source discovery supports current Codex and Claude Code JSONL. If the
runner exposes a known source explicitly:

```bash
python3 "$SCOREBENCH_OBSERVER" register \
  --provider codex \
  --source /absolute/path/to/session.jsonl \
  --cwd "$PWD"
```

Use `--provider claude` for Claude Code. `--provider generic` accepts normalized
events with timestamp, kind, operation ID, and operation kind. Do not build a
pane-text adapter and do not upload transcript content.

One quick registration attempt is enough. Because v2 is shadow-only, preserve
the exact error and continue with the existing v1 timing workflow if an older
server, old handoff, or unsupported coding harness lacks observer support.

## What It Measures

The observer derives completed or renewable spans from structured event
boundaries:

- agent/model request start and completion
- tool-call start and completion, including long foreground tests with no output
- terminal turn or task completion
- stable coding-harness process identity for bounded open-operation leases

An open operation is renewed only while the exact process ID and Linux process
start tick still match. Without a stable process identity, completed spans are
accepted but open spans are not extended indefinitely. Unsupported background
jobs and unclear gaps remain unknown rather than being guessed active or idle.

The local state and retry queue are owner-only. Evidence is split into at most
60-second leases and uploaded at most once per minute. Failed uploads retain the
same idempotent spans for retry; the observer never creates replacement IDs.

## Privacy Boundary

The parser uses only:

- timestamps
- event categories
- opaque, locally hashed operation identities
- provider name
- process liveness
- bounded counters and parser version

It never persists or uploads prompt text, assistant messages, private reasoning,
source code, tool arguments, tool output, environment values, or credentials.
`status` never prints the stored observer token. The server rejects unknown
metadata keys, nested content, oversized fields, and any observer attempt to
attest `verified_blocked` or `explicit_pause`.

## Finish And Disable

After final usage and the finish ping, flush and disable the exact workspace
registration:

```bash
python3 "$SCOREBENCH_OBSERVER" unregister --cwd "$PWD"
```

Unregister forces one final upload. If evidence is still queued because the
server is unavailable, it leaves the registration enabled and reports the exact
error; retry the same command after connectivity recovers. A registration also
disables automatically after the verified coding-harness process exits and its
retry queue is empty.

## Overhead Check

The parser has a built-in synthetic benchmark:

```bash
python3 "$SCOREBENCH_OBSERVER" benchmark --events 100000
```

The staging acceptance gates are linear parse time, idle CPU near zero,
bounded memory, no more than one upload per active minute per run, and a normal
one-minute payload below 1 KiB. Treat a regression in any gate as a rollout
blocker, not as a reason to move accounting work back into the agent prompt.
