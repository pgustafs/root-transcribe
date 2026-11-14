#!/usr/bin/env bash
# Refresh the skill's templates from the reference implementation (apps/chat-ui),
# which is the source of truth for the design. Run from ~/cuga-podman after
# changing the theme, the brand assets or the shared components there.
# app-shell.tsx is skill-only (a generic version of the chat page's shell) and is not touched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC="$ROOT/apps/chat-ui"
DST="$ROOT/.claude/skills/enrooted-glass-ui/templates"
[ -f "$SRC/src/index.css" ] || { echo "apps/chat-ui not found under $ROOT" >&2; exit 1; }
mkdir -p "$DST/components/ui" "$DST/assets"
cp "$SRC/src/index.css" "$DST/index.css"
cp "$SRC/index.html" "$DST/index.html"
sed -i -e 's|<title>.*</title>|<title>APP NAME · enrooted</title>|' -e 's|cuga-chat-theme|APP-theme|g' "$DST/index.html"
cp "$SRC/components.json" "$DST/components.json"
cp "$SRC/src/components/agent-mark.tsx" "$SRC/src/components/theme-toggle.tsx" "$DST/components/"
cp "$SRC/src/components/ui/spinner.tsx" "$DST/components/ui/spinner.tsx"
rm -rf "$DST/assets/brand" "$DST/assets/fonts"
cp -r "$SRC/src/assets/brand" "$SRC/src/assets/fonts" "$DST/assets/"
echo "templates refreshed from $SRC"
