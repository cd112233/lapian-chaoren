from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
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
        self.assertEqual(module.safe_filename("camera_capture.data", "video/x-custom"), "camera_capture.data")
        self.assertTrue(module.safe_filename("camera_capture", "video/mp4").endswith(".input"))
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
        self.assertIn('id="uploadProgressBar"', document)
        self.assertIn('id="deleteDialog"', document)
        self.assertIn('id="locationDialog"', document)
        self.assertIn('id="comparisonDialog"', document)
        self.assertIn('class="compare-project"', document)
        self.assertIn("/comparisons`,", document)
        self.assertIn("data-comparison-delete", document)
        self.assertIn('id="projectsRootInput"', document)
        self.assertIn("method: 'DELETE'", document)
        self.assertIn("method: 'PUT'", document)
        self.assertIn("project.progress_current", document)
        self.assertIn("project.progress_phase === 'detecting'", document)
        self.assertIn("video/*", document)
        self.assertIn("自动识别编码", document)

    def test_streamed_upload_hashes_and_moves_atomically(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source" / "fight.mp4"
            fingerprint = module.write_upload(BytesIO(b"abcdef"), target, 6, chunk_size=2)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertEqual(len(fingerprint), 64)
            self.assertFalse(target.with_suffix(".mp4.uploading").exists())

    def test_progress_event_parser_accepts_only_structured_updates(self) -> None:
        module = load_module()
        event = module.parse_progress_event(
            json.dumps(
                {
                    "type": "progress",
                    "phase": "evidence",
                    "progress": 63,
                    "stage": "正在生成逐镜证据",
                    "detail": "SH012",
                    "current": 12,
                    "total": 77,
                    "ignored": "not persisted",
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(
            event,
            {
                "progress": 63,
                "stage": "正在生成逐镜证据",
                "progress_detail": "SH012",
                "progress_phase": "evidence",
                "progress_current": 12,
                "progress_total": 77,
            },
        )
        self.assertIsNone(module.parse_progress_event("not json"))
        self.assertIsNone(module.parse_progress_event('{"type":"log","progress":20}'))
        self.assertIsNone(module.parse_progress_event('{"type":"progress","progress":"bad"}'))

    def test_project_store_delete_is_confined_to_project_root(self) -> None:
        module = load_module()
        project_id = "20260721-120000-abcdef12"
        with tempfile.TemporaryDirectory() as directory:
            store = module.ProjectStore(Path(directory))
            store.write(project_id, {"project_id": project_id, "title": "Delete Test", "status": "ready"})
            evidence = store.project_root(project_id) / "analysis" / "review.html"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("review", encoding="utf-8")
            deleted = store.delete(project_id)
            self.assertEqual(deleted["title"], "Delete Test")
            self.assertFalse(store.project_root(project_id).exists())
            with self.assertRaises(ValueError):
                store.delete("../../outside")

    def test_project_store_marks_interrupted_work_as_failed(self) -> None:
        module = load_module()
        project_id = "20260721-120003-abcdef12"
        with tempfile.TemporaryDirectory() as directory:
            store = module.ProjectStore(Path(directory))
            store.write(
                project_id,
                {
                    "project_id": project_id,
                    "title": "Interrupted",
                    "status": "analyzing",
                    "progress": 63,
                },
            )
            self.assertEqual(store.recover_interrupted(), 1)
            recovered = store.read(project_id)
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["progress"], 63)
        self.assertEqual(recovered["stage"], "上次处理被中断")

    def test_comparison_store_is_confined_and_recovers_interrupted_work(self) -> None:
        module = load_module()
        project_id = "20260721-120006-abcdef12"
        comparison_id = "cmp-20260721-120007-deadbeef"
        with tempfile.TemporaryDirectory() as directory:
            store = module.ProjectStore(Path(directory))
            store.write(project_id, {"project_id": project_id, "title": "Reference", "status": "ready"})
            store.write_comparison(
                project_id,
                comparison_id,
                {
                    "project_id": project_id,
                    "comparison_id": comparison_id,
                    "title": "Interrupted comparison",
                    "status": "comparing",
                },
            )
            self.assertEqual(store.recover_interrupted(), 1)
            recovered = store.read_comparison(project_id, comparison_id)
            with self.assertRaises(ValueError):
                store.read_comparison(project_id, "../../outside")
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["stage"], "上次对比被中断")

    def test_delete_api_removes_idle_project_and_rejects_active_project(self) -> None:
        module = load_module()
        idle_id = "20260721-120001-abcdef12"
        active_id = "20260721-120002-abcdef12"
        with tempfile.TemporaryDirectory() as directory:
            server = module.WorkbenchHTTPServer(("127.0.0.1", 0), module.WorkbenchHandler)
            server.store = module.ProjectStore(Path(directory))
            server.max_upload_bytes = 1024 * 1024
            server.store.write(idle_id, {"project_id": idle_id, "title": "Idle", "status": "ready"})
            server.store.write(active_id, {"project_id": active_id, "title": "Active", "status": "analyzing"})
            server.mark_project_active(active_id)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                idle_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/projects/{idle_id}",
                    method="DELETE",
                )
                with urllib.request.urlopen(idle_request) as response:
                    deleted = json.load(response)["deleted"]
                active_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/projects/{active_id}",
                    method="DELETE",
                )
                with self.assertRaises(urllib.error.HTTPError) as blocked:
                    urllib.request.urlopen(active_request)
                active_project_still_exists = server.store.project_root(active_id).exists()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(deleted["project_id"], idle_id)
        self.assertEqual(blocked.exception.code, 409)
        self.assertTrue(active_project_still_exists)

    def test_settings_api_switches_library_without_moving_old_projects(self) -> None:
        module = load_module()
        old_id = "20260721-120004-abcdef12"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_root = root / "old-library"
            new_root = root / "new-library"
            blocked_root = root / "blocked-library"
            server = module.WorkbenchHTTPServer(("127.0.0.1", 0), module.WorkbenchHandler)
            server.store = module.ProjectStore(old_root)
            server.settings_store = module.LocalSettingsStore(root / "settings.json")
            server.default_projects_root = (root / "default-library").resolve()
            server.max_upload_bytes = 1024 * 1024
            server.store.write(old_id, {"project_id": old_id, "title": "Old Project", "status": "ready"})
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings") as response:
                    before = json.load(response)["settings"]

                switch_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/settings/projects-root",
                    data=json.dumps({"path": str(new_root)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                with urllib.request.urlopen(switch_request) as response:
                    after = json.load(response)["settings"]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/projects") as response:
                    projects_after_switch = json.load(response)["projects"]

                server.mark_project_active("20260721-120005-abcdef12")
                blocked_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/settings/projects-root",
                    data=json.dumps({"path": str(blocked_root)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                with self.assertRaises(urllib.error.HTTPError) as blocked:
                    urllib.request.urlopen(blocked_request)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual(before["projects_root"], str(old_root.resolve()))
            self.assertEqual(before["project_count"], 1)
            self.assertEqual(after["projects_root"], str(new_root.resolve()))
            self.assertEqual(projects_after_switch, [])
            self.assertEqual(server.settings_store.projects_root(), new_root.resolve())
            self.assertTrue((old_root / old_id / "project.json").is_file())
            self.assertEqual(blocked.exception.code, 409)
            self.assertEqual(server.current_store().root, new_root.resolve())

    def test_settings_picker_returns_path_without_switching_library(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current"
            selected = root / "selected"
            selected.mkdir()
            server = module.WorkbenchHTTPServer(("127.0.0.1", 0), module.WorkbenchHandler)
            server.store = module.ProjectStore(current)
            server.settings_store = module.LocalSettingsStore(root / "settings.json")
            server.max_upload_bytes = 1024 * 1024
            original_picker = module.select_directory
            module.select_directory = lambda initial: selected.resolve()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/settings/select-projects-root",
                    data=b"",
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    result = json.load(response)
            finally:
                module.select_directory = original_picker
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertFalse(result["cancelled"])
        self.assertEqual(result["path"], str(selected.resolve()))
        self.assertEqual(server.current_store().root, current.resolve())

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

    def test_local_api_accepts_video_mime_with_unknown_extension_for_later_adaptation(self) -> None:
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
                    data=b"video-content",
                    headers={
                        "Content-Type": "video/x-custom",
                        "X-Filename": "camera-capture.data",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    created = json.load(response)["project"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(created["source_filename"], "camera-capture.data")
        self.assertEqual(created["format_adaptation"]["status"], "pending")

    def test_comparison_api_accepts_lists_and_deletes_remake_video(self) -> None:
        module = load_module()
        module.process_comparison = lambda store, project_id, comparison_id: None
        project_id = "20260721-120008-abcdef12"
        with tempfile.TemporaryDirectory() as directory:
            server = module.WorkbenchHTTPServer(("127.0.0.1", 0), module.WorkbenchHandler)
            server.store = module.ProjectStore(Path(directory))
            server.max_upload_bytes = 1024 * 1024
            server.store.write(
                project_id,
                {
                    "project_id": project_id,
                    "title": "Reference",
                    "status": "ready",
                    "source_relpath": "source/reference.mp4",
                },
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/projects/{project_id}/comparisons",
                    data=b"remake-video",
                    headers={"X-Filename": "remake.mov", "X-Comparison-Title": "V1"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    comparison = json.load(response)["comparison"]
                comparison_id = comparison["comparison_id"]
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/projects/{project_id}/comparisons"
                ) as response:
                    comparisons = json.load(response)["comparisons"]
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/projects/{project_id}/comparisons/{comparison_id}"
                ) as response:
                    detail = json.load(response)["comparison"]
                delete_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/projects/{project_id}/comparisons/{comparison_id}",
                    method="DELETE",
                )
                with urllib.request.urlopen(delete_request) as response:
                    deleted = json.load(response)["deleted"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(comparison["status"], "queued")
        self.assertEqual(comparison["title"], "V1")
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(detail["source_filename"], "remake.mov")
        self.assertEqual(deleted["comparison_id"], comparison_id)


if __name__ == "__main__":
    unittest.main()
