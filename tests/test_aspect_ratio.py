from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
MODULE_PATH = SRC / "analyze_reference.py"


def load_module():
    spec = importlib.util.spec_from_file_location("analyze_reference", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AspectRatioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_probe_uses_display_dimensions_for_rotated_video(self) -> None:
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": 640,
                    "height": 360,
                    "avg_frame_rate": "24/1",
                    "nb_frames": "24",
                    "side_data_list": [{"rotation": 90}],
                }
            ],
            "format": {"duration": "1.0"},
        }
        completed = type("Completed", (), {"stdout": json.dumps(payload)})()
        with patch.object(self.module, "run", return_value=completed):
            probe = self.module.probe_video(Path("/tmp/rotated.mp4"))

        self.assertEqual((probe.width, probe.height), (640, 360))
        self.assertEqual((probe.display_width, probe.display_height), (360, 640))
        self.assertEqual(probe.orientation, "portrait")
        self.assertEqual(probe.aspect_ratio_label, "9:16")

    def test_contain_image_preserves_both_ultrawide_edges(self) -> None:
        image = Image.new("RGB", (400, 100), "#2a2a2a")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 39, 99), fill="#ef2b2b")
        draw.rectangle((360, 0, 399, 99), fill="#2563eb")
        contained = self.module.contain_image(image, (200, 200))

        left = contained.getpixel((3, 100))
        right = contained.getpixel((196, 100))
        self.assertGreater(left[0], left[2] * 2)
        self.assertGreater(right[2], right[0] * 2)

    def test_contact_sheet_layout_adapts_to_source_orientation(self) -> None:
        self.assertEqual(self.module.shot_contact_tile_size((360, 640)), (360, 640))
        self.assertEqual(self.module.shot_contact_tile_size((512, 512)), (440, 440))
        self.assertEqual(self.module.shot_contact_tile_size((1280, 360)), (640, 220))
        portrait = self.module.overview_layout((360, 640))
        ultrawide = self.module.overview_layout((1280, 360))
        self.assertEqual(portrait[0], 6)
        self.assertEqual(ultrawide[0], 4)
        self.assertLess(ultrawide[2], portrait[2])

    def test_ultrawide_contact_sheet_does_not_crop_left_or_right(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = []
            for index in range(3):
                frame = Image.new("RGB", (320, 90), "#2a2a2a")
                draw = ImageDraw.Draw(frame)
                draw.rectangle((0, 0, 25, 89), fill="#ef2b2b")
                draw.rectangle((294, 0, 319, 89), fill="#2563eb")
                path = root / f"{index}.png"
                frame.save(path)
                frames.append(path)
            output = root / "contact.jpg"
            shot = self.module.Shot("SH001", 1, 0.0, 1.0, 1.0, 0, 24)
            self.module.make_shot_contact_sheet(shot, frames, output)
            with Image.open(output) as contact:
                sample_y = 58 + 110
                left = contact.getpixel((4, sample_y))
                right = contact.getpixel((635, sample_y))

        self.assertGreater(left[0], left[2] * 1.8)
        self.assertGreater(right[2], right[0] * 1.8)

    def test_split_clip_normalizes_even_dimensions_and_square_pixels(self) -> None:
        shot = self.module.Shot("SH001", 1, 0.0, 1.0, 1.0, 0, 24)
        with patch.object(self.module, "run") as mocked:
            self.module.split_clip(Path("/tmp/source.mp4"), shot, Path("/tmp/clip.mp4"))
        command = mocked.call_args.args[0]
        self.assertIn("scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1", command)
        self.assertIn("rotate=0", command)


if __name__ == "__main__":
    unittest.main()
