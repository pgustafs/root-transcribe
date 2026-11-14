#!/usr/bin/env python3
"""Build and launch the README's local vLLM demo using a prepared app manifest.

Run on the host after filling HF_TOKEN. The manifest is never regenerated.
Only manifests produced by configure.py (four JSON resource documents followed
by the infrastructure YAML) are supported, so no YAML package is needed.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

REPO = Path(__file__).resolve().parent.parent
MODEL = "RedHatAI/whisper-large-v3-quantized.w4a16"
SERVED_MODEL = "whisper-large-v3-w4a16"


def read_manifest(path: Path) -> tuple[dict, dict]:
    # Strip the generator's leading comment; JSON decoding remains strict.
    documents = path.read_text().split("\n", 1)[1].split("\n---\n")
    resources = [json.loads(part) for part in documents[:4]]
    by_name = {r["metadata"]["name"]: r for r in resources}
    config = by_name["transcribe-app-config"]["data"]
    credentials = by_name["transcribe-app-secret"]["stringData"]
    for key in ("HF_TOKEN", "STT_API_KEY"):
        value = credentials.get(key, "")
        if (
            not isinstance(value, str)
            or not value.strip()
            or any(character.isspace() for character in value)
            or value.lower().startswith(("change", "paste"))
        ):
            raise ValueError(f"Fill {key} in transcribe-app-secret in {path}")
    if (
        config["STT_BASE_URL"] != "http://host.containers.internal:8002/v1"
        or config["STT_MODEL"] != SERVED_MODEL
    ):
        raise ValueError(
            "This launcher requires the README's local vLLM endpoint and model alias"
        )
    return config, credentials


def run(*args: str, env: dict | None = None) -> None:
    print("Running: " + " ".join(args), flush=True)
    subprocess.run(args, cwd=REPO, env=env, check=True)


def container_state(name: str) -> dict:
    result = subprocess.run(
        ["podman", "inspect", name, "--format", "{{json .State}}"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def open_local_url(request: Request, *, timeout: float):
    # All readiness URLs are host loopback. Never send local health checks or
    # their Authorization header through an HTTP_PROXY configured for downloads.
    return build_opener(ProxyHandler({})).open(request, timeout=timeout)


def wait_for_http(
    url: str,
    *,
    token: str = "",
    model: str = "",
    timeout: int = 600,
    container: str = "",
) -> None:
    started = time.monotonic()
    deadline = started + timeout
    next_report = started
    headers = {"Authorization": "Bearer " + token} if token else {}
    while True:
        if container:
            state = container_state(container)
            if not state.get("Running"):
                raise RuntimeError(
                    f"{container} is {state.get('Status')} (exit code {state.get('ExitCode')}). "
                    f"Read the startup error with: podman logs --tail 100 {container}"
                )
        try:
            with open_local_url(Request(url, headers=headers), timeout=5) as response:
                if model:
                    ids = {item["id"] for item in json.load(response)["data"]}
                    if model not in ids:
                        raise ValueError("vLLM is serving an unexpected model alias")
                print(f"Ready: {url}", flush=True)
                return
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(
                    "Service rejected authentication; check that the manifest and vLLM use the same API key"
                ) from None
            reason = f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            cause = exc.reason if isinstance(exc, URLError) else exc
            # Only the local transport error, never request headers or body.
            reason = str(cause)
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError(
                f"Timed out waiting for {url}: {reason}. "
                "Check the host port and container logs."
            ) from None
        if now >= next_report:
            print(
                f"Waiting for {url} ({int(now - started)}s/{timeout}s): {reason}",
                flush=True,
            )
            next_report = now + 15
        time.sleep(3)


def finish_deployment(manifest: Path, credentials: dict) -> None:
    print("Checking vLLM on host port 8002...", flush=True)
    wait_for_http(
        "http://127.0.0.1:8002/v1/models",
        token=credentials["STT_API_KEY"],
        model=SERVED_MODEL,
        timeout=1200,
        container="root-transcribe-vllm",
    )
    # Explicit filename preserves the operator's token and other manifest edits.
    run("./deploy/start.sh", "start", str(manifest))
    for url in (
        "http://127.0.0.1:8010/health/ready",
        "http://127.0.0.1:8185/realms/root-transcribe/.well-known/openid-configuration",
        "http://127.0.0.1:8095",
    ):
        wait_for_http(url)
    print(
        "UI is reachable at http://localhost:8095. Complete a real recording to verify transcription and diarization."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        type=Path,
        nargs="?",
        default=REPO / "deploy/root-transcribe.prepared.local.yaml",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--retry",
        action="store_true",
        help="Reuse downloaded model and built images; recreate only a stopped vLLM container before app startup",
    )
    mode.add_argument(
        "--resume",
        action="store_true",
        help="Continue app deployment using the running vLLM container, without downloading, rebuilding or restarting it",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the prepared manifest only; do not build or start anything",
    )
    args = parser.parse_args()
    try:
        manifest = args.manifest.resolve()
        _, credentials = read_manifest(manifest)
        if args.check:
            print(
                "Prepared manifest settings are valid; live services and model access have not been tested."
            )
            return 0

        # Fail before downloads/builds when the host runtime or GPU is unavailable.
        run("podman", "info", "--format", "{{.Host.Arch}}")
        run("nvidia-smi", "-L")
        if args.resume:
            if not container_state("root-transcribe-vllm").get("Running"):
                raise ValueError(
                    "Resume requires a running vLLM container; use --retry if it exited"
                )
            existing = subprocess.run(
                ["podman", "pod", "exists", "root-transcribe"], cwd=REPO, timeout=10
            )
            if existing.returncode == 0:
                raise ValueError(
                    "The app pod already exists; inspect it with ./deploy/start.sh status"
                )
            if existing.returncode != 1:
                raise RuntimeError("Could not check the existing app pod")
            finish_deployment(manifest, credentials)
            return 0
        replace_vllm = False
        for kind, name in (
            ("container", "root-transcribe-vllm"),
            ("pod", "root-transcribe"),
        ):
            result = subprocess.run(["podman", kind, "exists", name], cwd=REPO)
            if result.returncode == 0:
                if args.retry and kind == "container":
                    state = container_state(name)
                    if state.get("Running") or state.get("Status") not in (
                        "exited",
                        "stopped",
                    ):
                        raise ValueError(
                            "Retry will not replace a running vLLM container"
                        )
                    replace_vllm = True
                    continue
                raise ValueError(
                    f"{name} already exists. Use the README's restart instructions; this launcher is for initial deployment."
                )
            if result.returncode != 1:
                raise RuntimeError(f"Could not check existing Podman {kind}s")

        model_dir = Path.home() / ".local/share/root-transcribe/vllm"
        key_path = model_dir / "api-key"
        if (
            key_path.exists()
            and key_path.read_text().strip() != credentials["STT_API_KEY"]
        ):
            raise ValueError(
                "Existing vLLM API key differs from the manifest; reconcile them before starting"
            )
        if args.retry:
            if not (model_dir / "model/config.json").is_file():
                raise ValueError(
                    "Retry requires the previously downloaded model; run initial deployment first"
                )
            for image in (
                "root-transcribe-vllm",
                "root-transcribe",
                "root-transcribe-stt",
                "root-transcribe-frontend",
            ):
                run("podman", "image", "exists", f"localhost/{image}:latest")
        model_dir.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            with open(
                key_path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)
            ) as stream:
                stream.write(credentials["STT_API_KEY"] + "\n")

        if not args.retry:
            download_env = {**os.environ, "HF_TOKEN": credentials["HF_TOKEN"]}
            run(
                "uvx",
                "--from",
                "huggingface_hub",
                "hf",
                "download",
                MODEL,
                "--local-dir",
                str(model_dir / "model"),
                env=download_env,
            )
            run(
                "podman",
                "build",
                "--pull=always",
                "-f",
                "deploy/vllm.Containerfile",
                "-t",
                "localhost/root-transcribe-vllm:latest",
                ".",
            )
            run("./deploy/start.sh", "build")
        if replace_vllm:
            # No force: if another operator starts it meanwhile, removal fails safely.
            run("podman", "rm", "root-transcribe-vllm")

        serving_env = {**os.environ, "VLLM_API_KEY": credentials["STT_API_KEY"]}
        run(
            "podman",
            "run",
            "-d",
            "--name",
            "root-transcribe-vllm",
            "--device",
            "nvidia.com/gpu=all",
            "--security-opt=label=disable",
            "--shm-size=1g",
            "-p",
            "8002:8000",
            "-v",
            f"{model_dir / 'model'}:/models/whisper:ro",
            "--env",
            "VLLM_API_KEY",
            "localhost/root-transcribe-vllm:latest",
            "/models/whisper",
            "--served-model-name",
            SERVED_MODEL,
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--dtype",
            "half",
            "--max-model-len",
            "448",
            "--max-num-batched-tokens",
            "2048",
            "--max-num-seqs",
            "1",
            "--gpu-memory-utilization",
            "0.5",
            env=serving_env,
        )
        finish_deployment(manifest, credentials)
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        # JSON decode errors can include secret-bearing input; report only location.
        if isinstance(exc, json.JSONDecodeError):
            print(
                f"Manifest JSON is invalid at line {exc.lineno}, column {exc.colno} of a resource",
                file=sys.stderr,
            )
        else:
            print(f"Deployment stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "\nLaunch interrupted. Detached containers remain running; use --resume if vLLM is running and the app pod has not started.",
            file=sys.stderr,
        )
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
