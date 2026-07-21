#!/usr/bin/env python3
"""Create a shot-by-shot reference breakdown package from a video.

The script automates mechanical work only: probe metadata, detect cuts, extract
start/middle/end frames, build contact sheets, write CSV/YAML scaffolds, split
review clips, and export an OTIO timeline. Director/action/VFX interpretation is
left blank for human or multimodal review.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps
import yaml

try:
    from scenedetect import SceneDetector, SceneManager, open_video
    from scenedetect.detectors import AdaptiveDetector, ContentDetector
except ImportError as exc:  # pragma: no cover - user-facing dependency gate
    raise SystemExit(
        "PySceneDetect is not installed. Activate the local venv and run "
        "`pip install -r requirements.txt`."
    ) from exc

try:
    import opentimelineio as otio
except ImportError:  # Optional at runtime.
    otio = None


CSV_FIELDS = [
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
    "analysis_status",
    "transition_in",
    "transition_out",
    "narrative_function",
    "shot_scale",
    "camera_height",
    "camera_angle",
    "camera_motion",
    "camera_speed_curve",
    "lens_feel",
    "axis_and_screen_direction",
    "focus_target",
    "actor_a_start_state",
    "actor_b_start_state",
    "dominant_mover",
    "attack_or_defense_intent",
    "attack_trajectory",
    "visible_body_parts",
    "contact_point",
    "contact_time",
    "force_direction",
    "reaction",
    "follow_through",
    "settle_state",
    "result_position",
    "next_shot_bridge",
    "scene_layout",
    "foreground_midground_background",
    "light_source",
    "destruction_state",
    "vfx_layers",
    "particles_debris",
    "interactive_light",
    "compositing_notes",
    "pre_hit_sound",
    "impact_sound",
    "body_weapon_foley",
    "environment_reaction",
    "music_beat",
    "silence_or_tail",
    "character_action_complexity",
    "contact_complexity",
    "camera_motion_complexity",
    "recommended_route",
    "required_assets",
    "fallback_route",
    "acceptance_criteria",
    "notes",
]


@dataclass(frozen=True)
class ProbeInfo:
    path: str
    duration_sec: float
    fps: float
    width: int
    height: int
    frame_count: int
    video_codec: str
    pixel_format: str
    audio_codec: str | None
    sample_rate: int | None


@dataclass(frozen=True)
class Shot:
    shot_id: str
    index: int
    start_sec: float
    end_sec: float
    duration_sec: float
    start_frame: int
    end_frame: int


class CommandError(RuntimeError):
    pass


class ProgressEmitter:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self._last_event: tuple[Any, ...] | None = None

    def emit(
        self,
        progress: float,
        stage: str,
        *,
        phase: str,
        detail: str = "",
        current: int | None = None,
        total: int | None = None,
        force: bool = False,
    ) -> None:
        if not self.enabled:
            return
        normalized = max(0, min(100, int(round(progress))))
        signature = (normalized, stage, detail, current, total, phase)
        if not force and signature == self._last_event:
            return
        payload: dict[str, Any] = {
            "type": "progress",
            "phase": phase,
            "progress": normalized,
            "stage": stage,
            "detail": detail,
        }
        if current is not None:
            payload["current"] = current
        if total is not None:
            payload["total"] = total
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        self._last_event = signature


class FrameProgressDetector(SceneDetector):
    """Reports decoded-frame progress without changing scene detection results."""

    def __init__(
        self,
        emitter: ProgressEmitter,
        *,
        total_frames: int,
        fps: float,
        progress_start: int = 14,
        progress_end: int = 34,
    ) -> None:
        super().__init__()
        self.emitter = emitter
        self.total_frames = max(1, total_frames)
        self.fps = max(0.001, fps)
        self.progress_start = progress_start
        self.progress_end = progress_end
        self.report_every = max(1, self.total_frames // 50)
        self.last_reported_frame = -self.report_every

    def process_frame(self, timecode: Any, frame_img: Any) -> list[Any]:
        current = min(self.total_frames, int(timecode.frame_num) + 1)
        if current - self.last_reported_frame >= self.report_every or current >= self.total_frames:
            ratio = current / self.total_frames
            progress = self.progress_start + (self.progress_end - self.progress_start) * ratio
            elapsed = current / self.fps
            duration = self.total_frames / self.fps
            self.emitter.emit(
                progress,
                "正在检测镜头切点",
                phase="detecting",
                detail=f"已扫描 {elapsed:.1f} / {duration:.1f} 秒",
                current=current,
                total=self.total_frames,
            )
            self.last_reported_frame = current
        return []


def run(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise CommandError(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{detail}")
    return result


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Required binary not found on PATH: {name}")


def parse_rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return float(value)


def probe_video(path: Path) -> ProbeInfo:
    payload = json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ]
        ).stdout
    )
    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise SystemExit(f"No video stream found: {path}")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(
        video.get("duration")
        or payload.get("format", {}).get("duration")
        or 0.0
    )
    fps = parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    if fps <= 0:
        fps = 24.0
    frames_raw = video.get("nb_frames")
    frame_count = int(frames_raw) if frames_raw and str(frames_raw).isdigit() else int(round(duration * fps))
    sample_rate_raw = audio.get("sample_rate") if audio else None
    return ProbeInfo(
        path=str(path.resolve()),
        duration_sec=duration,
        fps=fps,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        frame_count=frame_count,
        video_codec=str(video.get("codec_name") or "unknown"),
        pixel_format=str(video.get("pix_fmt") or "unknown"),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        sample_rate=int(sample_rate_raw) if sample_rate_raw and str(sample_rate_raw).isdigit() else None,
    )


def detect_shots(
    path: Path,
    probe: ProbeInfo,
    *,
    detector_name: str,
    threshold: float | None,
    min_scene_len: int,
    progress: ProgressEmitter | None = None,
) -> list[Shot]:
    video = open_video(str(path))
    manager = SceneManager()
    if detector_name == "adaptive":
        kwargs: dict[str, Any] = {"min_scene_len": min_scene_len}
        if threshold is not None:
            kwargs["adaptive_threshold"] = threshold
        detector = AdaptiveDetector(**kwargs)
    else:
        kwargs = {"min_scene_len": min_scene_len}
        if threshold is not None:
            kwargs["threshold"] = threshold
        detector = ContentDetector(**kwargs)
    manager.add_detector(detector)
    if progress and progress.enabled:
        manager.add_detector(
            FrameProgressDetector(
                progress,
                total_frames=probe.frame_count,
                fps=probe.fps,
            )
        )
    manager.detect_scenes(video, show_progress=not bool(progress and progress.enabled))
    scene_list = manager.get_scene_list(start_in_scene=True)

    if not scene_list:
        scene_list = [
            (
                video.base_timecode,
                video.base_timecode + int(round(probe.duration_sec * probe.fps)),
            )
        ]

    shots: list[Shot] = []
    for index, (start, end) in enumerate(scene_list, start=1):
        start_sec = max(0.0, float(start.seconds))
        end_sec = min(probe.duration_sec, float(end.seconds))
        if end_sec <= start_sec:
            continue
        shots.append(
            Shot(
                shot_id=f"SH{index:03d}",
                index=index,
                start_sec=start_sec,
                end_sec=end_sec,
                duration_sec=end_sec - start_sec,
                start_frame=int(start.frame_num),
                end_frame=max(int(start.frame_num) + 1, int(end.frame_num)),
            )
        )
    return shots


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def safe_sample_times(shot: Shot) -> tuple[float, float, float]:
    pad = min(0.08, shot.duration_sec * 0.08)
    start = shot.start_sec + pad
    middle = shot.start_sec + shot.duration_sec / 2.0
    end = max(start, shot.end_sec - pad)
    return start, middle, end


def extract_frame(video_path: Path, timestamp: float, output_path: Path, width: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale='min({width},iw)':-2"
    run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            vf,
            "-q:v",
            "2",
            str(output_path),
        ]
    )
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise CommandError(f"Frame extraction produced no file at {timestamp:.3f}s")


def split_clip(video_path: Path, shot: Shot, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{shot.start_sec:.6f}",
            "-i",
            str(video_path),
            "-t",
            f"{shot.duration_sec:.6f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
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
            str(output_path),
        ]
    )


def font(size: int = 18) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def fit_image(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)


def make_shot_contact_sheet(
    shot: Shot,
    frame_paths: list[Path],
    output_path: Path,
    *,
    tile_width: int = 480,
) -> None:
    images = [Image.open(path) for path in frame_paths]
    ratio = images[0].height / images[0].width if images[0].width else 9 / 16
    tile_height = max(240, int(tile_width * ratio))
    header_h = 58
    canvas = Image.new("RGB", (tile_width * 3, tile_height + header_h), (22, 22, 22))
    draw = ImageDraw.Draw(canvas)
    fnt = font(18)
    labels = ["START", "MIDDLE", "END"]
    for idx, (image, label) in enumerate(zip(images, labels)):
        tile = fit_image(image, (tile_width, tile_height))
        canvas.paste(tile, (idx * tile_width, header_h))
        draw.text((idx * tile_width + 10, 8), label, fill="white", font=fnt)
    draw.text(
        (10, 31),
        f"{shot.shot_id}  {format_time(shot.start_sec)} -> {format_time(shot.end_sec)}  {shot.duration_sec:.3f}s",
        fill=(225, 225, 225),
        font=fnt,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, quality=92)
    for image in images:
        image.close()


def make_overview_contact_sheet(
    shots: list[Shot],
    middle_frames: list[Path],
    output_path: Path,
    *,
    columns: int = 4,
    tile_width: int = 400,
) -> None:
    if not middle_frames:
        return
    first = Image.open(middle_frames[0])
    ratio = first.height / first.width if first.width else 9 / 16
    first.close()
    tile_height = max(220, int(tile_width * ratio))
    caption_h = 44
    rows = math.ceil(len(middle_frames) / columns)
    canvas = Image.new(
        "RGB",
        (columns * tile_width, rows * (tile_height + caption_h)),
        (18, 18, 18),
    )
    draw = ImageDraw.Draw(canvas)
    fnt = font(17)
    for idx, (shot, path) in enumerate(zip(shots, middle_frames)):
        row, col = divmod(idx, columns)
        x, y = col * tile_width, row * (tile_height + caption_h)
        with Image.open(path) as image:
            canvas.paste(fit_image(image, (tile_width, tile_height)), (x, y))
        caption = f"{shot.shot_id} {format_time(shot.start_sec)} {shot.duration_sec:.2f}s"
        draw.rectangle((x, y + tile_height, x + tile_width, y + tile_height + caption_h), fill=(28, 28, 28))
        draw.text((x + 8, y + tile_height + 11), caption, fill="white", font=fnt)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, quality=92)


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def csv_row(shot: Shot, output_root: Path, clip_exists: bool) -> dict[str, Any]:
    shot_dir = output_root / "shots" / shot.shot_id
    row: dict[str, Any] = {field: "" for field in CSV_FIELDS}
    row.update(
        {
            "shot_id": shot.shot_id,
            "source_in": format_time(shot.start_sec),
            "source_out": format_time(shot.end_sec),
            "duration_sec": f"{shot.duration_sec:.3f}",
            "source_start_frame": shot.start_frame,
            "source_end_frame": shot.end_frame,
            "start_frame_path": relative(shot_dir / "start.jpg", output_root),
            "middle_frame_path": relative(shot_dir / "middle.jpg", output_root),
            "end_frame_path": relative(shot_dir / "end.jpg", output_root),
            "clip_path": relative(output_root / "clips" / f"{shot.shot_id}.mp4", output_root) if clip_exists else "",
            "analysis_status": "pending_manual_review",
            "character_action_complexity": "pending",
            "contact_complexity": "pending",
            "camera_motion_complexity": "pending",
        }
    )
    return row


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_yaml(shots: list[Shot], output_root: Path, path: Path) -> None:
    data: dict[str, Any] = {
        "schema_version": "1.0",
        "source_video": "metadata.json",
        "analysis_status": "shot_boundaries_generated_manual_review_required",
        "characters": ["CHAR_A_TIGER", "CHAR_B_BOAR"],
        "shots": [],
    }
    for shot in shots:
        shot_dir = output_root / "shots" / shot.shot_id
        data["shots"].append(
            {
                "shot_id": shot.shot_id,
                "source": {
                    "in": format_time(shot.start_sec),
                    "out": format_time(shot.end_sec),
                    "duration_sec": round(shot.duration_sec, 3),
                    "start_frame": shot.start_frame,
                    "end_frame": shot.end_frame,
                    "frames": {
                        "start": relative(shot_dir / "start.jpg", output_root),
                        "middle": relative(shot_dir / "middle.jpg", output_root),
                        "end": relative(shot_dir / "end.jpg", output_root),
                    },
                },
                "camera": {
                    "shot_scale": "",
                    "height": "",
                    "angle": "",
                    "motion": "",
                    "speed_curve": "",
                    "axis_and_screen_direction": "",
                },
                "action_chain": {
                    "actor_a_start_state": "",
                    "actor_b_start_state": "",
                    "dominant_mover": "",
                    "intent": "",
                    "attack_trajectory": "",
                    "contact_point": "",
                    "force_direction": "",
                    "reaction": "",
                    "follow_through": "",
                    "settle_state": "",
                    "result_position": "",
                    "next_shot_bridge": "",
                },
                "scene_vfx": {
                    "layout": "",
                    "lighting": "",
                    "destruction_state": "",
                    "vfx_layers": [],
                    "interactive_light": "",
                },
                "sound": {
                    "pre_hit": "",
                    "impact": "",
                    "foley": "",
                    "environment_reaction": "",
                    "music_beat": "",
                },
                "complexity": {
                    "character_action": "pending",
                    "contact": "pending",
                    "camera_motion": "pending",
                },
                "production": {
                    "recommended_route": "",
                    "required_assets": [],
                    "fallback_route": "",
                    "acceptance_criteria": [],
                },
            }
        )
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def write_report(shots: list[Shot], output_root: Path, path: Path) -> None:
    lines = [
        "# Reference Fight Shot Breakdown",
        "",
        "> 自动检测结果只负责机械切点；快速甩镜、闪白、爆炸、粒子遮挡和渐变必须人工复核。",
        "",
        f"- 镜头数：{len(shots)}",
        f"- 总检测时长：{sum(s.duration_sec for s in shots):.3f}s",
        "- 当前状态：等待逐镜摄影、动作、接触、VFX、声音和生产路线分析",
        "",
        "![镜头总览](overview_contact_sheet.jpg)",
        "",
        "| 镜头 | 入点 | 出点 | 时长 | 首/中/尾帧 | 审核状态 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for shot in shots:
        sheet = f"shots/{shot.shot_id}/contact_sheet.jpg"
        lines.append(
            f"| {shot.shot_id} | {format_time(shot.start_sec)} | {format_time(shot.end_sec)} | "
            f"{shot.duration_sec:.3f}s | [查看]({sheet}) | 待人工拉片 |"
        )
    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "1. 人工校正镜头切点。",
            "2. 填写 `shots.csv` 或 `shot_manifest.yaml`。",
            "3. 选出最难且最有代表性的 6—10 秒作为黄金测试段。",
            "4. 建立目标角色的干净身份包，再做角色替换。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_otio(video_path: Path, probe: ProbeInfo, shots: list[Shot], path: Path) -> bool:
    if otio is None:
        return False
    timeline = otio.schema.Timeline(name=f"Reference Breakdown - {video_path.stem}")
    track = otio.schema.Track(name="Reference Video", kind=otio.schema.TrackKind.Video)
    available = otio.opentime.TimeRange(
        start_time=otio.opentime.RationalTime(0, probe.fps),
        duration=otio.opentime.RationalTime(probe.frame_count, probe.fps),
    )
    target = video_path.resolve().as_uri()
    for shot in shots:
        media_ref = otio.schema.ExternalReference(target_url=target, available_range=available)
        source_range = otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(shot.start_frame, probe.fps),
            duration=otio.opentime.RationalTime(shot.end_frame - shot.start_frame, probe.fps),
        )
        clip = otio.schema.Clip(
            name=shot.shot_id,
            media_reference=media_ref,
            source_range=source_range,
            metadata={
                "source_in": format_time(shot.start_sec),
                "source_out": format_time(shot.end_sec),
                "analysis_status": "pending_manual_review",
            },
        )
        track.append(clip)
    timeline.tracks.append(track)
    otio.adapters.write_to_file(timeline, str(path))
    return True


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="Reference video file")
    parser.add_argument("--output", type=Path, required=True, help="Output directory")
    parser.add_argument("--detector", choices=["adaptive", "content"], default="adaptive")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Adaptive threshold (adaptive) or content threshold (content). Defaults to detector preset.",
    )
    parser.add_argument("--min-scene-len", type=int, default=6, help="Minimum scene length in frames")
    parser.add_argument("--frame-width", type=int, default=960, help="Extracted review frame width")
    parser.add_argument("--split-clips", action="store_true", help="Re-encode exact review clip for each shot")
    parser.add_argument("--overwrite", action="store_true", help="Allow writing into a non-empty output directory")
    parser.add_argument(
        "--progress-json",
        action="store_true",
        help="Emit newline-delimited JSON progress events for the local workbench",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    progress = ProgressEmitter(args.progress_json)
    progress.emit(9, "正在检查分析环境", phase="preparing", force=True)
    require_binary("ffmpeg")
    require_binary("ffprobe")
    video_path = args.video.expanduser().resolve()
    if not video_path.exists():
        raise SystemExit(f"Reference video does not exist: {video_path}")
    output_root = args.output.expanduser().resolve()
    if output_root.exists() and any(output_root.iterdir()) and not args.overwrite:
        raise SystemExit(f"Output directory is not empty: {output_root}\nUse --overwrite or choose another path.")
    output_root.mkdir(parents=True, exist_ok=True)

    progress.emit(10, "正在读取视频信息", phase="probing")
    probe = probe_video(video_path)
    progress.emit(
        12,
        "已读取视频信息",
        phase="probing",
        detail=f"{probe.duration_sec:.1f} 秒 · {probe.width}x{probe.height} · {probe.fps:.2f} fps",
    )
    progress.emit(13, "正在检测镜头切点", phase="detecting", detail="准备逐帧扫描")
    shots = detect_shots(
        video_path,
        probe,
        detector_name=args.detector,
        threshold=args.threshold,
        min_scene_len=args.min_scene_len,
        progress=progress,
    )
    if not shots:
        raise SystemExit("No valid shots were detected.")
    progress.emit(
        35,
        "镜头切点检测完成",
        phase="detected",
        detail=f"识别到 {len(shots)} 个候选镜头",
        current=len(shots),
        total=len(shots),
    )

    metadata = {
        "tool": "fight-reference-breakdown-kit",
        "probe": asdict(probe),
        "detection": {
            "detector": args.detector,
            "threshold": args.threshold,
            "min_scene_len_frames": args.min_scene_len,
            "shot_count": len(shots),
            "manual_cut_review_required": True,
        },
    }
    (output_root / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    rows: list[dict[str, Any]] = []
    middle_frames: list[Path] = []
    shot_count = len(shots)
    for shot_index, shot in enumerate(shots, start=1):
        shot_start_progress = 38 + ((shot_index - 1) / shot_count) * 47
        progress.emit(
            shot_start_progress,
            "正在生成逐镜证据",
            phase="evidence",
            detail=f"{shot.shot_id} · 抽取首帧、中间帧和尾帧",
            current=shot_index,
            total=shot_count,
        )
        shot_dir = output_root / "shots" / shot.shot_id
        frame_paths = [shot_dir / "start.jpg", shot_dir / "middle.jpg", shot_dir / "end.jpg"]
        for timestamp, frame_path in zip(safe_sample_times(shot), frame_paths):
            extract_frame(video_path, timestamp, frame_path, args.frame_width)
        make_shot_contact_sheet(shot, frame_paths, shot_dir / "contact_sheet.jpg")
        middle_frames.append(frame_paths[1])

        clip_exists = False
        if args.split_clips:
            progress.emit(
                shot_start_progress + (47 / shot_count) * 0.6,
                "正在生成逐镜片段",
                phase="clips",
                detail=f"{shot.shot_id} · 编码审片视频",
                current=shot_index,
                total=shot_count,
            )
            clip_path = output_root / "clips" / f"{shot.shot_id}.mp4"
            split_clip(video_path, shot, clip_path)
            clip_exists = True
        rows.append(csv_row(shot, output_root, clip_exists))
        progress.emit(
            38 + (shot_index / shot_count) * 47,
            "正在生成逐镜证据",
            phase="evidence",
            detail=f"已完成 {shot_index} / {shot_count} 镜",
            current=shot_index,
            total=shot_count,
        )

    progress.emit(87, "正在生成镜头总览", phase="overview", detail=f"汇总 {shot_count} 个镜头")
    make_overview_contact_sheet(shots, middle_frames, output_root / "overview_contact_sheet.jpg")
    progress.emit(89, "正在写入分析清单", phase="manifests")
    write_csv(rows, output_root / "shots.csv")
    write_yaml(shots, output_root, output_root / "shot_manifest.yaml")
    write_report(shots, output_root, output_root / "report.md")
    progress.emit(90, "正在生成时间线", phase="timeline")
    otio_written = write_otio(video_path, probe, shots, output_root / "reference_timeline.otio")
    progress.emit(91, "机械分析完成", phase="analysis_complete", detail=f"共 {shot_count} 镜")

    print(f"Reference: {video_path}")
    print(f"Duration: {probe.duration_sec:.3f}s @ {probe.fps:.3f} fps")
    print(f"Detected shots: {len(shots)}")
    print(f"Output: {output_root}")
    print(f"OTIO timeline: {'written' if otio_written else 'skipped (dependency unavailable)'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CommandError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
