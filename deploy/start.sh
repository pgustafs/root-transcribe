#!/bin/sh
# Run from any directory. Local settings and generated credentials stay in deploy/.
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
action="${1:-start}"
manifest="${2:-deploy/root-transcribe.local.yaml}"
# Started with mac.sh: stop without naming the Mac manifest.
if [ "$action" = stop ] && [ "$#" -lt 2 ] && [ ! -f "$manifest" ] &&
  [ -f deploy/root-transcribe.mac.local.yaml ]; then
  manifest=deploy/root-transcribe.mac.local.yaml
fi
case "$action" in
  init)
    if [ ! -f deploy/settings.env ]; then
      (umask 077; cp deploy/settings.env.example deploy/settings.env)
    fi
    echo "Edit deploy/settings.env, then run ./deploy/start.sh check, build, and start."
    ;;
  check) exec python3 deploy/configure.py --check ;;
  build)
    podman build -f deploy/Containerfile -t localhost/root-transcribe:latest .
    podman build -f deploy/frontend.Containerfile -t localhost/root-transcribe-frontend:latest .
    # STT_BUILD_ARGS: extra build options, e.g. "--build-arg TORCH_BUILD=cpu" (see mac.sh).
    # shellcheck disable=SC2086
    podman build ${STT_BUILD_ARGS:-} -f deploy/stt.Containerfile -t localhost/root-transcribe-stt:latest .
    ;;
  start)
    if [ "$#" -lt 2 ]; then
      python3 deploy/configure.py
    else
      test -f "$manifest" || { echo "Manifest not found: $manifest" >&2; exit 1; }
    fi
    exec podman kube play "$manifest"
    ;;
  stop) exec podman kube down "$manifest" ;;
  status) exec podman ps -a --filter pod=root-transcribe ;;
  logs) exec podman logs --tail 100 -f "root-transcribe-${2:-backend}" ;;
  *) echo "Usage: $0 {init|check|build|start|stop|status|logs} [manifest or log container]" >&2; exit 2 ;;
esac
