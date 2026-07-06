#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '%s\n' "$*" >&2
}

fail() {
  log "error: $*"
  exit 1
}

if command -v harness >/dev/null 2>&1; then
  log "harness already available: $(command -v harness)"
  harness --help >/dev/null
  exit 0
fi

python_bin="${PYTHON:-python3}"
command -v "$python_bin" >/dev/null 2>&1 || fail "python3 is required to install the harness CLI"
command -v git >/dev/null 2>&1 || fail "git is required to locate or clone the Harness server repository"

is_harness_repo() {
  local candidate="$1"
  [[ -f "$candidate/pyproject.toml" && -d "$candidate/challenge_harness" ]]
}

repo="${HARNESS_REPO:-}"
if [[ -n "$repo" ]]; then
  repo="${repo/#\~/$HOME}"
  is_harness_repo "$repo" || fail "HARNESS_REPO is not a Harness server checkout: $repo"
else
  candidates=(
    "$PWD"
    "$PWD/.."
    "$PWD/../harness"
    "$HOME/dev/harness"
    "$HOME/harness"
    "/home/josu/dev/harness"
  )
  for candidate in "${candidates[@]}"; do
    if is_harness_repo "$candidate"; then
      repo="$candidate"
      break
    fi
  done
fi

if [[ -z "$repo" ]]; then
  data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
  repo="${HARNESS_INSTALL_REPO:-$data_home/harness/server}"
  if [[ -d "$repo/.git" ]]; then
    log "using existing cloned Harness server repo: $repo"
  else
    mkdir -p "$(dirname "$repo")"
    log "cloning Harness server repo into $repo"
    git clone https://github.com/josusanmartin/harness.git "$repo" || {
      cat >&2 <<'EOF'
Could not clone the Harness server repository.

If this is a private repository, authenticate git first or provide an existing
checkout explicitly:

  export HARNESS_REPO=/path/to/harness-server-checkout
  bash ~/.codex/skills/scorebench/scripts/install_harness_cli.sh
EOF
      exit 1
    }
  fi
fi

repo="$(cd "$repo" && pwd)"
is_harness_repo "$repo" || fail "not a Harness server checkout after resolution: $repo"

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
venv="${HARNESS_CLI_VENV:-$data_home/harness/venv}"
bin_dir="${HARNESS_INSTALL_BIN:-$HOME/.local/bin}"

log "installing Harness CLI wrappers from $repo"
log "venv: $venv"
"$python_bin" -m venv "$venv"
"$venv/bin/python" -m pip install --upgrade pip >/dev/null
"$venv/bin/python" -m pip install "cryptography>=3.4" "PyYAML>=5.0" >/dev/null

mkdir -p "$bin_dir"
cat >"$bin_dir/harness" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$repo:\${PYTHONPATH:-}"
exec "$venv/bin/python" -m challenge_harness.cli "\$@"
EOF
cat >"$bin_dir/harnessd" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$repo:\${PYTHONPATH:-}"
exec "$venv/bin/python" -m challenge_harness.daemon "\$@"
EOF
cat >"$bin_dir/expctl" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$repo:\${PYTHONPATH:-}"
exec "$venv/bin/python" -m challenge_harness.expctl "\$@"
EOF
chmod +x "$bin_dir/harness" "$bin_dir/harnessd" "$bin_dir/expctl"

if "$bin_dir/harness" --help >/dev/null; then
  log "installed harness CLI: $bin_dir/harness"
else
  fail "installed harness wrapper did not run successfully"
fi

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    cat >&2 <<EOF

Add this directory to PATH for the current shell:

  export PATH="$bin_dir:\$PATH"

EOF
    ;;
esac

printf '%s\n' "$bin_dir/harness"
