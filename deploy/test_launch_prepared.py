"""Prepared deployment orchestration checks; no containers or network needed."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import BaseHandler, Request
from urllib.response import addinfourl
from io import BytesIO

import configure
import launch_prepared


class PreparedLaunchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / "prepared.local.yaml"
        self.values = dict(
            configure.DEFAULTS,
            HF_TOKEN="test-token",
            STT_API_KEY="test-api-key",
            STT_MODEL=launch_prepared.SERVED_MODEL,
        )
        self.credentials = {key: "saved-" + key for key in configure.GENERATED_SECRETS}
        self.write_manifest()

    def write_manifest(self):
        configure.private_write(
            self.manifest, configure.render(self.values, self.credentials)
        )

    def test_missing_token_stops_before_host_operations(self):
        self.values["HF_TOKEN"] = ""
        self.write_manifest()
        with (
            patch("sys.argv", ["launch_prepared.py", str(self.manifest)]),
            patch.object(launch_prepared, "run") as run,
            redirect_stderr(StringIO()) as errors,
        ):
            self.assertEqual(launch_prepared.main(), 1)
        self.assertIn("Fill HF_TOKEN", errors.getvalue())
        run.assert_not_called()

    def test_check_does_not_start_services_or_expose_credentials(self):
        with (
            patch("sys.argv", ["launch_prepared.py", str(self.manifest), "--check"]),
            patch.object(launch_prepared, "run") as run,
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(launch_prepared.main(), 0)
        run.assert_not_called()
        self.assertNotIn(self.values["STT_API_KEY"], output.getvalue())
        self.assertNotIn(self.values["HF_TOKEN"], output.getvalue())

    def test_initial_launch_preserves_manifest_and_shares_the_vllm_key(self):
        original = self.manifest.read_bytes()
        with (
            patch("sys.argv", ["launch_prepared.py", str(self.manifest)]),
            patch.object(launch_prepared, "run") as run,
            patch.object(
                launch_prepared.subprocess, "run", return_value=Mock(returncode=1)
            ),
            patch.object(launch_prepared.Path, "home", return_value=self.root),
            patch.object(launch_prepared, "wait_for_http"),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(launch_prepared.main(), 0)
        self.assertEqual(self.manifest.read_bytes(), original)
        calls = run.call_args_list
        start = next(c for c in calls if c.args[:2] == ("./deploy/start.sh", "start"))
        self.assertEqual(start.args[2], str(self.manifest))
        serving = next(c for c in calls if c.args[:2] == ("podman", "run"))
        self.assertEqual(
            serving.kwargs["env"]["VLLM_API_KEY"], self.values["STT_API_KEY"]
        )
        self.assertNotIn(self.values["STT_API_KEY"], serving.args)
        self.assertEqual(
            serving.args[serving.args.index("--max-num-batched-tokens") + 1], "2048"
        )
        key_path = self.root / ".local/share/root-transcribe/vllm/api-key"
        self.assertEqual(key_path.read_text().strip(), self.values["STT_API_KEY"])
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)

    def test_mismatched_existing_vllm_key_is_not_overwritten(self):
        key_path = self.root / ".local/share/root-transcribe/vllm/api-key"
        key_path.parent.mkdir(parents=True)
        key_path.write_text("original-key\n")
        with (
            patch("sys.argv", ["launch_prepared.py", str(self.manifest)]),
            patch.object(launch_prepared, "run") as run,
            patch.object(
                launch_prepared.subprocess, "run", return_value=Mock(returncode=1)
            ),
            patch.object(launch_prepared.Path, "home", return_value=self.root),
            redirect_stderr(StringIO()) as errors,
        ):
            self.assertEqual(launch_prepared.main(), 1)
        self.assertIn("differs from the manifest", errors.getvalue())
        self.assertEqual(key_path.read_text(), "original-key\n")
        self.assertEqual(len(run.call_args_list), 2)  # Host checks only.

    def test_exited_vllm_fails_without_waiting_for_http_timeout(self):
        with (
            patch.object(
                launch_prepared,
                "container_state",
                return_value={"Running": False, "Status": "exited", "ExitCode": 1},
            ),
            patch.object(launch_prepared, "open_local_url") as http,
            patch.object(launch_prepared.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "exited .*exit code 1"):
                launch_prepared.wait_for_http(
                    "http://localhost:8002/v1/models", container="root-transcribe-vllm"
                )
        http.assert_not_called()
        sleep.assert_not_called()

    def test_authentication_failure_is_reported_immediately(self):
        with (
            patch.object(
                launch_prepared,
                "open_local_url",
                side_effect=HTTPError(
                    "http://localhost:8002", 401, "Unauthorized", {}, None
                ),
            ),
            patch.object(launch_prepared.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected authentication"):
                launch_prepared.wait_for_http(
                    "http://localhost:8002/v1/models", token="test-key"
                )
        sleep.assert_not_called()

    def test_retry_reuses_images_and_model_and_recreates_only_stopped_vllm(self):
        model = self.root / ".local/share/root-transcribe/vllm/model"
        model.mkdir(parents=True)
        (model / "config.json").write_text("{}")
        original = self.manifest.read_bytes()
        with (
            patch("sys.argv", ["launch_prepared.py", str(self.manifest), "--retry"]),
            patch.object(launch_prepared, "run") as run,
            patch.object(
                launch_prepared.subprocess,
                "run",
                side_effect=[Mock(returncode=0), Mock(returncode=1)],
            ),
            patch.object(
                launch_prepared,
                "container_state",
                return_value={"Status": "exited", "Running": False, "ExitCode": 1},
            ),
            patch.object(launch_prepared.Path, "home", return_value=self.root),
            patch.object(launch_prepared, "wait_for_http"),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(launch_prepared.main(), 0)
        calls = [c.args for c in run.call_args_list]
        self.assertIn(("podman", "rm", "root-transcribe-vllm"), calls)
        self.assertNotIn(("./deploy/start.sh", "build"), calls)
        self.assertFalse(
            any(c[:2] == ("podman", "build") or c[0] == "uvx" for c in calls)
        )
        self.assertIn(("./deploy/start.sh", "start", str(self.manifest)), calls)
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_retry_refuses_to_replace_a_running_container(self):
        with (
            patch("sys.argv", ["launch_prepared.py", str(self.manifest), "--retry"]),
            patch.object(launch_prepared, "run") as run,
            patch.object(
                launch_prepared.subprocess, "run", return_value=Mock(returncode=0)
            ),
            patch.object(
                launch_prepared,
                "container_state",
                return_value={"Status": "running", "Running": True},
            ),
            redirect_stderr(StringIO()) as errors,
        ):
            self.assertEqual(launch_prepared.main(), 1)
        self.assertIn("will not replace a running", errors.getvalue())
        self.assertFalse(
            any(c.args[:2] == ("podman", "rm") for c in run.call_args_list)
        )

    def test_local_readiness_does_not_use_environment_proxies(self):
        requests = []

        class LocalTransport(BaseHandler):
            handler_order = 100

            def http_open(self, request):
                requests.append(request)
                response = addinfourl(BytesIO(b"{}"), {}, request.full_url, 200)
                response.msg = "OK"
                return response

        original_builder = launch_prepared.build_opener
        with (
            patch.dict(
                "os.environ",
                {"http_proxy": "http://invalid-proxy:3128", "no_proxy": ""},
            ),
            patch.object(
                launch_prepared,
                "build_opener",
                side_effect=lambda *handlers: original_builder(
                    *handlers, LocalTransport()
                ),
            ),
        ):
            with launch_prepared.open_local_url(
                Request("http://127.0.0.1:8002/health"), timeout=5
            ) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(requests[0].host, "127.0.0.1:8002")
        self.assertFalse(requests[0].has_proxy())

    def test_wait_reports_transport_errors_and_elapsed_time(self):
        with (
            patch.object(
                launch_prepared,
                "open_local_url",
                side_effect=URLError(ConnectionRefusedError("connection refused")),
            ),
            patch.object(launch_prepared.time, "monotonic", side_effect=[0, 1, 16, 31]),
            patch.object(launch_prepared.time, "sleep"),
            redirect_stdout(StringIO()) as output,
        ):
            with self.assertRaisesRegex(RuntimeError, "connection refused"):
                launch_prepared.wait_for_http(
                    "http://127.0.0.1:8002/v1/models", timeout=30
                )
        self.assertIn("1s/30s", output.getvalue())
        self.assertIn("16s/30s", output.getvalue())

    def test_resume_uses_running_vllm_without_rebuild_or_replacement(self):
        original = self.manifest.read_bytes()
        with (
            patch("sys.argv", ["launch_prepared.py", str(self.manifest), "--resume"]),
            patch.object(launch_prepared, "run") as run,
            patch.object(
                launch_prepared.subprocess, "run", return_value=Mock(returncode=1)
            ),
            patch.object(
                launch_prepared,
                "container_state",
                return_value={"Status": "running", "Running": True},
            ),
            patch.object(launch_prepared, "wait_for_http") as wait,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(launch_prepared.main(), 0)
        self.assertEqual(
            [c.args for c in run.call_args_list],
            [
                ("podman", "info", "--format", "{{.Host.Arch}}"),
                ("nvidia-smi", "-L"),
                ("./deploy/start.sh", "start", str(self.manifest)),
            ],
        )
        self.assertEqual(
            wait.call_args_list[0].args[0], "http://127.0.0.1:8002/v1/models"
        )
        self.assertEqual(
            wait.call_args_list[0].kwargs["token"], self.values["STT_API_KEY"]
        )
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_resume_does_not_start_app_if_vllm_readiness_fails(self):
        with (
            patch("sys.argv", ["launch_prepared.py", str(self.manifest), "--resume"]),
            patch.object(launch_prepared, "run") as run,
            patch.object(
                launch_prepared.subprocess, "run", return_value=Mock(returncode=1)
            ),
            patch.object(
                launch_prepared, "container_state", return_value={"Running": True}
            ),
            patch.object(
                launch_prepared,
                "wait_for_http",
                side_effect=RuntimeError("unreachable"),
            ),
            redirect_stderr(StringIO()),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(launch_prepared.main(), 1)
        self.assertFalse(
            any(c.args[0] == "./deploy/start.sh" for c in run.call_args_list)
        )
