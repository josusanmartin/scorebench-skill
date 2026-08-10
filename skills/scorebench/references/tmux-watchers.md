# Durable tmux Worker Watchers

Use the bundled watcher for isolated long-running workers that must recover
attachments/processes and continue to a submitted Scorebench active-time
target. Run recovery and progress modes in separate tmux windows.

## Contents

- [Timing and marker contract](#timing-and-marker-contract)
- [Configuration](#configuration)
- [Launch and validate](#launch-and-validate)
- [Recovery behavior](#recovery-behavior)
- [Progress behavior](#progress-behavior)
- [Authentication and liveness limits](#authentication-and-liveness-limits)
- [Coordinator verification](#coordinator-verification)

## Timing And Marker Contract

Use `scorebench run progress` field `progress.active_seconds`. Never stop from
`progress.elapsed_seconds`, tmux/container age, wall-clock elapsed time,
dashboard HTML, or `scorebench best`. Progress measures through the latest
trusted candidate or activity ping and never advances merely because it is read.

Both `active_marker` and `GOAL_COMPLETE` are monotonic evidence: the watcher never
deletes either file. A premature completion marker is preserved and logged for
operator review.

Put the exact contract in every goal:

```text
Continue until /work/SCOREBENCH_ACTIVE_TARGET_REACHED exists. Do not create
/work/GOAL_COMPLETE before then. Base completion only on this run's trusted
Scorebench active_seconds. Submit periodically. At target, refresh pending
candidates, record final exact usage, verify the best valid terminal candidate,
then create /work/GOAL_COMPLETE.
```

## Configuration

Keep coordinator-owned JSON outside worker workspaces. Do not include tokens,
credentials, solution paths, or sibling code.

```json
{
  "tmux_session": "condition-model-high-20260730T190000Z",
  "docker_command": ["sudo", "-n", "docker"],
  "recovery_poll_seconds": 30,
  "active_poll_seconds": 120,
  "activity_heartbeat_seconds": 300,
  "target_active_seconds": 14400,
  "nudge_seconds": 300,
  "resume_cooldown_seconds": 300,
  "completion_marker": "/work/GOAL_COMPLETE",
  "active_marker": "/work/SCOREBENCH_ACTIVE_TARGET_REACHED",
  "workers": [
    {
      "run_id": "condition-model-high-20260730T190000Z-l1",
      "window": "condition-model-high-20260730T190000Z-l1",
      "container": "sb-condition-model-high-20260730T190000Z-l1",
      "client": "claude",
      "restart_command": [
        "/absolute/path/to/lane1/start-worker.sh"
      ]
    }
  ]
}
```

Every run ID, window, container, and restart command must be unique and exact.
Clients are `claude`, `codex`, `gemini`, `grok`, or `other`.

Each container needs a current CLI and its own scoped token:

```bash
docker exec <exact-container> scorebench run progress
```

Both `scope.run_id` and `progress.run_id` must match the configured worker.
Refresh old CLIs with the bootstrap helper.

Legacy `report_url` and `enforce_active_gate` fields remain accepted but unused.
They never authorize marker deletion.

## Launch And Validate

```bash
WATCHER="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/scorebench_watch.py"
CONFIG="/absolute/path/to/scorebench-watch.json"

python3 "$WATCHER" validate --config "$CONFIG"
python3 "$WATCHER" recovery --config "$CONFIG" --once
python3 "$WATCHER" active --config "$CONFIG" --once
```

Then launch separate windows:

```bash
tmux new-window -d -t "$SESSION" -n "sb-recovery" \
  "exec python3 '$WATCHER' recovery --config '$CONFIG' 2>&1 | tee -a '$LOG_DIR/recovery.log'"

tmux new-window -d -t "$SESSION" -n "sb-progress" \
  "exec python3 '$WATCHER' active --config '$CONFIG' 2>&1 | tee -a '$LOG_DIR/progress.log'"
```

Every watcher subprocess starts in a fresh process group and is hard-bounded.
On timeout, the watcher terminates and then SIGKILLs the group so one wedged
tool cannot freeze sibling monitoring.

## Recovery Behavior

Recovery mode:

- accepts known startup trust confirmations;
- resumes `/goal` after capacity/usage-limit blocks and a cooldown;
- reports authentication failures;
- exact-matches tmux window membership before capture, input, or pane-state
  queries;
- reattaches a running container when only the attachment died;
- uses the configured restart command when the container stopped;
- leaves completed workers alone.

Exact membership matters because `tmux display-message -t <missing-target>` can
silently resolve another pane.

Recovery never reads sibling workspaces and has no elapsed-time stop.

## Progress Behavior

For each worker, active mode runs `scorebench run progress` inside that exact
container. It rejects any response whose token scope or run ID differs from the
configured worker.

Before that read, a visibly busy worker emits a scoped `activity` ping no more
often than `activity_heartbeat_seconds`. The default is five minutes, safely
inside Scorebench's 15-minute unsupported-gap cap. The watcher withholds the ping
for idle or completed workers and when the recent busy evidence has not changed;
a frozen TUI therefore cannot manufacture indefinite active time. The ping and
progress response must both match the exact configured run.

It retains per-run high-water active time, elapsed time, and tokens. A transient
regression cannot reverse target evidence. An existing active marker remains
authoritative across watcher restarts.

Below target, a premature completion marker is preserved and reported. An idle
unmarked worker receives a throttled lane-only prompt. Busy detection considers
only recent nonblank TUI status lines: stale prose containing “running” does
not suppress recovery, while live `Working (` or `esc to interrupt` does.

Every continuation prompt explicitly requires:

```text
scorebench run ping --event resume
```

At target, the watcher creates the active marker. If the worker is idle and not
complete, it sends a throttled finalization prompt requiring pending refresh,
best-candidate verification, `scorebench run usage`, and completion-marker
creation. It does not interrupt a visibly busy worker.

A progress failure mutates no worker and does not block sibling checks.

## Authentication And Liveness Limits

Recovery reports auth failure but does not copy credentials. Prefer
`CLAUDE_CODE_OAUTH_TOKEN` for parallel Claude lanes.

If a separate repair monitor is unavoidable:

1. Validate the host credential with a real bounded inference request.
2. Stop and require operator action when that probe fails.
3. Copy only the allowlisted credential into the exact lane.
4. Restart only that lane and enforce a cooldown.

Never repair from an expired host credential.

Pane text remains bounded evidence, not proof of useful inference progress.
Separately monitor exact process arguments, process presence, frozen retry
counters, and scoped history. Sample retry counters twice before declaring a
request wedged.

## Coordinator Verification

Periodically confirm:

- watcher logs contain only assigned run IDs;
- every window exists by exact membership;
- provider processes match model and effort;
- progress logs show `active=...s tokens=...`;
- pending candidates advance or preserve an exact error;
- no completion decision uses elapsed time;
- final usage and completion marker follow target finalization.

Treat `premature_complete=1`, progress regression, or auth failure as an
operator-review event; preserve the evidence.
