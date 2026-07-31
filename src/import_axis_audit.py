#!/usr/bin/env python3
"""Import a compare-video-axes result into the local remake workbench."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, Iterable

import yaml

from build_comparison_page import build as build_comparison_page
from comparison_engine import normalize_event_segments
from workbench_server import ProjectStore, new_comparison_id, process_comparison


CLASSIFICATION_LABELS = {
    "keep_with_minor_edit": "可保留并小修",
    "partial_or_targeted_repair": "局部修复",
    "rebuild_or_post_insert": "重做或补插帧",
}
CLASSIFICATION_TONES = {
    "keep_with_minor_edit": "keep",
    "partial_or_targeted_repair": "repair",
    "rebuild_or_post_insert": "rebuild",
}
AXIS_LABELS = {
    "production_structure": "生成结构",
    "composition": "构图与画幅",
    "rhythm": "节奏",
    "camera_and_focus": "机位与焦点",
    "combat_causality": "动作因果",
    "vfx": "特效",
    "scene_continuity": "场景连续性",
    "color_and_light": "色彩与光线",
    "sound": "声音",
}
STAGE_KEYS = ("entry", "development", "peak", "reaction", "exit")


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be an object: {path}")
    return payload


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def shot_number(shot_id: str) -> int:
    match = re.fullmatch(r"SH(\d{3})", shot_id)
    if not match:
        raise ValueError(f"invalid shot id: {shot_id}")
    return int(match.group(1))


def inclusive_shot_ids(first: str, last: str) -> list[str]:
    start = shot_number(first)
    end = shot_number(last)
    if end < start:
        raise ValueError(f"shot range is reversed: {first} to {last}")
    return [f"SH{index:03d}" for index in range(start, end + 1)]


def parse_candidate_range(raw: Any) -> tuple[float, float] | None:
    match = re.fullmatch(r"\s*([0-9.]+)_to_([0-9.]+)\s*", str(raw or ""))
    if not match:
        return None
    start, end = (float(value) for value in match.groups())
    return (start, end) if end > start else None


def parse_timecode(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        raise ValueError("missing timecode")
    if re.fullmatch(r"[0-9.]+", text):
        return float(text)
    parts = text.split(":")
    if len(parts) != 3:
        raise ValueError(f"invalid timecode: {text}")
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def shot_range(shot: dict[str, Any]) -> tuple[float, float]:
    start = parse_timecode(shot.get("source_in_sec", shot.get("source_in")))
    end = parse_timecode(shot.get("source_out_sec", shot.get("source_out")))
    if end <= start:
        raise ValueError(f"invalid shot range: {shot.get('shot_id')}")
    return start, end


def piecewise_map(reference_time: float, anchors: Iterable[dict[str, Any]]) -> float:
    ordered = sorted(anchors, key=lambda item: float(item["reference_time_sec"]))
    if len(ordered) < 2:
        raise ValueError("at least two alignment anchors are required")
    value = float(reference_time)
    if value <= float(ordered[0]["reference_time_sec"]):
        return float(ordered[0]["candidate_time_sec"])
    for left, right in zip(ordered, ordered[1:]):
        source_start = float(left["reference_time_sec"])
        source_end = float(right["reference_time_sec"])
        if value <= source_end + 1e-9:
            phase = (value - source_start) / max(1e-9, source_end - source_start)
            candidate_start = float(left["candidate_time_sec"])
            candidate_end = float(right["candidate_time_sec"])
            return candidate_start + (candidate_end - candidate_start) * phase
    return float(ordered[-1]["candidate_time_sec"])


def classification_index(findings: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    source = findings.get("shot_classification") or {}
    if not isinstance(source, dict):
        return result
    for key in CLASSIFICATION_LABELS:
        values = source.get(key) or []
        if not isinstance(values, list):
            continue
        for shot_id in values:
            result[str(shot_id)] = key
    return result


def severity_tone(value: Any) -> str:
    return {"P0": "high", "P1": "medium", "P2": "low"}.get(str(value), "medium")


def source_description(shot: dict[str, Any]) -> str:
    fields = [
        ("真实画面", shot.get("literal_visual_description")),
        ("机位", shot.get("camera_position")),
        ("运镜", shot.get("camera_motion")),
        ("焦段", shot.get("lens_fov")),
        ("焦点", shot.get("focus_depth")),
        ("动作与受力", shot.get("force_contact_response")),
        ("特效", shot.get("vfx_layers_timing")),
        ("镜尾状态", shot.get("end_state")),
    ]
    body = "；".join(f"{label}：{value}" for label, value in fields if value)
    return f"{shot.get('shot_id')} {shot.get('title') or ''}。{body}".strip()


def segment_root_cause(segment: dict[str, Any]) -> str:
    risk = str(segment.get("execution_risk") or "unknown")
    if risk == "high":
        return "重点要求已写入提示词，但本段镜头密度和极短节拍过高，模型或生成链把逐镜合同重编为概括性蒙太奇。"
    return "重点要求已写入提示词且本段不属于最高密度，偏差更指向模型或生成链未遵守合同；缺少生成元数据时不能继续细分。"


def segment_next_action(critical_items: list[dict[str, Any]], shot_ids: list[str]) -> str:
    actions = [
        f"{item.get('shot_id')}：{str(item.get('repair') or '').strip()}"
        for item in critical_items
        if str(item.get("repair") or "").strip()
    ]
    if actions:
        return "\n".join(actions)
    return f"按单镜或单一机位运动重做 {' '.join(shot_ids)}，分别验收进入状态、动作轨迹、接触、受力和稳定结果。"


def build_import_payload(
    *,
    audit_dir: Path,
    comparison_id: str,
    prompt_dir: Path,
    config_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    findings = load_yaml(audit_dir / "human-findings.yaml")
    manifest = load_yaml(audit_dir / "comparison-manifest.yaml")
    prompt_contract = load_yaml(audit_dir / "prompt-contract.yaml")
    config = load_yaml(config_path)
    timeline_path = Path(str(manifest["reference_timeline"])).expanduser().resolve()
    timeline = load_yaml(timeline_path)
    shots = timeline.get("shots") or []
    if not isinstance(shots, list) or not shots:
        raise ValueError("reference timeline has no shots")
    shots_by_id = {str(item["shot_id"]): item for item in shots if isinstance(item, dict)}
    classification = classification_index(findings)
    if set(shots_by_id) != set(classification):
        missing = sorted(set(shots_by_id) - set(classification))
        extra = sorted(set(classification) - set(shots_by_id))
        raise ValueError(f"shot classification mismatch: missing={missing} extra={extra}")

    critical_items = findings.get("critical_shot_findings") or []
    critical_by_id = {
        str(item["shot_id"]): item for item in critical_items if isinstance(item, dict) and item.get("shot_id")
    }
    phase_by_id = {
        str(item["id"]): item
        for item in findings.get("phase_findings") or []
        if isinstance(item, dict) and item.get("id")
    }
    prompt_by_id = {
        str(item["segment_id"]): item
        for item in prompt_contract.get("segments") or []
        if isinstance(item, dict) and item.get("segment_id")
    }
    units = config.get("analysis_units") or []
    anchors = (manifest.get("alignment") or {}).get("anchors") or []
    if not isinstance(units, list) or not units:
        raise ValueError("comparison config must contain analysis units")
    if not isinstance(anchors, list) or len(anchors) < 2:
        raise ValueError("comparison manifest must contain alignment anchors")

    event_payload: dict[str, Any] = {
        "schema_version": "1.0",
        "statement": f"参考轴与生成轴保留独立时间码。{len(units)}段端点使用人工确认锚点，段内非关键镜头仅用单调插值定位，不把插值当成动作已对应。",
        "tolerance_policy": "角色、雪山场景和红黑配色属于授权改编；机位任务、焦点变化、动作因果、接触力向、节奏位置和特效作用关系仍必须保持。",
        "events": [],
    }
    review: dict[str, Any] = {
        "event_reviews": {},
        "pair_reviews": {},
        "technical_identifications": [],
        "fix_queue": [],
        "overall_note": "",
    }
    audit_shots: list[dict[str, Any]] = []

    for unit in units:
        segment_id = str(unit["id"])
        prompt = prompt_by_id.get(segment_id)
        phase = phase_by_id.get(segment_id)
        if not prompt or not phase:
            raise ValueError(f"missing prompt or human finding for {segment_id}")
        shot_ids = inclusive_shot_ids(str(prompt["first_shot"]), str(prompt["last_shot"]))
        source_start = float(unit["reference_start_sec"])
        source_end = float(unit["reference_end_sec"])
        remake_start = piecewise_map(source_start, anchors)
        remake_end = piecewise_map(source_end, anchors)
        segment_critical = [critical_by_id[item] for item in shot_ids if item in critical_by_id]
        event_status = "difference" if phase.get("verdict") == "fail" else "partial"
        event_severity = "high" if event_status == "difference" or any(
            item.get("severity") == "P0" for item in segment_critical
        ) else "medium"
        root_cause = segment_root_cause(prompt)
        next_action = segment_next_action(segment_critical, shot_ids)
        preserved_text = str(phase.get("finding") or "").split("；", 1)[0].strip()
        preserved = [preserved_text] if any(
            marker in preserved_text for marker in ("成立", "保留", "存在", "可用")
        ) else []
        prompt_path = Path(str(prompt["path"]))
        prompt_copy = f"attachments/prompts/{prompt_path.name}"
        event: dict[str, Any] = {
            "event_id": segment_id,
            "title": str(unit.get("title") or prompt.get("title") or segment_id),
            "generation_segment": segment_id,
            "source_shot_ids": shot_ids,
            "source": {"start_sec": source_start, "end_sec": source_end},
            "remake": {"start_sec": remake_start, "end_sec": remake_end},
            "stages": [],
            "invariants": [
                f"完整保留 {shot_ids[0]} 至 {shot_ids[-1]} 的事件顺序、主动者、接触和结果",
                "授权替换只改变角色、雪山场景与红黑配色，不改变机位任务和动作因果",
                "所有短镜、反打、遮切、拉焦和冲击插帧必须有可回看的连续证据",
            ],
            "tolerances": ["次级毛发、衣物、雪尘和随机粒子可浮动，但不能遮住身份、接触点和力向。"],
            "failure_conditions": [
                "关键动作被静态近脸、无因硬切或持续能量覆盖替代",
                "人物身份、手性、接触点、力向或稳定结果无法连续验证",
                "生成段保留大事件名称却删除承担因果的短镜和结果镜",
            ],
            "analysis": {
                "status": event_status,
                "severity": event_severity,
                "conclusion_level": "人工复核完成",
                "preserved": preserved,
                "observed_difference": str(phase.get("finding") or ""),
                "impact": f"本段相对参考轴时长变化 {float(phase.get('timing_delta_sec') or 0):+.3f} 秒；偏差影响镜头任务、动作可读性和前后因果。",
                "likely_root_cause": root_cause,
                "next_action": next_action,
            },
            "prompt_trace": {
                "file": prompt_copy,
                "instruction": prompt_path.read_text(encoding="utf-8")[:3000],
                "diagnosis": root_cause,
            },
            "evidence_sheet": f"comparisons/{comparison_id}/event_evidence/{segment_id}.jpg",
        }

        for index, shot_id in enumerate(shot_ids):
            shot = shots_by_id[shot_id]
            source_shot_start, source_shot_end = shot_range(shot)
            candidate_start = piecewise_map(source_shot_start, anchors)
            candidate_end = piecewise_map(source_shot_end, anchors)
            critical = critical_by_id.get(shot_id)
            exact_range = parse_candidate_range(critical.get("candidate_range_sec")) if critical else None
            if exact_range:
                candidate_start, candidate_end = exact_range
            class_key = classification[shot_id]
            stage_status = "difference" if class_key == "rebuild_or_post_insert" else "partial"
            observation = (
                str(critical.get("finding"))
                if critical
                else f"人工分流为{CLASSIFICATION_LABELS[class_key]}。当前只冻结分流结论，具体差异仍需结合该段连续画面复核。"
            )
            bucket = min(4, int(index * 5 / max(1, len(shot_ids))))
            stage_id = f"{segment_id}-{shot_id}"
            event["stages"].append(
                {
                    "stage_id": stage_id,
                    "key": STAGE_KEYS[bucket],
                    "label": f"{shot_id} · {shot.get('title') or '镜头核验'}",
                    "status": stage_status,
                    "source": {"start_sec": source_shot_start, "end_sec": source_shot_end},
                    "remake": {"start_sec": candidate_start, "end_sec": candidate_end},
                    "source_description": source_description(shot),
                    "remake_observation": observation,
                }
            )
            review["event_reviews"].setdefault(
                segment_id,
                {
                    "status": event_status,
                    "severity": event_severity,
                    "conclusion": "本段未达到忠实模仿标准。" if event_status == "difference" else "本段仅部分成立，需要按分流结果修复。",
                    "observed_difference": str(phase.get("finding") or ""),
                    "impact": event["analysis"]["impact"],
                    "root_cause": root_cause,
                    "next_action": next_action,
                    "stage_reviews": {},
                },
            )["stage_reviews"][stage_id] = {"status": stage_status, "note": observation}
            audit_shots.append(
                {
                    "shot_id": shot_id,
                    "title": str(shot.get("title") or ""),
                    "segment_id": segment_id,
                    "classification": class_key,
                    "classification_label": CLASSIFICATION_LABELS[class_key],
                    "tone": CLASSIFICATION_TONES[class_key],
                    "source_start_sec": source_shot_start,
                    "source_end_sec": source_shot_end,
                    "candidate_start_sec": round(candidate_start, 4),
                    "candidate_end_sec": round(candidate_end, 4),
                    "alignment_basis": "manual_dense_review" if critical else "segment_anchor_interpolation",
                    "critical": bool(critical),
                    "severity": str(critical.get("severity")) if critical else "",
                    "finding": str(critical.get("finding") or "") if critical else "",
                    "repair": str(critical.get("repair") or "") if critical else "",
                    "evidence_sheet": (
                        f"comparisons/{comparison_id}/audit_evidence/{shot_id}.jpg" if critical else ""
                    ),
                }
            )
        event_payload["events"].append(event)

    reference_duration = float((manifest.get("reference_axis") or {}).get("duration_sec") or 0)
    remake_duration = float((manifest.get("candidate_axis") or {}).get("duration_sec") or 0)
    event_segments = normalize_event_segments(
        event_payload,
        reference_duration=reference_duration,
        remake_duration=remake_duration,
    )

    severity_map = {"P0": "high", "P1": "medium", "P2": "low"}
    for item in findings.get("global_findings") or []:
        review["fix_queue"].append(
            {
                "id": f"Q-{item.get('id')}",
                "pair_id": "",
                "event_id": "",
                "severity": severity_map.get(str(item.get("severity")), "medium"),
                "status": "open",
                "problem": str(item.get("finding") or ""),
                "action": str(item.get("repair") or ""),
            }
        )
    next_test = str((findings.get("repair_plan") or {}).get("next_test_design") or "")
    golden_shots = list(dict.fromkeys(re.findall(r"SH\d{3}", next_test)))
    review["fix_queue"].insert(
        0,
        {
            "id": "Q-GOLDEN-SHOTS",
            "pair_id": "",
            "event_id": "",
            "severity": "high",
            "status": "open",
            "problem": "下一轮先验证八个黄金镜头，不直接重做整条。",
            "action": next_test,
        },
    )
    review["technical_identifications"] = [
        {
            "id": f"T-{item['shot_id']}",
            "pair_id": "",
            "event_id": next(
                shot["segment_id"] for shot in audit_shots if shot["shot_id"] == item["shot_id"]
            ),
            "term": f"{item['shot_id']} {shots_by_id[item['shot_id']].get('title') or '关键镜头'}",
            "evidence_level": "已观察",
            "basis": str(item.get("finding") or ""),
        }
        for item in critical_items
    ]
    category_lines = []
    for key, label in CLASSIFICATION_LABELS.items():
        ids = [item["shot_id"] for item in audit_shots if item["classification"] == key]
        category_lines.append(f"{label} {len(ids)} 镜：{' '.join(ids)}")
    review["overall_note"] = "\n\n".join(
        [
            str((findings.get("overall_verdict") or {}).get("summary") or ""),
            *category_lines,
            f"下一轮：{next_test}",
        ]
    )

    contract_checks = prompt_contract.get("contract_checks") or []
    passed_checks = sum(bool(item.get("contract_present")) for item in contract_checks if isinstance(item, dict))
    categories = []
    for key, label in CLASSIFICATION_LABELS.items():
        ids = [item["shot_id"] for item in audit_shots if item["classification"] == key]
        categories.append({"key": key, "label": label, "tone": CLASSIFICATION_TONES[key], "count": len(ids), "shot_ids": ids})
    audit_bundle = {
        "schema_version": "1.0",
        "review_status": "human_review_completed",
        "review_date": str(findings.get("review_date") or ""),
        "verdict": {
            "status": "fail",
            "label": "不通过忠实模仿",
            "usable_as": "角色、雪山方向与十段大事件的概念验证",
            "summary": str((findings.get("overall_verdict") or {}).get("summary") or ""),
        },
        "metrics": {
            "reference_duration_sec": reference_duration,
            "candidate_duration_sec": remake_duration,
            "duration_delta_sec": float(
                (findings.get("quantitative_evidence") or {}).get("candidate_longer_by_sec") or 0
            ),
            "reference_aspect_ratio": float(
                (findings.get("quantitative_evidence") or {}).get("reference_aspect_ratio") or 0
            ),
            "candidate_aspect_ratio": float(
                (findings.get("quantitative_evidence") or {}).get("candidate_aspect_ratio") or 0
            ),
        },
        "prompt_contract": {
            "file_count": int(prompt_contract.get("prompt_file_count") or 0),
            "total_unicode_characters": int(prompt_contract.get("total_unicode_characters") or 0),
            "all_under_10000": bool(prompt_contract.get("all_files_under_10000_unicode_characters")),
            "checks_passed": passed_checks,
            "checks_total": len(contract_checks),
            "high_risk_segments": [
                item["segment_id"]
                for item in prompt_contract.get("segments") or []
                if item.get("execution_risk") == "high"
            ],
            "segments": [
                {
                    "segment_id": str(item.get("segment_id") or ""),
                    "title": str(item.get("title") or ""),
                    "shot_count": int(item.get("shot_count") or 0),
                    "duration_sec": float(item.get("duration_sec") or 0),
                    "unicode_characters": int(item.get("total_unicode_characters") or 0),
                    "execution_risk": str(item.get("execution_risk") or "unknown"),
                }
                for item in prompt_contract.get("segments") or []
                if isinstance(item, dict)
            ],
        },
        "classification_categories": categories,
        "shots": audit_shots,
        "critical_shots": critical_items,
        "golden_shots": golden_shots,
        "global_findings": [
            {
                **item,
                "axis_label": AXIS_LABELS.get(str(item.get("axis")), str(item.get("axis") or "")),
                "tone": severity_tone(item.get("severity")),
            }
            for item in findings.get("global_findings") or []
            if isinstance(item, dict)
        ],
        "limitations": findings.get("limitations") or [],
        "alignment": {
            "method": "manual_segment_anchors_with_piecewise_shot_locator",
            "anchors": anchors,
            "note": "十段端点为人工确认；非关键镜头的生成轴范围只是段内插值定位，关键镜头使用密集帧人工确认范围。",
        },
        "overview_image": f"comparisons/{comparison_id}/audit_evidence/overview.jpg",
        "attachments": {
            "report": f"comparisons/{comparison_id}/attachments/audit/human-comparison-report.txt",
            "findings": f"comparisons/{comparison_id}/attachments/audit/human-findings.yaml",
            "prompt_contract": f"comparisons/{comparison_id}/attachments/audit/prompt-contract.yaml",
        },
    }
    return event_segments, review, audit_bundle


def copy_audit_assets(
    *,
    audit_dir: Path,
    prompt_dir: Path,
    comparison_root: Path,
    audit_bundle: dict[str, Any],
) -> None:
    attachments = comparison_root / "attachments"
    audit_attachments = attachments / "audit"
    prompt_attachments = attachments / "prompts"
    event_evidence = comparison_root / "event_evidence"
    audit_evidence = comparison_root / "audit_evidence"
    for directory in (audit_attachments, prompt_attachments, event_evidence, audit_evidence):
        directory.mkdir(parents=True, exist_ok=True)
    for name in ("human-comparison-report.txt", "human-findings.yaml", "prompt-contract.yaml"):
        shutil.copy2(audit_dir / name, audit_attachments / name)
    shutil.copy2(audit_dir / "contact_sheets" / "overview.jpg", audit_evidence / "overview.jpg")
    for image in (audit_dir / "contact_sheets" / "units").glob("SEG*.jpg"):
        shutil.copy2(image, event_evidence / image.name)
    for shot in audit_bundle.get("shots") or []:
        if not shot.get("critical"):
            continue
        source = audit_dir / "contact_sheets" / "shots" / f"{shot['shot_id']}.jpg"
        if source.is_file():
            shutil.copy2(source, audit_evidence / source.name)
    for prompt in prompt_dir.glob("*.txt"):
        shutil.copy2(prompt, prompt_attachments / prompt.name)


def reconcile_picture_duration(
    store: ProjectStore,
    project_id: str,
    comparison_id: str,
    comparison: dict[str, Any],
) -> dict[str, Any]:
    audit_bundle = comparison.get("audit_bundle")
    if not isinstance(audit_bundle, dict):
        return comparison
    metrics = audit_bundle.get("metrics")
    summary = comparison.get("summary")
    if not isinstance(metrics, dict) or not isinstance(summary, dict):
        return comparison
    container_duration = float(metrics.get("candidate_duration_sec") or 0)
    picture_duration = float(summary.get("remake_duration_sec") or 0)
    if not picture_duration or abs(container_duration - picture_duration) < 0.02:
        return comparison
    metrics["candidate_picture_duration_sec"] = picture_duration
    limitations = audit_bundle.setdefault("limitations", [])
    note = (
        f"网站机械辅轨按画面流 {picture_duration:.3f} 秒计算；审查包按容器含音频尾部 "
        f"{container_duration:.3f} 秒记录。两者相差 {container_duration - picture_duration:.3f} 秒，"
        "来自音频尾部，不代表镜头节奏冲突。"
    )
    if note not in limitations:
        limitations.append(note)
    comparison = store.update_comparison(
        project_id,
        comparison_id,
        audit_bundle=audit_bundle,
    )
    build_comparison_page(
        store.comparison_manifest_path(project_id, comparison_id),
        store.comparison_root(project_id, comparison_id) / "comparison.html",
    )
    return comparison


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects-root", type=Path, default=Path("outputs/projects"))
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--prompt-dir", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--title", default="双轴人工审查 · 生成片")
    parser.add_argument("--force-new", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    projects_root = args.projects_root.expanduser().resolve()
    candidate = args.candidate.expanduser().resolve()
    audit_dir = args.audit_dir.expanduser().resolve()
    if not candidate.is_file():
        raise SystemExit(f"candidate video not found: {candidate}")
    for name in ("human-findings.yaml", "comparison-manifest.yaml", "prompt-contract.yaml"):
        if not (audit_dir / name).is_file():
            raise SystemExit(f"audit artifact not found: {audit_dir / name}")
    audit_manifest = load_yaml(audit_dir / "comparison-manifest.yaml")
    config_path = (
        args.config.expanduser().resolve()
        if args.config
        else Path(str(audit_manifest.get("comparison_config") or "")).expanduser().resolve()
    )
    prompt_dir = (
        args.prompt_dir.expanduser().resolve()
        if args.prompt_dir
        else Path(str(audit_manifest.get("prompt_directory") or "")).expanduser().resolve()
    )
    if not config_path.is_file():
        raise SystemExit(f"comparison config not found: {config_path}")
    if not prompt_dir.is_dir():
        raise SystemExit(f"prompt directory not found: {prompt_dir}")

    store = ProjectStore(projects_root)
    project = store.read(args.project_id)
    candidate_digest = file_sha256(candidate)
    expected_candidate_digest = str((audit_manifest.get("candidate_axis") or {}).get("sha256") or "")
    if expected_candidate_digest and expected_candidate_digest != candidate_digest:
        raise SystemExit("candidate video does not match the audit manifest")
    expected_reference_digest = str((audit_manifest.get("reference_axis") or {}).get("sha256") or "")
    if expected_reference_digest and expected_reference_digest != project.get("source_fingerprint"):
        raise SystemExit("selected project reference does not match the audit manifest")
    existing = next(
        (
            item
            for item in store.list_comparisons(args.project_id)
            if item.get("source_fingerprint") == candidate_digest
        ),
        None,
    )
    if existing and not args.force_new:
        print(
            json.dumps(
                {
                    "status": "existing",
                    "project_id": args.project_id,
                    "comparison_id": existing["comparison_id"],
                    "page_relpath": existing.get("page_relpath"),
                },
                ensure_ascii=False,
            )
        )
        return 0

    comparison_id = new_comparison_id()
    comparison_root = store.comparison_root(args.project_id, comparison_id)
    source_dir = comparison_root / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_name = f"candidate{candidate.suffix.lower() or '.mp4'}"
    shutil.copy2(candidate, source_dir / source_name)
    event_segments, review, audit_bundle = build_import_payload(
        audit_dir=audit_dir,
        comparison_id=comparison_id,
        prompt_dir=prompt_dir,
        config_path=config_path,
    )
    copy_audit_assets(
        audit_dir=audit_dir,
        prompt_dir=prompt_dir,
        comparison_root=comparison_root,
        audit_bundle=audit_bundle,
    )
    created_at = now_iso()
    comparison = {
        "schema_version": "1.0",
        "comparison_id": comparison_id,
        "project_id": args.project_id,
        "title": args.title,
        "source_filename": candidate.name,
        "source_relpath": f"source/{source_name}",
        "source_size": candidate.stat().st_size,
        "source_fingerprint": candidate_digest,
        "status": "queued",
        "stage": "等待生成片分析",
        "progress": 8,
        "progress_phase": "queued",
        "progress_detail": f"已关联{len(event_segments['events'])}段人工锚点、77镜分流、提示词合同与关键证据",
        "created_at": created_at,
        "updated_at": created_at,
        "error": None,
        "format_adaptation": {"status": "pending", "preserved_original": True},
        "event_segments": event_segments,
        "audit_bundle": audit_bundle,
        "production_inputs": {
            "prompt_count": audit_bundle["prompt_contract"]["file_count"],
            "prompt_total_chars": audit_bundle["prompt_contract"]["total_unicode_characters"],
            "audit_source": str(audit_dir),
        },
        "review": review,
    }
    store.write_comparison(args.project_id, comparison_id, comparison)
    print(json.dumps({"status": "processing", "project_id": args.project_id, "comparison_id": comparison_id}, ensure_ascii=False))
    process_comparison(store, args.project_id, comparison_id)
    final = store.read_comparison(args.project_id, comparison_id)
    if final.get("status") == "ready":
        final = reconcile_picture_duration(store, args.project_id, comparison_id, final)
    print(
        json.dumps(
            {
                "status": final.get("status"),
                "project_id": args.project_id,
                "comparison_id": comparison_id,
                "page_relpath": final.get("page_relpath"),
                "summary": final.get("summary"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if final.get("status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
