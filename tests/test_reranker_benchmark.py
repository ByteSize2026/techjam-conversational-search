from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.benchmark_qwen_reranker import (
    FROZEN_TRACE_SCHEMA_VERSION,
    FrozenTraceMismatch,
    _FrozenTraceAgent,
    _parser,
    build_manifest,
    compare_result_data,
    compare_result_files,
    load_frozen_adaptive_trace,
    manifest_sample_ids,
    measure_callable,
    _offline_environment,
    percentile,
    resolve_artifact_root,
    run_reranked,
    summarize_latencies,
    validate_asset_directory,
    validate_external_path,
    validate_model_path,
)
from starter.shopping_agent.semantic_ranking import SemanticRankingResult


class FakeReranker:
    """Dependency-free semantic-ranker stand-in for the runner contract."""

    candidate_limit = 30

    def rank(self, intent_summary: object, candidates: list[object]) -> SemanticRankingResult:
        del intent_summary
        identifiers = [str(getattr(candidate, "parent_asin")) for candidate in candidates]
        ordered = tuple(reversed(identifiers))
        return SemanticRankingResult(
            ordered_parent_asins=ordered,
            scores={identifier: float(index) for index, identifier in enumerate(identifiers)},
            backend="fake-qwen",
        )


class RerankerBenchmarkTest(unittest.TestCase):
    @staticmethod
    def _write_frozen_fixture(
        root: Path,
        *,
        turn_values: list[int] | None = None,
        retrieval_ids: list[str] | None = None,
        feature_ids: list[str] | None = None,
        catalog_reference: Path | None = None,
        public_reference: Path | None = None,
    ) -> tuple[Path, Path, Path, str]:
        catalog_path = root / "catalog.jsonl"
        public_path = root / "public_set.jsonl"
        frozen_path = root / "frozen.json"
        catalog_path.write_text("{}\n", encoding="utf-8")
        public_path.write_text("{}\n", encoding="utf-8")
        turns = turn_values or [1]
        retrieval = retrieval_ids or ["A", "B", "C"]
        features = feature_ids or ["A", "B", "C"]
        frozen = {
            "schema_version": FROZEN_TRACE_SCHEMA_VERSION,
            "catalog": str(catalog_reference or catalog_path),
            "public_set": str(public_reference or public_path),
            "sample_ids": ["s1"],
            "model_guard": {
                "mode": "feature_only",
                "network": "disabled",
                "reported_token_usage": 0,
            },
            "adaptive": {
                "config": {"adaptive_category_recall_enabled": True},
                "sample_ids": ["s1"],
                "sample_count": 1,
                "target_free_trace": [
                    {
                        "session_id": "trace-session",
                        "turns": [
                            {
                                "turn": turn,
                                "retrieval_candidate_ids": retrieval,
                                "feature_ranked_ids": features,
                            }
                            for turn in turns
                        ],
                    }
                ],
            },
        }
        frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
        digest = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
        return frozen_path, catalog_path, public_path, digest

    def _load_fixture_bundle(
        self,
        root: Path,
        **kwargs: object,
    ) -> tuple[dict[str, object], Path]:
        frozen_path, catalog_path, public_path, digest = self._write_frozen_fixture(root, **kwargs)
        bundle = load_frozen_adaptive_trace(
            frozen_path,
            catalog_path=catalog_path,
            public_set_path=public_path,
            public_sample_ids=["s1"],
            expected_sha256=digest,
        )
        return bundle, frozen_path

    def test_manifest_is_deterministic_stratified_and_target_free(self) -> None:
        samples = []
        for scenario in ("buying", "browsing"):
            for difficulty in ("easy", "hard"):
                for index in range(6):
                    samples.append(
                        {
                            "sample_id": f"{scenario}_{difficulty}_{index}",
                            "scenario_type": scenario,
                            "difficulty_bucket": difficulty,
                            "ground_truth": {"parent_asin": f"SECRET_TARGET_{index}"},
                            "user_profile": {"summary": "must not enter manifest"},
                        }
                    )

        first = build_manifest(samples, seed=17)
        second = build_manifest(list(reversed(samples)), seed=17)
        changed = build_manifest(samples, seed=18)
        self.assertEqual(first, second)
        self.assertNotEqual(first["split_ids"], changed["split_ids"])

        all_ids = []
        for split in ("dev", "validation", "locked"):
            entries = first["splits"][split]
            self.assertTrue(entries)
            for entry in entries:
                self.assertEqual(set(entry), {"sample_id", "scenario", "difficulty"})
                all_ids.append(entry["sample_id"])
        self.assertEqual(len(all_ids), len(set(all_ids)))
        self.assertEqual(set(all_ids), {sample["sample_id"] for sample in samples})
        encoded = json.dumps(first, sort_keys=True)
        self.assertNotIn("ground_truth", encoded)
        self.assertNotIn("parent_asin", encoded)
        self.assertNotIn("SECRET_TARGET", encoded)
        self.assertEqual(len(manifest_sample_ids(first, "locked")), 4)

        for split in ("dev", "validation", "locked"):
            counts = first["split_scenario_counts"][split]
            self.assertEqual(counts["buying"], counts["browsing"])

    def test_percentiles_and_metric_delta_are_reported(self) -> None:
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(percentile([1, 2, 3, 4], 95), 3.85)
        latency = summarize_latencies([1, 2, 3, 4], wall_time_ms=12)
        self.assertEqual(latency["p50_ms"], 2.5)
        self.assertEqual(latency["p95_ms"], 3.85)
        self.assertEqual(latency["max_ms"], 4.0)
        self.assertEqual(latency["wall_time_ms"], 12.0)

        baseline = {
            "sessions": [
                {"sample_id": "a", "scenario_type": "buying", "hit": True, "best_rank": 2, "first_hit_turn": 2},
                {"sample_id": "b", "scenario_type": "buying", "hit": False, "best_rank": None, "first_hit_turn": None},
                {"sample_id": "c", "scenario_type": "browsing", "hit": True, "best_rank": 1, "first_hit_turn": 1},
                {
                    "sample_id": "d",
                    "scenario_type": "browsing",
                    "hit": False,
                    "best_rank": None,
                    "first_hit_turn": None,
                },
            ]
        }
        candidate = {
            "sessions": [
                {"sample_id": "a", "scenario_type": "buying", "hit": True, "best_rank": 1, "first_hit_turn": 1},
                {"sample_id": "b", "scenario_type": "buying", "hit": True, "best_rank": 3, "first_hit_turn": 3},
                {"sample_id": "c", "scenario_type": "browsing", "hit": True, "best_rank": 1, "first_hit_turn": 1},
                {
                    "sample_id": "d",
                    "scenario_type": "browsing",
                    "hit": False,
                    "best_rank": None,
                    "first_hit_turn": None,
                },
            ]
        }
        report = compare_result_data(baseline, candidate)
        self.assertAlmostEqual(report["baseline"]["metrics"]["hit_rate_at_10"], 0.5)
        self.assertAlmostEqual(report["candidate"]["metrics"]["hit_rate_at_10"], 0.75)
        self.assertGreater(report["delta"]["mrr"], 0)
        self.assertLess(report["delta"]["mttc"], 0)
        self.assertGreater(report["delta"]["technical_score"], 0)
        self.assertIn("buying", report["scenario_metrics"])
        self.assertIn("browsing", report["scenario_metrics"])
        self.assertTrue(report["guardrails"]["passed"])

    def test_compare_files_supports_multiple_candidates_and_writes_report(self) -> None:
        baseline = {
            "sample_count": 2,
            "hit_rate_at_10": 0.5,
            "mrr": 0.25,
            "mttc": 6.0,
            "efficiency": 0.5,
            "technical_score": 0.4,
            "scenario_metrics": {},
        }
        candidate = {
            "sample_count": 2,
            "hit_rate_at_10": 0.5,
            "mrr": 0.5,
            "mttc": 5.0,
            "efficiency": 0.6,
            "technical_score": 0.55,
            "scenario_metrics": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_path = root / "baseline.json"
            candidate_a = root / "reranked-a.json"
            candidate_b = root / "reranked-b.json"
            output = root / "report.json"
            for path, value in (
                (baseline_path, baseline),
                (candidate_a, candidate),
                (candidate_b, candidate),
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
            report = compare_result_files(
                baseline_path,
                [candidate_a, candidate_b],
                output_path=output,
            )
            self.assertEqual(len(report["comparisons"]), 2)
            self.assertTrue(output.exists())
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(written["comparisons"]), 2)

    def test_resource_measurement_reports_latency_and_rss_fields(self) -> None:
        report = measure_callable(lambda: sum(range(10)), iterations=3)
        self.assertGreaterEqual(report["wall_time_ms"], 0)
        self.assertEqual(report["latency_ms"]["sample_count"], 3)
        self.assertIn("current_rss_bytes", report["process_memory"])
        self.assertIn("peak_rss_bytes", report["process_memory"])

    def test_offline_environment_sets_flags_without_changing_caches(self) -> None:
        names = (
            "HF_HOME",
            "PIP_CACHE_DIR",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "TORCH_HOME",
        )
        sentinels = {name: f"system-{name.lower()}" for name in names}
        with patch.dict(os.environ, sentinels):
            with _offline_environment():
                for name, value in sentinels.items():
                    self.assertEqual(os.environ[name], value)
                self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
                self.assertEqual(os.environ["TRANSFORMERS_OFFLINE"], "1")
                self.assertEqual(os.environ["HF_DATASETS_OFFLINE"], "1")
            for name, value in sentinels.items():
                self.assertEqual(os.environ.get(name), value)

    def test_frozen_trace_validates_hash_portable_sources_and_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, frozen_path = self._load_fixture_bundle(root)
            self.assertEqual(bundle["sample_ids"], ["s1"])
            self.assertEqual(bundle["sha256"], hashlib.sha256(frozen_path.read_bytes()).hexdigest())

            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                load_frozen_adaptive_trace(
                    frozen_path,
                    catalog_path=root / "catalog.jsonl",
                    public_set_path=root / "public_set.jsonl",
                    public_sample_ids=["s1"],
                    expected_sha256="0" * 64,
                )

            portable = load_frozen_adaptive_trace(
                frozen_path,
                catalog_path=Path("/another-machine/catalog.jsonl"),
                public_set_path=Path("/another-machine/public_set.jsonl"),
                public_sample_ids=["s1"],
                expected_sha256=bundle["sha256"],
            )
            self.assertEqual(portable["sample_ids"], ["s1"])

            with self.assertRaisesRegex(ValueError, "sample ID/order mismatch"):
                load_frozen_adaptive_trace(
                    frozen_path,
                    catalog_path=root / "catalog.jsonl",
                    public_set_path=root / "public_set.jsonl",
                    public_sample_ids=["other"],
                    expected_sha256=bundle["sha256"],
                )

    def test_frozen_trace_rejects_turn_and_whitelist_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen_path, catalog_path, public_path, digest = self._write_frozen_fixture(
                root,
                turn_values=[1, 1],
            )
            with self.assertRaisesRegex(ValueError, "invalid turn sequence"):
                load_frozen_adaptive_trace(
                    frozen_path,
                    catalog_path=catalog_path,
                    public_set_path=public_path,
                    public_sample_ids=["s1"],
                    expected_sha256=digest,
                )

            frozen_path, catalog_path, public_path, digest = self._write_frozen_fixture(
                root,
                retrieval_ids=["A", "B"],
                feature_ids=["A", "unknown"],
            )
            with self.assertRaisesRegex(ValueError, "candidate whitelist"):
                load_frozen_adaptive_trace(
                    frozen_path,
                    catalog_path=catalog_path,
                    public_set_path=public_path,
                    public_sample_ids=["s1"],
                    expected_sha256=digest,
                )

    def test_frozen_agent_fails_closed_on_live_feature_top30_mismatch(self) -> None:
        class Delegate:
            def reset(self, session_id: str, user_profile: dict) -> None:
                del session_id, user_profile

            def _feature_rank(
                self,
                state: object,
                candidates: list[object],
                context: object,
                *,
                limit: int | None = None,
            ) -> list[object]:
                del state, context, limit
                return list(candidates)

        with tempfile.TemporaryDirectory() as directory:
            bundle, _ = self._load_fixture_bundle(Path(directory))
            traces = bundle["traces"]
            self.assertIsInstance(traces, list)
            controller = _FrozenTraceAgent(Delegate(), ["s1"], traces)
            controller.reset("session", {})
            candidates = [
                type("Candidate", (), {"parent_asin": "B"})(),
                type("Candidate", (), {"parent_asin": "A"})(),
                type("Candidate", (), {"parent_asin": "C"})(),
            ]
            with self.assertRaises(FrozenTraceMismatch):
                controller._feature_rank(
                    None,
                    candidates,
                    type("Context", (), {"turn": 1})(),
                    limit=3,
                )
            with self.assertRaises(FrozenTraceMismatch):
                controller.assert_complete()

    def test_rerank_cli_exposes_frozen_trace_and_cuda(self) -> None:
        args = _parser().parse_args(
            [
                "rerank",
                "--catalog",
                "catalog.jsonl",
                "--public-set",
                "public_set.jsonl",
                "--model-path",
                "/content/model",
                "--device",
                "cuda",
                "--frozen-trace",
                "/content/trace.json",
            ]
        )
        self.assertEqual(args.device, "cuda")
        self.assertEqual(args.frozen_trace, "/content/trace.json")

    def test_local_path_validation_is_portable_and_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model"
            model_path.mkdir()
            self.assertEqual(validate_model_path(model_path), model_path.resolve())
            self.assertEqual(validate_asset_directory(model_path), model_path.resolve())
            self.assertEqual(validate_external_path(model_path), model_path.resolve())

            with self.assertRaisesRegex(ValueError, "absolute"):
                validate_model_path("relative-model")
            with self.assertRaisesRegex(ValueError, "does not exist"):
                validate_model_path(root / "missing")
            not_a_directory = root / "checkpoint.bin"
            not_a_directory.write_bytes(b"fixture")
            with self.assertRaisesRegex(ValueError, "directory"):
                validate_model_path(not_a_directory)

        # Colab's /content path is a valid explicit artifact location.
        self.assertEqual(resolve_artifact_root("/content/qwen-results"), Path("/content/qwen-results"))
        self.assertEqual(resolve_artifact_root("/"), Path("/"))

    def test_rerank_runner_uses_qwen_config_and_emits_metrics_without_model(self) -> None:
        catalog_rows = [
            {
                "parent_asin": "A",
                "title": "Blue running shoe",
                "features": ["cotton"],
                "details": {"department": "womens"},
                "description": ["walking shoe"],
                "categories": ["Clothing", "Shoes"],
                "store": "Example",
                "price": 49.0,
            },
            {
                "parent_asin": "B",
                "title": "Black winter boot",
                "features": ["leather"],
                "details": {"department": "womens"},
                "description": ["winter boot"],
                "categories": ["Clothing", "Boots"],
                "store": "Example",
                "price": 89.0,
            },
        ]
        samples = [
            {
                "sample_id": "s1",
                "scenario_type": "buying",
                "difficulty_bucket": "easy",
                "user_profile": {"summary": "x"},
                "ground_truth": {"parent_asin": "A"},
            },
            {
                "sample_id": "s2",
                "scenario_type": "buying",
                "difficulty_bucket": "easy",
                "user_profile": {"summary": "x"},
                "ground_truth": {"parent_asin": "B"},
            },
        ]
        manifest = build_manifest(samples, seed=9, dev_ratio=0, validation_ratio=1, locked_ratio=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.jsonl"
            public_path = root / "public_set.jsonl"
            manifest_path = root / "manifest.json"
            output_path = root / "reranked.json"
            catalog_path.write_text(
                "".join(json.dumps(row) + "\n" for row in catalog_rows),
                encoding="utf-8",
            )
            public_path.write_text(
                "".join(json.dumps(row) + "\n" for row in samples),
                encoding="utf-8",
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            model_path = root / "qwen-model"
            model_path.mkdir()

            result = run_reranked(
                catalog_path,
                public_path,
                output_path,
                model_path=model_path,
                revision="test-revision",
                device="cpu",
                batch_size=2,
                candidate_limit=2,
                timeout_seconds=3,
                fusion_weight=0.25,
                manifest_path=manifest_path,
                split="validation",
                sample_limit=1,
                semantic_ranker=FakeReranker(),
            )

            self.assertEqual(result["sample_count"], 1)
            self.assertTrue(output_path.exists())
            benchmark = result["benchmark"]
            self.assertEqual(benchmark["mode"], "qwen3_reranked")
            self.assertEqual(benchmark["network"], "disabled")
            self.assertEqual(benchmark["revision"], "test-revision")
            self.assertEqual(benchmark["device"], "cpu")
            self.assertEqual(benchmark["batch_size"], 2)
            self.assertEqual(benchmark["candidate_limit"], 2)
            self.assertEqual(benchmark["sample_ids"], ["s1"])
            self.assertIn("hit_rate_at_10", benchmark["evaluator_metrics"])
            self.assertIn("wall_time_ms", benchmark["resource"])
            self.assertIn("fallback_count", benchmark["resource"])
            self.assertTrue(benchmark["resource"]["external_asset_path_exists"])

    def test_rerank_defaults_to_validation_and_requires_manifest_for_named_split(self) -> None:
        parser = __import__("scripts.benchmark_qwen_reranker", fromlist=["_parser"])._parser()
        args = parser.parse_args(
            [
                "rerank",
                "--catalog",
                "catalog.jsonl",
                "--public-set",
                "public_set.jsonl",
                "--model-path",
                "/content/model",
            ]
        )
        self.assertEqual(args.split, "validation")
        self.assertEqual(args.candidate_limit, 30)

        with self.assertRaises(ValueError):
            from scripts.benchmark_qwen_reranker import _select_samples

            _select_samples(
                [{"sample_id": "s1", "scenario_type": "buying"}],
                manifest=None,
                split="validation",
                sample_limit=1,
            )


if __name__ == "__main__":
    unittest.main()
