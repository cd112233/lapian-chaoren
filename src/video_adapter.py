"""Local video probing and compatibility adaptation for the workbench.

The workbench keeps the uploaded file as evidence.  When its container or
codec is inconvenient for the analysis toolchain, this module creates a
standard H.264/AAC MP4 beside it and uses that derived file for analysis.
Compatible files are never re-encoded.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Any


VIDEO_SUFFIXES = frozenset(
    {
        ".3g2",
        ".3gp",
        ".avi",
        ".asf",
        ".f4v",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mxf",
        ".mts",
        ".ogv",
        ".rm",
        ".rmvb",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    }
)
STANDARD_PIXEL_FORMATS = {"yuv420p", "yuvj420p"}
COMPATIBLE_VIDEO_CODECS = {"h264", "avc1"}
COMPATIBLE_AUDIO_CODECS = {None, "aac"}
ProgressCallback = Callable[[int], None]


class VideoAdaptationError(RuntimeError):
    """Raised when a local video cannot be probed or adapted."""


@dataclass(frozen=True)
class VideoProbe:
    path: Path
    format_name: str
    duration_sec: float
    width: int
    height: int
    fps: float
    video_codec: str
    pixel_format: str
    audio_codec: str | None
    rotation_deg: int = 0

    @property
    def format_tokens(self) -> set[str]:
        return {item.strip().lower() for item in self.format_name.split(",") if item.strip()}

    @property
    def stream_compatible(self) -> bool:
        return (
            self.video_codec.lower() in COMPATIBLE_VIDEO_CODECS
            and self.pixel_format.lower() in STANDARD_PIXEL_FORMATS
            and self.audio_codec in COMPATIBLE_AUDIO_CODECS
        )

    @property
    def requires_rotation_bake(self) -> bool:
        return self.rotation_deg % 360 != 0

    @property
    def direct_compatible(self) -> bool:
        return (
            self.stream_compatible
            and not self.requires_rotation_bake
            and bool(self.format_tokens & {"mp4", "mov"})
        )

    @property
    def display_width(self) -> int:
        return self.height if self.rotation_deg in {90, 270} else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.rotation_deg in {90, 270} else self.height

    @property
    def display_aspect_ratio(self) -> float:
        if self.display_height <= 0:
            return 0.0
        return self.display_width / self.display_height

    @property
    def aspect_ratio_label(self) -> str:
        return aspect_ratio_label(self.display_width, self.display_height)

    @property
    def orientation(self) -> str:
        return classify_orientation(self.display_width, self.display_height)

    @property
    def summary(self) -> str:
        container = next(iter(self.format_tokens), "未知容器")
        audio = self.audio_codec or "无音频"
        return (
            f"{container} · {self.video_codec} · {audio} · "
            f"{self.display_width}x{self.display_height} · {self.aspect_ratio_label}"
        )


@dataclass(frozen=True)
class AdaptationResult:
    input_probe: VideoProbe
    analysis_path: Path
    adapted: bool
    method: str
    reason: str

    def manifest(self) -> dict[str, Any]:
        return {
            "status": "adapted" if self.adapted else "not_needed",
            "method": self.method,
            "reason": self.reason,
            "input_format": self.input_probe.format_name,
            "input_video_codec": self.input_probe.video_codec,
            "input_pixel_format": self.input_probe.pixel_format,
            "input_audio_codec": self.input_probe.audio_codec,
            "input_width": self.input_probe.width,
            "input_height": self.input_probe.height,
            "input_rotation_deg": self.input_probe.rotation_deg,
            "display_width": self.input_probe.display_width,
            "display_height": self.input_probe.display_height,
            "display_aspect_ratio": round(self.input_probe.display_aspect_ratio, 6),
            "aspect_ratio_label": self.input_probe.aspect_ratio_label,
            "orientation": self.input_probe.orientation,
            "analysis_filename": self.analysis_path.name,
            "preserved_original": True,
        }


def _parse_rate(value: Any) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def normalize_rotation(value: Any) -> int:
    """Normalize FFprobe rotation metadata to 0/90/180/270 degrees."""

    try:
        degrees = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    normalized = degrees % 360
    candidates = (0, 90, 180, 270)

    def circular_distance(item: int) -> int:
        return min((normalized - item) % 360, (item - normalized) % 360)

    snapped = min(candidates, key=circular_distance)
    return snapped if circular_distance(snapped) <= 2 else normalized


def rotation_from_stream(stream: dict[str, Any]) -> int:
    tags = stream.get("tags")
    if isinstance(tags, dict) and tags.get("rotate") not in (None, ""):
        rotation = normalize_rotation(tags.get("rotate"))
        if rotation:
            return rotation
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if not isinstance(item, dict) or item.get("rotation") in (None, ""):
                continue
            rotation = normalize_rotation(item.get("rotation"))
            if rotation:
                return rotation
    return 0


def classify_orientation(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "landscape"
    ratio = width / height
    if 0.94 <= ratio <= 1.06:
        return "square"
    if ratio < 0.94:
        return "portrait"
    if ratio >= 2.1:
        return "ultrawide"
    return "landscape"


def aspect_ratio_label(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "未知画幅"
    ratio = Fraction(width, height)
    if ratio.numerator <= 100 and ratio.denominator <= 100:
        return f"{ratio.numerator}:{ratio.denominator}"
    limited = Fraction(width / height).limit_denominator(32)
    return f"{limited.numerator}:{limited.denominator}"


def _require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise VideoAdaptationError(f"未找到 {name}，请先安装 FFmpeg 并确保它在 PATH 中。")


def probe_video(path: Path) -> VideoProbe:
    _require_binary("ffprobe")
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise VideoAdaptationError(f"无法启动 ffprobe：{error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "无法读取视频格式").strip()
        raise VideoAdaptationError(f"无法读取视频格式：{detail[-1200:]}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise VideoAdaptationError("ffprobe 返回了无效的视频信息。") from error
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        streams = []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not isinstance(video, dict):
        raise VideoAdaptationError("文件中没有可读取的视频轨道。")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    format_info = payload.get("format") if isinstance(payload, dict) else {}
    if not isinstance(format_info, dict):
        format_info = {}
    duration = _parse_rate(video.get("duration") or format_info.get("duration"))
    fps = _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")) or 24.0
    return VideoProbe(
        path=path.resolve(),
        format_name=str(format_info.get("format_name") or "unknown"),
        duration_sec=max(0.0, duration),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps,
        video_codec=str(video.get("codec_name") or "unknown").lower(),
        pixel_format=str(video.get("pix_fmt") or "unknown").lower(),
        audio_codec=(
            str(audio.get("codec_name")).lower()
            if isinstance(audio, dict) and audio.get("codec_name")
            else None
        ),
        rotation_deg=rotation_from_stream(video),
    )


def _transcode_command(base: list[str], temporary: Path) -> list[str]:
    return [
        *base,
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
        "-metadata:s:v:0",
        "rotate=0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(temporary),
    ]


def _run_ffmpeg(
    command: list[str],
    *,
    duration_sec: float,
    on_progress: ProgressCallback | None = None,
) -> None:
    _require_binary("ffmpeg")
    try:
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        raise VideoAdaptationError(f"无法启动 ffmpeg：{error}") from error
    tail: deque[str] = deque(maxlen=40)
    last_progress = -1
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if line:
            tail.append(line)
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in {"out_time_us", "out_time_ms"}:
            continue
        try:
            # FFmpeg labels both values inconsistently across versions; in
            # practice out_time_ms is microseconds in the progress protocol.
            current_us = int(value)
            if key == "out_time_ms":
                current_us = int(value)
            if duration_sec > 0:
                percent = max(0, min(99, round(current_us / 1_000_000 / duration_sec * 100)))
            else:
                percent = min(99, max(0, last_progress + 1))
        except ValueError:
            continue
        if on_progress and percent != last_progress:
            on_progress(percent)
            last_progress = percent
    return_code = process.wait()
    if return_code != 0:
        detail = "\n".join(tail).strip() or "视频转换失败"
        raise VideoAdaptationError(f"视频转换失败（ffmpeg {return_code}）：{detail[-1600:]}")
    if on_progress:
        on_progress(100)


def adapt_video(
    source: Path,
    output: Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> AdaptationResult:
    """Probe *source* and return a path safe for the analysis pipeline.

    The original source is never overwritten.  A compatible MP4/MOV is used
    directly; compatible streams in another container are remuxed; all other
    inputs are transcoded to H.264/AAC/yuv420p MP4.
    """

    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if source == output:
        raise ValueError("视频适配输出不能覆盖原始片源。")
    if not source.is_file():
        raise VideoAdaptationError(f"片源不存在：{source}")
    probe = probe_video(source)
    if probe.direct_compatible:
        if on_progress:
            on_progress(100)
        return AdaptationResult(
            input_probe=probe,
            analysis_path=source,
            adapted=False,
            method="direct",
            reason="原片已是可直接分析的 MP4/MOV、H.264、yuv420p 格式。",
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the final extension so FFmpeg can infer the MP4 muxer even though
    # the temporary file is hidden and atomically renamed after validation.
    temporary = output.with_name(f".{output.stem}.tmp.mp4")
    temporary.unlink(missing_ok=True)
    base = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
    ]
    # A rotation tag changes the displayed geometry without changing the coded
    # width/height.  Remuxing would preserve that ambiguity, so bake the
    # orientation into pixels before the analysis pipeline sees the file.
    copy_possible = probe.stream_compatible and not probe.requires_rotation_bake
    if copy_possible:
        command = [
            *base,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(temporary),
        ]
        method = "remux"
        reason = "视频流已兼容，仅将原容器重新封装为 MP4。"
    else:
        command = _transcode_command(base, temporary)
        method = "transcode"
        reason = (
            "视频方向元数据已烘焙并转为方形像素 H.264/AAC MP4。"
            if probe.requires_rotation_bake
            else "容器或编码不适合直接分析，已转为方形像素 H.264/AAC/yuv420p MP4。"
        )
    try:
        try:
            _run_ffmpeg(command, duration_sec=probe.duration_sec, on_progress=on_progress)
        except VideoAdaptationError:
            if not copy_possible:
                raise
            # Some combinations of AAC/H.264 and legacy containers report as
            # stream-compatible but cannot be safely remuxed. Fall back to a
            # real transcode instead of making the user choose a setting.
            temporary.unlink(missing_ok=True)
            command = _transcode_command(base, temporary)
            method = "transcode"
            reason = "原容器无法安全封装，已自动转为方形像素 H.264/AAC/yuv420p MP4。"
            _run_ffmpeg(command, duration_sec=probe.duration_sec, on_progress=on_progress)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise VideoAdaptationError("视频适配没有生成有效的 MP4 文件。")
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return AdaptationResult(
        input_probe=probe,
        analysis_path=output,
        adapted=True,
        method=method,
        reason=reason,
    )
