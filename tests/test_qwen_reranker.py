from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from starter.agent import Agent
from starter.shopping_agent import AgentConfig, QwenCrossEncoderReranker
from starter.shopping_agent.catalog import CatalogRepository
from starter.shopping_agent.policy import CandidateGateDecision


class _Candidate:
    def __init__(self, parent_asin: str, title: str = "product", score: float = 0.0) -> None:
        self.parent_asin = parent_asin
        self.title = title
        self.score = score

    def compressed(self) -> dict[str, str]:
        return {"parent_asin": self.parent_asin, "title": self.title}


class _FakeCrossEncoder:
    def __init__(self, scores: object) -> None:
        self.scores = scores
        self.calls: list[tuple[object, dict[str, object]]] = []

    def predict(self, pairs: object, **kwargs: object) -> object:
        self.calls.append((pairs, kwargs))
        return self.scores


class QwenRerankerTests(unittest.TestCase):
    def test_constructor_is_lazy_and_does_not_import_or_load(self) -> None:
        calls: list[object] = []

        def model_factory(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return _FakeCrossEncoder([1.0])

        ranker = QwenCrossEncoderReranker(
            model_path="/tmp/qwen",
            model_factory=model_factory,
        )
        self.assertEqual(calls, [])
        self.assertIsNone(ranker.model)

    def test_scores_order_candidates_and_keep_input_order_for_ties(self) -> None:
        fake = _FakeCrossEncoder([0.2, 0.9, 0.9])
        ranker = QwenCrossEncoderReranker(
            model_path="/tmp/qwen",
            device="cpu",
            candidate_limit=10,
            scorer=fake,
        )
        result = ranker.rank(
            {"category_anchor": "shoes"},
            [_Candidate("A"), _Candidate("B"), _Candidate("C")],
        )
        self.assertEqual(result.ordered_parent_asins, ("B", "C", "A"))
        self.assertEqual(result.scores, {"A": 0.0, "B": 1.0, "C": 1.0})
        self.assertEqual(result.backend, "qwen3-reranker-0.6b")
        self.assertIsNone(result.usage)
        self.assertIn("<Instruct>:", fake.calls[0][0][0][0])

    def test_fusion_weight_blends_minmax_scores_and_zero_preserves_feature_order(self) -> None:
        candidates = [
            _Candidate("A", score=0.0),
            _Candidate("B", score=0.25),
            _Candidate("C", score=1.0),
        ]
        qwen_scores = [0.2, 0.9, 0.4]

        feature_only = QwenCrossEncoderReranker(
            model_path="/tmp/qwen",
            device="cpu",
            scorer=_FakeCrossEncoder(qwen_scores),
            fusion_weight=0.0,
        ).rank("request", candidates)
        self.assertEqual(feature_only.ordered_parent_asins, ("A", "B", "C"))
        self.assertAlmostEqual(feature_only.scores["A"], 0.0)
        self.assertAlmostEqual(feature_only.scores["B"], 0.25)
        self.assertAlmostEqual(feature_only.scores["C"], 1.0)

        blended = QwenCrossEncoderReranker(
            model_path="/tmp/qwen",
            device="cpu",
            scorer=_FakeCrossEncoder(qwen_scores),
            fusion_weight=0.5,
        ).rank("request", candidates)
        self.assertEqual(blended.ordered_parent_asins, ("C", "B", "A"))
        self.assertAlmostEqual(blended.scores["A"], 0.0)
        self.assertAlmostEqual(blended.scores["B"], 0.625)
        self.assertAlmostEqual(blended.scores["C"], 0.6428571428571429)

        qwen_only = QwenCrossEncoderReranker(
            model_path="/tmp/qwen",
            device="cpu",
            scorer=_FakeCrossEncoder(qwen_scores),
            fusion_weight=1.0,
        ).rank("request", candidates)
        self.assertEqual(qwen_only.ordered_parent_asins, ("B", "C", "A"))
        self.assertAlmostEqual(qwen_only.scores["A"], 0.0)
        self.assertAlmostEqual(qwen_only.scores["B"], 1.0)
        self.assertAlmostEqual(qwen_only.scores["C"], 2.0 / 7.0)

    def test_factory_is_given_local_only_and_offline_environment(self) -> None:
        captured: dict[str, object] = {}

        def factory(path: str, **kwargs: object) -> object:
            captured["path"] = path
            captured["kwargs"] = kwargs
            captured["env"] = {
                name: __import__("os").environ.get(name)
                for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")
            }
            return _FakeCrossEncoder([0.5])

        result = QwenCrossEncoderReranker(
            model_path="/tmp/qwen",
            device="cpu",
            model_factory=factory,
        ).rank("request", [_Candidate("A")])
        self.assertEqual(result.backend, "qwen3-reranker-0.6b")
        self.assertEqual(captured["env"], {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
        })
        self.assertTrue(captured["kwargs"]["local_files_only"])
        self.assertTrue(captured["kwargs"]["trust_remote_code"])

    def test_soft_timeout_returns_feature_order_after_synchronous_predict(self) -> None:
        class SlowScorer(_FakeCrossEncoder):
            def predict(self, pairs: object, **kwargs: object) -> object:
                time.sleep(0.01)
                return super().predict(pairs, **kwargs)

        scorer = SlowScorer([0.1, 0.9])
        result = QwenCrossEncoderReranker(
            model_path="/tmp/qwen",
            device="cpu",
            timeout_seconds=0.001,
            scorer=scorer,
        ).rank("request", [_Candidate("A"), _Candidate("B")])
        self.assertEqual(result.ordered_parent_asins, ("A", "B"))
        self.assertIsNone(result.backend)
        self.assertIn("timeout", result.failures[-1].error.lower())
        self.assertEqual(len(scorer.calls), 1)

    def test_candidate_gate_over_general_skips_qwen(self) -> None:
        scorer = _FakeCrossEncoder([0.1])
        ranker = QwenCrossEncoderReranker(
            model_path="/tmp/qwen",
            device="cpu",
            scorer=scorer,
        )

        class OverGeneralGate:
            def evaluate(self, *_args: object, **_kwargs: object) -> CandidateGateDecision:
                return CandidateGateDecision(
                    mode="over_general",
                    over_general=True,
                    run_semantic_ranker=False,
                    retrieval_limit=60,
                    reason_code="test_over_general",
                )

        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "A",
                    "title": "blue walking shoe",
                    "categories": ["Shoes"],
                    "average_rating": 4.5,
                }
            ]
        )
        agent = Agent(
            repository=repository,
            config=AgentConfig(),
            semantic_ranker=ranker,
            candidate_gate=OverGeneralGate(),
        )
        agent.reset("over-general", {})
        response = agent.respond("over-general", "Find me something.", 1, 10)
        self.assertEqual(response["recommendations"][0]["parent_asin"], "A")
        self.assertEqual(scorer.calls, [])
        self.assertEqual(agent.last_diagnostics["gate"], "over_general")
        self.assertEqual(agent.last_diagnostics["semantic_input_count"], 0)

    def test_duplicate_and_missing_candidate_ids_are_repaired_by_whitelist(self) -> None:
        fake = _FakeCrossEncoder([0.1, 0.8])
        ranker = QwenCrossEncoderReranker(model_path="/tmp/qwen", scorer=fake)
        result = ranker.rank(
            "request",
            [_Candidate("A"), _Candidate("A", "duplicate"), object(), _Candidate("B")],
        )
        self.assertEqual(result.ordered_parent_asins, ("B", "A"))
        self.assertEqual(set(result.ordered_parent_asins), {"A", "B"})

    def test_malformed_scores_fall_back_and_are_diagnosable(self) -> None:
        for scores in ([0.2], [0.2, float("nan")], ["bad", 0.2], {"A": 1.0}):
            with self.subTest(scores=scores):
                ranker = QwenCrossEncoderReranker(
                    model_path="/tmp/qwen",
                    device="cpu",
                    scorer=_FakeCrossEncoder(scores),
                )
                result = ranker.rank("request", [_Candidate("A"), _Candidate("B")])
                self.assertEqual(result.ordered_parent_asins, ("A", "B"))
                self.assertIsNone(result.backend)
                self.assertTrue(result.failures)
                self.assertEqual(result.failures[-1].stage, "scores")

    def test_lazy_load_failure_falls_back_without_network(self) -> None:
        calls: list[object] = []

        def broken_factory(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            raise ImportError("sentence_transformers is unavailable")

        ranker = QwenCrossEncoderReranker(
            model_path="/tmp/qwen",
            model_factory=broken_factory,
        )
        result = ranker.rank("request", [_Candidate("A"), _Candidate("B")])
        self.assertEqual(result.ordered_parent_asins, ("A", "B"))
        self.assertIsNone(result.backend)
        self.assertTrue(any(failure.stage == "load" for failure in result.failures))
        # MPS may fall back to CPU, but each attempted load is still bounded.
        self.assertLessEqual(len(calls), 2)

    def test_agent_only_selects_qwen_for_explicit_absolute_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AgentConfig(qwen_reranker_model_path=str(Path(directory).resolve()))
            repository = CatalogRepository(records=[{"parent_asin": "A", "title": "shoe", "categories": ["Shoes"]}])
            with patch("starter.agent.QwenCrossEncoderReranker") as qwen:
                agent = Agent(repository=repository, config=config)
            qwen.assert_called_once_with(config=config)
            self.assertEqual(agent.semantic_ranker, qwen.return_value)

        with patch("starter.agent.QwenCrossEncoderReranker") as qwen:
            agent = Agent(repository=CatalogRepository(records=[]), config=AgentConfig())
        qwen.assert_not_called()
        self.assertNotIsInstance(agent.semantic_ranker, QwenCrossEncoderReranker)


if __name__ == "__main__":
    unittest.main()
