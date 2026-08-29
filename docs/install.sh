#!/bin/sh
# Subactor Shell standalone installer (macOS/Linux).
# Served at https://subactor.github.io/shell/install.sh

set -eu

if [ -z "${HOME:-}" ]; then
  HOME="$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f6 || echo "/tmp")"
  export HOME
fi

RELEASE="${SUBACTOR_SHELL_RELEASE:-latest}"
NON_INTERACTIVE="${SUBACTOR_SHELL_NON_INTERACTIVE:-false}"
SKIP_INIT="${SUBACTOR_SHELL_SKIP_INIT:-false}"
DEFAULT_PREFER_GITHUB_IO="true"
PREFER_GITHUB_IO="${SUBACTOR_SHELL_INSTALLER_USE_GITHUB_IO:-$DEFAULT_PREFER_GITHUB_IO}"
GITHUB_IO_BASE_URL="https://subactor.github.io/shell"
GITHUB_REPO="subactor/shell"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11
CONNECT_TIMEOUT=10
METADATA_TIMEOUT=30
ASSET_TIMEOUT=300

BIN_DIR="${SUBACTOR_SHELL_INSTALL_DIR:-$HOME/.local/bin}"
SHELL_HOME="${SUBACTOR_SHELL_HOME:-$HOME/.local/share/subactor-shell}"
VENV_DIR="$SHELL_HOME/venv"
BIN_PATH="$BIN_DIR/subactor-shell"
release_source="github"

step() {
  printf '==> %s\n' "$1"
}

warn() {
  printf 'WARNING: %s\n' "$1" >&2
}

normalize_version() {
  case "$1" in
    "" | latest) printf 'latest\n' ;;
    v*) printf '%s\n' "${1#v}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

validate_version() {
  version="$1"
  if [ "$version" = "latest" ]; then
    return 0
  fi
  if ! printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.+-]+)?$'; then
    echo "Invalid Subactor Shell release version: $version. Expected latest or x.y.z." >&2
    return 1
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --release)
        [ "$#" -ge 2 ] || { echo "--release requires a value." >&2; exit 1; }
        RELEASE="$2"
        shift
        ;;
      --help | -h)
        cat <<'EOF'
Subactor Shell installer

Usage:
  curl -fsSL https://subactor.github.io/shell/install.sh | sh
  curl -fsSL https://subactor.github.io/shell/install.sh | SUBACTOR_SHELL_RELEASE=0.2.2 sh

Environment:
  SUBACTOR_SHELL_RELEASE=latest|x.y.z
  SUBACTOR_SHELL_INSTALL_DIR=~/.local/bin
  SUBACTOR_SHELL_HOME=~/.local/share/subactor-shell
  SUBACTOR_SHELL_INSTALLER_USE_GITHUB_IO=true|false
  SUBACTOR_SHELL_NON_INTERACTIVE=true|false
  SUBACTOR_SHELL_SKIP_INIT=true|false
EOF
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        exit 1
        ;;
    esac
    shift
  done
}

download_file() {
  url="$1"
  output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout "$CONNECT_TIMEOUT" --max-time "$ASSET_TIMEOUT" "$url" -o "$output"
    return 0
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -q -t 1 -T "$ASSET_TIMEOUT" -O "$output" "$url"
    return 0
  fi
  echo "curl or wget is required to install Subactor Shell." >&2
  exit 1
}

download_text() {
  url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout "$CONNECT_TIMEOUT" --max-time "$METADATA_TIMEOUT" "$url"
    return 0
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -q -t 1 -T "$METADATA_TIMEOUT" -O - "$url"
    return 0
  fi
  echo "curl or wget is required to install Subactor Shell." >&2
  exit 1
}

sha256_file() {
  file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
    return 0
  fi
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
    return 0
  fi
  if command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$file" | awk '{print $NF}'
    return 0
  fi
  echo "sha256sum, shasum, or openssl is required." >&2
  exit 1
}

verify_digest() {
  file="$1"
  expected="$2"
  if [ -z "$expected" ]; then
    return 0
  fi
  actual="$(sha256_file "$file")"
  if [ "$actual" != "$expected" ]; then
    echo "Checksum mismatch for $(basename "$file")." >&2
    echo "Expected: $expected" >&2
    echo "Actual:   $actual" >&2
    return 1
  fi
}

json_get() {
  key="$1"
  python3 - "$key" <<'PY'
import json, sys
key = sys.argv[1]
data = json.load(sys.stdin)
cur = data
for part in key.split("."):
    if isinstance(cur, dict):
        cur = cur.get(part)
    else:
        cur = None
        break
if cur is None:
    sys.exit(1)
print(cur)
PY
}

resolve_from_github_io() {
  normalized_version="$1"
  if [ "$normalized_version" = "latest" ]; then
    metadata_url="$GITHUB_IO_BASE_URL/channels/latest.json"
  else
    metadata_url="$GITHUB_IO_BASE_URL/releases/$normalized_version/release.json"
  fi
  release_json="$(download_text "$metadata_url")" || return 1
  resolved_version="$(printf '%s' "$release_json" | json_get version)" || return 1
  wheel_name="$(printf '%s' "$release_json" | json_get assets.wheel.filename)" || return 1
  wheel_url="$(printf '%s' "$release_json" | json_get assets.wheel.url)" || return 1
  wheel_sha="$(printf '%s' "$release_json" | json_get assets.wheel.sha256 2>/dev/null || true)"
  release_source="github.io"
  export RESOLVED_VERSION="$resolved_version"
  export WHEEL_NAME="$wheel_name"
  export WHEEL_URL="$wheel_url"
  export WHEEL_SHA256="${wheel_sha:-}"
}

resolve_from_github() {
  normalized_version="$1"
  if [ "$normalized_version" = "latest" ]; then
    metadata_url="https://api.github.com/repos/$GITHUB_REPO/releases/latest"
  else
    metadata_url="https://api.github.com/repos/$GITHUB_REPO/releases/tags/v$normalized_version"
  fi
  release_json="$(download_text "$metadata_url")" || {
    echo "Could not fetch GitHub release metadata for Subactor Shell $normalized_version." >&2
    exit 1
  }
  tag_name="$(printf '%s' "$release_json" | json_get tag_name)" || {
    echo "GitHub release metadata is missing tag_name." >&2
    exit 1
  }
  resolved_version="${tag_name#v}"
  wheel_name="$(printf '%s' "$release_json" | python3 - <<'PY'
import json, sys
data = json.load(sys.stdin)
assets = data.get("assets") or []
for asset in assets:
    name = asset.get("name") or ""
    if name.startswith("subactor_shell-") and name.endswith(".whl"):
        print(name)
        break
else:
    sys.exit(1)
PY
)" || {
    echo "No wheel asset found in GitHub release $tag_name." >&2
    exit 1
  }
  wheel_url="$(printf '%s' "$release_json" | python3 - "$wheel_name" <<'PY'
import json, sys
target = sys.argv[1]
data = json.load(sys.stdin)
for asset in data.get("assets") or []:
    if asset.get("name") == target:
        print(asset.get("browser_download_url") or "")
        break
else:
    sys.exit(1)
PY
)"
  release_source="github"
  export RESOLVED_VERSION="$resolved_version"
  export WHEEL_NAME="$wheel_name"
  export WHEEL_URL="$wheel_url"
  export WHEEL_SHA256=""
}

resolve_release() {
  normalized_version="$(normalize_version "$RELEASE")"
  validate_version "$normalized_version"

  if [ "$(printf '%s' "$PREFER_GITHUB_IO" | tr '[:upper:]' '[:lower:]')" != "false" ] \
    && [ "$(printf '%s' "$PREFER_GITHUB_IO" | tr '[:upper:]' '[:lower:]')" != "0" ] \
    && [ "$(printf '%s' "$PREFER_GITHUB_IO" | tr '[:upper:]' '[:lower:]')" != "no" ]; then
    if resolve_from_github_io "$normalized_version"; then
      return 0
    fi
    warn "Could not resolve release from $GITHUB_IO_BASE_URL; falling back to GitHub Releases."
  fi
  resolve_from_github "$normalized_version"
}

find_python() {
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

python_version_ok() {
  python_bin="$1"
  "$python_bin" - <<'PY'
import sys
major, minor = sys.version_info[:2]
sys.exit(0 if (major, minor) >= (3, 11) else 1)
PY
}

ensure_python() {
  python_bin="$(find_python)" || {
    echo "Python 3.11+ is required. Install python3 and retry." >&2
    exit 1
  }
  if ! python_version_ok "$python_bin"; then
    version="$("$python_bin" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
    echo "Python 3.11+ is required (found $version via $python_bin)." >&2
    exit 1
  fi
  printf '%s\n' "$python_bin"
}

ensure_bin_dir() {
  mkdir -p "$BIN_DIR"
}

install_wheel() {
  python_bin="$1"
  wheel_path="$2"
  step "Creating isolated environment in $VENV_DIR"
  mkdir -p "$SHELL_HOME"
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$python_bin" -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -q --upgrade pip wheel
  "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -q --upgrade "$wheel_path"
  ln -sf "$VENV_DIR/bin/subactor-shell" "$BIN_PATH"
}

path_hint() {
  case ":$PATH:" in
    *:"$BIN_DIR":*) return 0 ;;
  esac
  warn "$BIN_DIR is not on PATH."
  printf 'Add this to your shell profile:\n  export PATH="%s:$PATH"\n' "$BIN_DIR"
}

run_init() {
  if [ "$SKIP_INIT" = "true" ]; then
    return 0
  fi
  if [ -x "$BIN_PATH" ]; then
    step "Initializing Subactor Shell"
    "$BIN_PATH" init || warn "subactor-shell init failed; you can rerun it manually."
  fi
}

main() {
  parse_args "$@"
  step "Resolving Subactor Shell release ($RELEASE)"
  resolve_release
  step "Resolved $RESOLVED_VERSION from $release_source ($WHEEL_NAME)"

  python_bin="$(ensure_python)"
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT INT TERM
  wheel_path="$tmp_dir/$WHEEL_NAME"
  step "Downloading $WHEEL_URL"
  download_file "$WHEEL_URL" "$wheel_path"
  verify_digest "$wheel_path" "$WHEEL_SHA256"

  ensure_bin_dir
  install_wheel "$python_bin" "$wheel_path"
  path_hint
  run_init

  step "Installed Subactor Shell $RESOLVED_VERSION to $BIN_PATH"
  printf '\nRun:\n  subactor-shell chat\n\n'
}

main "$@"
