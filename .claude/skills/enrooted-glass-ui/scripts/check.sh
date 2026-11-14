#!/usr/bin/env bash
# Typecheck + lint a shadcn/Vite project in a node container (the host has no node).
# Usage: check.sh <project dir>     e.g. check.sh apps/chat-ui
# Prints nothing but CHECK_OK when both pass. Needs node_modules in the project
# (run `npm ci` once the same way if it's missing).
set -euo pipefail
DIR="$(cd "${1:?usage: check.sh <project dir>}" && pwd)"
podman run --rm -v "$DIR":/app:Z -w /app docker.io/library/node:22 \
  sh -c 'npx tsc -b && npx eslint . && echo CHECK_OK' 2>&1 | grep -v "npm notice"
