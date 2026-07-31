#!/usr/bin/env python3
"""Promote completed human event reviews into the authoritative comparison result."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from build_comparison_page import build as build_comparison_page


STATUS_LABELS = {
    "match": "复刻到位",
    "partial": "部分到位",
    "difference": "关键内容缺失",
    "uncertain": "无法确认",
}
STATUS_SEVERITY = {
    "match": "none",
    "partial": "medium",
    "difference": "high",
    "uncertain": "medium",
}
CLASSIFICATIONS = {
    "keep_with_minor_edit": ("可保留并小修", "keep"),
    "partial_or_targeted_repair": ("局部修复", "repair"),
    "rebuild_or_post_insert": ("重做或补插帧", "rebuild"),
}
ANALYSIS_FIELDS = (
    "status",
    "severity",
    "conclusion_level",
    "preserved",
    "observed_difference",
    "impact",
    "likely_root_cause",
    "next_action",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _clean_text(item))]


def _archive_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    existing = analysis.get("ai_draft")
    if isinstance(existing, dict) and existing:
        return deepcopy(existing)
    return {key: deepcopy(analysis.get(key)) for key in ANALYSIS_FIELDS if key in analysis}


def _feedback_texts(review: dict[str, Any], kinds: set[str]) -> list[str]:
    result: list[str] = []
    for entry in review.get("feedback_entries") or []:
        if not isinstance(entry, dict) or str(entry.get("kind")) not in kinds:
            continue
        text = _clean_text(entry.get("text"))
        if text and text not in result:
            result.append(text)
    return result


def _join_unique(parts: list[str]) -> str:
    result: list[str] = []
    for part in parts:
        text = _clean_text(part).rstrip("。；")
        if text and text not in result:
            result.append(text)
    return "；".join(result) + ("。" if result else "")


def _normalize_queue(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError("fix_queue must be a list")
    queue: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        problem = _clean_text(item.get("problem"))
        action = _clean_text(item.get("action"))
        if not problem or not action:
            raise ValueError("each fix queue item requires problem and action")
        severity = str(item.get("severity") or "medium")
        if severity not in {"high", "medium", "low"}:
            severity = "medium"
        queue.append(
            {
                "id": _clean_text(item.get("id")) or f"Q-HUMAN-{index:02d}",
                "pair_id": _clean_text(item.get("pair_id")),
                "event_id": _clean_text(item.get("event_id")),
                "severity": severity,
                "status": str(item.get("status") or "open"),
                "problem": problem,
                "action": action,
            }
        )
    return queue


def _default_queue(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for event in events:
        analysis = event.get("analysis") or {}
        if analysis.get("status") not in {"partial", "difference"}:
            continue
        queue.append(
            {
                "id": f"Q-HUMAN-{event['event_id']}",
                "pair_id": "",
                "event_id": event["event_id"],
                "severity": analysis.get("severity") or "medium",
                "status": "open",
                "problem": _clean_text(analysis.get("observed_difference")),
                "action": _clean_text(analysis.get("next_action")) or "按人工结论重做并逐项验收。",
            }
        )
    return queue


def _archive_audit(audit: dict[str, Any]) -> None:
    if isinstance(audit.get("pre_final_human_review"), dict):
        return
    shots = []
    for shot in audit.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        shots.append(
            {
                key: deepcopy(shot.get(key))
                for key in (
                    "shot_id",
                    "classification",
                    "classification_label",
                    "tone",
                    "critical",
                    "severity",
                    "finding",
                    "repair",
                )
            }
        )
    audit["pre_final_human_review"] = {
        "review_status": audit.get("review_status"),
        "verdict": deepcopy(audit.get("verdict") or {}),
        "global_findings": deepcopy(audit.get("global_findings") or []),
        "classification_categories": deepcopy(audit.get("classification_categories") or []),
        "shots": shots,
    }


def _apply_shot_decisions(
    audit: dict[str, Any],
    event_statuses: dict[str, str],
    overrides: dict[str, Any],
) -> None:
    shots = audit.get("shots") or []
    if not isinstance(shots, list):
        return
    shot_index = {
        str(shot.get("shot_id")): shot
        for shot in shots
        if isinstance(shot, dict) and shot.get("shot_id")
    }
    unknown = sorted(set(overrides) - set(shot_index))
    if unknown:
        raise ValueError(f"shot overrides reference unknown ids: {', '.join(unknown)}")

    for shot in shots:
        if not isinstance(shot, dict):
            continue
        if event_statuses.get(str(shot.get("segment_id"))) == "match":
            label, tone = CLASSIFICATIONS["keep_with_minor_edit"]
            shot.update(
                {
                    "classification": "keep_with_minor_edit",
                    "classification_label": label,
                    "tone": tone,
                    "critical": False,
                    "severity": "",
                    "finding": "",
                    "repair": "",
                    "evidence_sheet": "",
                }
            )

    for shot_id, changes in overrides.items():
        if not isinstance(changes, dict):
            continue
        shot = shot_index[shot_id]
        classification = str(changes.get("classification") or shot.get("classification") or "")
        if classification in CLASSIFICATIONS:
            label, tone = CLASSIFICATIONS[classification]
            shot["classification"] = classification
            shot["classification_label"] = label
            shot["tone"] = tone
        for key in ("critical", "severity", "finding", "repair", "evidence_sheet"):
            if key in changes:
                shot[key] = deepcopy(changes[key])

    categories: list[dict[str, Any]] = []
    for key, (label, tone) in CLASSIFICATIONS.items():
        ids = [
            str(shot.get("shot_id"))
            for shot in shots
            if isinstance(shot, dict) and shot.get("classification") == key
        ]
        categories.append(
            {"key": key, "label": label, "tone": tone, "count": len(ids), "shot_ids": ids}
        )
    audit["classification_categories"] = categories
    audit["critical_shots"] = [
        {
            "shot_id": shot.get("shot_id"),
            "severity": shot.get("severity") or "P1",
            "candidate_range_sec": (
                f"{shot.get('candidate_start_sec')}_to_{shot.get('candidate_end_sec')}"
                if shot.get("candidate_start_sec") is not None
                and shot.get("candidate_end_sec") is not None
                else ""
            ),
            "finding": shot.get("finding") or "",
            "repair": shot.get("repair") or "",
        }
        for shot in shots
        if isinstance(shot, dict) and shot.get("critical")
    ]


def finalize_human_review(
    comparison: dict[str, Any],
    corrections: dict[str, Any] | None = None,
    *,
    finalized_at: str | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Return a copy whose current conclusions and repair queue follow human review."""

    result = deepcopy(comparison)
    corrections = corrections or {}
    finalized_at = finalized_at or utc_now()
    events = result.get("event_segments", {}).get("events", [])
    reviews = result.get("review", {}).get("event_reviews", {})
    if not isinstance(events, list) or not events:
        raise ValueError("comparison has no event segments")
    if not isinstance(reviews, dict):
        raise ValueError("comparison review has no event reviews")
    event_overrides = corrections.get("events") or {}
    if not isinstance(event_overrides, dict):
        raise ValueError("corrections.events must be an object")
    event_ids = {str(event.get("event_id")) for event in events if isinstance(event, dict)}
    unknown_events = sorted(set(event_overrides) - event_ids)
    if unknown_events:
        raise ValueError(f"event corrections reference unknown ids: {', '.join(unknown_events)}")
    pending = [
        event_id
        for event_id in sorted(event_ids)
        if str((reviews.get(event_id) or {}).get("human_status") or "unreviewed") == "unreviewed"
    ]
    if pending and require_complete:
        raise ValueError(f"human review is incomplete: {', '.join(pending)}")

    event_statuses: dict[str, str] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id"))
        review = reviews.setdefault(event_id, {})
        status = str(review.get("human_status") or "unreviewed")
        if status == "unreviewed":
            continue
        if status not in STATUS_LABELS:
            raise ValueError(f"invalid human status for {event_id}: {status}")
        override = event_overrides.get(event_id) or {}
        if not isinstance(override, dict):
            raise ValueError(f"event correction must be an object: {event_id}")
        old_analysis = event.get("analysis") if isinstance(event.get("analysis"), dict) else {}
        ai_draft = _archive_analysis(old_analysis)
        summary = _clean_text(review.get("human_summary"))
        negative_feedback = _feedback_texts(review, {"missing", "incorrect"})
        confirmed_feedback = _feedback_texts(review, {"confirmed"})
        if status == "match":
            default_observation = summary or "人工复核确认本段复刻到位。"
        else:
            default_observation = _join_unique([summary, *negative_feedback])
            if not default_observation:
                default_observation = _clean_text(old_analysis.get("observed_difference"))
        severity = str(override.get("severity") or STATUS_SEVERITY[status])
        preserved = (
            _clean_list(override.get("preserved"))
            if "preserved" in override
            else confirmed_feedback
            or (_clean_list(old_analysis.get("preserved")) if status != "match" else ["人工复核确认本段整体复刻到位。"])
        )
        observed = _clean_text(override.get("observed_difference")) or default_observation
        impact = (
            _clean_text(override.get("impact"))
            if "impact" in override
            else ("" if status == "match" else _clean_text(old_analysis.get("impact")))
        )
        root_cause = (
            _clean_text(override.get("likely_root_cause"))
            if "likely_root_cause" in override
            else ("" if status == "match" else _clean_text(old_analysis.get("likely_root_cause")))
        )
        next_action = (
            _clean_text(override.get("next_action"))
            if "next_action" in override
            else ("保留当前版本。" if status == "match" else _clean_text(old_analysis.get("next_action")))
        )
        conclusion = _clean_text(override.get("conclusion")) or STATUS_LABELS[status]
        event["analysis"] = {
            "status": status,
            "severity": severity,
            "conclusion_level": "人工终审 · 已覆盖 AI 初判",
            "preserved": preserved,
            "observed_difference": observed,
            "impact": impact,
            "likely_root_cause": root_cause,
            "next_action": next_action,
            "ai_draft": ai_draft,
            "finalized_at": finalized_at,
        }
        review.update(
            {
                "status": status,
                "severity": severity,
                "conclusion": conclusion,
                "observed_difference": observed,
                "impact": impact,
                "root_cause": root_cause,
                "next_action": next_action,
                "finalized_at": finalized_at,
            }
        )
        for entry in review.get("feedback_entries") or []:
            if isinstance(entry, dict):
                entry["status"] = "applied"
        event_statuses[event_id] = status

    review_root = result.setdefault("review", {})
    review_root["fix_queue"] = (
        _normalize_queue(corrections["fix_queue"])
        if "fix_queue" in corrections
        else _default_queue(events)
    )
    if "overall_note" in corrections:
        review_root["overall_note"] = _clean_text(corrections.get("overall_note"))

    counts = Counter(event_statuses.values())
    result["human_review_summary"] = {
        "status": "finalized",
        "finalized_at": finalized_at,
        "total_events": len(event_statuses),
        "match": counts["match"],
        "partial": counts["partial"],
        "difference": counts["difference"],
        "uncertain": counts["uncertain"],
        "open_feedback": sum(
            1
            for review in reviews.values()
            if isinstance(review, dict)
            for entry in review.get("feedback_entries") or []
            if isinstance(entry, dict) and entry.get("status") != "applied"
        ),
    }
    result["human_review_finalized_at"] = finalized_at
    result["review_updated_at"] = finalized_at
    result["updated_at"] = finalized_at

    audit = result.get("audit_bundle")
    if isinstance(audit, dict):
        _archive_audit(audit)
        audit_changes = corrections.get("audit") or {}
        if not isinstance(audit_changes, dict):
            raise ValueError("corrections.audit must be an object")
        audit["review_status"] = "human_review_finalized"
        audit["review_date"] = finalized_at[:10]
        if "verdict" in audit_changes:
            audit["verdict"] = deepcopy(audit_changes["verdict"])
        if "global_findings" in audit_changes:
            audit["global_findings"] = deepcopy(audit_changes["global_findings"])
        if "limitations" in audit_changes:
            audit["limitations"] = _clean_list(audit_changes["limitations"])
        if "golden_shots" in audit_changes:
            audit["golden_shots"] = _clean_list(audit_changes["golden_shots"])
        _apply_shot_decisions(
            audit,
            event_statuses,
            audit_changes.get("shot_overrides") or {},
        )
        metrics = audit.setdefault("metrics", {})
        metrics["event_review_counts"] = dict(counts)

    return result


def final_report(comparison: dict[str, Any]) -> str:
    summary = comparison.get("human_review_summary") or {}
    events = comparison.get("event_segments", {}).get("events", [])
    queue = comparison.get("review", {}).get("fix_queue", [])
    lines = [
        "复刻对比人工终审",
        "",
        f"终审时间  {summary.get('finalized_at') or ''}",
        (
            f"段落统计  复刻到位 {summary.get('match', 0)}  "
            f"部分到位 {summary.get('partial', 0)}  "
            f"关键内容缺失 {summary.get('difference', 0)}  "
            f"无法确认 {summary.get('uncertain', 0)}"
        ),
        "",
        "逐段结论",
    ]
    for event in events:
        analysis = event.get("analysis") or {}
        lines.extend(
            [
                "",
                f"{event.get('event_id')}  {STATUS_LABELS.get(str(analysis.get('status')), str(analysis.get('status') or ''))}",
                _clean_text(analysis.get("observed_difference")),
                f"修改  {_clean_text(analysis.get('next_action'))}",
            ]
        )
    lines.extend(["", "修改清单"])
    for index, item in enumerate(queue, start=1):
        lines.extend(
            [
                "",
                f"{index}. {_clean_text(item.get('problem'))}",
                f"修改  {_clean_text(item.get('action'))}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _write_review_attachments(manifest_path: Path, comparison: dict[str, Any]) -> None:
    audit = comparison.get("audit_bundle")
    if not isinstance(audit, dict):
        return
    attachment_root = manifest_path.parent / "attachments" / "audit"
    attachment_root.mkdir(parents=True, exist_ok=True)
    report_path = attachment_root / "human-final-review.txt"
    data_path = attachment_root / "human-final-review.json"
    report_path.write_text(final_report(comparison), encoding="utf-8")
    write_json(
        data_path,
        {
            "human_review_summary": comparison.get("human_review_summary") or {},
            "events": [
                {
                    "event_id": event.get("event_id"),
                    "title": event.get("title"),
                    "analysis": event.get("analysis"),
                    "review": comparison.get("review", {}).get("event_reviews", {}).get(event.get("event_id"), {}),
                }
                for event in comparison.get("event_segments", {}).get("events", [])
            ],
            "fix_queue": comparison.get("review", {}).get("fix_queue", []),
        },
    )
    comparison_id = str(comparison.get("comparison_id") or manifest_path.parent.name)
    prefix = f"comparisons/{comparison_id}/attachments/audit"
    attachments = audit.setdefault("attachments", {})
    attachments["report"] = f"{prefix}/{report_path.name}"
    attachments["findings"] = f"{prefix}/{data_path.name}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="comparison.json to finalize")
    parser.add_argument("--corrections", type=Path, help="optional structured final-review corrections")
    parser.add_argument("--output", type=Path, help="write to another manifest instead of replacing the input")
    parser.add_argument("--allow-incomplete", action="store_true", help="finalize reviewed events only")
    parser.add_argument("--no-page", action="store_true", help="do not rebuild comparison.html")
    args = parser.parse_args()

    manifest = args.manifest.expanduser().resolve()
    output = (args.output or manifest).expanduser().resolve()
    comparison = read_json(manifest)
    corrections = read_json(args.corrections.expanduser().resolve()) if args.corrections else {}
    finalized = finalize_human_review(
        comparison,
        corrections,
        require_complete=not args.allow_incomplete,
    )
    _write_review_attachments(output, finalized)
    write_json(output, finalized)
    if not args.no_page:
        build_comparison_page(output, output.parent / "comparison.html")
    summary = finalized["human_review_summary"]
    print(
        f"finalized {summary['total_events']} events: "
        f"match={summary['match']} partial={summary['partial']} "
        f"difference={summary['difference']} uncertain={summary['uncertain']}"
    )


if __name__ == "__main__":
    main()
