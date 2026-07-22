from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "learning_project.py"


def load_module():
    spec = importlib.util.spec_from_file_location("learning_project", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LearningProjectTests(unittest.TestCase):
    def test_builds_review_scaffold_from_mechanical_analysis(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fight.mp4"
            source.write_bytes(b"synthetic-video")
            analysis = root / "analysis"
            analysis.mkdir()
            fields = [
                "shot_id",
                "source_in",
                "source_out",
                "duration_sec",
                "source_start_frame",
                "source_end_frame",
                "start_frame_path",
                "middle_frame_path",
                "end_frame_path",
                "clip_path",
            ]
            with (analysis / "shots.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(
                    {
                        "shot_id": "SH001",
                        "source_in": "00:00:00.000",
                        "source_out": "00:00:01.500",
                        "duration_sec": "1.5",
                        "source_start_frame": "0",
                        "source_end_frame": "36",
                        "start_frame_path": "shots/SH001/start.jpg",
                        "middle_frame_path": "shots/SH001/middle.jpg",
                        "end_frame_path": "shots/SH001/end.jpg",
                        "clip_path": "clips/SH001.mp4",
                    }
                )
            (analysis / "metadata.json").write_text(
                json.dumps(
                    {
                        "probe": {
                            "duration_sec": 1.5,
                            "fps": 24,
                            "display_width": 1080,
                            "display_height": 1920,
                            "display_aspect_ratio": 0.5625,
                            "aspect_ratio_label": "9:16",
                            "orientation": "portrait",
                            "rotation_deg": 0,
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = analysis / "learning_shots.yaml"
            module.build_learning_yaml(
                analysis,
                output,
                project_id="20260721-120000-deadbeef",
                title="动作参考",
                source_path=source,
            )
            payload = yaml.safe_load(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], "2.0")
        self.assertEqual(payload["project"]["title"], "动作参考")
        self.assertEqual(payload["project"]["shot_count"], 1)
        self.assertEqual(payload["project"]["storage_scope"], "20260721-120000-deadbeef")
        self.assertEqual(payload["project"]["advisor_asset_prefix"], "20260721-120000-deadbeef/analysis")
        self.assertEqual(payload["project"]["frame_width"], 1080)
        self.assertEqual(payload["project"]["frame_height"], 1920)
        self.assertEqual(payload["project"]["orientation"], "portrait")
        self.assertEqual(payload["project"]["aspect_ratio_label"], "9:16")
        shot = payload["shots"][0]
        self.assertEqual(shot["shot_id"], "SH001")
        self.assertEqual(shot["dense_contact_sheet"], "shots/SH001/contact_sheet.jpg")
        self.assertEqual(shot["analysis_confidence"], "待核")
        self.assertIn("人工复核", shot["boundary_evidence"])


if __name__ == "__main__":
    unittest.main()
