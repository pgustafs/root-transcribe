# Root Transcribe

A transcription and speaker diarization application built with FastAPI,
React/shadcn/ui, PostgreSQL, Redis/Celery, Keycloak and SeaweedFS audio storage.
Record or upload audio, review speaker-attributed transcripts and export the
results as documents or subtitles.

## Documentation

- [Architecture](ARCHITECTURE.md): components, processing flow, persistence, permissions and operational limits.
- [Backend development](backend/README.md) and [frontend development](frontend/README.md).
- [Deferred Nemotron research](Nemotron.md): a future integration study, not an installed feature.

## Included

- Workspace membership and owner/manager/editor/viewer permissions.
- Browser microphone recording, audio uploads, resumable multipart uploads and
  recovery of locally stored recordings.
- Remote speech-to-text, pyannote diarization, timestamp alignment, encrypted
  speaker voiceprints, speaker identification and uncertain-match review.
- Audio playback, transcript editing, revisions, approval and exports in
  Markdown, text, SRT, VTT, DOCX and PDF.
- Transcript comments, audio clips, sensitive-data review and redaction, consent,
  audio retention, legal holds, speaker erasure and data-subject exports.
- Meeting notes written by agents from the transcript (summary, task list and your
  own), editable in the app and exportable as Markdown, Word and PDF. Optional:
  needs a second, text model. See [Meeting notes](#documents-written-by-agents).
- Keycloak sign-in, local account suspension, notifications and worker job status.
- English and Swedish interfaces, light/dark themes, structured logs and metrics.
- Auto / English / Swedish recording language, preserved through recovery and retries.

## Prerequisites

For local development, use Python 3.12+, uv, Node.js 24+, PostgreSQL 16 and Redis 7.
The Podman deployment bundles the application runtimes, PostgreSQL, Redis and SeaweedFS;
its host needs Podman and Python 3 to generate configuration.

**Transcription requires a running vLLM instance**, or another compatible server
providing `/v1/audio/transcriptions`. For example, serve
[RedHatAI/whisper-large-v3-quantized.w4a16](https://huggingface.co/RedHatAI/whisper-large-v3-quantized.w4a16),
an INT4 version of Whisper large-v3 with documented vLLM support. Follow the
[Podman serving guide below](#serving-whisper-with-vllm-and-podman) before starting
the app. The Podman manifest includes SeaweedFS and creates the audio bucket
automatically. Local development outside Podman needs a reachable S3-compatible
store and an existing bucket configured in `backend/.env`.
The dedicated worker needs a Hugging Face token with access to the configured
pyannote models; accept their model terms in the token owner's account.
For the default diarization model, sign in and accept the conditions on
[pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1).
Creating a token or downloading Whisper does not grant access to this gated model.
The token must have read access to it; a fine-grained token must include the
required repository access.

**Meeting notes are optional and need a second model**: an OpenAI-compatible
LLM server (for example another vLLM instance) reachable from the containers,
with a context window of 32,000 tokens or more. Without one the app runs as a
transcription tool. See [Meeting notes](#documents-written-by-agents).

Speech-to-text is remote. The application does not start a Whisper server.
Diarization and speaker embeddings run in the dedicated transcription worker.
The supplied app manifest gives that worker no GPU device, so it uses CPU;
the separate vLLM container below uses the NVIDIA GPU. See
[deployment boundaries](ARCHITECTURE.md#components-and-deployment-boundaries).

## Serving Whisper with vLLM and Podman

This example runs the model server separately on the same Linux NVIDIA host as
the app. Run commands from this repository's root, in the same terminal. It uses
the latest official vLLM image at build time, with audio dependencies added.
The app's existing API, queue and diarization architecture stays the same.
This is the manual serving path. The [prepared-manifest workflow](#prepared-manifest-workflow)
can automate the download, build and initial launch instead; choose one startup path.

### 1. Check NVIDIA access

Install a working NVIDIA driver, Podman, uv and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
for your distribution. Check the host and its CDI devices:

```sh
nvidia-smi
nvidia-ctk cdi list
```

The device list must include `nvidia.com/gpu=all`. Toolkit 1.18+ normally maintains
this automatically. If it is missing, inspect or restart
`nvidia-cdi-refresh.service`; on older installations follow NVIDIA's manual
generation instructions. Check container access too:

```sh
podman run --rm --device nvidia.com/gpu=all \
  --security-opt=label=disable docker.io/library/ubuntu:24.04 nvidia-smi -L
```

This follows [NVIDIA's Podman/CDI instructions](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html).
The host driver must also support the CUDA version in the image you pull.
Current CUDA 13 images require R580 or newer for normal operation; do not assume
the older-driver compatibility mode supports a GeForce card. See
[vLLM's container requirements](https://docs.vllm.ai/en/latest/deployment/docker/#running-on-systems-with-older-cuda-drivers).

### 2. Download the model

Keep model files outside the source tree and application build context:

```sh
TRANSCRIBE_VLLM_DIR="$HOME/.local/share/root-transcribe/vllm"
mkdir -p "$TRANSCRIBE_VLLM_DIR/model"
uvx --from huggingface_hub hf download \
  RedHatAI/whisper-large-v3-quantized.w4a16 \
  --local-dir "$TRANSCRIBE_VLLM_DIR/model"
```

Download the complete repository, including tokenizer and processor files.
The [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/guides/cli#download-an-entire-repository)
reuses files on subsequent downloads. If authentication is requested, run
`uvx --from huggingface_hub hf auth login` first. This is separate from accepting
the pyannote model terms for the app's diarization worker.

### 3. Build the latest vLLM image with audio support

```sh
podman pull docker.io/vllm/vllm-openai:latest
podman build --pull=always -f deploy/vllm.Containerfile \
  -t localhost/root-transcribe-vllm:latest .
```

[deploy/vllm.Containerfile](deploy/vllm.Containerfile) adds `vllm[audio]` at exactly
the version already installed in the base image, following
[vLLM's container guidance](https://docs.vllm.ai/en/latest/deployment/docker/).
`latest` changes over time. Record the base image digest after a successful demo
if you need to reproduce that environment:

```sh
podman image inspect docker.io/vllm/vllm-openai:latest --format '{{.Digest}}'
```

### 4. Create an API key and start serving

Create the key once and preserve it across restarts:

```sh
if [ ! -f "$TRANSCRIBE_VLLM_DIR/api-key" ]; then
  (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
    > "$TRANSCRIBE_VLLM_DIR/api-key")
fi
VLLM_API_KEY="$(cat "$TRANSCRIBE_VLLM_DIR/api-key")"
export VLLM_API_KEY

podman run -d --name root-transcribe-vllm \
  --device nvidia.com/gpu=all \
  --security-opt=label=disable \
  --shm-size=1g \
  -p 8002:8000 \
  -v "$TRANSCRIBE_VLLM_DIR/model:/models/whisper:ro" \
  --env VLLM_API_KEY \
  localhost/root-transcribe-vllm:latest \
  /models/whisper \
  --served-model-name whisper-large-v3-w4a16 \
  --host 0.0.0.0 --port 8000 \
  --dtype half \
  --max-model-len 448 \
  --max-num-batched-tokens 2048 \
  --max-num-seqs 1 \
  --gpu-memory-utilization 0.5

podman logs -f root-transcribe-vllm
```

Wait for startup to complete; Ctrl+C leaves the detached server running.
The model's quantization is read from its configuration. The single-request
limit and 50% GPU memory budget are starting values for a local demo on the
16 GB host, not measured throughput or memory guarantees. Check startup logs
and available VRAM before increasing them.

Keep the batch token budget separate from Whisper's 448-token decoder limit.
Whisper's audio encoder needs 1,500 tokens per input; the explicit 2,048 budget
avoids vLLM rejecting startup when its batch default follows the decoder limit.
See vLLM's [scheduler settings](https://docs.vllm.ai/en/stable/cli/run-batch/#--max-num-batched-tokens).

Port 8002 is published on all host interfaces so the separate app pod can reach
it. Keep it on a trusted network or restrict it with the host firewall; use HTTPS
if sending recordings over another network. `VLLM_API_KEY` protects inference
requests, but does not authenticate every management endpoint. See
[vLLM's authentication scope](https://docs.vllm.ai/en/latest/usage/security/).

For later restarts, use `podman stop root-transcribe-vllm` and
`podman start root-transcribe-vllm`. To change arguments or use a rebuilt image,
stop and remove only that container, then repeat the run command. The downloaded
model and key stay in the host directory. In a new terminal, restore
`TRANSCRIBE_VLLM_DIR` and `VLLM_API_KEY` using the assignments above.

### 5. Test an actual transcription

Check the served model name, then replace the audio path with a real short WAV
recording containing speech:

```sh
curl --noproxy '*' --fail-with-body http://127.0.0.1:8002/v1/models \
  -H "Authorization: Bearer $VLLM_API_KEY"

curl --noproxy '*' --fail-with-body http://127.0.0.1:8002/v1/audio/transcriptions \
  -H "Authorization: Bearer $VLLM_API_KEY" \
  -F model=whisper-large-v3-w4a16 \
  -F file=@/absolute/path/to/real-recording.wav \
  -F response_format=json \
  -F language=en
```

Use `language=sv` for Swedish or omit the language field for automatic detection.
The second response should contain recognized text. These are vLLM's
[speech-to-text endpoints](https://docs.vllm.ai/en/latest/serving/online_serving/speech_to_text/).
A successful model listing alone does not prove that audio inference works.
This test covers transcription; speaker separation is tested through the app.

### 6. Add the service to the app manifest configuration

Initialize the app settings if needed:

```sh
./deploy/start.sh init
```

Edit `deploy/settings.env` and set these three values, copying the contents of
`$HOME/.local/share/root-transcribe/vllm/api-key` into `STT_API_KEY`:

```dotenv
STT_BASE_URL=http://host.containers.internal:8002/v1
STT_MODEL=whisper-large-v3-w4a16
STT_API_KEY=PASTE_THE_GENERATED_API_KEY_HERE
```

Use the literal key: this file does not expand `$VLLM_API_KEY`. The model value
must match `--served-model-name`, including spelling. Keep `/v1` in the base URL;
do not append `/audio/transcriptions`. `localhost` inside the app pod refers to
the app pod, so use `host.containers.internal` for this host-published service.
For a server on another machine, use its reachable HTTPS base URL instead.

Set `HF_TOKEN`; SeaweedFS storage credentials are generated automatically. If the app is already running, let
active jobs finish and run `./deploy/start.sh stop` before regenerating its
manifest. Then validate and generate:

```sh
./deploy/start.sh check
python3 deploy/configure.py
```

The generator writes `deploy/root-transcribe.local.yaml` with this wiring:

| Generated resource | Setting | Value from this guide |
| --- | --- | --- |
| ConfigMap `transcribe-app-config` | `STT_BASE_URL` | `http://host.containers.internal:8002/v1` |
| ConfigMap `transcribe-app-config` | `STT_MODEL` | `whisper-large-v3-w4a16` |
| Secret `transcribe-app-secret` | `STT_API_KEY` | Your generated API key |

The transcription worker already imports these resources through `envFrom`.
This connects the external vLLM container to the app; it does not add a second
model server inside the app pod. For a manually maintained manifest, update the
same entries while preserving its other settings and secrets. For the generated
manifest, always edit `settings.env` because generation overwrites manual edits.

Build and start the app as described below. If you stopped an existing app,
run `./deploy/start.sh start` to apply changed settings. Keep
`deploy/secrets.local.json` and the persistent volumes.

After startup, verify connectivity from the actual worker as well:

```sh
podman exec root-transcribe-stt-worker python -c '
import os, urllib.request
url = os.environ["STT_BASE_URL"].rstrip("/") + "/models"
request = urllib.request.Request(url, headers={
    "Authorization": "Bearer " + os.environ["STT_API_KEY"]
})
with urllib.request.urlopen(request, timeout=10) as response:
    print(response.status, response.read().decode())
'
```

If the host curl succeeds but this fails, check Podman host-name resolution,
the published port and firewall access from containers. Finally upload a real
two-speaker recording in the app to verify STT, S3 and diarization together.
The local deployment has been exercised with vLLM 0.33.0, the quantized Whisper
model and real recordings on an RTX 4070 Ti SUPER (16 GB). English language hints
were also verified on full-file and sliced requests. This is a tested configuration,
not a throughput benchmark or a guarantee for future `latest` images.

## Local development

1. Configure Keycloak using `deploy/keycloak/root-transcribe-realm.json`.
   Set `KEYCLOAK_APP_ADMIN_EMAIL` and `KEYCLOAK_APP_ADMIN_PASSWORD` when importing
   the realm. The initial administrator must change their password at first login.
2. Start PostgreSQL, Redis, the speech-to-text endpoint and the S3 store.
3. Prepare the API:

   ```sh
   cd backend
   cp .env.example .env
   # Edit .env with your database, identity, S3, STT and Hugging Face settings.
   uv sync --frozen --extra dev
   uv run --no-sync alembic upgrade head
   uv run --no-sync fastapi dev app/main.py
   ```

4. In another terminal, start the general worker and one scheduler:

   ```sh
   cd backend
   uv run --no-sync celery -A app.worker.celery_app worker --hostname=main@%h -Q celery -l info
   # Run this separately, once per deployment:
   uv run --no-sync celery -A app.worker.celery_app beat -l info -s /tmp/transcribe-beat
   ```

5. Start the transcription worker. For local development, install its extra in
   the backend environment. Container deployment keeps these heavy dependencies
   in a separate image.

   ```sh
   cd backend
   uv sync --frozen --extra dev --extra stt
   uv run --no-sync celery -A app.worker.celery_app worker \
     --hostname=stt@%h -Q transcription --concurrency=1 -l info
   ```

6. Start the frontend:

   ```sh
   cd frontend
   npm ci
   npm run dev
   ```

Open `http://localhost:3000`. The frontend proxies `/api` to port 8000. Sign in,
create a workspace, and select **Record audio** or **Upload audio**. Use **Members**
to share the workspace. The API documentation is at `http://localhost:8000/docs`.

The UI reports a degraded service until a worker consumes the `transcription`
queue. The worker downloads models lazily on the first job. Queue readiness
does not prove model access or STT/S3 connectivity.

## Run on a Mac with Podman Desktop

On an Apple silicon Mac the whole app starts with one command and one input:
the address of an OpenAI-compatible speech-to-text server.

```sh
./deploy/mac.sh http://my-stt-host:8002/v1
```

To also get [meeting notes](#documents-written-by-agents), add the address of
an OpenAI-compatible LLM server as a second input:

```sh
./deploy/mac.sh http://my-stt-host:8002/v1 http://my-llm-host:8001/v1
```

Before the first run:

1. Install [Podman Desktop](https://podman-desktop.io) and give its machine
   8 GB of memory and 4 CPUs (Settings → Resources, or
   `podman machine stop && podman machine set --memory 8192 --cpus 4 && podman machine start`).
2. Accept the terms of
   [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
   and save a Hugging Face read token: run `hf auth login`, or write the token
   to `~/.cache/huggingface/token`, or export `HF_TOKEN`. The script finds it
   by itself; it is not a parameter.
3. Keep this repository under your home folder, which Podman shares with its machine.

The script checks Podman, builds the three images (several minutes the
first time), asks the server which model it serves, generates passwords into
`deploy/secrets.local.json`, starts the pod and opens **http://localhost:8095**.
Sign in as `admin@example.com` with `KEYCLOAK_APP_ADMIN_PASSWORD` from that file.
The pod and its volumes also appear in Podman Desktop.

- A server on the Mac itself can be given as `http://localhost:PORT/v1`; it is
  reached from the containers as `host.containers.internal`.
- Optional environment variables: `STT_API_KEY` if the server needs a key,
  `STT_MODEL` if it serves several models, `REBUILD=1` after pulling new code.
- Stop with `./deploy/start.sh stop`;
  data is kept. Logs: `./deploy/start.sh logs backend` (or `stt-worker`, `keycloak`).
- Speaker detection runs on the CPU inside Podman's Linux machine, so long
  recordings take longer than on a GPU host. The Mac build therefore installs
  PyTorch's CPU-only packages (`TORCH_BUILD=cpu` in `deploy/stt.Containerfile`),
  which keeps the worker image at about 5 GB instead of 17 GB.
- The Mac manifest uses the multi-architecture `postgres:16` and `redis:7`
  images in place of the amd64-only ones in `deploy/root-transcribe.yaml`.
  Its data volumes are therefore not interchangeable with a Linux deployment's.

## Local demo with Podman

The app uses real audio only. Bring an OpenAI-compatible STT service and a
Hugging Face token with access to the configured
pyannote models. Accept the model terms before the first transcription.
SeaweedFS is deployed with the app; no separate S3 installation is needed.

```sh
./deploy/start.sh init
# Edit deploy/settings.env: STT endpoint/model/key, HF_TOKEN and admin email.
./deploy/start.sh check
./deploy/start.sh build
./deploy/start.sh start
```

Open **http://localhost:8095**. Sign in using `KEYCLOAK_APP_ADMIN_EMAIL` from
`deploy/settings.env` and `KEYCLOAK_APP_ADMIN_PASSWORD` from
`deploy/secrets.local.json`. First sign-in asks you to change the password.
After changing it, use the new password: the secret file contains the initial
bootstrap password and is not updated by Keycloak password changes. The bootstrap
Keycloak admin password is a separate credential, not the app login.

The small settings file is the normal configuration surface. Model thresholds,
queue limits, database pool sizes and other internal defaults stay in
[backend configuration](backend/app/core/config.py). For an advanced deployment,
edit a separate local manifest and pass its path to `start.sh start` and `stop`.
Do not edit the generated default manifest: the next start regenerates it.

### Prepared-manifest workflow

Use this alternative for an initial deployment when you want to add the Hugging
Face token directly to the manifest. First run `./deploy/start.sh init` and set
`STT_BASE_URL`, `STT_MODEL` and `STT_API_KEY` in `deploy/settings.env` as shown in
the serving guide. The launcher requires exactly the local endpoint and
`whisper-large-v3-w4a16` alias, plus a nonempty API key. The example settings file's
`whisper-large-v3-turbo` default must be changed for this model. If the vLLM API-key
file already exists, use that same key.

Prepare the manifest while `HF_TOKEN` is still empty:

```sh
python3 deploy/configure.py --prepare
```

This creates `deploy/root-transcribe.prepared.local.yaml` with private permissions
and allows `HF_TOKEN` to be empty. Set its `"HF_TOKEN": ""` value in the
`transcribe-app-secret` resource's `stringData` section. Preparation refuses to
overwrite an existing file, preserving manual edits and credentials.

For an initial deployment using the local NVIDIA/vLLM setup above, run these
commands **on the host**, after adding the token:

```sh
python3 deploy/launch_prepared.py --check
python3 deploy/launch_prepared.py
```

`--check` validates the manifest only. The actual launcher checks Podman and
NVIDIA access, downloads the Whisper model, builds vLLM with audio support and
the three app images, starts vLLM, then starts
the exact prepared manifest. It uses the manifest's `STT_API_KEY` for vLLM too
and saves it privately at `$HOME/.local/share/root-transcribe/vllm/api-key`.
It waits for the model listing, API, sign-in discovery and frontend to respond;
a real audio job is still needed to verify inference. It refuses to replace an
existing app pod or vLLM container. On a failed startup, inspect their logs;
created containers and downloaded files remain available for diagnosis.

If vLLM exited before the app pod started, retry with the existing model and
images after correcting its launch settings:

```sh
python3 deploy/launch_prepared.py --retry
```

Retry recreates an existing stopped `root-transcribe-vllm` container (or creates
it if missing). It skips model downloads and image builds, keeps the manifest
and credentials, and refuses to
replace a running vLLM container or an existing app pod. The launcher checks
vLLM's container state while waiting and reports an exit immediately instead
of waiting for the HTTP timeout.

If vLLM is running but you interrupted the launcher before the app pod started,
continue without rebuilding or restarting the model server:

```sh
python3 deploy/launch_prepared.py --resume
```

Stop any previous waiting launcher with Ctrl+C first; its detached containers
remain running. Resume verifies the served model and API key before starting
the app from your existing manifest. Local readiness checks use `127.0.0.1`
directly, bypass environment proxy settings, and report their last connection
error and elapsed time every 15 seconds while waiting. To check the vLLM host
port independently, use `curl --noproxy '*' --max-time 5 -i http://127.0.0.1:8002/health`.

If you already started vLLM using the manual serving guide, skip the initial
launcher. Build the app and use the explicit manifest path:

```sh
./deploy/start.sh build
./deploy/start.sh start deploy/root-transcribe.prepared.local.yaml
# To stop this deployment, preserving data:
./deploy/start.sh stop deploy/root-transcribe.prepared.local.yaml
```

Keep using this explicit filename when starting or stopping the prepared app.
Plain `start.sh start` generates a different manifest from `settings.env` and
does not use the token you added to the prepared file. Subsequent vLLM restarts
use `podman start root-transcribe-vllm`. If both containers and pod already exist
but are stopped, use the lifecycle commands below instead of the initial launcher.

The generated manifest combines:

- Local settings and stable generated secrets, all in ignored files with private permissions.
- The realm import as a ConfigMap, avoiding host-path and SELinux mount setup.
- [Infrastructure](deploy/root-transcribe.yaml): identity, databases, queue, SeaweedFS, API,
  workers, scheduler and frontend, with named persistent volumes.

The API and workers wait for authenticated access to the audio bucket, and API
migrations run before workers start consuming jobs. The frontend (8095),
API (8010) and sign-in service (8185) bind to loopback. STT runs externally;
`host.containers.internal` points back to the host from the pod. The transcription
worker runs diarization and embeddings; it does not start an ASR server.

```sh
./deploy/start.sh status
./deploy/start.sh logs stt-worker
./deploy/start.sh logs backend
./deploy/start.sh logs seaweedfs
```

Removing the app through `start.sh stop` preserves named volumes. Keep `deploy/secrets.local.json` stable and
back it up with the databases and SeaweedFS volume: its `SECRET_KEY` protects playback
links and existing encrypted voiceprints, and its S3 keys authenticate storage access.
The script refuses to regenerate keys
when its output manifest already exists but that secret file is missing. Do not delete both to
reset a deployment that still has data. For an existing manually configured
installation, continue passing its local manifest rather than generating new keys.

`check` validates configuration syntax and required values; it does not prove
STT/S3 connectivity or model access. The UI's availability signal checks configuration and queue
consumers, not successful model inference. The first job downloads models and
may take longer. Always complete a real recording before a presentation.

For access from another computer, put HTTPS proxies in front of the frontend
and Keycloak and configure `APP_URL` and `KEYCLOAK_URL`. The container ports stay
loopback-only. Keycloak's bundled `start-dev` is for local demos; a production
identity service and TLS setup are outside this demo manifest. Realm import
only initializes a new realm: changing origins later requires updating the
existing Keycloak client as well. Never delete its database to apply such changes.

### Stop, restart and rebuild

To stop both services while keeping their containers and configuration:

```sh
podman pod stop root-transcribe
podman stop root-transcribe-vllm
```

To restart those existing containers, without applying new images or settings:

```sh
podman start root-transcribe-vllm
# Wait for model startup; check podman logs root-transcribe-vllm.
podman pod start root-transcribe
```

`start.sh stop` performs a different operation: it runs `podman kube down`,
removing the app pod and its containers while preserving named volumes. It does
not stop the separate vLLM server. Use the **same manifest that created the pod**:

```sh
# Default generated-manifest deployment:
./deploy/start.sh stop
# Or, for a prepared deployment:
./deploy/start.sh stop deploy/root-transcribe.prepared.local.yaml
```

To apply changed application code, finish active work, remove the app pod with
its matching stop command, build, then start from the matching configuration:

```sh
./deploy/start.sh build
# Choose ONE:
./deploy/start.sh start
# Or preserve the manually edited prepared manifest:
./deploy/start.sh start deploy/root-transcribe.prepared.local.yaml
```

A rebuild alone does not update existing containers. The API applies pending
migrations on startup; workers wait for them. `podman kube down` without `--force`
preserves volumes; `--force` removes them. See
[Podman's teardown semantics](https://docs.podman.io/en/latest/markdown/podman-kube-down.1.html).
These scripts do not install automatic host-boot startup. The model server has
its own image/container lifecycle, described in the serving guide.

### Bundled audio storage

The manifest pins SeaweedFS to `4.52` and runs its
[all-in-one mini service](https://github.com/seaweedfs/seaweedfs/blob/4.52/weed/command/mini.go).
It starts the master, filer, volume server and S3 gateway together and creates
`AUDIO_S3_BUCKET` (default `meeting-audio`). The volume server uses port 9340
to avoid the frontend's port 8080. WebDAV, the admin UI and table catalogs are disabled.

The app connects to `http://127.0.0.1:8333` inside the shared pod. Storage ports
are not published to the host. Audio is uploaded and played through the app API;
the browser does not need to reach SeaweedFS directly. `AUDIO_S3_ACCESS_KEY` and
`AUDIO_S3_SECRET_KEY` are generated once in `deploy/secrets.local.json` and passed
to both SeaweedFS and the app through the same Kubernetes Secret. Keep these
values stable across restarts. The normal settings file needs only the bucket name.

The named volume `transcribe-seaweedfs-data`, mounted at `/data`, holds audio,
filer metadata and master state. `./deploy/start.sh stop` preserves it. For a
consistent backup, finish active jobs, stop the stack, and back up all of this
volume together with both database volumes and the secret file. The Podman PVC
size is a declaration, not a disk quota; monitor free space on the host.
This is a single-host demo storage service without replication.

Existing external S3 installations are not migrated automatically. Continue
using their custom manifest until objects and configuration have been migrated;
pointing an existing database at a new empty bucket would lose access to recordings.

### Diarization model access errors

If the worker reports `GatedRepoError` or HTTP 401/403 for
`pyannote/speaker-diarization-community-1`, check that the account owning
`HF_TOKEN` has accepted that model's conditions and that the token permits
reading it. Test the deployed worker's access without printing its token:

```sh
podman exec root-transcribe-stt-worker python -c '
from huggingface_hub import hf_hub_download
from app.core.config import settings
hf_hub_download(settings.DIARIZATION_MODEL, "config.yaml", token=settings.HF_TOKEN)
print("Diarization model configuration is accessible.")
'
```

This checks configuration-file access, not a full model download or inference.
After granting access to the same token, retry the existing recording in the UI;
no new upload or worker rebuild is needed. If replacing the token, update the
manifest and recreate the app pod with that configuration; restarting an existing
container alone keeps its old environment. Preserve the storage/database volumes
and saved credentials. A failed model download does not mean the audio was lost.

### A short demo walkthrough

1. Create a workspace such as **Interviews**. A workspace groups recordings and controls sharing.
2. Choose **Upload audio** for a prepared, real two-speaker clip, or **Record audio**.
   Select **Recording language: Auto / English / Swedish**, then confirm participant consent.
   Choose English or Swedish explicitly to avoid incorrect automatic detection, especially
   on short recordings. The choice is saved with the recording and used for every
   transcription segment and retry. Existing recordings default to Auto.
   A 30–90 second clip is enough to demonstrate the flow.
3. Wait for the processing stage to change from separating voices to transcription.
   You can navigate away and return; the job continues.
4. Click a timestamp to listen. Use the speaker chips and transcript search to
   focus the review. Turn off **Follow playback** when editing another part.
5. Name the speakers. Above the transcript, **Rename speakers** lets you type a
   name for each detected voice; the name applies to this recording only and is
   used in the transcript, exports and new notes. With an LLM configured (see
   Meeting notes), **Identify speakers** reads the transcript for introductions
   such as "my name is Peter" or "jag heter Anna" and suggests names. Each
   suggestion shows the words it is based on and is applied only when you
   choose **Use**; check it first, because the model can attach a real quote to
   the wrong voice.
6. Correct text, or assign a person from the speaker directory from a speaker
   label. The menu distinguishes a single turn from all turns for that detected
   voice. Use **Speakers → Add speaker** if a person is missing from the
   directory; adding one needs no voice sample. Voice recognition is optional.
   An administrator who enables it for another person must record how consent
   was obtained before enrolling a sample.
7. Export a text file, Word/PDF document or timestamped subtitles. Revision review,
   comments and privacy controls are under **Review, comments & privacy**.

### Research behind the interface

[Otter's speaker identification guide](https://help.otter.ai/hc/en-us/articles/21665587209367-Speaker-Identification-Overview)
uses numbered labels for voices without a known identity. [Pixel Recorder's
speaker guide](https://support.google.com/pixelphone/answer/16269004?hl=en)
puts label correction and speaker search in the transcript. These support the
app's emphasis on readable speaker labels, explicit correction scope, filtering
and playback alongside text. They do not imply that our model has those products'
accuracy or that labeling a person enrolls their voice.

Deployment structure follows [Podman's kube-play documentation](https://docs.podman.io/en/latest/markdown/podman-kube-play.1.html):
ConfigMaps for configuration/import files, Secrets for credentials, and named
persistent volumes for data.

## Documents written by agents

With a second model configured, a finished recording gets a **Documents** tab. An
agent reads the transcript (with speaker names and times) and writes a document:
the built-in agents are **Summary** and **Task list**. Each document can be edited
in the app (Markdown, with preview), restored to what the agent wrote, copied,
and exported as Markdown, Word or PDF. Long meetings are processed in parts and
merged.

**Agents** are sets of instructions, managed on the Agents page:

- *Shared* agents are available to everyone; administrators and power users add
  them and change their instructions. The built-in ones can be edited, switched
  off and reset to their default, but not deleted.
- *My agents* are personal: any user can add their own, and only they see and
  run them. A document is visible to everyone who can read the transcript, whichever
  agent wrote it.

A change to an agent applies the next time it runs; documents already written keep
their text.

**Setup.** The agents run on the [CUGA](https://github.com/cuga-project/cuga-agent)
agent SDK in a separate `agent-runner` container, which talks to an
OpenAI-compatible LLM endpoint. On a Mac pass the endpoint as the second
argument to `mac.sh` (above). On Linux set it in `deploy/settings.env`:

```dotenv
LLM_BASE_URL=http://host.containers.internal:8001/v1
LLM_MODEL=<the server's model name>
LLM_API_KEY=
```

then run `./deploy/start.sh build` and `start`. Without `LLM_BASE_URL` the app
runs as before: the agent image is not built, the container is not started and
existing notes stay readable.

| Generated resource | Setting | Used by |
| --- | --- | --- |
| ConfigMap `transcribe-app-config` | `LLM_BASE_URL`, `LLM_MODEL` | API (feature on/off) and `agent-runner` |
| Secret `transcribe-app-secret` | `LLM_API_KEY` | `agent-runner` only |

The model name is detected from the server when you pass the address to
`mac.sh`; set `LLM_MODEL` in the environment if the server offers several.
A `localhost` address is rewritten so the containers reach the host. For local
development outside Podman, set `LLM_BASE_URL` and `LLM_MODEL` in `backend/.env`
and start the runner as described in [agent-runner/README.md](agent-runner/README.md).

Things to know:

- The transcript text is sent to the LLM endpoint. With a model you serve
  yourself nothing leaves your machines; with a hosted endpoint it does.
  Recordings marked *restricted* need the same explicit grant as an export.
- The agent image is about 2.5 GB and the container uses about 0.6 GB of memory.
- **Context window.** CUGA adds a prompt of roughly 4,750 tokens to every run.
  Serve the model with a context window of 32,000 tokens or more (vLLM:
  `--max-model-len 32768`). The app reads the window from the server and sizes
  the transcript parts to fit, up to `NOTES_MAX_INPUT_CHARS` (default 48,000
  characters). Below 16,000 tokens a note fails straight away with that reason.
- **Reasoning models.** The runner switches the model's "thinking" off
  (`enable_thinking: false` in the chat template, as understood by vLLM and
  llama.cpp), because thinking spends the answer budget and time without
  improving a summary. Set `LLM_ENABLE_THINKING=true` on the `agent-runner`
  container to allow it, or `default` for a server that rejects unknown request
  fields.
- The first note after a start downloads a small embedding model (about 130 MB,
  `BAAI/bge-small-en-v1.5`) that CUGA loads; the container needs internet access
  for that once per start.
- Quality depends on the model. The feature has been tested end to end with a
  stand-in model only; try it with yours before relying on it.

## Notebooks: take notes while recording

While you record a browser tab (a lecture, a course video, a video meeting), **Take note** saves
the frame that is showing together with the speech around it. All notes for one
video collect in one notebook, which exports as Markdown with its images.
Chrome or Edge on a desktop is needed (tab audio, frame capture).

1. In a workspace choose **Record audio**, tick *Also capture another tab*,
   and turn on **Take notes**. Paste the video's link (a YouTube link in
   any form always opens the same notebook), or pick an existing notebook, or
   name a new one. The full recording is saved as before.
2. Pick the tab in the browser's share dialog, with *Also share tab audio* on.
   The app then shows the shared tab once: keep the whole frame, or select the
   video area to leave out comments and recommendations. A video in fullscreen
   needs no cropping.
3. Press **Take note**. When you choose **Done** in the previous step, a small
   floating window with that button opens and stays above the tab you are
   watching, so you need not switch back to Root Transcribe. The same button
   is in the recording bar, where **Show floating button** opens the window
   again if you closed it.
   - *Until stopped* (default): the first press saves the frame and up to 15
     seconds of tab audio before it; the next press ends the note. Playback
     and the recording continue.
   - *Last 15 seconds only*: every press is one complete note.
   Only the tab's own audio is used for notes, never the microphone.
4. **View notes** opens the notebook. Each entry shows its image at once and
   says what state its text is in (Transcribing…, Ready, No speech detected,
   Transcription failed · Retry, Screenshot only). Under **My thoughts** you
   can write your own words on each note; they are saved when you leave the
   field and are kept apart from the transcribed text. **Export Markdown**
   downloads a ZIP with one `.md` file and an `images/` folder; the transcript
   is a quote and your thoughts follow under their own heading.

Good to know: pausing the recording ends an open note, and nothing from before
the pause reaches a later note. If the tab moves on to another video, use
**Change source** so the next notes go to the right notebook. Cancelling the
recording discards the notes taken in it; deleting a recording later removes
its notes too. A note is at most 10 minutes long. Times are "in this
recording" (the recorder's clock); a link to the exact moment in the video is
only added when you paste the player's *Copy video URL at current time* link
on an entry.

The floating window needs Chrome or Edge 116 or later. It is a window of its
own, so it is never part of a saved frame. With the window focused, Space or
Enter presses the button.

### Marks, a quick thought and slide changes

Next to **Take note**, in the floating window and in the recording bar:

- **Important** and **Don't understand** take a note that carries that mark.
  On a note that is still open they switch the mark on or off instead.
- **Thought**: type a line and press Enter. It is saved with the open note, or
  takes a note of its own. A line typed before pressing any of the buttons
  goes along with that press. It ends up under *My thoughts*.
- **Keep previous slide**: with *Offer to keep a slide when it changes* on
  (the default), the app looks at the shared tab every two seconds. When one
  still picture is replaced by another, the slide that went away can be kept
  as a note for 20 seconds, with up to 15 seconds of what was said before the
  change. Moving video never counts as a slide.

### What the language model adds

With a text model configured (see [Documents written by agents](#documents-written-by-agents))
and *Improve notes with AI* on, each note gets, a little while after its
transcript:

- a short **title**;
- a **tidy transcript** without filler words. *Show original* brings back the
  words as they were transcribed; the original is never changed, and a tidy
  version that lost or added too much is thrown away;
- **key terms** with a definition each, collected in the notebook's
  **Glossary** tab;
- for notes marked *Don't understand*, a plain **explanation**;
- the **text on the slide** (text, code, formulas, tables), which needs a
  model that can read images. With a text-only model this part is skipped and
  the rest still works.

The image and the text of a note are sent to the installation's own model
endpoint, nowhere else. **Improve with AI again** in a note's menu repeats it.

**Show 30 seconds more** under a transcript shows what was said before and
after the note, taken from the transcript of the full recording once that has
been uploaded and transcribed. The notebook can be searched (titles,
transcripts, slide text, thoughts, terms) and filtered by mark.

### Question: an answer while the meeting goes on

When somebody asks a question in a web meeting, press **Question** (in the
floating window or the recording bar). It needs the text model.

1. The last **minute** of the tab's audio is kept as a note of its own (an
   open note is not affected), and transcribed.
2. The model picks the question out of what was said and shows it back, with a
   first short answer when it is sure of one. This appears in the floating
   window within some seconds.
3. The **workspace is searched**: transcripts of earlier recordings you may
   read (recordings marked *restricted* are left out) and your own notes.
4. With *Research questions on the web* switched on for this recording, an
   agent **researches the question on the web**.
5. The answer is rewritten from what was found. The window shows two or three
   sentences to say aloud; the notebook has the full answer and its sources:
   links to the moment in an earlier recording, to your notes, and to the web
   pages the search returned.

Every answer is labelled a suggestion. Marking an existing note as *Question*
in its menu, or **Try again**, runs the same steps.

**Web research setup.** It is off unless a [Tavily](https://tavily.com) key is
given at deployment (`TAVILY_API_KEY` in `deploy/settings.env`, or in the
environment for `deploy/mac.sh`, together with the LLM endpoint):

```sh
TAVILY_API_KEY=tvly-... ./deploy/mac.sh http://localhost:8000/v1 http://localhost:8001/v1
```

That adds a `search-mcp` container to the pod: an MCP server with one tool,
`web_search`, which the CUGA agent in the agent runner calls. What leaves the
installation is the search queries the agent writes from the **question**; the
agent is never given the recording, the transcript or your notes. The key is
held by that container only. Each recording still has to switch web research
on, and the default is off.

Ideas that are not built yet are in [NOTEBOOK_IDEAS.md](NOTEBOOK_IDEAS.md).

## Measure transcription quality

`eval/ami_eval.py` scores a running installation against a meeting from the
[AMI Meeting Corpus](https://groups.inf.ed.ac.uk/ami/corpus/) (English, four
speakers, CC BY 4.0). It downloads the audio and the human transcript, uploads
the audio through the normal API, waits for the result and compares the two. It
needs only Python 3; nothing is installed.

```bash
python3 eval/ami_eval.py                                  # ES2002a, first 10 minutes
python3 eval/ami_eval.py --meeting IS1009b --minutes 0    # a whole meeting
python3 eval/ami_eval.py --recording <id>                 # score an earlier upload again
```

The recording id is in the result file of the earlier run.

It signs in like the browser does, as `admin@example.com` unless `--username`
says otherwise. Set `RT_PASSWORD` or type the password when asked; the initial
password must already have been changed in the browser. `--url` selects another
installation than `http://localhost:8095`.

| Score | Meaning (lower is better) |
|---|---|
| WER | Words wrong, ignoring who said them. Approximate where people talk over each other. |
| cpWER | Words wrong when each word must also be under the right speaker. |
| DER | Share of speech time that was missed, invented or given to the wrong speaker. A quarter second around each turn edge is not scored. |

Results and the transcript are written to `eval/results/` (not committed). The
uploaded recording stays in a workspace named "AMI evaluation", so it can be
inspected in the app. Downloads are cached in `~/.cache/root-transcribe-eval`.

## Verification

Deployment configuration and storage-startup unit checks, from the repository
root after installing the backend dependencies below. Storage tests use botocore;
no running containers are required:

```sh
backend/.venv/bin/python -m unittest discover -s deploy
```

```sh
cd backend
uv sync --frozen --extra dev
uv lock --check
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync alembic heads
uv run --no-sync pytest
# Optional real audio-decoder tests:
uv sync --frozen --extra dev --extra ffmpeg
REQUIRE_FFMPEG=1 uv run --no-sync pytest tests/unit/test_media_validation_ffmpeg.py

cd ../agent-runner        # only when working on meeting notes (about 2 GB)
uv sync --frozen
uv run --no-sync pytest

cd ../frontend
npm ci
npm run lint
npm run format:check
npm run typecheck
npm test -- --maxWorkers=1
npm run build
```

The normal test suite mocks STT, diarization, S3 and the agent runner. It does
not download models or prove live model quality. Use a real recording to verify the configured
services together. Browser smoke tests and the visual audit require a running
stack; see the [frontend guide](frontend/README.md). A documentation-only review
does not need to start the services.
