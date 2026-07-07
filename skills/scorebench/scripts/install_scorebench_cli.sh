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

base_url="${SCOREBENCH_URL:-${HARNESS_URL:-https://scorebench.dev/}}"
base_url="${base_url%/}"

fetch() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$url"
  else
    return 127
  fi
}

log "installing the ScoreBench CLI from $base_url/install.sh"
if fetch "$base_url/install.sh" | bash; then
  exit 0
fi
log "hosted installer failed; falling back to a local server checkout"

python_bin="${PYTHON:-python3}"
command -v "$python_bin" >/dev/null 2>&1 || fail "python3 is required to install the ScoreBench CLI"

is_harness_repo() {
  local candidate="$1"
  [[ -f "$candidate/pyproject.toml" && -d "$candidate/challenge_harness" ]]
}

repo="${HARNESS_REPO:-${SCOREBENCH_REPO:-}}"
if [[ -n "$repo" ]]; then
  repo="${repo/#\~/$HOME}"
  is_harness_repo "$repo" || fail "HARNESS_REPO is not a ScoreBench server checkout: $repo"
else
  candidates=(
    "$PWD"
    "$PWD/.."
    "$PWD/../harness"
    "$PWD/../scorebench"
    "$HOME/dev/harness"
    "$HOME/dev/scorebench"
    "$HOME/harness"
    "$HOME/scorebench"
  )
  for candidate in "${candidates[@]}"; do
    if is_harness_repo "$candidate"; then
      repo="$candidate"
      break
    fi
  done
fi

[[ -n "$repo" ]] || fail "no hosted installer and no server checkout found; set SCOREBENCH_URL to your deployment or HARNESS_REPO to a server checkout"

repo="$(cd "$repo" && pwd)"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
venv="${SCOREBENCH_CLI_VENV:-$data_home/scorebench/venv}"
bin_dir="${SCOREBENCH_INSTALL_BIN:-$HOME/.local/bin}"

log "installing ScoreBench CLI wrappers from $repo"
"$python_bin" -m venv "$venv"
"$venv/bin/python" -m pip install --quiet --upgrade pip
"$venv/bin/python" -m pip install --quiet "cryptography>=3.4" "PyYAML>=5.0"

mkdir -p "$bin_dir"
for name in scorebench harness; do
  cat >"$bin_dir/$name" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$repo:\${PYTHONPATH:-}"
exec "$venv/bin/python" -m challenge_harness.cli "\$@"
EOF
  chmod +x "$bin_dir/$name"
done
cat >"$bin_dir/harnessd" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$repo:\${PYTHONPATH:-}"
exec "$venv/bin/python" -m challenge_harness.daemon "\$@"
EOF
chmod +x "$bin_dir/harnessd"

"$bin_dir/scorebench" --help >/dev/null || fail "installed scorebench wrapper did not run successfully"
log "installed: $bin_dir/scorebench (legacy alias: $bin_dir/harness)"

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    log ""
    log 'Add this directory to PATH for the current shell:'
    log "  export PATH=\"$bin_dir:\$PATH\""
    ;;
esac

printf '%s\n' "$bin_dir/scorebench"
