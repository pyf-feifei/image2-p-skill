from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "image2_p.py"
SPEC = importlib.util.spec_from_file_location("image2_p_under_test", SCRIPT)
assert SPEC and SPEC.loader
image2_p = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = image2_p
SPEC.loader.exec_module(image2_p)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nAAAAABJRU5ErkJggg=="
)


class ApiHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    echo_secret_error = False
    echo_secret_success = False

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        record = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }
        type(self).requests.append(record)

        if type(self).echo_secret_error:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            payload = {"error": {"message": f"rejected token {token}"}}
            encoded = json.dumps(payload).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        count = 1
        if self.headers.get_content_type() == "application/json":
            request_json = json.loads(body.decode())
            count = int(request_json.get("n", 1))
        response = {
            "created": 1,
            "data": [{"b64_json": base64.b64encode(PNG).decode()}] * count,
            "usage": {"total_tokens": 1},
        }
        if type(self).echo_secret_success:
            response["diagnostic"] = self.headers.get("Authorization", "")
            response["data"][0]["url"] = (
                "https://cdn.example.test/image.png?token=signed-secret#fragment"
            )
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class Image2PTests(unittest.TestCase):
    def setUp(self) -> None:
        ApiHandler.requests = []
        ApiHandler.echo_secret_error = False
        ApiHandler.echo_secret_success = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env_file = self.root / "config.env"
        self.env_file.write_text(
            f"IMAGE2_P_BASE_URL={self.base_url}\n"
            "IMAGE2_P_API_KEY=sentinel-test-key\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def run_cli(self, argv: list[str], env: dict[str, str] | None = None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = image2_p.run(argv, process_env=env or {})
        return code, stdout.getvalue(), stderr.getvalue()

    def test_legacy_generate_form_and_multiple_outputs(self) -> None:
        out = self.root / "result.png"
        response = self.root / "response.json"
        code, stdout, stderr = self.run_cli(
            [
                "--env-file",
                str(self.env_file),
                "--prompt",
                "test prompt",
                "--n",
                "2",
                "--out",
                str(out),
                "--response-out",
                str(response),
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertTrue((self.root / "result-1.png").is_file())
        self.assertTrue((self.root / "result-2.png").is_file())
        self.assertTrue(response.is_file())
        self.assertIn("operation: generate", stdout)
        request = ApiHandler.requests[0]
        self.assertEqual(request["path"], "/v1/images/generations")
        payload = json.loads(request["body"].decode())
        self.assertEqual(payload["n"], 2)
        self.assertEqual(payload["response_format"], "b64_json")

    def test_edit_uses_multipart_and_repeated_image_parts(self) -> None:
        first = self.root / "first.png"
        second = self.root / "second.png"
        first.write_bytes(PNG)
        second.write_bytes(PNG)
        out = self.root / "edited.png"
        response = self.root / "edit-response.json"
        code, _, stderr = self.run_cli(
            [
                "edit",
                "--env-file",
                str(self.env_file),
                "--prompt",
                "change the color",
                "--image",
                str(first),
                "--image",
                str(second),
                "--out",
                str(out),
                "--response-out",
                str(response),
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertTrue(out.is_file())
        request = ApiHandler.requests[0]
        self.assertEqual(request["path"], "/v1/images/edits")
        self.assertIn("multipart/form-data; boundary=", request["headers"]["Content-Type"])
        self.assertEqual(request["body"].count(b'name="image"'), 2)
        self.assertIn(b"change the color", request["body"])

    def test_codex_provider_fallback(self) -> None:
        codex_home = self.root / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            'model_provider = "custom"\n'
            '[model_providers.custom]\n'
            f'base_url = "{self.base_url}/v1"\n'
            'experimental_bearer_token = "codex-sentinel"\n',
            encoding="utf-8",
        )
        code, stdout, stderr = self.run_cli(
            ["generate", "--source", "codex", "--dry-run"],
            {"CODEX_HOME": str(codex_home)},
        )
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["credential_source"], "Codex model provider 'custom'")
        self.assertNotIn("codex-sentinel", stdout + stderr)

    def test_incomplete_pair_is_not_mixed_with_codex(self) -> None:
        self.env_file.write_text(
            f"IMAGE2_P_BASE_URL={self.base_url}\n", encoding="utf-8"
        )
        code, stdout, stderr = self.run_cli(
            ["generate", "--env-file", str(self.env_file), "--dry-run"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Incomplete variable file common values", stderr)

    def test_complete_higher_priority_pair_ignores_broken_lower_pair(self) -> None:
        process_env = {
            "IMAGE2_P_GENERATE_BASE_URL": self.base_url,
            "IMAGE2_P_GENERATE_API_KEY": "operation-key",
            "OPENAI_BASE_URL": "http://lower-priority.invalid",
        }
        code, stdout, stderr = self.run_cli(
            ["generate", "--dry-run"], process_env
        )
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["credential_source"], "process operation variables")

    def test_file_pair_precedes_legacy_openai_pair(self) -> None:
        code, stdout, stderr = self.run_cli(
            ["generate", "--env-file", str(self.env_file), "--dry-run"],
            {
                "OPENAI_BASE_URL": "http://127.0.0.1:9",
                "OPENAI_API_KEY": "legacy-key",
            },
        )
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["credential_source"], "variable file common values")
        self.assertEqual(summary["endpoint"], f"{self.base_url}/v1/images/generations")

    def test_edit_refuses_to_overwrite_source_before_request(self) -> None:
        source = self.root / "source.png"
        source.write_bytes(PNG)
        response = self.root / "response.json"
        code, stdout, stderr = self.run_cli(
            [
                "edit",
                "--env-file",
                str(self.env_file),
                "--prompt",
                "change it",
                "--image",
                str(source),
                "--out",
                str(source),
                "--response-out",
                str(response),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("must not overwrite", stderr)
        self.assertEqual(ApiHandler.requests, [])

    def test_api_error_redacts_echoed_key(self) -> None:
        ApiHandler.echo_secret_error = True
        code, stdout, stderr = self.run_cli(
            ["generate", "--env-file", str(self.env_file)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("<redacted>", stderr)
        self.assertNotIn("sentinel-test-key", stderr)

    def test_success_response_is_redacted_before_persisting(self) -> None:
        ApiHandler.echo_secret_success = True
        out = self.root / "safe.png"
        response = self.root / "safe-response.json"
        code, _, stderr = self.run_cli(
            [
                "generate",
                "--env-file",
                str(self.env_file),
                "--out",
                str(out),
                "--response-out",
                str(response),
            ]
        )
        self.assertEqual(code, 0, stderr)
        saved = response.read_text(encoding="utf-8")
        self.assertNotIn("sentinel-test-key", saved)
        self.assertIn("Bearer <redacted>", saved)
        self.assertNotIn("signed-secret", saved)
        self.assertIn("?<redacted>", saved)

    def test_dry_run_redacts_image_url_query(self) -> None:
        code, stdout, stderr = self.run_cli(
            [
                "edit",
                "--env-file",
                str(self.env_file),
                "--prompt",
                "change it",
                "--image-url",
                "https://cdn.example.test/input.png?token=signed-secret",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("signed-secret", stdout)
        self.assertIn("?<redacted>", stdout)

    def test_non_loopback_http_requires_opt_in(self) -> None:
        env_file = self.root / "remote.env"
        env_file.write_text(
            "IMAGE2_P_BASE_URL=http://192.0.2.1:3000\n"
            "IMAGE2_P_API_KEY=sentinel\n",
            encoding="utf-8",
        )
        code, _, stderr = self.run_cli(
            ["generate", "--env-file", str(env_file), "--dry-run"]
        )
        self.assertEqual(code, 1)
        self.assertIn("Refusing non-loopback HTTP", stderr)

    def test_lookalike_loopback_hostname_is_not_trusted(self) -> None:
        self.assertFalse(image2_p.is_loopback_host("127.attacker.example"))
        with self.assertRaises(image2_p.Image2PError):
            image2_p.ensure_transport_allowed(
                "http://127.attacker.example/v1/images/generations", False
            )

    def test_returned_private_url_on_another_host_is_rejected(self) -> None:
        for url in (
            "http://127.0.0.1/private.png",
            "http://localhost/private.png",
        ):
            with self.subTest(url=url), self.assertRaises(image2_p.Image2PError):
                image2_p.ensure_download_target_allowed(
                    url,
                    "https://images.example.test/v1/images/generations",
                )

    def test_returned_same_host_on_different_local_port_is_rejected(self) -> None:
        with self.assertRaises(image2_p.Image2PError):
            image2_p.ensure_download_target_allowed(
                "http://127.0.0.1:9999/private.png",
                "http://127.0.0.1:3000/v1/images/generations",
            )

    def test_returned_dns_host_resolving_private_is_rejected(self) -> None:
        records = [
            (2, 1, 6, "", ("169.254.169.254", 443)),
        ]
        with mock.patch.object(image2_p.socket, "getaddrinfo", return_value=records):
            with self.assertRaises(image2_p.Image2PError):
                image2_p.ensure_download_target_allowed(
                    "https://metadata.internal/image.png",
                    "https://images.example.test/v1/images/generations",
                )

    def test_base_url_query_is_rejected_cleanly(self) -> None:
        with self.assertRaisesRegex(image2_p.Image2PError, "query string"):
            image2_p.build_endpoint(
                "https://example.test/v1?api-version=x", "generate"
            )

    def test_response_cannot_exceed_requested_image_count(self) -> None:
        response = {
            "data": [
                {"b64_json": base64.b64encode(PNG).decode()},
                {"b64_json": base64.b64encode(PNG).decode()},
            ]
        }
        with self.assertRaisesRegex(image2_p.Image2PError, "more images"):
            image2_p.extract_images(
                response,
                timeout=1,
                endpoint="https://example.test/v1/images/generations",
                allow_insecure=False,
                expected_count=1,
            )

    def test_malformed_default_file_does_not_block_forced_codex(self) -> None:
        bad_default = self.root / "bad.env"
        bad_default.write_text("not-an-assignment", encoding="utf-8")
        codex_home = self.root / "codex-malformed-env"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            'model_provider = "custom"\n'
            '[model_providers.custom]\n'
            f'base_url = "{self.base_url}/v1"\n'
            'experimental_bearer_token = "codex-sentinel"\n',
            encoding="utf-8",
        )
        original = image2_p.DEFAULT_ENV_FILE
        image2_p.DEFAULT_ENV_FILE = bad_default
        try:
            code, _, stderr = self.run_cli(
                ["generate", "--source", "codex", "--dry-run"],
                {"CODEX_HOME": str(codex_home)},
            )
        finally:
            image2_p.DEFAULT_ENV_FILE = original
        self.assertEqual(code, 0, stderr)


if __name__ == "__main__":
    unittest.main()
