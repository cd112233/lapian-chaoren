from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_PATH = SRC / "build_comparison_page.py"


def load_module():
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("build_comparison_page", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComparisonPageTests(unittest.TestCase):
    def test_builds_self_contained_comparison_workspace(self) -> None:
        module = load_module()
        comparison = {
            "comparison_id": "cmp-20260723-120000-deadbeef",
            "project_id": "20260723-110000-abcdef12",
            "title": "复刻 V1",
            "status": "ready",
            "progress": 100,
            "reference": {"title": "原片", "video_path": "source/reference.mp4"},
            "remake": {"title": "复刻", "video_path": "comparisons/cmp/source/remake.mp4"},
            "summary": {"reference_shot_count": 1, "remake_shot_count": 1},
            "dimensions": [],
            "alignment": {"mode": "automatic", "pairs": []},
            "findings": [],
            "audit_bundle": {
                "review_status": "human_review_completed",
                "verdict": {"label": "不通过忠实模仿", "summary": "镜头语言未稳定复现。"},
                "classification_categories": [
                    {
                        "key": "rebuild_or_post_insert",
                        "label": "重做或补插帧",
                        "tone": "rebuild",
                        "count": 1,
                        "shot_ids": ["SH001"],
                    }
                ],
                "shots": [
                    {
                        "shot_id": "SH001",
                        "segment_id": "SEG01",
                        "classification": "rebuild_or_post_insert",
                        "classification_label": "重做或补插帧",
                        "tone": "rebuild",
                    }
                ],
                "prompt_contract": {"checks_passed": 1, "checks_total": 1, "segments": []},
            },
            "review": {"pair_reviews": {}, "technical_identifications": [], "fix_queue": [], "overall_note": ""},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "comparison.json"
            output = root / "comparison.html"
            manifest.write_text(json.dumps(comparison, ensure_ascii=False), encoding="utf-8")
            module.build(manifest, output)
            document = output.read_text(encoding="utf-8")
            scripts = re.findall(r"<script>\s*([\s\S]*?)\s*</script>", document)
            app = root / "comparison_app.js"
            app.write_text(scripts[-1], encoding="utf-8")
            syntax = subprocess.run(["node", "--check", str(app)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertNotIn("__COMPARISON_DATA__", document)
        self.assertNotIn("__COMPARISON_APP_JS__", document)
        self.assertIn("双轨声音", document)
        self.assertIn("审查结论", document)
        self.assertIn("function renderAudit", document)
        self.assertIn("七十七镜分流", document)
        self.assertIn("关键事件段", document)
        self.assertIn("function renderEvents", document)
        self.assertIn("逐段审核", document)
        self.assertIn("你的审核", document)
        self.assertIn("AI 漏识别", document)
        self.assertIn("AI 写错", document)
        self.assertIn("历史草稿 · 已被人工覆盖", document)
        self.assertIn("已采纳的终审结论", document)
        self.assertIn("确认到位", document)
        self.assertIn("镜头形态", document)
        self.assertIn("feedbackAxis", document)
        self.assertIn("data-event-verdict", document)
        self.assertIn("feedback_entries", document)
        self.assertIn("逐镜证据与高级结论", document)
        self.assertIn("机器候选", document)
        self.assertIn("帧只用于定位证据", document)
        self.assertIn("关键节点对照", document)
        self.assertIn("电影技术鉴定", document)
        self.assertIn("function applyAlignment", document)
        self.assertIn('data-orientation="landscape"', document)
        self.assertIn("data-orientation=portrait", document)
        self.assertIn("body{min-width:0", document)
        self.assertIn("object-fit:contain", document)
        ids = re.findall(r'\sid="([^"]+)"', document)
        self.assertEqual(len(ids), len(set(ids)), "comparison page contains duplicate static ids")

    def test_inline_json_blocks_script_breakout(self) -> None:
        module = load_module()
        value = module.safe_json({"title": "</script>&"})
        self.assertNotIn("</script>", value)
        self.assertIn("\\u003c/script\\u003e\\u0026", value)


if __name__ == "__main__":
    unittest.main()
