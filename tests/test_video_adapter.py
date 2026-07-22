from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import video_adapter  # noqa: E402


class VideoAdapterTests(unittest.TestCase):
    def probe(self, **changes):
        values = {
            "path": Path("/tmp/reference.input"),
            "format_name": "matroska,webm",
            "duration_sec": 12.0,
            "width": 1920,
            "height": 1080,
            "fps": 24.0,
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "audio_codec": "aac",
        }
        values.update(changes)
        return video_adapter.VideoProbe(**values)

    def test_compatible_mp4_is_used_without_reencoding(self) -> None:
        probe = self.probe(format_name="mov,mp4,m4a,3gp,3g2,mj2")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "reference.mp4"
            output = Path(directory) / "analysis_source.mp4"
            source.write_bytes(b"source")
            with patch.object(video_adapter, "probe_video", return_value=probe) as mocked_probe:
                result = video_adapter.adapt_video(source, output)

        mocked_probe.assert_called_once_with(source.resolve())
        self.assertFalse(result.adapted)
        self.assertEqual(result.method, "direct")
        self.assertEqual(result.analysis_path, source.resolve())
        self.assertFalse(output.exists())

    def test_compatible_streams_are_remuxed_to_mp4(self) -> None:
        probe = self.probe()
        calls = []

        def fake_run(command, *, duration_sec, on_progress):
            calls.append(command)
            Path(command[-1]).write_bytes(b"remuxed")
            if on_progress:
                on_progress(50)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "reference.mkv"
            output = Path(directory) / "analysis_source.mp4"
            source.write_bytes(b"source")
            with patch.object(video_adapter, "probe_video", return_value=probe), patch.object(
                video_adapter, "_run_ffmpeg", side_effect=fake_run
            ):
                result = video_adapter.adapt_video(source, output)

            self.assertTrue(output.is_file())
            self.assertEqual(output.read_bytes(), b"remuxed")

        self.assertTrue(result.adapted)
        self.assertEqual(result.method, "remux")
        self.assertIn("-c", calls[0])
        self.assertIn("copy", calls[0])

    def test_unsupported_streams_are_transcoded_to_standard_mp4(self) -> None:
        probe = self.probe(video_codec="hevc", pixel_format="yuv420p10le")
        calls = []

        def fake_run(command, *, duration_sec, on_progress):
            calls.append(command)
            Path(command[-1]).write_bytes(b"transcoded")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "reference.webm"
            output = Path(directory) / "analysis_source.mp4"
            source.write_bytes(b"source")
            with patch.object(video_adapter, "probe_video", return_value=probe), patch.object(
                video_adapter, "_run_ffmpeg", side_effect=fake_run
            ):
                result = video_adapter.adapt_video(source, output)
            output_bytes = output.read_bytes()

        self.assertTrue(result.adapted)
        self.assertEqual(result.method, "transcode")
        self.assertEqual(output_bytes, b"transcoded")
        self.assertIn("libx264", calls[0])
        self.assertIn("yuv420p", calls[0])
        self.assertIn("aac", calls[0])

    def test_probe_rejects_a_file_without_a_video_stream(self) -> None:
        payload = '{"streams":[{"codec_type":"audio"}],"format":{}}'
        completed = type("Completed", (), {"returncode": 0, "stdout": payload, "stderr": ""})()
        with patch.object(video_adapter.subprocess, "run", return_value=completed), patch.object(
            video_adapter, "_require_binary"
        ):
            with self.assertRaises(video_adapter.VideoAdaptationError):
                video_adapter.probe_video(Path("/tmp/not-a-video"))


if __name__ == "__main__":
    unittest.main()
