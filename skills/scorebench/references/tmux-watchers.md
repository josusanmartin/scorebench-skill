# Durable tmux Worker Watchers

Use this reference for long-running, isolated ScoreBench workers that must:

- recover a dead tmux attachment or stopped worker process,
- resume `/goal` after a transient capacity block,
- continue until a submitted ScoreBench active-time target is reached, and
- report the latest submitted token total while they run.

The recovery watcher and active-time watcher are separate processes. Run each
in its own tmux window so an outage or parsing failure in one cannot disable the
other.

## Timing Rule

Use ScoreBench point `wall_seconds` as the active-time clock. Never stop a
worker based on report `run_elapsed_seconds`, tmux window age, container age, or
ordinary wall-clock elapsed time. `wall_seconds` is updated by ScoreBench run
activity and submissions; the monitor therefore reports the latest submitted
active time and may lag work that has not yet been submitted.

The active watcher creates an in-container marker after the target is reached.
While below target, its default completion gate removes both that marker and a
premature `GOAL_COMPLETE`. Put the marker contract in every worker goal:

```text
Continue until /work/SCOREBENCH_ACTIVE_TARGET_REACHED exists. Do not create
/work/GOAL_COMPLETE before then. Base completion only on ScoreBench active time,
never elapsed clock time. Submit periodically so the active-time and exact-token
monitor can observe progress.
```

## Configuration

Create a coordinator-owned JSON file outside every isolated worker. Do not put
run tokens, connector credentials, solution paths, or sibling source code in
this file.

```json
{
  "tmux_session": "ant",
  "report_url": "https://scorebench.dev/ui/reports/strategy-compare-vliw-without-indices.html",
  "docker_command": ["sudo", "-n", "docker"],
  "recovery_poll_seconds": 30,
  "active_poll_seconds": 120,
  "target_active_seconds": 14400,
  "nudge_seconds": 300,
  "resume_cooldown_seconds": 300,
  "completion_marker": "/work/GOAL_COMPLETE",
  "active_marker": "/work/SCOREBENCH_ACTIVE_TARGET_REACHED",
  "enforce_active_gate": true,
  "workers": [
    {
      "run_id": "vliw-clean-codex-max-20260713-001",
      "window": "v6-sol56-max",
      "container": "sb-vliw-sol56-max-001",
      "client": "codex",
      "restart_command": [
        "/absolute/path/to/v6-sol56-max/start-isolated-worker.sh"
      ]
    }
  ]
}
```

Each worker needs a unique exact ScoreBench `run_id`, tmux `window`, Docker
`container`, and `restart_command`. Supported client values are `claude`,
`codex`, `gemini`, `grok`, and `other`. The client only controls small TUI input
differences; it does not grant cross-run access.

The marker path is configurable. For an existing four-hour goal that already
uses `/work/SCOREBENCH_4H_REACHED`, set `active_marker` to that exact path in
the watcher config and retain the same path in the worker goal.

The restart command must recreate or restart only that worker's existing
isolated environment. Clean-room workers should still follow
`clean-room-docker.md`: fresh named volumes, no host bind mounts, one scoped run
token, and no shared prior-attempt artifacts.

Validate the file before launching anything:

```bash
WATCHER="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/scorebench_watch.py"
CONFIG="/absolute/path/to/scorebench-watch.json"
python3 "$WATCHER" validate --config "$CONFIG"
```

## Launch Two Watcher Windows

The script logs to stdout. Use `tee` to keep each tmux window observable while
also preserving a coordinator log:

```bash
SESSION="ant"
LOG_DIR="/absolute/path/to/coordinator-logs"
mkdir -p "$LOG_DIR"

tmux new-window -d -t "$SESSION" -n "v6-watch" \
  "exec python3 '$WATCHER' recovery --config '$CONFIG' 2>&1 | tee -a '$LOG_DIR/recovery.log'"

tmux new-window -d -t "$SESSION" -n "v6-active" \
  "exec python3 '$WATCHER' active --config '$CONFIG' 2>&1 | tee -a '$LOG_DIR/active.log'"
```

Attach with `tmux attach -t "$SESSION"`, then select `v6-watch` for
process/capacity recovery or `v6-active` for active-time and token progress.

## Recovery Behavior

The `recovery` mode:

- accepts known startup trust prompts,
- detects capacity or usage-limit `/goal` blocks and sends `/goal resume` after
  a cooldown,
- reports authentication failures without attempting to bypass login,
- reattaches a running container when only its tmux attachment died,
- restarts the configured isolated worker when both worker and attachment died,
  and
- recreates a missing worker window unless `GOAL_COMPLETE` exists.

It has no elapsed-time stop. It never reads report data or another worker's
workspace.

## Active-Time Behavior

The `active` mode parses the public report's embedded `report-data` JSON. For
each exact `run_id`, it selects the point with the greatest `wall_seconds` and
logs that active value plus `tokens_total`.

Below the target it removes premature completion markers when
`enforce_active_gate` is true. If the matching TUI appears idle, it sends a
throttled prompt containing only that worker's run ID, active time, token total,
and target. It does not include sibling metrics, code, or solution details.

At or above the target it creates `active_marker`. The worker remains
responsible for finishing its current safe operation, recording final exact run
usage with `scorebench run usage`, and exiting cleanly. A report fetch or parse
failure changes no worker state.

## Verification

Run one non-looping poll when checking a new setup:

```bash
python3 "$WATCHER" recovery --config "$CONFIG" --once
python3 "$WATCHER" active --config "$CONFIG" --once
```

Then inspect both watcher panes and the exact worker panes. Confirm that active
logs contain `active=...s tokens=...`, no message references an unassigned run,
and no stop decision uses elapsed time.
