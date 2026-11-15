#!/usr/bin/env python3
"""Generate a Podman manifest from a small settings file, preserving secrets.

Uses only the Python standard library. JSON documents are valid YAML and are
prepended to the readable infrastructure manifest consumed by podman kube play.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

DEPLOY = Path(__file__).resolve().parent
DEFAULTS = {
    "STT_BASE_URL": "http://host.containers.internal:8002/v1",
    "STT_MODEL": "whisper-large-v3-turbo",
    "STT_API_KEY": "",
    # Optional second model for meeting notes; empty = the feature is off.
    "LLM_BASE_URL": "",
    "LLM_MODEL": "",
    "LLM_API_KEY": "",
    # Optional: web research for questions marked in a notebook (needs the LLM).
    "TAVILY_API_KEY": "",
    "AUDIO_S3_BUCKET": "meeting-audio",
    "AUDIO_S3_REGION": "us-east-1",
    "HF_TOKEN": "",
    "KEYCLOAK_APP_ADMIN_EMAIL": "admin@example.com",
    "APP_URL": "http://localhost:8095",
    "KEYCLOAK_URL": "http://localhost:8185",
}
BASE_SECRETS = (
    "SECRET_KEY",
    "POSTGRES_PASSWORD",
    "KEYCLOAK_DB_PASSWORD",
    "KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD",
    "KEYCLOAK_APP_ADMIN_PASSWORD",
)
STORAGE_SECRETS = ("AUDIO_S3_ACCESS_KEY", "AUDIO_S3_SECRET_KEY")
GENERATED_SECRETS = BASE_SECRETS + STORAGE_SECRETS
# Inside the pod, loopback is the pod itself; the host is reached by this name.
CONTAINER_HOST = "host.containers.internal"
# The default manifest uses two amd64-only images. Each pair must match exactly
# once, so a change in root-transcribe.yaml fails loudly instead of silently.
ARM64_REPLACEMENTS = (
    (
        "image: quay.io/sclorg/postgresql-16-c10s:latest",
        "image: docker.io/library/postgres:16",
    ),
    ("- name: POSTGRESQL_USER\n", "- name: POSTGRES_USER\n"),
    ("- name: POSTGRESQL_PASSWORD\n", "- name: POSTGRES_PASSWORD\n"),
    ("- name: POSTGRESQL_DATABASE\n", "- name: POSTGRES_DB\n"),
    ("mountPath: /var/lib/pgsql/data\n", "mountPath: /var/lib/postgresql/data\n"),
    ("image: quay.io/sclorg/redis-7-c9s:latest", "image: docker.io/library/redis:7"),
    ("    - run-redis\n", "    - redis-server\n"),
    ("mountPath: /var/lib/redis/data\n", "mountPath: /data\n"),
)


AGENT_BEGIN = "  # agent-runner:begin"
AGENT_END = "  # agent-runner:end\n"


SEARCH_BEGIN = "  # search-mcp:begin"
SEARCH_END = "  # search-mcp:end\n"
SEARCH_MCP_URL = "http://127.0.0.1:8012/mcp"


def without_search_mcp(manifest: str) -> str:
    """Drop the search MCP container: no Tavily key, no web research."""
    if manifest.count(SEARCH_BEGIN) != 1 or manifest.count(SEARCH_END) != 1:
        raise ValueError("Cannot find the search-mcp block in the manifest")
    start = manifest.index(SEARCH_BEGIN)
    return manifest[:start] + manifest[manifest.index(SEARCH_END) + len(SEARCH_END) :]


def without_agent_runner(manifest: str) -> str:
    """Drop the agent-runner container: no LLM, nothing for it to do."""
    if manifest.count(AGENT_BEGIN) != 1 or manifest.count(AGENT_END) != 1:
        raise ValueError("Cannot find the agent-runner block in the manifest")
    start = manifest.index(AGENT_BEGIN)
    return manifest[:start] + manifest[manifest.index(AGENT_END) + len(AGENT_END) :]


def for_arm64(manifest: str) -> str:
    """Swap the amd64-only database and queue images for multi-arch ones."""
    for old, new in ARM64_REPLACEMENTS:
        if manifest.count(old) != 1:
            raise ValueError(
                f"Cannot adapt the manifest for arm64: expected exactly one {old.strip()!r}"
            )
        manifest = manifest.replace(old, new)
    return manifest


def normalize_stt_url(url: str, what: str = "speech-to-text") -> str:
    """Trim the URL and point a loopback host at the container host."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"The {what} endpoint must be an HTTP(S) URL")
    if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        port = f":{parsed.port}" if parsed.port else ""
        parsed = parsed._replace(netloc=CONTAINER_HOST + port)
    return urlunsplit(parsed._replace(path=parsed.path.rstrip("/")))


def list_stt_models(url: str, api_key: str = "", timeout: float = 10) -> list[str]:
    request = Request(url + "/models", headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    # Never send the key through an HTTP_PROXY configured for downloads.
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        body = json.load(response)
    entries = body.get("data") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{url}/models did not return an OpenAI-style model list")
    return [e["id"] for e in entries if isinstance(e, dict) and e.get("id")]


def detect_stt_model(
    url: str, api_key: str = "", *, llm: bool = False
) -> tuple[str, str]:
    """Return (base URL, model) by asking the server which models it serves.

    For speech-to-text a single Whisper model is preferred; for the LLM
    (``llm=True``) Whisper models are ignored instead.
    """
    what, key_name, model_name = (
        ("LLM", "LLM_API_KEY", "LLM_MODEL")
        if llm
        else ("speech-to-text", "STT_API_KEY", "STT_MODEL")
    )
    candidates = [url] if url.endswith("/v1") else [url, url + "/v1"]
    failure = ""
    for base in candidates:
        try:
            models = list_stt_models(base, api_key)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise ValueError(
                    f"{base} rejected the request ({exc.code}); export {key_name} and retry"
                ) from exc
            failure = f"{base}/models answered HTTP {exc.code}"
            continue
        except (URLError, OSError, json.JSONDecodeError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ValueError(
                f"Cannot reach the {what} endpoint {base} from a container: {reason}"
            ) from exc
        whisper = [m for m in models if "whisper" in m.lower()]
        if llm:
            choice = [m for m in models if m not in whisper]
        else:
            choice = whisper if len(whisper) == 1 else models
        if len(choice) == 1:
            return base, choice[0]
        listed = ", ".join(models) or "none"
        raise ValueError(
            f"Cannot choose a model at {base} (served: {listed}); export {model_name}=<name> and retry"
        )
    raise ValueError(
        f"{failure}. Check the endpoint, or export {model_name}=<name> to skip detection"
    )


def read_settings(
    path: Path,
    *,
    allow_empty_hf_token: bool = False,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    values = dict(DEFAULTS)
    # With overrides (single-parameter start) the settings file is optional.
    lines = (
        []
        if overrides is not None and not path.exists()
        else path.read_text().splitlines()
    )
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator or key not in DEFAULTS:
            raise ValueError(f"Unknown setting on line {number}: {key}")
        raw = raw.strip()
        # Unquoted values remain literal (including # and $ in credentials).
        if raw.startswith(('"', "'")):
            parts = shlex.split(raw, comments=True)
            if len(parts) != 1:
                raise ValueError(f"Invalid quoted value on line {number}")
            raw = parts[0]
        values[key] = raw
    values.update(overrides or {})
    for key in (
        "HF_TOKEN",
        "STT_MODEL",
        "AUDIO_S3_BUCKET",
        "KEYCLOAK_APP_ADMIN_EMAIL",
    ):
        if key == "HF_TOKEN" and allow_empty_hf_token and not values[key]:
            continue
        if not values[key] or values[key].lower().startswith(
            ("changethis", "change-me")
        ):
            raise ValueError(
                f"Set {key} in the environment"
                if overrides is not None and key in overrides
                else f"Set {key} in {path.name}"
            )
    bucket = values["AUDIO_S3_BUCKET"]
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or ".." in bucket
        or ".-" in bucket
        or "-." in bucket
        or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", bucket)
    ):
        raise ValueError("AUDIO_S3_BUCKET must be a valid S3 bucket name")
    if values["LLM_BASE_URL"] and not values["LLM_MODEL"]:
        raise ValueError(
            "Set LLM_MODEL in the environment"
            if overrides is not None and "LLM_BASE_URL" in overrides
            else f"Set LLM_MODEL in {path.name} (LLM_BASE_URL is set)"
        )
    for key in ("APP_URL", "KEYCLOAK_URL", "STT_BASE_URL", "LLM_BASE_URL"):
        if key == "LLM_BASE_URL" and not values[key]:
            continue  # optional: meeting notes stay off
        parsed = urlsplit(values[key])
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"{key} must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                f"{key} must not contain credentials, a query or a fragment"
            )
        if key in ("APP_URL", "KEYCLOAK_URL") and parsed.path not in ("", "/"):
            raise ValueError(f"{key} must be an origin without a path")
        if (
            key == "APP_URL"
            and parsed.scheme == "http"
            and parsed.hostname not in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("Remote microphone recording requires an HTTPS APP_URL")
        values[key] = values[key].rstrip("/")
    return values


def private_write(path: Path, content: str, *, exclusive: bool = False) -> None:
    # All generated files may contain credentials; never create them world-readable.
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)


def load_secrets(path: Path, output: Path) -> dict[str, str]:
    if path.exists():
        values = json.loads(path.read_text())
        if not isinstance(values, dict) or not all(
            isinstance(values.get(k), str) and values[k] for k in BASE_SECRETS
        ):
            raise ValueError(
                "Existing secret file is incomplete; restore it from backup"
            )
        # Upgrade pre-SeaweedFS settings without changing encryption/login keys.
        # Never replace missing credentials for an already generated SeaweedFS pod.
        if not any(k in values for k in STORAGE_SECRETS) and not (
            output.exists() and "name: seaweedfs" in output.read_text()
        ):
            values.update({key: secrets.token_urlsafe(32) for key in STORAGE_SECRETS})
            private_write(path, json.dumps(values, indent=2) + "\n")
        if not all(
            isinstance(values.get(k), str) and values[k] for k in STORAGE_SECRETS
        ):
            raise ValueError(
                "Storage credentials are incomplete; restore the secret file from backup"
            )
        return values
    if output.exists():
        raise ValueError(
            "Manifest exists but its secret file is missing. Restore the secret file; do not rotate encryption keys accidentally."
        )
    values = {key: secrets.token_urlsafe(32) for key in GENERATED_SECRETS}
    private_write(path, json.dumps(values, indent=2) + "\n", exclusive=True)
    return values


def resource(kind: str, name: str, data: dict[str, str]) -> dict:
    result = {"apiVersion": "v1", "kind": kind, "metadata": {"name": name}}
    if kind == "Secret":
        result.update(type="Opaque", stringData=data)
    else:
        result["data"] = data
    return result


def render(
    values: dict[str, str], credentials: dict[str, str], *, arch: str = ""
) -> str:
    web_search = bool(values["LLM_BASE_URL"] and values["TAVILY_API_KEY"])
    config = {
        "PROJECT_NAME": "Root Transcribe",
        "ENVIRONMENT": "production",
        "KEYCLOAK_INTERNAL_URL": "http://localhost:8185",
        "KEYCLOAK_URL": values["KEYCLOAK_URL"],
        "KEYCLOAK_APP_ADMIN_EMAIL": values["KEYCLOAK_APP_ADMIN_EMAIL"],
        "POSTGRES_SERVER": "localhost",
        "POSTGRES_USER": "app",
        "POSTGRES_DB": "app",
        "REDIS_URL": "redis://localhost:6379/0",
        "AUDIO_S3_ENDPOINT_URL": "http://127.0.0.1:8333",
        "WAIT_FOR_AUDIO_STORE": "true",
        # Web research needs both the LLM (the agent) and a Tavily key.
        "WEB_SEARCH_ENABLED": "true" if web_search else "false",
        "SEARCH_MCP_URL": SEARCH_MCP_URL if web_search else "",
        **{
            key: values[key]
            for key in (
                "STT_BASE_URL",
                "STT_MODEL",
                "LLM_BASE_URL",
                "LLM_MODEL",
                "AUDIO_S3_BUCKET",
                "AUDIO_S3_REGION",
            )
        },
    }
    app_secrets = {
        **{
            key: credentials[key]
            for key in ("SECRET_KEY", "POSTGRES_PASSWORD", *STORAGE_SECRETS)
        },
        **{
            key: values[key]
            for key in (
                "STT_API_KEY",
                "LLM_API_KEY",
                "HF_TOKEN",
            )
        },
    }
    manifest = (DEPLOY / "root-transcribe.yaml").read_text()
    if not web_search:
        manifest = without_search_mcp(manifest)
    if not values["LLM_BASE_URL"]:
        manifest = without_agent_runner(manifest)
    realm = json.loads((DEPLOY / "keycloak/root-transcribe-realm.json").read_text())
    client = realm["clients"][0]
    client["redirectUris"] = [values["APP_URL"] + "/*"]
    client["webOrigins"] = [values["APP_URL"]]
    client["attributes"]["post.logout.redirect.uris"] = values["APP_URL"] + "/*"
    documents = [
        resource("ConfigMap", "transcribe-app-config", config),
        resource("Secret", "transcribe-app-secret", app_secrets),
        resource(
            "Secret",
            "transcribe-keycloak-secret",
            {k: credentials[k] for k in GENERATED_SECRETS if k.startswith("KEYCLOAK_")},
        ),
        # Its own secret: only the search container may read the Tavily key.
        *(
            [
                resource(
                    "Secret",
                    "transcribe-search-secret",
                    {"TAVILY_API_KEY": values["TAVILY_API_KEY"]},
                )
            ]
            if web_search
            else []
        ),
        resource(
            "ConfigMap",
            "transcribe-keycloak-realm",
            {"root-transcribe-realm.json": json.dumps(realm, indent=2)},
        ),
    ]
    return (
        "# Generated by deploy/configure.py. Contains secrets; do not commit.\n"
        + "\n---\n".join(json.dumps(doc, indent=2) for doc in documents)
        + "\n---\n"
        + (for_arm64(manifest) if arch == "arm64" else manifest)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=DEPLOY / "settings.env")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--secrets", type=Path, default=DEPLOY / "secrets.local.json")
    parser.add_argument(
        "--stt-url",
        help="Speech-to-text endpoint as the only input: the settings file becomes optional, "
        "HF_TOKEN (and optional STT_API_KEY, STT_MODEL) come from the environment, "
        "and the model is detected from the server",
    )
    parser.add_argument(
        "--llm-url",
        help="LLM endpoint for meeting notes (OpenAI-compatible). Optional LLM_API_KEY "
        "and LLM_MODEL come from the environment; the model is detected from the server",
    )
    parser.add_argument(
        "--arch",
        choices=("arm64",),
        help="Adapt the manifest for this architecture (arm64: Apple silicon)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate settings without generating files or connecting to services",
    )
    mode.add_argument(
        "--prepare",
        action="store_true",
        help="Create a separate manifest with HF_TOKEN allowed empty; never overwrite an existing file",
    )
    args = parser.parse_args()
    args.output = args.output or DEPLOY / (
        "root-transcribe.prepared.local.yaml"
        if args.prepare
        else "root-transcribe.mac.local.yaml"
        if args.arch == "arm64"
        else "root-transcribe.local.yaml"
    )
    try:
        if args.prepare and args.output.exists():
            raise ValueError(
                "Prepared manifest already exists; edit it directly to preserve its token and credentials"
            )
        overrides = None
        if args.stt_url:
            url = normalize_stt_url(args.stt_url)
            api_key = os.environ.get("STT_API_KEY", "")
            model = os.environ.get("STT_MODEL", "")
            if not os.environ.get("HF_TOKEN") and not args.prepare:
                raise ValueError("Set HF_TOKEN in the environment")
            if not model and not args.check:
                url, model = detect_stt_model(url, api_key)
                print(f"Speech-to-text: {url} (model {model})")
            overrides = {
                "STT_BASE_URL": url,
                "STT_API_KEY": api_key,
                "HF_TOKEN": os.environ.get("HF_TOKEN", ""),
                **({"STT_MODEL": model} if model else {}),
            }
        if args.llm_url:
            llm_url = normalize_stt_url(args.llm_url, "LLM")
            llm_key = os.environ.get("LLM_API_KEY", "")
            llm_model = os.environ.get("LLM_MODEL", "")
            if not llm_model and not args.check:
                llm_url, llm_model = detect_stt_model(llm_url, llm_key, llm=True)
                print(f"Meeting notes: {llm_url} (model {llm_model})")
            overrides = {
                **(overrides or {}),
                "LLM_BASE_URL": llm_url,
                "LLM_API_KEY": llm_key,
                "TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY", ""),
                **({"LLM_MODEL": llm_model} if llm_model else {}),
            }
            if os.environ.get("TAVILY_API_KEY"):
                print("Web research for notebook questions: on (Tavily)")
        values = read_settings(
            args.env, allow_empty_hf_token=args.prepare, overrides=overrides
        )
        if args.check:
            print(
                "Settings are valid. Live STT, S3, model access and container readiness have not been tested."
            )
            return 0
        credentials = load_secrets(args.secrets, args.output)
        private_write(
            args.output,
            render(values, credentials, arch=args.arch or ""),
            exclusive=args.prepare,
        )
        print(f"Wrote {args.output}. Saved passwords remain in {args.secrets}.")
        if args.prepare:
            print("Fill HF_TOKEN in the transcribe-app-secret stringData section.")
            print(f"Start this exact file: ./deploy/start.sh start {args.output}")
            print(
                "Preparation only: no images were built and no services were started."
            )
            return 0
        print(
            f"After startup, open {values['APP_URL']} and sign in as {values['KEYCLOAK_APP_ADMIN_EMAIL']}."
        )
        print(
            "Use KEYCLOAK_APP_ADMIN_PASSWORD from the secret file; first sign-in asks you to change it."
        )
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
