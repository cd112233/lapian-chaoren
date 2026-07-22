from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_PATH = ROOT / "src" / "ai_advisor_server.py"
sys.path.insert(0, str(SRC))


def load_module():
    spec = importlib.util.spec_from_file_location("ai_advisor_server", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AIAdvisorTests(unittest.TestCase):
    def test_builds_professional_context_messages(self) -> None:
        module = load_module()
        payload = module.validate_payload(
            {
                "shot_id": "sh018",
                "mode": "camera",
                "question": "人物被吸近后画面变大，这叫什么？",
                "conversation": [{"role": "assistant", "text": "先看背景透视。"}],
                "context": {
                    "shot_id": "SH018",
                    "title": "风场吸近后抬腿踹击",
                    "evidence": {"camera_motion": "机位基本固定"},
                },
            }
        )
        messages = module.build_messages(payload, "system prompt")
        self.assertEqual(payload["shot_id"], "SH018")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("机位基本固定", messages[-1]["content"])
        self.assertIn("用户问题", messages[-1]["content"])

    def test_visual_assets_are_confined_to_configured_root(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frame.jpg").write_bytes(b"\xff\xd8mock-jpeg\xff\xd9")
            payload = {
                "context": {
                    "assets": {
                        "start_frame": "frame.jpg",
                        "middle_frame": "../outside.jpg",
                    }
                }
            }
            images = module.collect_visual_assets(payload, root)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["label"], "首帧")
        self.assertTrue(images[0]["data"])

    def test_visual_asset_root_follows_saved_project_library(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first-library"
            second = root / "second-library"
            relative_frame = Path("project-id") / "analysis" / "frame.jpg"
            for library, content in ((first, b"first"), (second, b"second")):
                frame = library / relative_frame
                frame.parent.mkdir(parents=True)
                frame.write_bytes(content)
            settings = module.LocalSettingsStore(root / "settings.json")
            settings.save_projects_root(first)
            config = module.AdvisorConfig(
                provider="mock",
                model="film-advisor-mock",
                project_settings=settings,
            )
            payload = {"context": {"assets": {"start_frame": relative_frame.as_posix()}}}

            first_images = module.collect_visual_assets(payload, config.current_asset_root())
            settings.save_projects_root(second)
            second_images = module.collect_visual_assets(payload, config.current_asset_root())

        self.assertEqual(len(first_images), 1)
        self.assertEqual(len(second_images), 1)
        self.assertNotEqual(first_images[0]["data"], second_images[0]["data"])
        self.assertEqual(config.public_dict()["asset_root_mode"], "project-library")

    def test_browser_origin_allowlist(self) -> None:
        module = load_module()
        self.assertTrue(module.is_allowed_browser_origin(None))
        self.assertTrue(module.is_allowed_browser_origin("null"))
        self.assertTrue(module.is_allowed_browser_origin("http://127.0.0.1:8765"))
        self.assertTrue(module.is_allowed_browser_origin("http://localhost:8765"))
        self.assertFalse(module.is_allowed_browser_origin("https://example.com"))
        self.assertFalse(module.is_allowed_browser_origin("http://127.0.0.1.example.com"))

    def test_mock_gateway_streams_ndjson_and_health(self) -> None:
        module = load_module()
        config = module.AdvisorConfig(provider="mock", model="film-advisor-mock")
        server = module.AdvisorHTTPServer(("127.0.0.1", 0), module.AdvisorHandler)
        server.config = config
        server.system_prompt = module.load_system_prompt(module.DEFAULT_PROMPT)
        server.initial_prompt = module.load_system_prompt(module.DEFAULT_INITIAL_PROMPT)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health") as response:
                health = json.load(response)
                self.assertIsNone(response.headers["Access-Control-Allow-Origin"])
                self.assertEqual(response.headers["Access-Control-Allow-Private-Network"], "true")
            self.assertTrue(health["ok"])
            self.assertEqual(health["provider"], "mock")

            body = json.dumps(
                {
                    "shot_id": "SH001",
                    "mode": "lighting",
                    "question": "人物头发边缘有一圈亮光，这是什么灯？",
                    "context": {"shot_id": "SH001", "assets": {}},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/advisor",
                data=body,
                headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://127.0.0.1:8765")
                events = [json.loads(line) for line in response.read().decode("utf-8").splitlines()]
            remote_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/health",
                headers={"Origin": "https://example.com"},
            )
            with self.assertRaises(urllib.error.HTTPError) as blocked:
                urllib.request.urlopen(remote_request)
            self.assertEqual(blocked.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(events[0]["type"], "meta")
        self.assertTrue(any(event["type"] == "delta" for event in events))
        done = next(event for event in events if event["type"] == "done")
        self.assertIn("轮廓光", done["answer"])
        self.assertIn("还需确认", done["answer"])
        self.assertIn("模拟 provider", done["answer"])
        self.assertIn("低可信", done["answer"])

    def test_mock_gateway_returns_structured_initial_breakdown(self) -> None:
        module = load_module()
        config = module.AdvisorConfig(provider="mock", model="film-advisor-mock")
        server = module.AdvisorHTTPServer(("127.0.0.1", 0), module.AdvisorHandler)
        server.config = config
        server.system_prompt = module.load_system_prompt(module.DEFAULT_PROMPT)
        server.initial_prompt = module.load_system_prompt(module.DEFAULT_INITIAL_PROMPT)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            body = json.dumps(
                {
                    "shot_id": "SH018",
                    "fields": ["camera_motion", "timeline_beats"],
                    "context": {"shot_id": "SH018", "assets": {}, "evidence": {}},
                    "trusted_memory": [
                        {
                            "id": "K-SH002-camera",
                            "field": "camera_motion",
                            "conclusion": "主体位移不等于推镜",
                            "reason": "摄影机与主体位移混淆",
                            "evidence": "背景透视保持稳定",
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/analyze-shot",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result["shot_id"], "SH018")
        self.assertEqual(result["provider"], "mock")
        self.assertEqual(set(result["fields"]), {"camera_motion", "timeline_beats"})
        self.assertEqual(result["fields"]["camera_motion"]["confidence"], "low")
        self.assertIn("模拟 provider", result["uncertainties"][0])


if __name__ == "__main__":
    unittest.main()
