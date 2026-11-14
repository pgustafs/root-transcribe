#!/bin/sh
# Root Transcribe on Podman Desktop (Apple silicon). One input: the
# OpenAI-compatible speech-to-text endpoint.
#
#   ./deploy/mac.sh http://my-stt-host:8002/v1
#
# Everything else is automatic: the Hugging Face token comes from $HF_TOKEN or
# the saved `hf auth login`, the model name is asked from the endpoint, and
# passwords are generated once into deploy/secrets.local.json.
# Optional environment: STT_API_KEY, STT_MODEL (skips detection), REBUILD=1.
# Stop:  ./deploy/start.sh stop
set -eu

usage() {
  echo "Usage: $0 <speech-to-text endpoint, e.g. http://my-stt-host:8002/v1>" >&2
  exit 2
}
fail() {
  printf '\nError: %s\n' "$1" >&2
  exit 1
}

[ "$#" -eq 1 ] || usage
case "$1" in
  http://*|https://*) stt_url=$1 ;;
  *) usage ;;
esac

# An endpoint on this Mac: check that it answers here before building anything.
case "$stt_url" in
  http://localhost*|http://127.0.0.1*|https://localhost*|https://127.0.0.1*) local_stt=true ;;
  *) local_stt=false ;;
esac
if [ "$local_stt" = true ]; then
  curl --silent --max-time 5 -o /dev/null "$stt_url" ||
    fail "Nothing answers at $stt_url on this Mac. Start the speech-to-text server
  and wait until it has finished loading the model, then run this again."
fi

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
manifest=deploy/root-transcribe.mac.local.yaml
app_url=http://localhost:8095
keycloak_url=http://localhost:8185

# 1. Podman must be installed, running and large enough for the speaker models.
command -v podman >/dev/null 2>&1 ||
  fail "podman was not found. Install Podman Desktop from https://podman-desktop.io and open it once."
host=$(podman info --format '{{.Host.MemTotal}} {{.Host.CPUs}}' 2>/dev/null) ||
  fail "Podman is not running. Start the Podman machine in Podman Desktop (or run: podman machine start)."
memory=${host% *}
cpus=${host#* }
resize="podman machine stop && podman machine set --memory 8192 --cpus 4 && podman machine start"
# A machine set to 8192 MiB reports slightly less than 8 GiB.
[ "$memory" -ge 7000000000 ] ||
  fail "The Podman machine has $((memory / 1048576)) MiB of memory; speaker detection needs 8 GB. Run:
  $resize"
[ "$cpus" -ge 4 ] ||
  echo "Note: the Podman machine has $cpus CPUs; 4 or more make transcription faster ($resize)."

# 2. Hugging Face token for the gated pyannote models: environment, then saved login.
if [ -z "${HF_TOKEN:-}" ]; then
  token_file="${HF_HOME:-$HOME/.cache/huggingface}/token"
  [ -f "$token_file" ] && HF_TOKEN=$(tr -d '[:space:]' <"$token_file")
fi
[ -n "${HF_TOKEN:-}" ] || fail "No Hugging Face token found. Do this once:
  1. Accept the terms at https://huggingface.co/pyannote/speaker-diarization-community-1
  2. Create a read token at https://huggingface.co/settings/tokens
  3. Save it:  mkdir -p ~/.cache/huggingface && printf %s 'hf_...' > ~/.cache/huggingface/token
     (or run: hf auth login, or export HF_TOKEN=hf_...)"
export HF_TOKEN

# 3. Build the application images once (REBUILD=1 after pulling new code).
images_ready=true
for image in root-transcribe root-transcribe-frontend root-transcribe-stt; do
  podman image exists "localhost/$image:latest" || images_ready=false
done
if [ "${REBUILD:-}" = 1 ] || [ "$images_ready" = false ]; then
  echo "Building the application images. The first build takes several minutes."
  # No NVIDIA GPU in Podman's machine: install the CPU-only PyTorch build.
  STT_BUILD_ARGS="--build-arg TORCH_BUILD=cpu" ./deploy/start.sh build
fi

# 4. Generate the manifest inside the backend image, so the Mac needs no Python
#    and the endpoint is checked from where the workers will call it.
podman run --rm --user 0 --security-opt label=disable --entrypoint python3 \
  -e HF_TOKEN -e STT_API_KEY="${STT_API_KEY:-}" -e STT_MODEL="${STT_MODEL:-}" \
  -v "$repo_dir/deploy:/deploy" localhost/root-transcribe:latest \
  /deploy/configure.py --stt-url "$stt_url" --arch arm64 ||
  {
    if [ "$local_stt" = true ]; then
      printf '%s\n' "" \
        "If the error above says the endpoint cannot be reached: it answers on this" \
        "Mac but not from the containers. Start the speech-to-text server with" \
        "--host 0.0.0.0 instead of 127.0.0.1 and run this again. If that is not" \
        "enough, pass the Mac's network address instead of" \
        "localhost:  $0 http://\$(ipconfig getifaddr en0):<port>/v1" >&2
    fi
    fail "Could not generate the configuration (details above)."
  }

# 5. Start, replacing a previous run. Data volumes are kept.
podman kube play --replace "$manifest" >/dev/null ||
  fail "Podman could not start the pod (details above)."

# 6. Wait until sign-in and the app answer.
echo "Starting Root Transcribe. The first start downloads images and can take a few minutes."
deadline=$(($(date +%s) + 600))
until curl --fail --silent --max-time 3 -o /dev/null "$keycloak_url/realms/root-transcribe" &&
  curl --fail --silent --max-time 3 -o /dev/null "$app_url/health"; do
  [ "$(date +%s)" -lt "$deadline" ] ||
    fail "The app did not become ready within 10 minutes. Check: ./deploy/start.sh status
  and ./deploy/start.sh logs backend   (also: keycloak, stt-worker, frontend)"
  sleep 5
done

cat <<EOF

Root Transcribe is running: $app_url
  Sign in as:        admin@example.com
  Initial password:  KEYCLOAK_APP_ADMIN_PASSWORD in deploy/secrets.local.json
                     (you are asked to change it at first sign-in)
  Stop:              ./deploy/start.sh stop
  Logs:              ./deploy/start.sh logs backend

The first transcription downloads the speaker models and takes longer.
EOF
if [ "$(uname -s)" = Darwin ]; then open "$app_url"; fi
