"""Configuration safety checks; run python3 -m unittest discover -s deploy."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO

import configure


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = self.root / "settings.env"
        self.env.write_text("HF_TOKEN=test-token\nSTT_API_KEY=literal$secret#value\n")

    def test_credentials_remain_literal_and_invalid_settings_fail(self):
        values = configure.read_settings(self.env)
        self.assertEqual(values["STT_API_KEY"], "literal$secret#value")
        self.env.write_text(self.env.read_text() + "UNKNOWN_FLAG=true\n")
        with self.assertRaisesRegex(ValueError, "Unknown setting"):
            configure.read_settings(self.env)

    def test_missing_service_credentials_fail_before_generating_secrets(self):
        self.env.write_text("HF_TOKEN=\n")
        with self.assertRaisesRegex(ValueError, "HF_TOKEN"):
            configure.read_settings(self.env)

    def test_preparation_allows_only_an_empty_token(self):
        self.env.write_text("HF_TOKEN=\n")
        self.assertEqual(
            configure.read_settings(self.env, allow_empty_hf_token=True)["HF_TOKEN"], ""
        )
        self.env.write_text("HF_TOKEN=\nSTT_MODEL=\n")
        with self.assertRaisesRegex(ValueError, "STT_MODEL"):
            configure.read_settings(self.env, allow_empty_hf_token=True)

    def test_preparation_does_not_overwrite_a_manifest_edited_by_the_operator(self):
        self.env.write_text("HF_TOKEN=\n")
        output = self.root / "prepared.local.yaml"
        args = [
            "configure.py",
            "--prepare",
            "--env",
            str(self.env),
            "--output",
            str(output),
            "--secrets",
            str(self.root / "secrets.json"),
        ]
        with patch("sys.argv", args), redirect_stdout(StringIO()):
            self.assertEqual(configure.main(), 0)
        self.assertIn('"HF_TOKEN": ""', output.read_text())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        edited = output.read_text().replace(
            '"HF_TOKEN": ""', '"HF_TOKEN": "operator-token"'
        )
        output.write_text(edited)
        with patch("sys.argv", args), redirect_stderr(StringIO()):
            self.assertEqual(configure.main(), 1)
        self.assertEqual(output.read_text(), edited)

    def test_generated_keys_are_private_and_stable_on_restart(self):
        path = self.root / "secrets.json"
        output = self.root / "manifest.yaml"
        original = configure.load_secrets(path, output)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(configure.load_secrets(path, output), original)
        self.assertEqual(len(set(original.values())), len(original))
        configure.private_write(
            output, configure.render(configure.read_settings(self.env), original)
        )
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "secret file is missing"):
            configure.load_secrets(path, output)

    def test_generated_realm_uses_only_the_configured_origin(self):
        values = configure.read_settings(self.env)
        values["APP_URL"] = "https://transcribe.example"
        credentials = {key: "test-only" for key in configure.GENERATED_SECRETS}
        manifest = configure.render(values, credentials)
        # The first four resources are JSON documents, a YAML-compatible subset.
        resources = [
            json.loads(part) for part in manifest.split("\n", 1)[1].split("\n---\n")[:4]
        ]
        realm = json.loads(resources[3]["data"]["root-transcribe-realm.json"])
        self.assertEqual(
            realm["clients"][0]["redirectUris"], ["https://transcribe.example/*"]
        )
        self.assertEqual(
            realm["clients"][0]["webOrigins"], ["https://transcribe.example"]
        )
        self.assertNotIn("hostPath:", manifest)
        self.assertNotIn("SECRET_KEY", resources[0]["data"])
        self.assertEqual(
            resources[0]["data"]["AUDIO_S3_ENDPOINT_URL"], "http://127.0.0.1:8333"
        )
        self.assertEqual(resources[0]["data"]["WAIT_FOR_AUDIO_STORE"], "true")
        for key in configure.STORAGE_SECRETS:
            self.assertEqual(resources[1]["stringData"][key], credentials[key])
            self.assertNotIn(key, resources[0]["data"])

    def test_adding_storage_preserves_existing_admin_and_encryption_keys(self):
        path = self.root / "secrets.json"
        original = {key: "saved-" + key for key in configure.BASE_SECRETS}
        configure.private_write(path, json.dumps(original))
        upgraded = configure.load_secrets(path, self.root / "manifest.yaml")
        for key, value in original.items():
            self.assertEqual(upgraded[key], value)
        self.assertEqual(
            configure.load_secrets(path, self.root / "manifest.yaml"), upgraded
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_storage_keys_in_existing_seaweed_deployment_are_not_rotated(self):
        path = self.root / "secrets.json"
        output = self.root / "manifest.yaml"
        configure.private_write(
            path, json.dumps({key: "saved" for key in configure.BASE_SECRETS})
        )
        output.write_text("containers:\n- name: seaweedfs\n")
        with self.assertRaisesRegex(ValueError, "Storage credentials are incomplete"):
            configure.load_secrets(path, output)

    def test_partial_storage_credentials_are_not_rotated(self):
        path = self.root / "secrets.json"
        original = {key: "saved" for key in configure.GENERATED_SECRETS}
        del original["AUDIO_S3_SECRET_KEY"]
        configure.private_write(path, json.dumps(original))
        with self.assertRaisesRegex(ValueError, "Storage credentials are incomplete"):
            configure.load_secrets(path, self.root / "manifest.yaml")
        self.assertEqual(json.loads(path.read_text()), original)

    def test_invalid_bucket_names_fail_before_startup(self):
        for name in ("Audio", "ab", "two,buckets", "../audio", "a..b", "127.0.0.1"):
            with self.subTest(name=name):
                self.env.write_text(f"HF_TOKEN=test-token\nAUDIO_S3_BUCKET={name}\n")
                with self.assertRaisesRegex(ValueError, "valid S3 bucket name"):
                    configure.read_settings(self.env)

    def test_remote_plain_http_is_rejected_for_microphone_capture(self):
        self.env.write_text(self.env.read_text() + "APP_URL=http://demo.example\n")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            configure.read_settings(self.env)

    def test_arm64_manifest_swaps_only_the_amd64_only_images(self):
        original = (configure.DEPLOY / "root-transcribe.yaml").read_text()
        adapted = configure.for_arm64(original)
        self.assertNotIn("quay.io/sclorg", adapted)
        for expected in (
            "image: docker.io/library/redis:7",
            "- redis-server\n",
            "mountPath: /data\n",
            "- name: POSTGRES_DB\n",
            "mountPath: /var/lib/postgresql/data\n",
        ):
            self.assertIn(expected, adapted)
        # Keycloak's database already used the multi-arch image.
        self.assertEqual(adapted.count("image: docker.io/library/postgres:16"), 2)
        changed = [
            (before, after)
            for before, after in zip(
                original.splitlines(), adapted.splitlines(), strict=True
            )
            if before != after
        ]
        self.assertEqual(len(changed), len(configure.ARM64_REPLACEMENTS))
        with self.assertRaisesRegex(ValueError, "arm64"):
            configure.for_arm64(adapted)

    def test_loopback_endpoint_is_reached_through_the_container_host(self):
        for given, expected in (
            ("http://localhost:8002/v1/", "http://host.containers.internal:8002/v1"),
            ("http://127.0.0.1:8002", "http://host.containers.internal:8002"),
            ("https://stt.example/v1", "https://stt.example/v1"),
        ):
            self.assertEqual(configure.normalize_stt_url(given), expected)
        with self.assertRaisesRegex(ValueError, "HTTP"):
            configure.normalize_stt_url("stt.example:8002")

    def serve_models(self, ids, *, prefix="/v1"):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                found = self.path == prefix + "/models"
                body = json.dumps({"data": [{"id": i} for i in ids]}).encode()
                self.send_response(200 if found else 404)
                self.end_headers()
                self.wfile.write(body if found else b"")

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", seen

    def test_model_is_detected_from_the_endpoint(self):
        origin, seen = self.serve_models(["whisper-large-v3"])
        self.assertEqual(
            configure.detect_stt_model(origin + "/v1", "key"),
            (origin + "/v1", "whisper-large-v3"),
        )
        self.assertEqual(seen, ["Bearer key"])
        # A URL given without /v1 is completed when only that path answers.
        self.assertEqual(
            configure.detect_stt_model(origin), (origin + "/v1", "whisper-large-v3")
        )
        origin, _ = self.serve_models(["llm", "my-whisper"])
        self.assertEqual(configure.detect_stt_model(origin + "/v1")[1], "my-whisper")

    def test_ambiguous_or_unreachable_endpoints_explain_the_fix(self):
        origin, _ = self.serve_models(["model-a", "model-b"])
        with self.assertRaisesRegex(ValueError, "model-a, model-b.*STT_MODEL"):
            configure.detect_stt_model(origin + "/v1")
        with self.assertRaisesRegex(ValueError, "HTTP 404"):
            configure.detect_stt_model(origin + "/other/v1")
        with self.assertRaisesRegex(ValueError, "Cannot reach"):
            configure.detect_stt_model("http://127.0.0.1:9/v1")

    def run_main(self, *args, env):
        argv = ["configure.py", "--env", str(self.root / "absent.env"), *args]
        out, err = StringIO(), StringIO()
        # The stub server listens on this host's loopback, not a container host.
        with (
            patch.object(configure, "CONTAINER_HOST", "127.0.0.1"),
            patch("sys.argv", argv),
            patch.dict(os.environ, env, clear=True),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            return configure.main(), out.getvalue() + err.getvalue()

    def test_endpoint_is_the_only_input_for_an_arm64_start(self):
        origin, _ = self.serve_models(["whisper-large-v3"])
        output = self.root / "mac.yaml"
        common = (
            "--stt-url", origin, "--arch", "arm64",
            "--output", str(output), "--secrets", str(self.root / "secrets.json"),
        )  # fmt: skip
        code, message = self.run_main(*common, env={})
        self.assertEqual(code, 1)
        self.assertIn("Set HF_TOKEN in the environment", message)
        self.assertFalse(output.exists())
        code, _ = self.run_main(*common, env={"HF_TOKEN": "hf-test"})
        self.assertEqual(code, 0)
        manifest = output.read_text()
        resources = [
            json.loads(part) for part in manifest.split("\n", 1)[1].split("\n---\n")[:4]
        ]
        self.assertEqual(resources[0]["data"]["STT_BASE_URL"], origin + "/v1")
        self.assertEqual(resources[0]["data"]["STT_MODEL"], "whisper-large-v3")
        self.assertEqual(resources[1]["stringData"]["HF_TOKEN"], "hf-test")
        self.assertNotIn("quay.io/sclorg", manifest)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        # An explicit model skips detection, so the endpoint may be offline.
        code, _ = self.run_main(
            "--stt-url", "http://localhost:9/v1", "--arch", "arm64",
            "--output", str(output), "--secrets", str(self.root / "secrets.json"),
            env={"HF_TOKEN": "hf-test", "STT_MODEL": "chosen"},
        )  # fmt: skip
        self.assertEqual(code, 0)
        self.assertIn(
            '"STT_BASE_URL": "http://127.0.0.1:9/v1"',
            output.read_text(),
        )

    def test_agent_runner_is_deployed_only_with_an_llm(self):
        secrets = configure.load_secrets(self.root / "s.json", self.root / "m.yaml")
        without = configure.render(configure.read_settings(self.env), secrets)
        self.assertNotIn("agent-runner", without)
        self.assertNotIn("root-transcribe-agent", without)
        self.assertIn("- name: frontend", without)  # the neighbours are intact
        self.assertIn("- name: stt-worker", without)

        self.env.write_text(
            self.env.read_text()
            + "LLM_BASE_URL=http://host.containers.internal:8001/v1/\n"
            + "LLM_MODEL=qwen3\nLLM_API_KEY=llm$key\n"
        )
        with_llm = configure.render(configure.read_settings(self.env), secrets)
        self.assertIn("  - name: agent-runner\n", with_llm)
        self.assertIn("image: localhost/root-transcribe-agent:latest", with_llm)
        config, app_secret = (
            json.loads(part) for part in with_llm.split("\n", 1)[1].split("\n---\n")[:2]
        )
        self.assertEqual(
            config["data"]["LLM_BASE_URL"], "http://host.containers.internal:8001/v1"
        )
        self.assertEqual(config["data"]["LLM_MODEL"], "qwen3")
        self.assertEqual(app_secret["stringData"]["LLM_API_KEY"], "llm$key")
        # The runner gets the LLM settings only, never the app's secrets.
        block = with_llm[
            with_llm.index(configure.AGENT_BEGIN) : with_llm.index(configure.AGENT_END)
        ]
        self.assertNotIn("envFrom", block)
        self.assertEqual(block.count("- name: LLM_"), 3)
        # The Mac variant keeps it too.
        self.assertIn(
            "- name: agent-runner",
            configure.render(configure.read_settings(self.env), secrets, arch="arm64"),
        )

        # Web research: off without a Tavily key (no container, no secret) ...
        self.assertNotIn("- name: search-mcp", with_llm)
        self.assertNotIn("transcribe-search-secret", with_llm)
        self.assertEqual(config["data"]["WEB_SEARCH_ENABLED"], "false")
        self.assertEqual(config["data"]["SEARCH_MCP_URL"], "")
        # ... and on with one: the key goes to the search container only.
        self.env.write_text(self.env.read_text() + "TAVILY_API_KEY=tvly-secret\n")
        with_search = configure.render(configure.read_settings(self.env), secrets)
        self.assertIn("  - name: search-mcp\n", with_search)
        documents = [
            json.loads(part)
            for part in with_search.split("\n", 1)[1].split("\n---\n")
            if part.startswith("{")
        ]
        by_name = {doc["metadata"]["name"]: doc for doc in documents}
        self.assertEqual(
            by_name["transcribe-search-secret"]["stringData"],
            {"TAVILY_API_KEY": "tvly-secret"},
        )
        self.assertNotIn(
            "TAVILY_API_KEY", by_name["transcribe-app-secret"]["stringData"]
        )
        self.assertEqual(
            by_name["transcribe-app-config"]["data"]["WEB_SEARCH_ENABLED"], "true"
        )
        self.assertEqual(
            by_name["transcribe-app-config"]["data"]["SEARCH_MCP_URL"],
            configure.SEARCH_MCP_URL,
        )
        self.assertEqual(with_search.count("tvly-secret"), 1)
        search = with_search[
            with_search.index(configure.SEARCH_BEGIN) : with_search.index(
                configure.SEARCH_END
            )
        ]
        self.assertNotIn("envFrom", search)
        self.assertNotIn("LLM_", search)

    def test_llm_needs_a_model_name_and_a_valid_url(self):
        self.env.write_text(self.env.read_text() + "LLM_BASE_URL=http://llm:8001/v1\n")
        with self.assertRaisesRegex(ValueError, "LLM_MODEL"):
            configure.read_settings(self.env)
        self.env.write_text("HF_TOKEN=test-token\nLLM_BASE_URL=llm:8001\nLLM_MODEL=m\n")
        with self.assertRaisesRegex(ValueError, "LLM_BASE_URL must be an HTTP"):
            configure.read_settings(self.env)

    def test_llm_model_is_detected_ignoring_whisper(self):
        origin, seen = self.serve_models(["whisper-large-v3", "qwen3-27b"])
        self.assertEqual(
            configure.detect_stt_model(origin, "k", llm=True),
            (origin + "/v1", "qwen3-27b"),
        )
        self.assertIn("Bearer k", seen)
        origin, _ = self.serve_models(["model-a", "model-b"])
        with self.assertRaisesRegex(ValueError, "model-a, model-b.*LLM_MODEL"):
            configure.detect_stt_model(origin + "/v1", llm=True)
        with self.assertRaisesRegex(ValueError, "Cannot reach the LLM endpoint"):
            configure.detect_stt_model("http://127.0.0.1:9/v1", llm=True)

    def test_second_endpoint_turns_on_meeting_notes(self):
        stt, _ = self.serve_models(["whisper-large-v3"])
        llm, _ = self.serve_models(["qwen3-27b"])
        output = self.root / "mac.yaml"
        code, message = self.run_main(
            "--stt-url", stt, "--llm-url", llm.replace("127.0.0.1", "localhost"),
            "--arch", "arm64",
            "--output", str(output), "--secrets", str(self.root / "secrets.json"),
            env={"HF_TOKEN": "hf-test", "LLM_API_KEY": "llm-key"},
        )  # fmt: skip
        self.assertEqual(code, 0, message)
        self.assertIn("Meeting notes:", message)
        manifest = output.read_text()
        config, app_secret = (
            json.loads(part) for part in manifest.split("\n", 1)[1].split("\n---\n")[:2]
        )
        self.assertEqual(config["data"]["LLM_BASE_URL"], llm + "/v1")
        self.assertEqual(config["data"]["LLM_MODEL"], "qwen3-27b")
        self.assertEqual(config["data"]["STT_MODEL"], "whisper-large-v3")
        self.assertEqual(app_secret["stringData"]["LLM_API_KEY"], "llm-key")
        self.assertIn("- name: agent-runner", manifest)
        # Without it the same command still works and leaves the runner out.
        code, _ = self.run_main(
            "--stt-url", stt, "--arch", "arm64",
            "--output", str(output), "--secrets", str(self.root / "secrets.json"),
            env={"HF_TOKEN": "hf-test"},
        )  # fmt: skip
        self.assertEqual(code, 0)
        self.assertNotIn("agent-runner", output.read_text())


if __name__ == "__main__":
    unittest.main()
