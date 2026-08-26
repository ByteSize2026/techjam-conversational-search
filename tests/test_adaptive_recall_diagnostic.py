from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.diagnose_adaptive_recall import run_diagnostic


def _write_fixture(root: Path) -> tuple[Path, Path]:
    catalog_path = root / "catalog.jsonl"
    public_set_path = root / "public_set.jsonl"

    rows: list[dict[str, object]] = []
    for index in range(501):
        rows.append(
            {
                "parent_asin": f"P{index:04d}",
                "title": f"Large Category item {index}",
                "features": ["catalog item"],
                "categories": ["Root", "Large Category"],
                "average_rating": 4.0,
                "rating_number": 1,
            }
        )
    catalog_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    sample = {
        "sample_id": "diagnostic-1",
        "scenario_type": "browsing",
        "user_profile": {"summary": "fixture profile"},
        "ground_truth": {"parent_asin": "P0250"},
    }
    public_set_path.write_text(json.dumps(sample) + "\n", encoding="utf-8")
    return catalog_path, public_set_path


class AdaptiveRecallDiagnosticTests(unittest.TestCase):
    def test_runs_both_modes_without_constructing_any_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_path, public_set_path = _write_fixture(Path(directory))
            with (
                patch(
                    "starter.agent.LLMSemanticRanker",
                    side_effect=AssertionError("diagnostic attempted to construct an LLM ranker"),
                ),
                patch(
                    "starter.agent.QwenCrossEncoderReranker",
                    side_effect=AssertionError("diagnostic attempted to construct Qwen"),
                ),
            ):
                report = run_diagnostic(catalog_path, public_set_path, sample_limit=1)

        self.assertEqual(report["sample_ids"], ["diagnostic-1"])
        self.assertEqual(report["model_guard"]["reported_token_usage"], 0)
        self.assertFalse(report["fixed"]["config"]["adaptive_category_recall_enabled"])
        self.assertTrue(report["adaptive"]["config"]["adaptive_category_recall_enabled"])

        for mode_name in ("fixed", "adaptive"):
            mode = report[mode_name]
            self.assertEqual(mode["sample_ids"], ["diagnostic-1"])
            self.assertEqual(mode["sample_count"], 1)
            self.assertEqual(mode["config"]["semantic_ranker"], "deterministic_passthrough")
            self.assertIsNone(mode["config"]["model"])
            self.assertEqual(
                mode["config"]["reported_token_usage"],
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

            recall = mode["recall"]
            for key in (
                "retrieval_recall_at_30",
                "retrieval_recall_at_100",
                "retrieval_recall_at_200",
                "feature_recall_at_30",
            ):
                self.assertIn(key, recall)
                self.assertGreaterEqual(recall[key], 0.0)
                self.assertLessEqual(recall[key], 1.0)

            evaluator = mode["evaluator"]
            self.assertIn("overall", evaluator)
            self.assertIn("scenario", evaluator)
            self.assertIn("technical_score", evaluator["overall"])
            self.assertIn("browsing", evaluator["scenario"])

            latency = mode["latency"]
            self.assertGreater(latency["turn_count"], 0)
            self.assertGreaterEqual(latency["p50_ms"], 0.0)
            self.assertGreaterEqual(latency["p95_ms"], latency["p50_ms"])

            traces = mode["target_free_trace"]
            self.assertEqual(len(traces), 1)
            self.assertTrue(traces[0]["turns"])
            self.assertTrue(
                any(turn["retrieval_candidate_ids"] for turn in traces[0]["turns"])
            )
            self.assertTrue(mode["posthoc"])
            self.assertEqual(mode["posthoc"][0]["sample_id"], "diagnostic-1")
            self.assertIn("feature_hit_at_30", mode["posthoc"][0])

            diagnostics = mode["target_free_diagnostics"]
            self.assertTrue(diagnostics)
            self.assertNotIn("ground_truth", json.dumps(mode, sort_keys=True))

        adaptive_diagnostics = report["adaptive"]["target_free_diagnostics"]
        self.assertEqual(adaptive_diagnostics[0]["category_route_budget"], 251)
        self.assertEqual(adaptive_diagnostics[0]["category_route_candidate_count"], 251)
        self.assertEqual(report["fixed"]["target_free_diagnostics"][0]["category_resolution"], "disabled")


if __name__ == "__main__":
    unittest.main()
