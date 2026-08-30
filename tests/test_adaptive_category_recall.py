from __future__ import annotations

from collections.abc import Iterable
import unittest

from starter.agent import Agent
from starter.shopping_agent.catalog import CatalogRepository
from starter.shopping_agent.config import AgentConfig
from starter.shopping_agent.policy import (
    CandidateGateDecision,
    adaptive_category_budget,
)
from starter.shopping_agent.semantic_ranking import SemanticRankingResult


def _records(
    category: str,
    count: int,
    *,
    title_prefix: str = "catalog item",
    parent_prefix: str = "P",
) -> list[dict[str, object]]:
    return [
        {
            "parent_asin": f"{parent_prefix}{index:04d}",
            "title": f"{title_prefix} {category} {index}",
            "categories": ["Root", category],
            "features": ["durable"],
            "average_rating": 4.0,
            "rating_number": index,
        }
        for index in range(count)
    ]


def _agent_records(*groups: Iterable[dict[str, object]]) -> CatalogRepository:
    rows: list[dict[str, object]] = []
    for group in groups:
        rows.extend(group)
    return CatalogRepository(records=rows)


class _FixedGate:
    def __init__(self, *, run_semantic_ranker: bool, retrieval_limit: int) -> None:
        self.run_semantic_ranker = run_semantic_ranker
        self.retrieval_limit = retrieval_limit

    def evaluate(self, *_args: object, **_kwargs: object) -> CandidateGateDecision:
        return CandidateGateDecision(
            mode="focused" if self.run_semantic_ranker else "over_general",
            over_general=not self.run_semantic_ranker,
            run_semantic_ranker=self.run_semantic_ranker,
            retrieval_limit=self.retrieval_limit,
            reason_code="test_gate",
        )


class _RecordingRanker:
    candidate_limit = 30

    def __init__(self) -> None:
        self.input_sizes: list[int] = []

    def rank(self, _context: object, candidates: list[object]) -> SemanticRankingResult:
        self.input_sizes.append(len(candidates))
        identifiers = [str(getattr(candidate, "parent_asin")) for candidate in candidates]
        return SemanticRankingResult(
            ordered_parent_asins=tuple(identifiers),
            scores={},
            backend=None,
            usage=None,
            failures=(),
        )


class AdaptiveCategoryRecallTests(unittest.TestCase):
    def test_budget_boundary_ratio_and_clamp(self) -> None:
        self.assertEqual(adaptive_category_budget(0), 0)
        self.assertEqual(adaptive_category_budget(1), 1)
        self.assertEqual(adaptive_category_budget(500), 500)
        self.assertEqual(adaptive_category_budget(501, 0), 251)
        self.assertEqual(adaptive_category_budget(501, 1), 176)
        self.assertEqual(adaptive_category_budget(501, 2), 101)
        self.assertEqual(adaptive_category_budget(2001, 0), 400)
        self.assertEqual(adaptive_category_budget(2001, 1), 400)
        self.assertEqual(adaptive_category_budget(2001, 2), 400)

    def test_category_index_resolves_combined_anchor_and_unions_shared_leaf(self) -> None:
        repository = _agent_records(
            [
                {
                    "parent_asin": "M",
                    "title": "men wallet",
                    "categories": [
                        "Root",
                        "Men",
                        "Accessories",
                        "Wallets, Card Cases & Money Organizers",
                        "Wallets",
                    ],
                },
                {
                    "parent_asin": "W",
                    "title": "women wallet",
                    "categories": [
                        "Root",
                        "Women",
                        "Accessories",
                        "Wallets, Card Cases & Money Organizers",
                        "Wallets",
                    ],
                },
                {
                    "parent_asin": "C",
                    "title": "card case",
                    "categories": ["Root", "Accessories", "Card Cases"],
                },
            ]
        )

        direct = repository.resolve_category("Wallets")
        self.assertEqual(direct.status, "resolved_union")
        self.assertEqual(set(direct.product_ids), {"M", "W"})

        combined = repository.resolve_category("Card Cases & Money Organizers Wallets")
        self.assertIn(combined.status, {"resolved", "resolved_union"})
        self.assertEqual(set(combined.product_ids), {"M", "W"})
        self.assertEqual(combined.category_size, 2)

    def test_coarse_anchor_index_matches_protocol_category_shapes(self) -> None:
        repository = _agent_records(
            [
                {
                    "parent_asin": "BOD1",
                    "title": "women bodysuit one",
                    "categories": [
                        "Clothing, Shoes & Jewelry",
                        "Women",
                        "Clothing",
                        "Bodysuits",
                    ],
                },
                {
                    "parent_asin": "BOD2",
                    "title": "women bodysuit two",
                    "categories": ["Other Root", "Women", "Bodysuits"],
                },
                {
                    "parent_asin": "SET",
                    "title": "sleep set",
                    "categories": [
                        "Clothing, Shoes & Jewelry",
                        "Lingerie, Sleep & Lounge",
                        "Sleep & Lounge",
                        "Sets",
                    ],
                },
                {
                    "parent_asin": "TSH",
                    "title": "tee shirt",
                    "categories": [
                        "Clothing, Shoes & Jewelry",
                        "Tops, Tees & Blouses",
                        "T-Shirts",
                    ],
                },
                {
                    "parent_asin": "TOP",
                    "title": "tops",
                    "categories": [
                        "Clothing, Shoes & Jewelry",
                        "Tops, Tees & Blouses",
                    ],
                },
            ]
        )

        women = repository.resolve_category("Women Bodysuits")
        self.assertEqual(women.status, "resolved_union")
        self.assertEqual(set(women.product_ids), {"BOD1", "BOD2"})

        sleep = repository.resolve_category("Sleep & Lounge Sets")
        self.assertEqual(sleep.status, "resolved_union")
        self.assertEqual(set(sleep.product_ids), {"SET"})

        tees = repository.resolve_category("Tees & Blouses T-Shirts")
        self.assertEqual(tees.status, "resolved_union")
        self.assertEqual(set(tees.product_ids), {"TSH"})

        tops = repository.resolve_category("Tops Tees & Blouses")
        self.assertEqual(tops.status, "resolved_union")
        self.assertEqual(set(tops.product_ids), {"TOP"})

    def test_unknown_and_incomparable_category_anchors_are_bounded(self) -> None:
        repository = _agent_records(
            [
                {"parent_asin": "A", "title": "blue shoes", "categories": ["Root", "Blue Shoes"]},
                {"parent_asin": "B", "title": "red shoes", "categories": ["Root", "Red Shoes"]},
            ]
        )
        self.assertEqual(repository.resolve_category("nonexistent category").status, "unknown")
        self.assertEqual(repository.resolve_category("blue red shoes").status, "ambiguous")

        agent = Agent(repository=repository, config=AgentConfig())
        agent.reset("unknown", {})
        agent.respond("unknown", "I'm looking for nonexistent category.", 1, 10)
        diagnostics = agent.last_diagnostics
        self.assertEqual(diagnostics["category_resolution"], "unknown")
        self.assertEqual(diagnostics["category_route_budget"], 0)
        self.assertLessEqual(
            diagnostics["cheap_candidate_pool_size"],
            diagnostics["cheap_candidate_pool_limit"],
        )

    def test_small_category_is_complete_and_large_category_uses_specificity_ratios(self) -> None:
        small = _records("Small Category", 3, parent_prefix="S")
        large = _records("Large Category", 501, parent_prefix="L")
        # The production parser admits structured values only when the local
        # catalog contains them.  Ground this fixture's later hard constraints
        # so the test measures category-budget specificity, not value rejection.
        for record in large:
            record["features"] = ["durable", "waterproof"]
            record["details"] = {"Color": "Black"}
        repository = _agent_records(small, large)

        agent = Agent(repository=repository, config=AgentConfig())
        agent.reset("small", {})
        agent.respond("small", "I'm looking for Small Category.", 1, 10)
        small_diagnostics = agent.last_diagnostics
        self.assertIn(small_diagnostics["category_resolution"], {"resolved", "resolved_union"})
        self.assertEqual(small_diagnostics["category_size"], 3)
        self.assertEqual(small_diagnostics["category_route_budget"], 3)
        self.assertEqual(small_diagnostics["category_route_candidate_count"], 3)

        agent.reset("large", {})
        agent.respond("large", "I'm looking for Large Category.", 1, 10)
        browsing_diagnostics = agent.last_diagnostics
        self.assertEqual(browsing_diagnostics["category_route_budget"], 251)

        agent.reset("large-hard", {})
        agent.respond("large-hard", "I'm looking for Large Category. A key requirement is: durable.", 1, 10)
        one_hard_diagnostics = agent.last_diagnostics
        self.assertEqual(one_hard_diagnostics["category_route_budget"], 176)

        agent.respond("large-hard", "A key requirement is: waterproof; black.", 2, 10)
        many_hard_diagnostics = agent.last_diagnostics
        self.assertEqual(many_hard_diagnostics["category_route_budget"], 101)

    def test_category_quota_survives_gate_cutoff_and_semantic_input_stays_top30(self) -> None:
        repository = _agent_records(_records("Large Category", 501, parent_prefix="L"))
        agent = Agent(
            repository=repository,
            config=AgentConfig(),
            candidate_gate=_FixedGate(run_semantic_ranker=False, retrieval_limit=10),
        )
        agent.reset("gate", {})
        agent.respond("gate", "I'm looking for Large Category.", 1, 10)
        diagnostics = agent.last_diagnostics
        self.assertEqual(diagnostics["gate"], "over_general")
        self.assertEqual(diagnostics["semantic_input_count"], 0)
        self.assertEqual(diagnostics["category_route_budget"], 251)
        self.assertEqual(diagnostics["category_route_candidate_count"], 251)
        self.assertEqual(diagnostics["category_route_preserved_count"], 251)
        self.assertEqual(diagnostics["feature_input_count"], diagnostics["cheap_candidate_pool_size"])
        self.assertGreaterEqual(diagnostics["feature_input_count"], 251)

        ranker = _RecordingRanker()
        semantic_agent = Agent(
            repository=repository,
            config=AgentConfig(),
            semantic_ranker=ranker,
            candidate_gate=_FixedGate(run_semantic_ranker=True, retrieval_limit=999),
        )
        semantic_agent.reset("semantic", {})
        semantic_agent.respond("semantic", "I'm looking for Large Category.", 1, 10)
        self.assertEqual(ranker.input_sizes, [30])
        self.assertEqual(semantic_agent.last_diagnostics["semantic_input_count"], 30)
        self.assertGreaterEqual(semantic_agent.last_diagnostics["feature_input_count"], 251)

    def test_hard_constraint_is_ranked_before_popularity_truncation(self) -> None:
        rows: list[dict[str, object]] = []
        for index in range(501):
            organic = index < 149 or index == 500
            rows.append(
                {
                    "parent_asin": f"L{index:04d}",
                    "title": f"Large Category item {index}",
                    "features": ["organic"] if organic else ["standard"],
                    "categories": ["Root", "Large Category"],
                    "average_rating": 1.0 if index == 500 else 5.0,
                    "rating_number": 0 if index == 500 else 10000,
                }
            )
        repository = _agent_records(rows)

        resolution, candidates = repository.category_with_scores(
            "Large Category",
            176,
            hard_constraints=["organic"],
        )
        self.assertEqual(resolution.category_size, 501)
        self.assertEqual(len(candidates), 176)
        candidate_ids = [item.parent_asin for item in candidates]
        self.assertIn("L0500", candidate_ids)
        self.assertEqual(candidate_ids.index("L0500"), 149)

    def test_query_evidence_precedes_popularity_and_popularity_breaks_only_ties(self) -> None:
        rows: list[dict[str, object]] = []
        for index in range(501):
            rows.append(
                {
                    "parent_asin": f"Q{index:04d}",
                    "title": "Large Category match" if index == 500 else "Large Category item",
                    "features": ["needle"] if index == 500 else ["standard"],
                    "categories": ["Root", "Large Category"],
                    "average_rating": 1.0 if index == 500 else 5.0,
                    "rating_number": 0 if index == 500 else 10000,
                }
            )
        repository = _agent_records(rows)
        _, candidates = repository.category_with_scores(
            "Large Category",
            176,
            query_evidence=["needle"],
        )
        self.assertEqual(candidates[0].parent_asin, "Q0500")

        tie_repository = _agent_records(
            [
                {
                    "parent_asin": "T1",
                    "title": "Small Category match",
                    "features": ["needle"],
                    "categories": ["Root", "Small Category"],
                    "average_rating": 4.0,
                    "rating_number": 10,
                },
                {
                    "parent_asin": "T2",
                    "title": "Small Category match",
                    "features": ["needle"],
                    "categories": ["Root", "Small Category"],
                    "average_rating": 4.0,
                    "rating_number": 100,
                },
            ]
        )
        _, tied = tie_repository.category_with_scores(
            "Small Category",
            2,
            query_evidence=["needle"],
        )
        self.assertEqual([item.parent_asin for item in tied], ["T2", "T1"])

    def test_category_quota_and_non_category_tail_survive_together(self) -> None:
        category_rows = _records("Large Category", 501, parent_prefix="L")
        lexical_only = {
            "parent_asin": "X0001",
            "title": "rare constraint product",
            "features": ["rare"],
            "categories": ["Root", "Other Category"],
            "average_rating": 5.0,
            "rating_number": 10000,
        }
        repository = _agent_records(category_rows, [lexical_only])
        agent = Agent(repository=repository, config=AgentConfig())
        agent.reset("tail", {})
        message = "I'm looking for Large Category. A key requirement is: rare."
        agent.respond("tail", message, 1, 10)

        state = agent.store.require("tail")
        route = agent.router.decide(state.runtime_context(turn=1, remaining_turns=9))
        candidates = agent._retrieve(state, message, route.retrieval_budget, route=route)
        resolution = repository.resolve_category("Large Category")
        category_ids = set(resolution.product_ids)
        candidate_ids = [item.parent_asin for item in candidates]
        self.assertTrue(all(parent_asin in category_ids for parent_asin in candidate_ids[:176]))
        self.assertIn("X0001", candidate_ids[176:])
        self.assertEqual(agent.last_diagnostics["category_route_preserved_count"], 176)
        self.assertGreater(agent.last_diagnostics["non_category_tail_count"], 0)
        self.assertEqual(agent.last_diagnostics["cheap_candidate_pool_limit"], 276)

    def test_same_category_lexical_hit_outside_quota_survives_in_tail(self) -> None:
        category_rows = _records("Large Category", 500, parent_prefix="L")
        for row in category_rows:
            row["features"] = ["rare"]
            row["rating_number"] = 10000
        target = {
            "parent_asin": "TARGET",
            "title": "rare Large Category target",
            "features": ["rare"],
            "categories": ["Root", "Large Category"],
            "average_rating": 1.0,
            "rating_number": 0,
        }
        repository = _agent_records(category_rows, [target])
        agent = Agent(repository=repository, config=AgentConfig())
        agent.reset("same-category-tail", {})
        message = "I'm looking for Large Category. A key requirement is: rare."
        agent.respond("same-category-tail", message, 1, 10)

        state = agent.store.require("same-category-tail")
        route = agent.router.decide(state.runtime_context(turn=1, remaining_turns=9))
        lexical_ids = [item.parent_asin for item in repository.search_with_scores(message, 120)]
        candidates = agent._retrieve(state, message, route.retrieval_budget, route=route)
        candidate_ids = [item.parent_asin for item in candidates]

        self.assertEqual(agent.last_diagnostics["category_route_budget"], 176)
        self.assertEqual(agent.last_diagnostics["category_route_preserved_count"], 176)
        self.assertEqual(lexical_ids[0], "TARGET")
        self.assertNotIn("TARGET", candidate_ids[:176])
        self.assertIn("TARGET", candidate_ids[176:])

    def test_feature_only_no_model_guard_reports_zero_usage(self) -> None:
        repository = CatalogRepository(records=_records("Small Category", 2, parent_prefix="S"))
        agent = Agent(repository=repository, config=AgentConfig())
        self.assertFalse(agent.config.qwen_reranker_enabled)
        self.assertEqual(agent._model_backends(), [])
        agent.reset("offline", {})
        response = agent.respond("offline", "I'm looking for Small Category.", 1, 10)
        self.assertNotIn("usage", response)
        self.assertIsNone(agent.last_diagnostics["model_backend"])
        self.assertEqual(agent.last_diagnostics["usage"], None)
        failures = agent.last_diagnostics["model_failures"]
        self.assertTrue(failures)
        self.assertEqual(failures[0]["stage"], "configuration")


if __name__ == "__main__":
    unittest.main()
