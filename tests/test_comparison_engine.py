from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_PATH = SRC / "comparison_engine.py"


def load_module():
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("comparison_engine", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_shots(path: Path, durations: list[float]) -> None:
    fields = [
        "shot_id",
        "source_in",
        "source_out",
        "duration_sec",
        "start_frame_path",
        "middle_frame_path",
        "end_frame_path",
        "clip_path",
    ]
    cursor = 0.0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, duration in enumerate(durations, start=1):
            end = cursor + duration
            writer.writerow(
                {
                    "shot_id": f"SH{index:03d}",
                    "source_in": f"00:00:{cursor:06.3f}",
                    "source_out": f"00:00:{end:06.3f}",
                    "duration_sec": duration,
                    "start_frame_path": f"shots/SH{index:03d}/start.jpg",
                    "middle_frame_path": f"shots/SH{index:03d}/middle.jpg",
                    "end_frame_path": f"shots/SH{index:03d}/end.jpg",
                    "clip_path": f"clips/SH{index:03d}.mp4",
                }
            )
            cursor = end


class ComparisonEngineTests(unittest.TestCase):
    def test_signal_normalization_does_not_amplify_near_constant_noise(self) -> None:
        module = load_module()
        self.assertEqual(module._normalize([0.2, 0.2002, 0.1999, 0.2001]), [0.0, 0.0, 0.0, 0.0])

    def test_alignment_keeps_order_and_exposes_extra_shot(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference_path = root / "reference.csv"
            remake_path = root / "remake.csv"
            write_shots(reference_path, [1.0, 2.0, 1.0])
            write_shots(remake_path, [1.0, 0.4, 2.0, 1.0])
            operations = module.align_shots(
                module.load_shots(reference_path),
                module.load_shots(remake_path),
            )
        self.assertEqual(len(operations), 4)
        self.assertEqual(sum(1 for source, remake in operations if source is None and remake), 1)
        matched = [(source.shot_id, remake.shot_id) for source, remake in operations if source and remake]
        self.assertEqual([item[0] for item in matched], ["SH001", "SH002", "SH003"])

    def test_manual_alignment_rejects_duplicate_or_reordered_remake_shots(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference_path = root / "reference.csv"
            remake_path = root / "remake.csv"
            write_shots(reference_path, [1.0, 1.0])
            write_shots(remake_path, [1.0, 1.0])
            reference = module.load_shots(reference_path)
            remake = module.load_shots(remake_path)
            with self.assertRaises(ValueError):
                module.alignment_from_override(
                    reference,
                    remake,
                    [
                        {"source_shot_id": "SH001", "remake_shot_id": "SH002"},
                        {"source_shot_id": "SH002", "remake_shot_id": "SH001"},
                    ],
                )

    def test_build_analysis_separates_measured_findings_from_semantic_review(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference_path = root / "reference.csv"
            remake_path = root / "remake.csv"
            write_shots(reference_path, [1.0, 1.0])
            write_shots(remake_path, [1.5, 0.5])
            metadata = {"probe": {"duration_sec": 2.0, "fps": 24}}
            remake_metadata = {"probe": {"duration_sec": 2.0, "fps": 24}}
            reference_track = [
                {"time": 0.5, "motion": 0.9, "light_change": 0.1, "audio": 0.8, "composite": 0.9}
            ]
            remake_track = [
                {"time": 0.9, "motion": 0.8, "light_change": 0.1, "audio": 0.7, "composite": 0.8}
            ]
            result = module.build_comparison_analysis(
                reference_shots_path=reference_path,
                remake_shots_path=remake_path,
                reference_metadata=metadata,
                remake_metadata=remake_metadata,
                reference_track=reference_track,
                remake_track=remake_track,
                reference_video=None,
                remake_video=None,
                evidence_root=None,
                evidence_project_prefix="comparisons/cmp/evidence",
            )
        self.assertEqual(result["summary"]["matched_pair_count"], 2)
        self.assertTrue(result["findings"])
        self.assertTrue(all(item["conclusion_level"] == "工程数据验证" for item in result["findings"]))
        camera = next(item for item in result["dimensions"] if item["key"] == "camera")
        self.assertIn("不自动命名", camera["automatic_scope"])
        self.assertEqual(result["review"]["pair_reviews"], {})

    def test_review_payload_is_whitelisted_and_bounded(self) -> None:
        module = load_module()
        review = module.normalize_review_payload(
            {
                "event_reviews": {
                    "EV001": {
                        "status": "partial",
                        "human_status": "difference",
                        "human_summary": "风场因果没有成立",
                        "severity": "high",
                        "root_cause": "吸附事件没有连续成立",
                        "feedback_entries": [
                            {
                                "id": "FB1",
                                "kind": "missing",
                                "axis": "camera_geometry",
                                "stage_id": "EV001-peak",
                                "text": "AI 漏掉了低机位倾斜和广角透视拉伸",
                            },
                            {
                                "id": "FB2",
                                "kind": "incorrect",
                                "stage_id": "OTHER-peak",
                                "text": "这不是反制动作",
                                "status": "applied",
                            },
                        ],
                        "finalized_at": "2026-07-31T06:00:00+00:00",
                        "stage_reviews": {
                            "EV001-peak": {"status": "difference", "note": "接触点缺失"}
                        },
                    }
                },
                "pair_reviews": {
                    "PAIR001": {
                        "dimensions": {
                            "rhythm": {"status": "difference", "note": "慢了"},
                            "unknown": {"status": "match", "note": "drop"},
                        },
                        "node_reviews": {
                            "PAIR001-N01": {"event_label": "启动", "status": "match"},
                        },
                    }
                },
                "technical_identifications": [
                    {
                        "id": "T1",
                        "pair_id": "PAIR001",
                        "term": "低机位固定镜头",
                        "evidence_level": "已观察",
                        "basis": "机位空间位置稳定",
                    }
                ],
                "fix_queue": [{"id": "Q1", "pair_id": "PAIR001", "problem": "节拍慢", "action": "提前峰值"}],
            },
            pair_ids=["PAIR001"],
            event_ids=["EV001"],
        )
        self.assertEqual(review["event_reviews"]["EV001"]["status"], "partial")
        self.assertEqual(review["event_reviews"]["EV001"]["human_status"], "difference")
        self.assertEqual(review["event_reviews"]["EV001"]["human_summary"], "风场因果没有成立")
        feedback = review["event_reviews"]["EV001"]["feedback_entries"]
        self.assertEqual([item["kind"] for item in feedback], ["missing", "incorrect"])
        self.assertEqual(feedback[0]["axis"], "camera_geometry")
        self.assertEqual(feedback[1]["axis"], "other")
        self.assertEqual(feedback[0]["stage_id"], "EV001-peak")
        self.assertEqual(feedback[1]["stage_id"], "")
        self.assertEqual(feedback[1]["status"], "applied")
        self.assertEqual(review["event_reviews"]["EV001"]["finalized_at"], "2026-07-31T06:00:00+00:00")
        self.assertEqual(review["event_reviews"]["EV001"]["stage_reviews"]["EV001-peak"]["status"], "difference")
        self.assertEqual(review["pair_reviews"]["PAIR001"]["dimensions"]["rhythm"]["status"], "difference")
        self.assertNotIn("unknown", review["pair_reviews"]["PAIR001"]["dimensions"])
        self.assertEqual(review["technical_identifications"][0]["evidence_level"], "已观察")
        self.assertEqual(review["fix_queue"][0]["status"], "open")

    def test_event_segments_keep_causal_windows_and_reject_out_of_range_stages(self) -> None:
        module = load_module()
        payload = {
            "statement": "帧是证据，不是复刻目标。",
            "events": [
                {
                    "event_id": "EV001",
                    "title": "吸近后踹飞",
                    "source": {"start_sec": 10, "end_sec": 14},
                        "remake": {"start_sec": 11, "end_sec": 15.5},
                        "analysis": {
                            "status": "partial",
                            "observed_difference": "人工终审差异",
                            "finalized_at": "2026-07-31T06:00:00+00:00",
                            "ai_draft": {
                                "status": "difference",
                                "severity": "high",
                                "observed_difference": "旧 AI 草稿",
                            },
                        },
                    "invariants": ["先吸近，再踹离"],
                    "stages": [
                        {
                            "stage_id": "EV001-peak",
                            "key": "peak",
                            "label": "踹击接触",
                            "source": {"start_sec": 12.2, "end_sec": 12.8},
                            "remake": {"start_sec": 13.1, "end_sec": 13.8},
                        }
                    ],
                }
            ],
        }
        result = module.normalize_event_segments(
            payload,
            reference_duration=20,
            remake_duration=22,
        )
        analysis = result["events"][0]["analysis"]
        self.assertEqual(analysis["status"], "partial")
        self.assertEqual(analysis["ai_draft"]["status"], "difference")
        self.assertEqual(analysis["ai_draft"]["observed_difference"], "旧 AI 草稿")
        self.assertEqual(analysis["finalized_at"], "2026-07-31T06:00:00+00:00")
        event = result["events"][0]
        self.assertEqual(result["comparison_unit"], "causal_event_window")
        self.assertEqual(event["source"]["source_in"], "00:00:10.000")
        self.assertEqual(event["stages"][0]["key"], "peak")
        payload["events"][0]["stages"][0]["source"]["end_sec"] = 15
        with self.assertRaises(ValueError):
            module.normalize_event_segments(
                payload,
                reference_duration=20,
                remake_duration=22,
            )

    def test_review_follows_unchanged_pair_instead_of_pair_number(self) -> None:
        module = load_module()
        review, summary = module.remap_review_for_alignment(
            {
                "pair_reviews": {"PAIR002": {"dimensions": {"rhythm": {"status": "difference", "note": "慢"}}}},
                "technical_identifications": [{"id": "T1", "pair_id": "PAIR002", "term": "固定机位", "basis": "稳定", "evidence_level": "已观察"}],
                "fix_queue": [],
                "overall_note": "全片备注",
            },
            [
                {"pair_id": "PAIR001", "source": None, "remake": {"shot_id": "SH001"}},
                {"pair_id": "PAIR002", "source": {"shot_id": "SH001"}, "remake": {"shot_id": "SH002"}},
            ],
            [
                {"pair_id": "PAIR001", "source": {"shot_id": "SH001"}, "remake": {"shot_id": "SH002"}},
                {"pair_id": "PAIR002", "source": None, "remake": {"shot_id": "SH001"}},
            ],
        )
        self.assertIn("PAIR001", review["pair_reviews"])
        self.assertNotIn("PAIR002", review["pair_reviews"])
        self.assertEqual(review["technical_identifications"][0]["pair_id"], "PAIR001")
        self.assertEqual(summary["preserved_pair_reviews"], 1)


if __name__ == "__main__":
    unittest.main()
