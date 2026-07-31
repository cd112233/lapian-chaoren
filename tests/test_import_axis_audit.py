from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_PATH = SRC / "import_axis_audit.py"


def load_module():
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("import_axis_audit", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AxisAuditImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_piecewise_mapping_preserves_independent_axis_anchors(self) -> None:
        anchors = [
            {"reference_time_sec": 0.0, "candidate_time_sec": 0.0},
            {"reference_time_sec": 5.0, "candidate_time_sec": 6.0},
            {"reference_time_sec": 10.0, "candidate_time_sec": 10.0},
        ]
        self.assertAlmostEqual(self.module.piecewise_map(2.5, anchors), 3.0)
        self.assertAlmostEqual(self.module.piecewise_map(7.5, anchors), 8.0)

    def test_timecode_and_dense_candidate_ranges_are_parsed(self) -> None:
        self.assertAlmostEqual(self.module.parse_timecode("00:01:02.500"), 62.5)
        self.assertEqual(self.module.parse_candidate_range("1.392_to_3.247"), (1.392, 3.247))
        self.assertIsNone(self.module.parse_candidate_range("unknown"))

    def test_classification_index_keeps_all_three_repair_routes(self) -> None:
        result = self.module.classification_index(
            {
                "shot_classification": {
                    "keep_with_minor_edit": ["SH001"],
                    "partial_or_targeted_repair": ["SH002"],
                    "rebuild_or_post_insert": ["SH003"],
                }
            }
        )
        self.assertEqual(result["SH001"], "keep_with_minor_edit")
        self.assertEqual(result["SH002"], "partial_or_targeted_repair")
        self.assertEqual(result["SH003"], "rebuild_or_post_insert")


if __name__ == "__main__":
    unittest.main()
