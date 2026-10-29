"""Security boundary tests for the local workbench and AI advisor servers.

Covers Host validation (DNS rebinding), Origin checks, the per-process write
token, refusal of CORS "simple request" body types, the upload disk-space
guard and loopback-only startup.
"""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SRC / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def request(port: int, method: str, path: str, *, host: str | None = None, headers=None, body: bytes | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        for key, value in (headers or {}).items():
            connection.putheader(key, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()


class WorkbenchSecurityTests(unittest.TestCase):
    IDLE_ID = "20260721-120001-abcdef12"

    def setUp(self) -> None:
        self.module = load("workbench_server")
        self.module.process_project = lambda store, project_id: None
        self.tmp = tempfile.TemporaryDirectory()
        self.server = self.module.WorkbenchHTTPServer(("127.0.0.1", 0), self.module.WorkbenchHandler)
        self.server.store = self.module.ProjectStore(Path(self.tmp.name) / "library")
        self.server.settings_store = self.module.LocalSettingsStore(Path(self.tmp.name) / "settings.json")
        self.server.max_upload_bytes = 1024 * 1024
        self.server.store.write(self.IDLE_ID, {"project_id": self.IDLE_ID, "title": "Idle", "status": "ready"})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.token = self.server.api_token

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def auth(self, **extra: str) -> dict[str, str]:
        return {"X-Workbench-Token": self.token, **extra}

    def test_token_is_random_per_process(self) -> None:
        other = self.module.WorkbenchHTTPServer(("127.0.0.1", 0), self.module.WorkbenchHandler)
        try:
            self.assertGreaterEqual(len(self.token), 32)
            self.assertNotEqual(self.token, other.api_token)
        finally:
            other.server_close()

    def test_bad_host_header_is_rejected(self) -> None:
        for host in ("attacker.example", f"attacker.example:{self.port}", "127.0.0.1:1", ""):
            status, _, body = request(self.port, "GET", "/api/session", host=host)
            self.assertEqual(status, 403, host)
            self.assertNotIn(self.token.encode(), body)
        status, _, _ = request(
            self.port, "DELETE", f"/api/projects/{self.IDLE_ID}", host="attacker.example", headers=self.auth()
        )
        self.assertEqual(status, 403)
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"):
            status, _, _ = request(self.port, "GET", "/api/health", host=host)
            self.assertEqual(status, 200, host)

    def test_page_and_session_endpoint_deliver_token(self) -> None:
        status, headers, body = request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        self.assertIn(f'content="{self.token}"', page)
        self.assertNotIn(self.module.TOKEN_PLACEHOLDER, page)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))
        status, headers, body = request(self.port, "GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["token"], self.token)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_cross_origin_write_is_rejected_even_with_token(self) -> None:
        for origin in ("https://evil.example", "null", f"http://127.0.0.1:{self.port}.evil.example", "http://127.0.0.1:1"):
            status, _, _ = request(
                self.port,
                "DELETE",
                f"/api/projects/{self.IDLE_ID}",
                headers=self.auth(Origin=origin),
            )
            self.assertEqual(status, 403, origin)
        self.assertTrue(self.server.store.project_root(self.IDLE_ID).exists())

    def test_missing_or_wrong_token_is_rejected(self) -> None:
        for headers in ({}, {"X-Workbench-Token": ""}, {"X-Workbench-Token": "wrong"}, {"X-Workbench-Token": self.token + "x"}):
            for method, path in (
                ("DELETE", f"/api/projects/{self.IDLE_ID}"),
                ("POST", "/api/settings/select-projects-root"),
                ("PATCH", f"/api/projects/{self.IDLE_ID}"),
            ):
                status, _, _ = request(self.port, method, path, headers=headers)
                self.assertEqual(status, 403, (method, headers))
        status, _, _ = request(
            self.port,
            "POST",
            "/api/projects",
            headers={"Content-Type": "video/mp4", "X-Filename": "a.mp4"},
            body=b"video",
        )
        self.assertEqual(status, 403)
        self.assertEqual(len(self.server.store.list()), 1)
        self.assertTrue(self.server.store.project_root(self.IDLE_ID).exists())

    def test_valid_same_origin_requests_with_token_are_accepted(self) -> None:
        status, _, body = request(
            self.port,
            "POST",
            "/api/projects",
            headers=self.auth(
                **{"Content-Type": "video/mp4", "X-Filename": "fight.mp4", "Origin": f"http://127.0.0.1:{self.port}"}
            ),
            body=b"small-video",
        )
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["project"]["source_filename"], "fight.mp4")
        status, _, body = request(
            self.port,
            "DELETE",
            f"/api/projects/{self.IDLE_ID}",
            host=f"localhost:{self.port}",
            headers=self.auth(Origin=f"http://localhost:{self.port}"),
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["deleted"]["project_id"], self.IDLE_ID)

    def test_simple_request_content_types_are_rejected(self) -> None:
        for content_type in ("text/plain", "text/plain;charset=UTF-8", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
            status, _, _ = request(
                self.port,
                "PUT",
                "/api/settings/projects-root",
                headers=self.auth(**{"Content-Type": content_type}),
                body=json.dumps({"path": self.tmp.name}).encode(),
            )
            self.assertEqual(status, 415, content_type)
            status, _, _ = request(
                self.port,
                "POST",
                "/api/projects",
                headers=self.auth(**{"Content-Type": content_type, "X-Filename": "a.mp4"}),
                body=b"video",
            )
            self.assertEqual(status, 415, content_type)
        # JSON endpoints also refuse other non-JSON bodies.
        status, _, _ = request(
            self.port,
            "PUT",
            "/api/settings/projects-root",
            headers=self.auth(**{"Content-Type": "application/octet-stream"}),
            body=json.dumps({"path": self.tmp.name}).encode(),
        )
        self.assertEqual(status, 400)
        self.assertEqual(len(self.server.store.list()), 1)

    def test_upload_is_rejected_when_disk_space_is_insufficient(self) -> None:
        self.server.upload_disk_margin_bytes = 1 << 62
        status, _, body = request(
            self.port,
            "POST",
            "/api/projects",
            headers=self.auth(**{"Content-Type": "video/mp4", "X-Filename": "big.mp4"}),
            body=b"video",
        )
        self.assertEqual(status, 507, body)
        self.assertEqual(len(self.server.store.list()), 1)
        status, _, body = request(
            self.port,
            "POST",
            f"/api/projects/{self.IDLE_ID}/comparisons",
            headers=self.auth(**{"Content-Type": "video/mp4", "X-Filename": "remake.mp4"}),
            body=b"video",
        )
        self.assertEqual(status, 507, body)

    def test_legacy_comparison_page_is_rebuilt_with_token_wiring(self) -> None:
        comparison_id = "cmp-20260721-120002-abcdef12"
        self.server.store.write_comparison(
            self.IDLE_ID,
            comparison_id,
            {"comparison_id": comparison_id, "project_id": self.IDLE_ID, "title": "V1", "status": "ready"},
        )
        page = self.server.store.comparison_root(self.IDLE_ID, comparison_id) / "comparison.html"
        page.write_text("<html>legacy page without token</html>", encoding="utf-8")
        status, _, body = request(
            self.port, "GET", f"/projects/{self.IDLE_ID}/comparisons/{comparison_id}/comparison.html"
        )
        self.assertEqual(status, 200)
        self.assertIn(b"X-Workbench-Token", body)
        self.assertNotIn(self.token.encode(), body)


class WorkbenchStartupTests(unittest.TestCase):
    def test_loopback_detection(self) -> None:
        module = load("workbench_server")
        for host in ("127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]"):
            self.assertTrue(module.is_loopback_host(host), host)
        for host in ("0.0.0.0", "::", "192.168.1.10", "example.com", ""):
            self.assertFalse(module.is_loopback_host(host), host)

    def test_non_loopback_host_refuses_to_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SRC / "workbench_server.py"),
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "0",
                    "--no-open",
                    "--projects-root",
                    directory,
                    "--settings-path",
                    str(Path(directory) / "settings.json"),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--allow-remote", result.stderr)
        self.assertNotIn("Lapian local workbench:", result.stdout)


class FrontendTokenWiringTests(unittest.TestCase):
    def test_workbench_page_sends_token_on_every_write(self) -> None:
        document = (SRC / "workbench.html").read_text(encoding="utf-8")
        self.assertIn('<meta name="lapian-workbench-token" content="__LAPIAN_WORKBENCH_TOKEN__">', document)
        write_fetches = re.findall(r"fetch\([^\n]*?method: '(?:POST|PUT|DELETE|PATCH)'[^\n]*", document)
        multiline_put = re.findall(r"method: '(?:POST|PUT|DELETE|PATCH)',\n\s*headers: writeHeaders", document)
        self.assertTrue(all("writeHeaders(" in line for line in write_fetches), write_fetches)
        total_writes = len(re.findall(r"method: '(?:POST|PUT|DELETE|PATCH)'", document))
        self.assertEqual(total_writes, len(write_fetches) + len(multiline_put))
        xhr_posts = len(re.findall(r"request\.open\('POST'", document))
        self.assertEqual(xhr_posts, len(re.findall(r"setRequestHeader\(TOKEN_HEADER, WORKBENCH_TOKEN\)", document)))

    def test_comparison_app_sends_token_on_every_write(self) -> None:
        script = (SRC / "comparison_app.js").read_text(encoding="utf-8")
        self.assertIn("fetch('/api/session'", script)
        writes = re.findall(r"method: '(?:POST|PUT|DELETE|PATCH)'[^\n]*", script)
        self.assertTrue(writes)
        for line in re.findall(r"fetch\([^\n]*method: '(?:POST|PUT|DELETE|PATCH)'[^\n]*", script):
            self.assertIn("jsonWriteHeaders(", line)
        self.assertEqual(script.count("headers: { 'Content-Type': 'application/json' }"), 0)
        self.assertNotIn("EventSource", script)


class AdvisorSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load("ai_advisor_server")
        self.server = self.module.AdvisorHTTPServer(("127.0.0.1", 0), self.module.AdvisorHandler)
        self.server.config = self.module.AdvisorConfig(provider="mock", model="film-advisor-mock")
        self.server.system_prompt = self.module.load_system_prompt(self.module.DEFAULT_PROMPT)
        self.server.initial_prompt = self.module.load_system_prompt(self.module.DEFAULT_INITIAL_PROMPT)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.body = json.dumps(
            {"shot_id": "SH001", "mode": "lighting", "question": "这是什么灯？", "context": {"shot_id": "SH001", "assets": {}}},
            ensure_ascii=False,
        ).encode("utf-8")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_origin_null_is_rejected_and_never_echoed(self) -> None:
        for method, body in (("OPTIONS", None), ("GET", None), ("POST", self.body)):
            path = "/api/health" if method == "GET" else "/api/advisor"
            status, headers, _ = request(
                self.port, method, path, headers={"Origin": "null", "Content-Type": "application/json"}, body=body
            )
            self.assertEqual(status, 403, method)
            self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "null")
            self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_only_exact_workbench_origins_are_echoed(self) -> None:
        status, headers, _ = request(
            self.port, "OPTIONS", "/api/advisor", headers={"Origin": "http://localhost:8765"}
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://localhost:8765")
        for origin in ("http://127.0.0.1:3000", "https://evil.example", "http://localhost"):
            status, headers, _ = request(self.port, "OPTIONS", "/api/advisor", headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
            self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_post_requires_json_content_type(self) -> None:
        status, _, _ = request(
            self.port,
            "POST",
            "/api/advisor",
            headers={"Origin": "http://127.0.0.1:8765", "Content-Type": "text/plain"},
            body=self.body,
        )
        self.assertEqual(status, 415)

    def test_bad_host_is_rejected(self) -> None:
        status, _, _ = request(self.port, "GET", "/api/health", host="attacker.example")
        self.assertEqual(status, 403)
        status, _, _ = request(self.port, "GET", "/api/health")
        self.assertEqual(status, 200)

    def test_cli_api_key_is_deprecated_with_warning(self) -> None:
        argv = ["ai_advisor_server.py", "--provider", "mock", "--api-key", "sk-test", "--workbench-port", "9001"]
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(stderr):
            config = self.module.config_from_args(self.module.parse_args())
        self.assertIn("deprecated", stderr.getvalue())
        self.assertEqual(config.api_key, "sk-test")
        self.assertIn("http://127.0.0.1:9001", config.allowed_origins)
        self.assertNotIn("http://127.0.0.1:8765", config.allowed_origins)
        self.assertFalse(config.allow_file_origin)
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["ai_advisor_server.py", "--provider", "mock"]),
            mock.patch.dict("os.environ", {"AI_ADVISOR_API_KEY": "sk-env"}),
            contextlib.redirect_stderr(stderr),
        ):
            config = self.module.config_from_args(self.module.parse_args())
        self.assertEqual(config.api_key, "sk-env")
        self.assertNotIn("deprecated", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
