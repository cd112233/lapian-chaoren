#!/usr/bin/env python3
"""Mechanical comparison primitives for reference and remake videos.

The module deliberately separates measurable timing/signal evidence from film
language interpretation. It can align shot sequences and expose candidate
visual/audio peaks, but it never calls those peaks a hit, zoom, VFX event, or
other semantic technique without a later human/AI review.
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import subprocess
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable


ProgressCallback = Callable[[int, str, str], None]


@dataclass(frozen=True)
class ShotRecord:
    shot_id: str
    index: int
    start_sec: float
    end_sec: float
    duration_sec: float
    start_frame_path: str
    middle_frame_path: str
    end_frame_path: str
    clip_path: str

    @property
    def midpoint_sec(self) -> float:
        return self.start_sec + self.duration_sec / 2.0


def parse_timecode(value: str) -> float:
    parts = str(value or "0").strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"invalid timecode: {value}")
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remainder = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{remainder:06.3f}"


def load_shots(path: Path) -> list[ShotRecord]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    shots: list[ShotRecord] = []
    for index, row in enumerate(rows, start=1):
        start = parse_timecode(row.get("source_in", "0"))
        end = parse_timecode(row.get("source_out", "0"))
        duration = float(row.get("duration_sec") or max(0.0, end - start))
        if duration <= 0:
            continue
        shots.append(
            ShotRecord(
                shot_id=str(row.get("shot_id") or f"SH{index:03d}"),
                index=index,
                start_sec=start,
                end_sec=end,
                duration_sec=duration,
                start_frame_path=str(row.get("start_frame_path") or ""),
                middle_frame_path=str(row.get("middle_frame_path") or ""),
                end_frame_path=str(row.get("end_frame_path") or ""),
                clip_path=str(row.get("clip_path") or ""),
            )
        )
    if not shots:
        raise ValueError(f"no valid shots in {path}")
    return shots


def _shot_cost(
    reference: ShotRecord,
    remake: ShotRecord,
    reference_total: float,
    remake_total: float,
) -> float:
    reference_share = reference.duration_sec / max(reference_total, 1e-6)
    remake_share = remake.duration_sec / max(remake_total, 1e-6)
    ratio = max(reference_share, 1e-6) / max(remake_share, 1e-6)
    duration_cost = min(1.0, abs(math.log(ratio, 2)))
    reference_mid = reference.midpoint_sec / max(reference_total, 1e-6)
    remake_mid = remake.midpoint_sec / max(remake_total, 1e-6)
    position_cost = min(1.0, abs(reference_mid - remake_mid) * 3.0)
    return duration_cost * 0.58 + position_cost * 0.42


def align_shots(
    reference_shots: list[ShotRecord],
    remake_shots: list[ShotRecord],
) -> list[tuple[ShotRecord | None, ShotRecord | None]]:
    """Monotonically align two shot sequences with insert/delete support."""

    reference_total = max(shot.end_sec for shot in reference_shots)
    remake_total = max(shot.end_sec for shot in remake_shots)
    gap_cost = 0.72
    rows = len(reference_shots) + 1
    columns = len(remake_shots) + 1
    scores = [[0.0] * columns for _ in range(rows)]
    moves = [[""] * columns for _ in range(rows)]
    for row in range(1, rows):
        scores[row][0] = row * gap_cost
        moves[row][0] = "missing"
    for column in range(1, columns):
        scores[0][column] = column * gap_cost
        moves[0][column] = "extra"
    for row in range(1, rows):
        for column in range(1, columns):
            candidates = [
                (
                    scores[row - 1][column - 1]
                    + _shot_cost(
                        reference_shots[row - 1],
                        remake_shots[column - 1],
                        reference_total,
                        remake_total,
                    ),
                    "match",
                ),
                (scores[row - 1][column] + gap_cost, "missing"),
                (scores[row][column - 1] + gap_cost, "extra"),
            ]
            scores[row][column], moves[row][column] = min(
                candidates,
                key=lambda item: (item[0], {"match": 0, "missing": 1, "extra": 2}[item[1]]),
            )

    result: list[tuple[ShotRecord | None, ShotRecord | None]] = []
    row, column = len(reference_shots), len(remake_shots)
    while row or column:
        move = moves[row][column]
        if move == "match":
            result.append((reference_shots[row - 1], remake_shots[column - 1]))
            row -= 1
            column -= 1
        elif move == "missing":
            result.append((reference_shots[row - 1], None))
            row -= 1
        else:
            result.append((None, remake_shots[column - 1]))
            column -= 1
    result.reverse()
    return result


def alignment_from_override(
    reference_shots: list[ShotRecord],
    remake_shots: list[ShotRecord],
    override: list[dict[str, Any]],
) -> list[tuple[ShotRecord | None, ShotRecord | None]]:
    reference_by_id = {shot.shot_id: shot for shot in reference_shots}
    remake_by_id = {shot.shot_id: shot for shot in remake_shots}
    mapping: dict[str, str | None] = {}
    for item in override:
        if not isinstance(item, dict):
            raise ValueError("alignment rows must be objects")
        source_id = str(item.get("source_shot_id") or "")
        remake_id_raw = item.get("remake_shot_id")
        remake_id = str(remake_id_raw) if remake_id_raw else None
        if source_id not in reference_by_id or source_id in mapping:
            raise ValueError("alignment contains an unknown or duplicate source shot")
        if remake_id is not None and remake_id not in remake_by_id:
            raise ValueError("alignment contains an unknown remake shot")
        mapping[source_id] = remake_id
    if set(mapping) != set(reference_by_id):
        raise ValueError("alignment must contain every source shot exactly once")

    used = [mapping[shot.shot_id] for shot in reference_shots if mapping[shot.shot_id]]
    if len(used) != len(set(used)):
        raise ValueError("a remake shot cannot be mapped more than once")
    remake_indexes = [remake_by_id[shot_id].index for shot_id in used]
    if remake_indexes != sorted(remake_indexes):
        raise ValueError("alignment must keep the remake shot order")

    result: list[tuple[ShotRecord | None, ShotRecord | None]] = []
    remake_cursor = 0
    for reference in reference_shots:
        remake_id = mapping[reference.shot_id]
        if remake_id is None:
            result.append((reference, None))
            continue
        remake = remake_by_id[remake_id]
        while remake_cursor < len(remake_shots) and remake_shots[remake_cursor].index < remake.index:
            result.append((None, remake_shots[remake_cursor]))
            remake_cursor += 1
        result.append((reference, remake))
        remake_cursor = remake.index
    while remake_cursor < len(remake_shots):
        result.append((None, remake_shots[remake_cursor]))
        remake_cursor += 1
    return result


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    floor = _percentile(values, 0.2)
    ceiling = _percentile(values, 0.95)
    span = ceiling - floor
    relative_span = span / max(abs(ceiling), 1e-6)
    if span <= 1e-6 or relative_span < 0.08:
        return [0.0 for _ in values]
    return [max(0.0, min(1.0, (value - floor) / span)) for value in values]


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _visual_track(video_path: Path, sample_fps: float) -> list[dict[str, float]]:
    width, height = 160, 90
    frame_size = width * height
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-an",
        "-vf",
        (
            f"fps={sample_fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,format=gray"
        ),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    frames: list[dict[str, float]] = []
    previous: bytes | None = None
    index = 0
    while True:
        frame = _read_exact(process.stdout, frame_size)
        if len(frame) != frame_size:
            break
        luma = sum(frame) / frame_size / 255.0
        motion = 0.0
        if previous is not None:
            motion = sum(abs(current - old) for current, old in zip(frame, previous)) / frame_size / 255.0
        frames.append({"time": index / sample_fps, "motion_raw": motion, "luma_raw": luma})
        previous = frame
        index += 1
    return_code = process.wait()
    error_text = (process.stderr.read() if process.stderr else b"").decode("utf-8", errors="replace")
    if return_code != 0:
        raise RuntimeError(error_text.strip() or "ffmpeg visual signal extraction failed")
    return frames


def _audio_track(video_path: Path, sample_fps: float) -> list[float]:
    sample_rate = 8000
    samples_per_window = max(1, int(round(sample_rate / sample_fps)))
    bytes_per_window = samples_per_window * 2
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    values: list[float] = []
    while True:
        chunk = _read_exact(process.stdout, bytes_per_window)
        if len(chunk) < 2:
            break
        chunk = chunk[: len(chunk) - (len(chunk) % 2)]
        samples = array("h")
        samples.frombytes(chunk)
        if sys.byteorder != "little":
            samples.byteswap()
        if samples:
            rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768.0
            values.append(rms)
    return_code = process.wait()
    error_text = (process.stderr.read() if process.stderr else b"").decode("utf-8", errors="replace")
    if return_code != 0 and "does not contain any stream" not in error_text.lower():
        raise RuntimeError(error_text.strip() or "ffmpeg audio signal extraction failed")
    return values


def analyze_signal_track(video_path: Path, *, sample_fps: float = 8.0) -> list[dict[str, float]]:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for comparison signal analysis")
    visual = _visual_track(video_path, sample_fps)
    if not visual:
        return []
    try:
        audio = _audio_track(video_path, sample_fps)
    except RuntimeError:
        audio = []
    motion_values = [point["motion_raw"] for point in visual]
    luma_changes = [0.0]
    for previous, current in zip(visual, visual[1:]):
        luma_changes.append(abs(current["luma_raw"] - previous["luma_raw"]))
    audio_values = [audio[index] if index < len(audio) else 0.0 for index in range(len(visual))]
    motion_norm = _normalize(motion_values)
    light_norm = _normalize(luma_changes)
    audio_norm = _normalize(audio_values)
    track: list[dict[str, float]] = []
    for index, point in enumerate(visual):
        composite = max(
            motion_norm[index] * 0.92,
            light_norm[index] * 0.82,
            audio_norm[index] * 0.78,
            motion_norm[index] * 0.55 + audio_norm[index] * 0.3 + light_norm[index] * 0.15,
        )
        track.append(
            {
                "time": round(point["time"], 4),
                "motion": round(motion_norm[index], 4),
                "light_change": round(light_norm[index], 4),
                "audio": round(audio_norm[index], 4),
                "composite": round(max(0.0, min(1.0, composite)), 4),
            }
        )
    return track


def save_signal_tracks(path: Path, reference: list[dict[str, float]], remake: list[dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sample_fps": 8.0, "reference": reference, "remake": remake}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_signal_tracks(path: Path) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    reference = payload.get("reference", []) if isinstance(payload, dict) else []
    remake = payload.get("remake", []) if isinstance(payload, dict) else []
    return list(reference), list(remake)


def _candidate_kind(point: dict[str, float]) -> tuple[str, str]:
    channels = [
        ("motion", float(point.get("motion", 0.0))),
        ("light_change", float(point.get("light_change", 0.0))),
        ("audio", float(point.get("audio", 0.0))),
    ]
    channels.sort(key=lambda item: item[1], reverse=True)
    if channels[0][1] >= 0.62 and channels[1][1] >= 0.55:
        return "combined_peak", "视听联合峰值候选"
    labels = {
        "motion": ("motion_peak", "运动变化峰值候选"),
        "light_change": ("light_peak", "亮度 / VFX 变化候选"),
        "audio": ("audio_peak", "声音峰值候选"),
    }
    return labels[channels[0][0]]


def event_candidates(
    track: list[dict[str, float]],
    shot: ShotRecord,
    *,
    maximum: int = 3,
) -> list[dict[str, Any]]:
    if not track or shot.duration_sec <= 0:
        return []
    pad = min(0.1, shot.duration_sec * 0.08)
    points = [
        point
        for point in track
        if shot.start_sec + pad <= float(point.get("time", -1)) <= shot.end_sec - pad
    ]
    if not points:
        return []
    peaks: list[dict[str, float]] = []
    for index, point in enumerate(points):
        score = float(point.get("composite", 0.0))
        previous = float(points[index - 1].get("composite", 0.0)) if index else -1.0
        following = float(points[index + 1].get("composite", 0.0)) if index + 1 < len(points) else -1.0
        if score >= 0.34 and score >= previous and score >= following:
            peaks.append(point)
    if not peaks:
        strongest = max(points, key=lambda item: float(item.get("composite", 0.0)))
        if float(strongest.get("composite", 0.0)) >= 0.42:
            peaks.append(strongest)

    separation = max(0.2, min(0.65, shot.duration_sec * 0.1))
    selected: list[dict[str, float]] = []
    for point in sorted(peaks, key=lambda item: float(item.get("composite", 0.0)), reverse=True):
        timestamp = float(point.get("time", 0.0))
        if any(abs(timestamp - float(existing.get("time", 0.0))) < separation for existing in selected):
            continue
        selected.append(point)
        if len(selected) >= maximum:
            break

    candidates: list[dict[str, Any]] = []
    for point in sorted(selected, key=lambda item: float(item.get("time", 0.0))):
        kind, label = _candidate_kind(point)
        timestamp = float(point.get("time", shot.midpoint_sec))
        candidates.append(
            {
                "kind": kind,
                "label": label,
                "time": round(timestamp, 4),
                "phase": round((timestamp - shot.start_sec) / shot.duration_sec, 4),
                "score": round(float(point.get("composite", 0.0)), 4),
                "signals": {
                    "motion": round(float(point.get("motion", 0.0)), 4),
                    "light_change": round(float(point.get("light_change", 0.0)), 4),
                    "audio": round(float(point.get("audio", 0.0)), 4),
                },
            }
        )
    return candidates


def align_events(
    reference_events: list[dict[str, Any]],
    remake_events: list[dict[str, Any]],
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    rows = len(reference_events) + 1
    columns = len(remake_events) + 1
    gap_cost = 0.48
    scores = [[0.0] * columns for _ in range(rows)]
    moves = [[""] * columns for _ in range(rows)]
    for row in range(1, rows):
        scores[row][0] = row * gap_cost
        moves[row][0] = "missing"
    for column in range(1, columns):
        scores[0][column] = column * gap_cost
        moves[0][column] = "extra"
    for row in range(1, rows):
        for column in range(1, columns):
            source = reference_events[row - 1]
            remake = remake_events[column - 1]
            phase_cost = abs(float(source["phase"]) - float(remake["phase"])) * 1.5
            kind_cost = 0.0 if source["kind"] == remake["kind"] else 0.18
            candidates = [
                (scores[row - 1][column - 1] + phase_cost + kind_cost, "match"),
                (scores[row - 1][column] + gap_cost, "missing"),
                (scores[row][column - 1] + gap_cost, "extra"),
            ]
            scores[row][column], moves[row][column] = min(
                candidates,
                key=lambda item: (item[0], {"match": 0, "missing": 1, "extra": 2}[item[1]]),
            )
    result: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    row, column = len(reference_events), len(remake_events)
    while row or column:
        move = moves[row][column]
        if move == "match":
            result.append((reference_events[row - 1], remake_events[column - 1]))
            row -= 1
            column -= 1
        elif move == "missing":
            result.append((reference_events[row - 1], None))
            row -= 1
        else:
            result.append((None, remake_events[column - 1]))
            column -= 1
    result.reverse()
    return result


def _shot_payload(shot: ShotRecord | None, *, path_prefix: str) -> dict[str, Any] | None:
    if shot is None:
        return None
    return {
        "shot_id": shot.shot_id,
        "index": shot.index,
        "start_sec": round(shot.start_sec, 4),
        "end_sec": round(shot.end_sec, 4),
        "duration_sec": round(shot.duration_sec, 4),
        "source_in": format_time(shot.start_sec),
        "source_out": format_time(shot.end_sec),
        "start_frame": f"{path_prefix}/{shot.start_frame_path}" if shot.start_frame_path else "",
        "middle_frame": f"{path_prefix}/{shot.middle_frame_path}" if shot.middle_frame_path else "",
        "end_frame": f"{path_prefix}/{shot.end_frame_path}" if shot.end_frame_path else "",
        "clip": f"{path_prefix}/{shot.clip_path}" if shot.clip_path else "",
    }


def _severity_for_duration(delta_percent: float) -> str:
    magnitude = abs(delta_percent)
    if magnitude >= 0.28:
        return "high"
    if magnitude >= 0.14:
        return "medium"
    if magnitude >= 0.06:
        return "low"
    return "none"


def _anchor_time(shot: ShotRecord, phase: float) -> float:
    if phase <= 0:
        return shot.start_sec + min(0.08, shot.duration_sec * 0.08)
    if phase >= 1:
        return max(shot.start_sec, shot.end_sec - min(0.08, shot.duration_sec * 0.08))
    return shot.start_sec + shot.duration_sec * phase


def extract_evidence_frame(video_path: Path, timestamp: float, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, timestamp):.6f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale='min(640,iw)':-2,setsar=1",
        "-q:v",
        "3",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not output_path.is_file():
        raise RuntimeError((result.stderr or "evidence frame extraction failed").strip())


def _event_side(
    event: dict[str, Any] | None,
    *,
    image: str = "",
) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "time": round(float(event["time"]), 4),
        "timecode": format_time(float(event["time"])),
        "phase": round(float(event["phase"]), 4),
        "score": round(float(event.get("score", 0.0)), 4),
        "signals": event.get("signals", {}),
        "image": image,
    }


def build_nodes(
    pair_id: str,
    reference: ShotRecord | None,
    remake: ShotRecord | None,
    *,
    reference_track: list[dict[str, float]],
    remake_track: list[dict[str, float]],
    reference_video: Path | None,
    remake_video: Path | None,
    evidence_root: Path | None,
    evidence_project_prefix: str,
    reference_path_prefix: str,
    remake_path_prefix: str,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    anchors = [
        ("opening", "开场状态", 0.0, "start_frame"),
        ("middle", "中段检查点", 0.5, "middle_frame"),
        ("bridge", "收势与下一镜桥接", 1.0, "end_frame"),
    ]
    for kind, label, phase, frame_field in anchors:
        source_side = None
        remake_side = None
        if reference is not None:
            source_time = _anchor_time(reference, phase)
            source_side = {
                "time": round(source_time, 4),
                "timecode": format_time(source_time),
                "phase": phase,
                "score": None,
                "signals": {},
                "image": f"{reference_path_prefix}/{getattr(reference, frame_field.replace('_frame', '_frame_path'))}",
            }
        if remake is not None:
            remake_time = _anchor_time(remake, phase)
            remake_side = {
                "time": round(remake_time, 4),
                "timecode": format_time(remake_time),
                "phase": phase,
                "score": None,
                "signals": {},
                "image": f"{remake_path_prefix}/{getattr(remake, frame_field.replace('_frame', '_frame_path'))}",
            }
        nodes.append(
            {
                "node_id": f"{pair_id}-N{len(nodes) + 1:02d}",
                "kind": kind,
                "label": label,
                "status": "matched" if source_side and remake_side else ("missing" if source_side else "extra"),
                "source": source_side,
                "remake": remake_side,
                "phase_delta_percent": 0.0 if source_side and remake_side else None,
                "conclusion_level": "已观察",
            }
        )

    if reference is None and remake is None:
        return nodes
    reference_events = event_candidates(reference_track, reference) if reference else []
    remake_events = event_candidates(remake_track, remake) if remake else []
    for source_event, remake_event in align_events(reference_events, remake_events):
        label = (source_event or remake_event or {}).get("label", "变化峰值候选")
        source_image = ""
        remake_image = ""
        node_number = len(nodes) + 1
        if evidence_root is not None:
            if source_event is not None and reference_video is not None:
                relative = Path(pair_id) / f"N{node_number:02d}_source.jpg"
                target = evidence_root / relative
                extract_evidence_frame(reference_video, float(source_event["time"]), target)
                source_image = f"{evidence_project_prefix}/{relative.as_posix()}"
            if remake_event is not None and remake_video is not None:
                relative = Path(pair_id) / f"N{node_number:02d}_remake.jpg"
                target = evidence_root / relative
                extract_evidence_frame(remake_video, float(remake_event["time"]), target)
                remake_image = f"{evidence_project_prefix}/{relative.as_posix()}"
        phase_delta = None
        if source_event is not None and remake_event is not None:
            phase_delta = round((float(remake_event["phase"]) - float(source_event["phase"])) * 100, 1)
        nodes.append(
            {
                "node_id": f"{pair_id}-N{node_number:02d}",
                "kind": (source_event or remake_event or {}).get("kind", "signal_peak"),
                "label": label,
                "status": "matched" if source_event and remake_event else ("missing" if source_event else "extra"),
                "source": _event_side(source_event, image=source_image),
                "remake": _event_side(remake_event, image=remake_image),
                "phase_delta_percent": phase_delta,
                "conclusion_level": "工程数据验证",
            }
        )
    return sorted(
        nodes,
        key=lambda item: (
            float((item.get("source") or item.get("remake") or {}).get("phase", 0.0)),
            item["node_id"],
        ),
    )


def _finding_for_pair(pair: dict[str, Any]) -> dict[str, Any] | None:
    reference = pair.get("source")
    remake = pair.get("remake")
    if reference is None:
        return {
            "finding_id": f"F-{pair['pair_id']}",
            "pair_id": pair["pair_id"],
            "dimension": "镜间连续性",
            "severity": "high",
            "source_fact": "原片没有这个独立镜头。",
            "remake_fact": f"复刻片包含 {remake['shot_id']}，时长 {remake['duration_sec']:.3f} 秒。",
            "visible_difference": "复刻片新增了一个切换或把连续动作拆成了额外镜头。",
            "impact": "动作因果和观看节拍可能被中断，需要确认这是不是误切或多余镜头。",
            "recommendation": "先核对切点；若确为额外镜头，判断它是否承担原片不存在的信息，再决定合并或删除。",
            "conclusion_level": "工程数据验证",
        }
    if remake is None:
        return {
            "finding_id": f"F-{pair['pair_id']}",
            "pair_id": pair["pair_id"],
            "dimension": "镜间连续性",
            "severity": "high",
            "source_fact": f"原片包含 {reference['shot_id']}，时长 {reference['duration_sec']:.3f} 秒。",
            "remake_fact": "复刻片没有对应镜头。",
            "visible_difference": "原片的一个完整镜头在复刻片中缺失或与相邻镜头错误合并。",
            "impact": "该镜承担的动作阶段、反应或桥接可能丢失。",
            "recommendation": "先核对切点与对应关系；若确实缺失，按原片入点、出点和关键节点补回。",
            "conclusion_level": "工程数据验证",
        }
    delta_percent = float(pair["metrics"]["duration_delta_percent"])
    severity = _severity_for_duration(delta_percent)
    if severity == "none":
        return None
    direction = "更长" if delta_percent > 0 else "更短"
    return {
        "finding_id": f"F-{pair['pair_id']}",
        "pair_id": pair["pair_id"],
        "dimension": "节奏",
        "severity": severity,
        "source_fact": f"原片 {reference['shot_id']} 为 {reference['duration_sec']:.3f} 秒。",
        "remake_fact": f"复刻 {remake['shot_id']} 为 {remake['duration_sec']:.3f} 秒。",
        "visible_difference": f"复刻镜头比原片{direction} {abs(pair['metrics']['duration_delta_sec']):.3f} 秒（{abs(delta_percent) * 100:.1f}%）。",
        "impact": "镜头内部的启动、峰值、反应和收势可能发生整体挤压或拖延。",
        "recommendation": "不要直接匀速拉伸；逐个核对关键节点的相位偏移，再调整动作段或切点。",
        "conclusion_level": "工程数据验证",
    }


DIMENSIONS = [
    {
        "key": "rhythm",
        "label": "节奏",
        "automatic_scope": "镜头数量、时长、切点漂移和候选峰值相位可测；情绪节奏仍需审核。",
    },
    {
        "key": "composition",
        "label": "构图",
        "automatic_scope": "仅提供成对帧和连续时间段，不用角色替换后的像素差判定好坏。",
    },
    {
        "key": "action_physics",
        "label": "动作与力学",
        "automatic_scope": "运动变化只能定位候选节点；启动、接触、重击、反应和受力方向需人机复核。",
    },
    {
        "key": "camera",
        "label": "摄影机画面效果",
        "automatic_scope": "画面变化不自动命名为推拉摇移、变焦或拉焦。",
    },
    {
        "key": "look_vfx",
        "label": "VFX、灯光、色调",
        "automatic_scope": "亮度变化只作为候选证据，不等同于特效、交互光或调色结论。",
    },
    {
        "key": "sound",
        "label": "声音",
        "automatic_scope": "可测声音能量峰值，音效类型、同步意图和混音层次需复核。",
    },
    {
        "key": "continuity",
        "label": "镜间连续性",
        "automatic_scope": "可发现缺镜、增镜和切点漂移；姿态、视线、屏幕方向与力向需审核。",
    },
]


def _default_review() -> dict[str, Any]:
    return {
        "pair_reviews": {},
        "technical_identifications": [],
        "fix_queue": [],
        "overall_note": "",
    }


def build_comparison_analysis(
    *,
    reference_shots_path: Path,
    remake_shots_path: Path,
    reference_metadata: dict[str, Any],
    remake_metadata: dict[str, Any],
    reference_track: list[dict[str, float]],
    remake_track: list[dict[str, float]],
    reference_video: Path | None,
    remake_video: Path | None,
    evidence_root: Path | None,
    evidence_project_prefix: str,
    reference_path_prefix: str = "analysis",
    remake_path_prefix: str = "analysis",
    alignment_override: list[dict[str, Any]] | None = None,
    existing_review: dict[str, Any] | None = None,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    reference_shots = load_shots(reference_shots_path)
    remake_shots = load_shots(remake_shots_path)
    operations = (
        alignment_from_override(reference_shots, remake_shots, alignment_override)
        if alignment_override is not None
        else align_shots(reference_shots, remake_shots)
    )
    if evidence_root is not None:
        shutil.rmtree(evidence_root, ignore_errors=True)
        evidence_root.mkdir(parents=True, exist_ok=True)

    reference_probe = reference_metadata.get("probe", {})
    remake_probe = remake_metadata.get("probe", {})
    reference_duration = float(reference_probe.get("duration_sec") or reference_shots[-1].end_sec)
    remake_duration = float(remake_probe.get("duration_sec") or remake_shots[-1].end_sec)
    pairs: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    operation_count = max(1, len(operations))
    for index, (reference, remake) in enumerate(operations, start=1):
        pair_id = f"PAIR{index:03d}"
        source_payload = _shot_payload(reference, path_prefix=reference_path_prefix)
        remake_payload = _shot_payload(remake, path_prefix=remake_path_prefix)
        metrics: dict[str, Any] = {}
        if reference is not None and remake is not None:
            duration_delta = remake.duration_sec - reference.duration_sec
            metrics = {
                "duration_delta_sec": round(duration_delta, 4),
                "duration_delta_percent": round(duration_delta / max(reference.duration_sec, 1e-6), 4),
                "cut_in_drift_sec": round(
                    remake.start_sec / max(remake_duration, 1e-6) * reference_duration - reference.start_sec,
                    4,
                ),
                "cut_out_drift_sec": round(
                    remake.end_sec / max(remake_duration, 1e-6) * reference_duration - reference.end_sec,
                    4,
                ),
            }
        nodes = build_nodes(
            pair_id,
            reference,
            remake,
            reference_track=reference_track,
            remake_track=remake_track,
            reference_video=reference_video,
            remake_video=remake_video,
            evidence_root=evidence_root,
            evidence_project_prefix=evidence_project_prefix,
            reference_path_prefix=reference_path_prefix,
            remake_path_prefix=remake_path_prefix,
        )
        pair = {
            "pair_id": pair_id,
            "status": "matched" if reference and remake else ("missing" if reference else "extra"),
            "source": source_payload,
            "remake": remake_payload,
            "metrics": metrics,
            "nodes": nodes,
        }
        pair_finding = _finding_for_pair(pair)
        if pair_finding:
            pair["finding_ids"] = [pair_finding["finding_id"]]
            findings.append(pair_finding)
        else:
            pair["finding_ids"] = []
        pairs.append(pair)
        if on_progress:
            on_progress(
                round(index / operation_count * 100),
                "正在建立逐镜与关键节点证据",
                f"{pair_id} · {index} / {operation_count}",
            )

    duration_delta = remake_duration - reference_duration
    matched = sum(1 for pair in pairs if pair["status"] == "matched")
    missing = sum(1 for pair in pairs if pair["status"] == "missing")
    extra = sum(1 for pair in pairs if pair["status"] == "extra")
    source_durations = [shot.duration_sec for shot in reference_shots]
    remake_durations = [shot.duration_sec for shot in remake_shots]
    summary = {
        "reference_shot_count": len(reference_shots),
        "remake_shot_count": len(remake_shots),
        "matched_pair_count": matched,
        "missing_shot_count": missing,
        "extra_shot_count": extra,
        "reference_duration_sec": round(reference_duration, 4),
        "remake_duration_sec": round(remake_duration, 4),
        "duration_delta_sec": round(duration_delta, 4),
        "duration_delta_percent": round(duration_delta / max(reference_duration, 1e-6), 4),
        "reference_median_shot_sec": round(median(source_durations), 4),
        "remake_median_shot_sec": round(median(remake_durations), 4),
        "finding_count": len(findings),
        "high_finding_count": sum(1 for finding in findings if finding["severity"] == "high"),
    }
    return {
        "analysis_version": "1.0",
        "summary": summary,
        "dimensions": DIMENSIONS,
        "alignment": {
            "mode": "manual" if alignment_override is not None else "automatic",
            "manual_review_required": True,
            "pairs": pairs,
        },
        "findings": findings,
        "signal_scope": {
            "reference_samples": len(reference_track),
            "remake_samples": len(remake_track),
            "statement": "运动、亮度和声音曲线只用于定位候选节点，不直接命名电影技术或动作语义。",
        },
        "review": existing_review if isinstance(existing_review, dict) else _default_review(),
    }


def _limited_text(value: Any, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def normalize_review_payload(payload: Any, *, pair_ids: Iterable[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("review must be an object")
    valid_pairs = set(pair_ids)
    valid_dimensions = {item["key"] for item in DIMENSIONS}
    valid_statuses = {"unreviewed", "match", "difference", "uncertain"}
    normalized = _default_review()
    pair_reviews = payload.get("pair_reviews", {})
    if not isinstance(pair_reviews, dict):
        raise ValueError("pair_reviews must be an object")
    for pair_id, review in pair_reviews.items():
        if pair_id not in valid_pairs or not isinstance(review, dict):
            continue
        dimensions: dict[str, Any] = {}
        raw_dimensions = review.get("dimensions", {})
        if isinstance(raw_dimensions, dict):
            for key, value in raw_dimensions.items():
                if key not in valid_dimensions or not isinstance(value, dict):
                    continue
                status = str(value.get("status") or "unreviewed")
                dimensions[key] = {
                    "status": status if status in valid_statuses else "unreviewed",
                    "note": _limited_text(value.get("note"), 4000),
                    "evidence": _limited_text(value.get("evidence"), 1500),
                }
        node_reviews: dict[str, Any] = {}
        raw_nodes = review.get("node_reviews", {})
        if isinstance(raw_nodes, dict):
            for node_id, value in raw_nodes.items():
                if not isinstance(value, dict) or not str(node_id).startswith(f"{pair_id}-N"):
                    continue
                node_reviews[str(node_id)[:40]] = {
                    "event_label": _limited_text(value.get("event_label"), 120),
                    "status": (
                        str(value.get("status"))
                        if str(value.get("status")) in valid_statuses
                        else "unreviewed"
                    ),
                    "note": _limited_text(value.get("note"), 2000),
                }
        normalized["pair_reviews"][pair_id] = {
            "dimensions": dimensions,
            "node_reviews": node_reviews,
            "conclusion": _limited_text(review.get("conclusion"), 5000),
        }

    identifications = payload.get("technical_identifications", [])
    if isinstance(identifications, list):
        for item in identifications[:200]:
            if not isinstance(item, dict):
                continue
            level = str(item.get("evidence_level") or "无法确认")
            if level not in {"已观察", "与某技术特征匹配", "有工程数据验证", "无法确认"}:
                level = "无法确认"
            normalized["technical_identifications"].append(
                {
                    "id": _limited_text(item.get("id"), 80),
                    "pair_id": str(item.get("pair_id") or "") if item.get("pair_id") in valid_pairs else "",
                    "term": _limited_text(item.get("term"), 160),
                    "evidence_level": level,
                    "basis": _limited_text(item.get("basis"), 4000),
                }
            )

    queue = payload.get("fix_queue", [])
    if isinstance(queue, list):
        for item in queue[:300]:
            if not isinstance(item, dict):
                continue
            pair_id = str(item.get("pair_id") or "")
            if pair_id and pair_id not in valid_pairs:
                continue
            status = str(item.get("status") or "open")
            if status not in {"open", "doing", "done"}:
                status = "open"
            normalized["fix_queue"].append(
                {
                    "id": _limited_text(item.get("id"), 80),
                    "pair_id": pair_id,
                    "severity": (
                        str(item.get("severity"))
                        if str(item.get("severity")) in {"high", "medium", "low"}
                        else "medium"
                    ),
                    "status": status,
                    "problem": _limited_text(item.get("problem"), 1000),
                    "action": _limited_text(item.get("action"), 3000),
                }
            )
    normalized["overall_note"] = _limited_text(payload.get("overall_note"), 8000)
    return normalized


def remap_review_for_alignment(
    review: dict[str, Any] | None,
    old_pairs: list[dict[str, Any]],
    new_pairs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Keep reviews only on an unchanged source/remake pairing after realignment."""

    if not isinstance(review, dict):
        return _default_review(), {"preserved_pair_reviews": 0, "dropped_pair_reviews": 0}

    def pair_key(pair: dict[str, Any]) -> tuple[str, str]:
        return (
            str((pair.get("source") or {}).get("shot_id") or ""),
            str((pair.get("remake") or {}).get("shot_id") or ""),
        )

    old_id_to_key = {
        str(pair.get("pair_id")): pair_key(pair)
        for pair in old_pairs
        if isinstance(pair, dict) and pair.get("pair_id")
    }
    new_key_to_id = {
        pair_key(pair): str(pair.get("pair_id"))
        for pair in new_pairs
        if isinstance(pair, dict) and pair.get("pair_id")
    }
    remapped = _default_review()
    remapped["overall_note"] = _limited_text(review.get("overall_note"), 8000)
    preserved = 0
    dropped = 0
    pair_reviews = review.get("pair_reviews", {})
    if isinstance(pair_reviews, dict):
        for old_id, value in pair_reviews.items():
            new_id = new_key_to_id.get(old_id_to_key.get(str(old_id), ("", "")))
            if new_id and isinstance(value, dict):
                remapped["pair_reviews"][new_id] = value
                preserved += 1
            else:
                dropped += 1

    def remap_items(name: str) -> None:
        items = review.get(name, [])
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            old_id = str(copied.get("pair_id") or "")
            if old_id:
                copied["pair_id"] = new_key_to_id.get(old_id_to_key.get(old_id, ("", "")), "")
            remapped[name].append(copied)

    remap_items("technical_identifications")
    remap_items("fix_queue")
    valid_ids = [str(pair.get("pair_id")) for pair in new_pairs if isinstance(pair, dict)]
    return normalize_review_payload(remapped, pair_ids=valid_ids), {
        "preserved_pair_reviews": preserved,
        "dropped_pair_reviews": dropped,
    }
