#!/usr/bin/env bash
# Install the ScoreBench CLI (`scorebench`, with a legacy `harness` alias).
#
# Preferred path: download the hosted installer from the ScoreBench deployment
# (no repository access needed). Fallback for private/local development: build
# wrappers from a server repo checkout pointed at by HARNESS_REPO.
set -euo pipefail

log() {
  printf '%s\n' "$*" >&2
}

fail() {
  log "error: $*"
  exit 1
}

if command -v scorebench >/dev/null 2>&1; then
  log "scorebench already available: $(command -v scorebench)"
  scorebench --help >/dev/null
  exit 0
fi
if command -v harness >/dev/null 2>&1; then
  log "legacy harness CLI already available: $(command -v harness)"
  harness --help >/dev/null
  exit 0
fi

CLI_REPO="${SCOREBENCH_CLI_REPO:-https://github.com/josusanmartin/scorebench-cli}"
VENV_DIR="${SCOREBENCH_CLI_VENV:-$HOME/.local/share/scorebench-cli}"
BIN_DIR="${SCOREBENCH_BIN_DIR:-$HOME/.local/bin}"

log "installing scorebench-cli from $CLI_REPO"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
python3 -m venv "$VENV_DIR" || fail "could not create venv at $VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip || fail "could not upgrade pip in $VENV_DIR"
if ! "$VENV_DIR/bin/pip" install --quiet "git+$CLI_REPO"; then
  # Offline/dev fallback: install from a local checkout of the public CLI or
  # a ScoreBench server checkout that still vendors the CLI modules.
  local_repo="${SCOREBENCH_CLI_CHECKOUT:-${HARNESS_REPO:-}}"
  [[ -n "$local_repo" && -e "$local_repo" ]] || fail "could not install from $CLI_REPO and no SCOREBENCH_CLI_CHECKOUT/HARNESS_REPO fallback exists"
  "$VENV_DIR/bin/pip" install --quiet "$local_repo" || fail "could not install the CLI from $local_repo"
fi

mkdir -p "$BIN_DIR"
for name in scorebench harness; do
  ln -sf "$VENV_DIR/bin/$name" "$BIN_DIR/$name"
done
log "installed: $BIN_DIR/scorebench (and legacy alias $BIN_DIR/harness)"
"$BIN_DIR/scorebench" --help >/dev/null 2>&1 || "$BIN_DIR/scorebench" >/dev/null 2>&1 || true
log "done"
