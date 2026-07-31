from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_PATH = SRC / "finalize_human_review.py"


def load_module():
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("finalize_human_review", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FinalizeHumanReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def comparison(self) -> dict:
        return {
            "comparison_id": "cmp-test",
            "event_segments": {
                "events": [
                    {
                        "event_id": "SEG01",
                        "title": "开场",
                        "analysis": {
                            "status": "difference",
                            "severity": "high",
                            "observed_difference": "旧 AI 错判",
                            "next_action": "旧动作",
                        },
                    },
                    {
                        "event_id": "SEG02",
                        "title": "交手",
                        "analysis": {
                            "status": "partial",
                            "severity": "medium",
                            "observed_difference": "旧 AI 发现",
                            "next_action": "重做交手",
                        },
                    },
                ]
            },
            "review": {
                "event_reviews": {
                    "SEG01": {
                        "human_status": "match",
                        "human_summary": "人工确认通过",
                        "feedback_entries": [],
                    },
                    "SEG02": {
                        "human_status": "partial",
                        "human_summary": "接触缺失",
                        "feedback_entries": [
                            {
                                "kind": "missing",
                                "text": "石子没有溅起",
                                "status": "open",
                            }
                        ],
                    },
                },
                "fix_queue": [{"id": "OLD", "problem": "声音偏小", "action": "增大声音"}],
            },
            "audit_bundle": {
                "review_status": "human_review_completed",
                "verdict": {"label": "旧结论"},
                "global_findings": [{"id": "OLD"}],
                "shots": [
                    {
                        "shot_id": "SH001",
                        "segment_id": "SEG01",
                        "classification": "rebuild_or_post_insert",
                        "classification_label": "重做或补插帧",
                        "tone": "rebuild",
                        "critical": True,
                        "finding": "旧错判",
                        "repair": "旧动作",
                    },
                    {
                        "shot_id": "SH002",
                        "segment_id": "SEG02",
                        "classification": "partial_or_targeted_repair",
                        "classification_label": "局部修复",
                        "tone": "repair",
                        "critical": False,
                    },
                ],
            },
        }

    def test_human_status_overrides_ai_and_rebuilds_queue(self) -> None:
        result = self.module.finalize_human_review(
            self.comparison(),
            {
                "events": {
                    "SEG02": {
                        "observed_difference": "接触和石子反馈缺失。",
                        "next_action": "单独重做接触镜。",
                    }
                },
                "fix_queue": [
                    {
                        "id": "Q-HUMAN",
                        "event_id": "SEG02",
                        "severity": "high",
                        "problem": "接触缺失",
                        "action": "单独重做接触镜",
                    }
                ],
                "audit": {
                    "verdict": {"label": "人工终审"},
                    "global_findings": [],
                    "shot_overrides": {
                        "SH002": {
                            "critical": True,
                            "severity": "P1",
                            "finding": "石子缺失",
                            "repair": "补环境反馈",
                        }
                    },
                },
            },
            finalized_at="2026-07-31T06:00:00+00:00",
        )
        first = result["event_segments"]["events"][0]["analysis"]
        second = result["event_segments"]["events"][1]["analysis"]
        self.assertEqual(first["status"], "match")
        self.assertEqual(first["severity"], "none")
        self.assertEqual(first["ai_draft"]["observed_difference"], "旧 AI 错判")
        self.assertEqual(second["observed_difference"], "接触和石子反馈缺失。")
        self.assertEqual(result["review"]["event_reviews"]["SEG02"]["feedback_entries"][0]["status"], "applied")
        self.assertEqual([item["id"] for item in result["review"]["fix_queue"]], ["Q-HUMAN"])
        self.assertEqual(result["human_review_summary"]["match"], 1)
        self.assertEqual(result["human_review_summary"]["partial"], 1)
        self.assertEqual(result["human_review_summary"]["open_feedback"], 0)
        shots = {item["shot_id"]: item for item in result["audit_bundle"]["shots"]}
        self.assertEqual(shots["SH001"]["classification"], "keep_with_minor_edit")
        self.assertFalse(shots["SH001"]["critical"])
        self.assertTrue(shots["SH002"]["critical"])
        self.assertEqual(result["audit_bundle"]["verdict"]["label"], "人工终审")
        self.assertIn("pre_final_human_review", result["audit_bundle"])

    def test_complete_review_is_required(self) -> None:
        comparison = self.comparison()
        comparison["review"]["event_reviews"]["SEG02"]["human_status"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "SEG02"):
            self.module.finalize_human_review(comparison)


if __name__ == "__main__":
    unittest.main()
