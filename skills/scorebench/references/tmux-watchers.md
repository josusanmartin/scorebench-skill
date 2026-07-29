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

Use `scorebench run progress` field `progress.active_seconds` as the
active-time clock. Never stop a worker based on
`progress.elapsed_seconds`, tmux window age, container age, ordinary wall-clock
elapsed time, dashboard HTML, or `scorebench best`. The progress API uses the
same canonical idle-gap heuristic as reports and measures active time at the
latest submitted candidate, so it may lag work that has not yet been
submitted.

The active watcher creates an in-container marker after the target is reached.
Both that marker and `GOAL_COMPLETE` are monotonic evidence: the watcher never
deletes either file. If it observes a premature `GOAL_COMPLETE`, it logs
`premature_complete=1 action=preserved` for operator review and does not mutate
the worker. Put the marker contract in every worker goal:

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
  "docker_command": ["sudo", "-n", "docker"],
  "recovery_poll_seconds": 30,
  "active_poll_seconds": 120,
  "target_active_seconds": 14400,
  "nudge_seconds": 300,
  "resume_cooldown_seconds": 300,
  "completion_marker": "/work/GOAL_COMPLETE",
  "active_marker": "/work/SCOREBENCH_ACTIVE_TARGET_REACHED",
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

Every container must have a current `scorebench` CLI and its own scoped run
token. Verify this before launching the watcher:

```bash
docker exec sb-vliw-sol56-max-001 scorebench run progress
```

The returned `scope.run_id` and `progress.run_id` must both exactly match the
configured worker `run_id`. Refresh an old CLI with the skill bootstrap helper
and `SCOREBENCH_CLI_FORCE=1` before starting the watcher. The coordinator never
reads or stores worker run tokens; the command executes inside each container.

Legacy configs may still contain `report_url` and `enforce_active_gate`. They
remain accepted for compatibility but are not used. In particular,
`enforce_active_gate` never authorizes marker deletion.

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

For each worker, `active` mode runs `scorebench run progress` inside that exact
container. ScoreBench authenticates with the container's own run token and
returns active time, elapsed time, working tokens, their sources, and
measurement timestamps. The watcher rejects a response whose scope or run ID
does not exactly match the configured worker.

The watcher retains per-run high-water values for active time, elapsed time,
and tokens. A transient regression is logged and cannot reverse a target
decision. An existing `active_marker` remains authoritative across watcher
restarts.

Below the target, an idle worker without either marker receives a throttled
prompt containing only its own run ID, active time, token total, and target. A
premature `GOAL_COMPLETE` is preserved and reported rather than deleted. The
watcher does not include sibling metrics, code, or solution details.

At or above the target it creates `active_marker`. The worker remains
responsible for finishing its current safe operation, recording final exact run
usage with `scorebench run usage`, and exiting cleanly. A report fetch or parse
is not part of this workflow. A progress-command failure changes no worker
state and does not block sibling checks.

## Verification

Run one non-looping poll when checking a new setup:

```bash
python3 "$WATCHER" recovery --config "$CONFIG" --once
python3 "$WATCHER" active --config "$CONFIG" --once
```

Then inspect both watcher panes and the exact worker panes. Confirm that active
logs contain `active=...s tokens=...`, no message references an unassigned run,
no stop decision uses elapsed time, and each container's
`scorebench run progress` reports its exact configured run ID. Treat any
`premature_complete=1` or progress regression log as an operator-review event;
the watcher deliberately preserves the evidence.
