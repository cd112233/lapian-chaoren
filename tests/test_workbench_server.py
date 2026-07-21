from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from io import BytesIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_PATH = SRC / "workbench_server.py"


def load_module():
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("workbench_server", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkbenchServerTests(unittest.TestCase):
    def test_safe_filename_rejects_non_video(self) -> None:
        module = load_module()
        self.assertEqual(module.safe_filename("%E6%89%93%E6%96%97%20%E5%8F%82%E8%80%83.mp4"), "打斗 参考.mp4")
        long_name = module.safe_filename(f"{'a' * 300}.mp4")
        self.assertLessEqual(len(long_name), 180)
        self.assertTrue(long_name.endswith(".mp4"))
        with self.assertRaises(ValueError):
            module.safe_filename("notes.txt")

    def test_workbench_inline_script_has_valid_javascript(self) -> None:
        document = (SRC / "workbench.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script>\s*([\s\S]*?)\s*</script>", document)
        self.assertEqual(len(scripts), 1)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "workbench.js"
            script.write_text(scripts[0], encoding="utf-8")
            result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_streamed_upload_hashes_and_moves_atomically(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source" / "fight.mp4"
            fingerprint = module.write_upload(BytesIO(b"abcdef"), target, 6, chunk_size=2)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertEqual(len(fingerprint), 64)
            self.assertFalse(target.with_suffix(".mp4.uploading").exists())

    def test_local_api_accepts_raw_video_and_lists_project(self) -> None:
        module = load_module()
        module.process_project = lambda store, project_id: None
        with tempfile.TemporaryDirectory() as directory:
            server = module.WorkbenchHTTPServer(("127.0.0.1", 0), module.WorkbenchHandler)
            server.store = module.ProjectStore(Path(directory))
            server.max_upload_bytes = 1024 * 1024
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/projects",
                    data=b"small-video",
                    headers={"X-Filename": "fight.mp4", "X-Project-Title": "Learning Test"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    created = json.load(response)["project"]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/projects") as response:
                    projects = json.load(response)["projects"]
                range_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/projects/{created['project_id']}/source/fight.mp4",
                    headers={"Range": "bytes=0-4"},
                )
                with urllib.request.urlopen(range_request) as response:
                    range_status = response.status
                    range_header = response.headers["Content-Range"]
                    range_body = response.read()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(created["status"], "queued")
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["title"], "Learning Test")
        self.assertEqual(range_status, 206)
        self.assertEqual(range_header, "bytes 0-4/11")
        self.assertEqual(range_body, b"small")


if __name__ == "__main__":
    unittest.main()
