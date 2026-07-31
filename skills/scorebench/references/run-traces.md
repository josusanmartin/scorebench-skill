# End-Of-Run Agent Traces

Use the bundled helper to attach an observable agent transcript to the exact
ScoreBench run without adding a background process.

## Contract

- `start` only discovers the current Codex or Claude JSONL and records its byte
  offset in local state.
- `finish` freezes that offset range, normalizes it, redacts secrets, applies
  size limits, writes deterministic gzip NDJSON, and uploads it.
- Upload uses `SCOREBENCH_URL` and the exact `SCOREBENCH_RUN_TOKEN`.
- Upload and trace reads are observational and must not create activity time.
- Trace failure never changes a candidate score, status, or completion.

This is an activity transcript, not hidden chain-of-thought. The helper drops
Codex reasoning ciphertext, Claude thinking/signature blocks, system/developer
messages, repeated token counters, duplicate patch events, binary payloads, and
other unsupported records.

## Start Boundary

After `scorebench run ping --event start` or `--event resume`:

```bash
SCOREBENCH_SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/scorebench"
test -f "$SCOREBENCH_SKILL_DIR/scripts/run_trace.py" || \
  SCOREBENCH_SKILL_DIR="$HOME/.claude/skills/scorebench"
SCOREBENCH_TRACE_HELPER="$SCOREBENCH_SKILL_DIR/scripts/run_trace.py"
python3 "$SCOREBENCH_TRACE_HELPER" start
```

Normally source discovery is automatic:

- Codex uses `CODEX_THREAD_ID` when available, then matches session metadata to
  the current workspace.
- Claude uses a session environment identifier when available, then matches
  project JSONL records to the current workspace.

For a runner-managed JSONL, pin it explicitly:

```bash
python3 "$SCOREBENCH_TRACE_HELPER" start \
  --provider codex \
  --source /absolute/path/to/session.jsonl
```

Do not place trace state inside a submitted solution directory. Automatic state
lives under `${XDG_STATE_HOME:-$HOME/.local/state}/scorebench/run-traces/`.

## Final Upload

After the final exact usage snapshot and finish ping:

```bash
SCOREBENCH_TRACE_HELPER="${CODEX_HOME:-$HOME/.codex}/skills/scorebench/scripts/run_trace.py"
test -f "$SCOREBENCH_TRACE_HELPER" || \
  SCOREBENCH_TRACE_HELPER="$HOME/.claude/skills/scorebench/scripts/run_trace.py"
python3 "$SCOREBENCH_TRACE_HELPER" finish
```

The default limits are 64 KiB per normalized event and 32 MiB of normalized
event data. Oversized events become hash-addressed compact records; once the
detailed budget is exhausted, the helper retains compact timeline entries and
records omitted counts in the manifest. The server rejects compressed uploads
larger than 50 MiB.

The gzip starts with a bounded manifest and contains only normalized NDJSON
events. It is stored outside candidate bundles, SQLite, and generated report
payloads.

To inspect the artifact before uploading:

```bash
python3 "$SCOREBENCH_TRACE_HELPER" finish --no-upload
gzip -cd /path/reported/by/the/helper.jsonl.gz | sed -n '1,20p'
```

Running `start` again for the same session preserves its original byte offset.
Running `finish` again reuses the frozen artifact and content-derived trace ID,
so retries are safe. On failure, keep the local artifact and report the exact
HTTP error. Do not paste raw session JSONL into notes, pings, submissions, or
chat.

## Security

The helper recursively removes known secret-bearing fields and redacts:

- run tokens and bearer authorization,
- connector/API keys, passwords, cookies, and credentials,
- common provider/GitHub token formats,
- secret values currently present in environment variables,
- embedded image/audio data.

Tool output can still contain unexpected sensitive material. Traces remain
run-scoped and should be reviewed before any later public export.
