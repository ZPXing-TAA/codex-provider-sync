#!/bin/sh

set -eu

rm -f "$HOME/.local/bin/codex-switch"
rm -rf "$HOME/.local/share/codex-provider-sync"
echo "Removed codex-switch. Existing recovery backups under ~/.codex were kept."
