from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_PATH = SRC / "analyze_reference.py"


def load_module():
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("analyze_reference", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AnalyzeReferenceTests(unittest.TestCase):
    def test_video_duration_does_not_follow_a_longer_audio_tail(self) -> None:
        module = load_module()
        duration = module.resolve_video_duration(
            stream_duration=None,
            format_duration=73.723356,
            frame_count=2210,
            fps=30,
        )
        self.assertAlmostEqual(duration, 73.666667, places=5)

    def test_stream_duration_can_bound_missing_frame_metadata(self) -> None:
        module = load_module()
        duration = module.resolve_video_duration(
            stream_duration=12.5,
            format_duration=12.6,
            frame_count=0,
            fps=24,
        )
        self.assertEqual(duration, 12.5)


if __name__ == "__main__":
    unittest.main()
