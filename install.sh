#!/bin/sh

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_ROOT="$HOME/.local/share/codex-provider-sync"
BIN_DIR="$HOME/.local/bin"
PYTHON=${PYTHON:-python3}

"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "Python 3.10 or newer is required")'

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
"$PYTHON" -m venv "$INSTALL_ROOT/venv"
"$INSTALL_ROOT/venv/bin/python" -m pip install --disable-pip-version-check "$ROOT"

install -m 0755 "$ROOT/scripts/codex-switch" "$BIN_DIR/.codex-switch.new"
mv "$BIN_DIR/.codex-switch.new" "$BIN_DIR/codex-switch"

echo "Installed codex-switch to $BIN_DIR/codex-switch"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add $BIN_DIR to PATH before using codex-switch." ;;
esac
"$BIN_DIR/codex-switch" --version
