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
