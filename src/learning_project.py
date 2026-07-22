#!/usr/bin/env python3
"""Build the learning-loop review payload from mechanical analysis artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


LEARNING_SCHEMA_VERSION = "2.0"
WORKFLOW_VERSION = "0.2.2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _integer(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _shot_scaffold(row: dict[str, str], index: int) -> dict[str, Any]:
    shot_id = row.get("shot_id") or f"SH{index:03d}"
    source_in_frame = _integer(row.get("source_start_frame"))
    source_out_frame = _integer(row.get("source_end_frame"), source_in_frame + 1)
    start = row.get("start_frame_path") or f"shots/{shot_id}/start.jpg"
    middle = row.get("middle_frame_path") or f"shots/{shot_id}/middle.jpg"
    end = row.get("end_frame_path") or f"shots/{shot_id}/end.jpg"
    return {
        "shot_id": shot_id,
        "shot_index": index,
        "title": f"待分析 · {shot_id}",
        "source_in_frame": source_in_frame,
        "source_out_frame": source_out_frame,
        "source_in": row.get("source_in") or "00:00:00.000",
        "source_out": row.get("source_out") or "00:00:00.000",
        "duration_sec": round(_number(row.get("duration_sec")), 6),
        "previous_shot_result": "",
        "next_shot_bridge": "",
        "phase": "待人工分类",
        "boundary_type": "自动候选切点",
        "boundary_evidence": (
            f"自动检测候选边界 F{source_in_frame}–F{source_out_frame}；"
            "快速运动、闪白、粒子遮挡和渐变仍需人工复核。"
        ),
        "opening_frame_literal": "",
        "screen_layout": "",
        "literal_visual_description": "",
        "end_state": "",
        "character_positions": "",
        "clone_count_and_positions": "",
        "body_part_specifics": "",
        "timeline_beats": "",
        "force_contact_response": "",
        "performance_intent": "",
        "camera_position": "",
        "camera_motion": "",
        "lens_fov": "",
        "focus_depth": "",
        "composition_editing": "",
        "vfx_layers_timing": "",
        "interactive_lighting": "",
        "environment_response": "",
        "sound_beats": "",
        "often_missed": "等待 AI 初拉或人工盲审后确认本镜最容易遗漏的内容。",
        "uncertainties": "自动流程只确认机械证据，语义判断全部待核。",
        "rebuild_route": "完成最终拉片结论后再生成复刻任务。",
        "source_fidelity_vs_adaptation": "先记录原片事实，再单独决定角色与场景适配。",
        "analysis_confidence": "待核",
        "start_frame": start,
        "middle_frame": middle,
        "end_frame": end,
        "dense_contact_sheet": f"shots/{shot_id}/contact_sheet.jpg",
        "clip": row.get("clip_path") or "",
    }


def load_analysis_rows(analysis_root: Path) -> list[dict[str, str]]:
    path = analysis_root / "shots.csv"
    if not path.is_file():
        raise FileNotFoundError(f"missing mechanical shot table: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("mechanical shot table contains no shots")
    return rows


def build_learning_payload(
    analysis_root: Path,
    *,
    project_id: str,
    title: str,
    source_path: Path,
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    rows = load_analysis_rows(analysis_root)
    metadata_path = analysis_root / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    probe = metadata.get("probe", {}) if isinstance(metadata, dict) else {}
    return {
        "schema_version": LEARNING_SCHEMA_VERSION,
        "project": {
            "project_id": project_id,
            "title": title,
            "source_filename": source_path.name,
            "source_fingerprint": source_fingerprint or sha256_file(source_path),
            "workflow_version": WORKFLOW_VERSION,
            "storage_scope": project_id,
            "advisor_asset_prefix": f"{project_id}/analysis",
            "created_at": utc_now(),
            "duration_sec": _number(probe.get("duration_sec")),
            "fps": _number(probe.get("fps")),
            "shot_count": len(rows),
        },
        "learning_contract": {
            "layers": [
                "source_evidence",
                "ai_draft",
                "human_draft",
                "final_decision",
                "correction_reason",
                "trusted_knowledge",
                "replication_evaluation",
            ],
            "ai_is_optional": True,
            "human_promotion_required": True,
        },
        "shots": [_shot_scaffold(row, index) for index, row in enumerate(rows, start=1)],
    }


def write_learning_payload(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def build_learning_yaml(
    analysis_root: Path,
    output_path: Path,
    *,
    project_id: str,
    title: str,
    source_path: Path,
    source_fingerprint: str | None = None,
) -> Path:
    payload = build_learning_payload(
        analysis_root,
        project_id=project_id,
        title=title,
        source_path=source_path,
        source_fingerprint=source_fingerprint,
    )
    return write_learning_payload(payload, output_path)
