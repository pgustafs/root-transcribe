#!/usr/bin/env bash
# Build a UI image and put its static files into the already-running nginx
# container, without re-creating the pod (kube play --replace would restart the
# agent and cut off running conversations).
# Usage: hot_deploy.sh <project dir> <image> <running container>
#   e.g. hot_deploy.sh apps/chat-ui localhost/cuga-chat-ui:latest cuga-chat-ui
# Only for changes under src/, index.html or assets. nginx.conf or manifest
# changes need the pod re-created.
set -euo pipefail
DIR="${1:?project dir}"; IMAGE="${2:?image}"; CONTAINER="${3:?running container}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
podman build -q -t "$IMAGE" "$DIR" >/dev/null
c="$(podman create "$IMAGE")"
podman cp "$c:/usr/share/nginx/html" "$TMP/html"
podman rm "$c" >/dev/null
podman cp "$TMP/html/." "$CONTAINER:/usr/share/nginx/html/"
echo "deployed $IMAGE into $CONTAINER (reload the browser tab)"
