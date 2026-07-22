from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "build_review_page.py"


def load_module():
    spec = importlib.util.spec_from_file_location("build_review_page", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReviewPageTests(unittest.TestCase):
    def test_builds_demo_without_template_marker(self) -> None:
        module = load_module()
        source = ROOT / "examples" / "demo" / "demo_shots.yaml"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "review.html"
            module.build(source, output)
            document = output.read_text(encoding="utf-8")
            scripts = re.findall(r"<script>\s*([\s\S]*?)\s*</script>", document)
            app_script = Path(directory) / "review_app.js"
            app_script.write_text(scripts[-1], encoding="utf-8")
            syntax = subprocess.run(
                ["node", "--check", str(app_script)],
                capture_output=True,
                text=True,
            )
        self.assertNotIn("__SHOT_DATA__", document)
        self.assertNotIn("__PROJECT_DATA__", document)
        self.assertNotIn("__APP_JS__", document)
        shot_match = re.search(r"const SHOTS = (\[.*?\]);\s*</script>", document, re.S)
        self.assertIsNotNone(shot_match)
        self.assertEqual(len(json.loads(shot_match.group(1))), 2)
        self.assertIn("审核检查器", document)
        self.assertIn("观看舒适度", document)
        self.assertIn("Scroll and clipping comfort patch", document)
        self.assertIn('id="globalViewBtn"', document)
        self.assertIn('id="aiImportDialog"', document)
        self.assertIn('id="nextAttentionBtn"', document)
        self.assertIn('data-tab="mentor"', document)
        self.assertIn("AI 影像导师", document)
        self.assertIn("function diagnoseShot", document)
        self.assertIn("function renderGlobalView", document)
        self.assertIn("function sendAdvisorQuestion", document)
        self.assertIn("http://127.0.0.1:8766/api/advisor", document)
        self.assertIn('id="aiEnabled"', document)
        self.assertIn('id="learningMode"', document)
        self.assertIn('data-tab="learning"', document)
        self.assertIn('id="exportKnowledge"', document)
        self.assertIn('id="reviewBackupFile"', document)
        self.assertIn("function requestInitialAnalysis", document)
        self.assertIn("scopedStorageKey", document)
        self.assertIn("advisor_asset_prefix", document)
        self.assertIn("if (!path) return ''", document)
        self.assertIn("function promoteKnowledge", document)
        self.assertIn("function buildReplicationTask", document)
        self.assertIn("http://127.0.0.1:8766/api/analyze-shot", document)
        self.assertIn("aiDefaultEnabled: false", document)
        self.assertIn('data-view-tone', document)
        self.assertIn("window.scrollTo({ top: 0, behavior: 'auto' })", document)
        self.assertIn('id="mediaFormat"', document)
        self.assertIn("function setMediaGeometry", document)
        self.assertIn("dataset.orientation", document)
        self.assertIn("data-orientation=portrait", document)
        self.assertIn("data-orientation=ultrawide", document)
        self.assertIn("--asset-height-ratio", document)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        ids = re.findall(r'\sid="([^"]+)"', document)
        self.assertEqual(len(ids), len(set(ids)), "generated page contains duplicate ids")

    def test_loads_project_metadata_for_learning_contract(self) -> None:
        module = load_module()
        source = ROOT / "examples" / "demo" / "demo_shots.yaml"
        project, shots = module.load_payload(source)
        self.assertEqual(len(shots), 2)
        self.assertEqual(project["shot_count"], 2)
        self.assertTrue(project["project_id"])

    def test_inline_json_blocks_script_breakout(self) -> None:
        module = load_module()
        value = module.safe_json({"text": "</script>&"})
        self.assertNotIn("</script>", value)
        self.assertIn("\\u003c/script\\u003e\\u0026", value)


if __name__ == "__main__":
    unittest.main()
