#!/usr/bin/env bash
# Installs pstack (https://github.com/cursor/plugins/tree/main/pstack) skills and agents
# into this repo's .cursor/ and the user's ~/.cursor/ so every cloud agent has them.
# Idempotent; refreshes to the latest pstack on each environment install.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git clone -q --depth 1 --filter=blob:none --sparse https://github.com/cursor/plugins "$TMP/plugins"
git -C "$TMP/plugins" sparse-checkout set pstack >/dev/null
for dest in "$ROOT/.cursor" "$HOME/.cursor"; do
  mkdir -p "$dest/skills" "$dest/agents"
  cp -R "$TMP/plugins/pstack/skills/." "$dest/skills/"
  cp -R "$TMP/plugins/pstack/agents/." "$dest/agents/"
done
echo "pstack installed: $(ls "$TMP/plugins/pstack/skills" | wc -l) skills"
