#!/usr/bin/env bash
# Put the enrooted glass theme into a shadcn (Vite, base-nova) project.
# Usage: apply_theme.sh <project dir>      e.g. apply_theme.sh apps/my-ui
# Safe to re-run (also after `shadcn add`, which brings back text-destructive).
#   1. copies index.css (tokens, fonts, glass), brand SVGs and font files
#   2. copies agent-mark.tsx, theme-toggle.tsx and the trilogin spinner
#   3. renames text-destructive → text-warn-text in src/ (orange is never a text colour)
# It does not touch index.html, App.tsx or package.json: see SKILL.md step 3.
set -euo pipefail
DIR="$(cd "${1:?usage: apply_theme.sh <project dir>}" && pwd)"
T="$(cd "$(dirname "${BASH_SOURCE[0]}")/../templates" && pwd)"
[ -f "$DIR/components.json" ] || { echo "$DIR is not a shadcn project (no components.json)" >&2; exit 1; }
mkdir -p "$DIR/src/assets" "$DIR/src/components/ui"
cp "$T/index.css" "$DIR/src/index.css"
cp -r "$T/assets/brand" "$T/assets/fonts" "$DIR/src/assets/"
cp "$T/components/agent-mark.tsx" "$T/components/theme-toggle.tsx" "$DIR/src/components/"
cp "$T/components/ui/spinner.tsx" "$DIR/src/components/ui/spinner.tsx"
n=$(grep -rlE 'text-destructive([^-a-z]|$)' "$DIR/src" --include='*.tsx' --include='*.ts' | wc -l || true)
grep -rlE 'text-destructive([^-a-z]|$)' "$DIR/src" --include='*.tsx' --include='*.ts' \
  | xargs -r sed -i -E 's/text-destructive([^-a-z]|$)/text-warn-text\1/g'
echo "theme applied to $DIR ($n files had text-destructive renamed)"
echo "next: index.html theme script + favicon, ThemeProvider in App.tsx, npm install next-themes (SKILL.md step 3)"
