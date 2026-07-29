#!/usr/bin/env bash
# Install the ScoreBench CLI (`scorebench`, with a legacy `harness` alias).
#
# Preferred path: download the current CLI from the ScoreBench deployment.
# Fallback for offline development: install a current local server/CLI checkout.
set -euo pipefail

log() {
  printf '%s\n' "$*" >&2
}

fail() {
  log "error: $*"
  exit 1
}

FORCE="${SCOREBENCH_CLI_FORCE:-0}"
if [[ "$FORCE" != "1" ]] && command -v scorebench >/dev/null 2>&1; then
  log "scorebench already available: $(command -v scorebench)"
  scorebench --help >/dev/null
  exit 0
fi
if [[ "$FORCE" != "1" ]] && command -v harness >/dev/null 2>&1; then
  log "legacy harness CLI already available: $(command -v harness)"
  harness --help >/dev/null
  exit 0
fi

VENV_DIR="${SCOREBENCH_CLI_VENV:-$HOME/.local/share/scorebench-cli}"
BIN_DIR="${SCOREBENCH_INSTALL_BIN:-${SCOREBENCH_BIN_DIR:-$HOME/.local/bin}}"
BASE_URL="${SCOREBENCH_URL:-${HARNESS_URL:-https://scorebench.dev/}}"
INSTALL_URL="${SCOREBENCH_INSTALL_URL:-${BASE_URL%/}/install.sh}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if command -v curl >/dev/null 2>&1; then
  if curl -fsSL "$INSTALL_URL" -o "$TMP_DIR/install.sh"; then
    log "installing the current ScoreBench CLI from $INSTALL_URL"
    SCOREBENCH_INSTALL_BIN="$BIN_DIR" bash "$TMP_DIR/install.sh"
    "$BIN_DIR/scorebench" --help >/dev/null
    exit 0
  fi
elif command -v wget >/dev/null 2>&1; then
  if wget -qO "$TMP_DIR/install.sh" "$INSTALL_URL"; then
    log "installing the current ScoreBench CLI from $INSTALL_URL"
    SCOREBENCH_INSTALL_BIN="$BIN_DIR" bash "$TMP_DIR/install.sh"
    "$BIN_DIR/scorebench" --help >/dev/null
    exit 0
  fi
else
  log "curl/wget unavailable; trying an explicit local checkout"
fi

local_repo="${SCOREBENCH_CLI_CHECKOUT:-${HARNESS_REPO:-}}"
[[ -n "$local_repo" && -e "$local_repo" ]] || fail \
  "could not download $INSTALL_URL and no SCOREBENCH_CLI_CHECKOUT/HARNESS_REPO fallback exists"

log "installing the ScoreBench CLI from local checkout $local_repo"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
python3 -m venv "$VENV_DIR" || fail "could not create venv at $VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip || fail "could not upgrade pip in $VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet "$local_repo" || fail "could not install the CLI from $local_repo"

mkdir -p "$BIN_DIR"
for name in scorebench harness; do
  ln -sf "$VENV_DIR/bin/$name" "$BIN_DIR/$name"
done
log "installed: $BIN_DIR/scorebench (and legacy alias $BIN_DIR/harness)"
"$BIN_DIR/scorebench" --help >/dev/null 2>&1 || "$BIN_DIR/scorebench" >/dev/null 2>&1 || true
log "done"
