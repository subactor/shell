#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
install -m 0755 "$ROOT/scripts/install/install.sh" "$ROOT/docs/install.sh"
install -m 0644 "$ROOT/scripts/install/install.ps1" "$ROOT/docs/install.ps1"
