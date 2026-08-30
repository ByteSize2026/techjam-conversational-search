from __future__ import annotations

import unittest

from starter.agent import Agent
from starter.shopping_agent.catalog import CatalogRepository, RetrievedProduct
from starter.shopping_agent.config import AgentConfig
from starter.shopping_agent.policy import ClarificationPolicy, RouteDecision
from starter.shopping_agent.retrieval import RetrievalEngine
from starter.shopping_agent.state import Constraint, SessionState
from starter.shopping_agent.structured_pool import build_structured_pool


class _RecordingRepository(CatalogRepository):
    def __init__(self) -> None:
        super().__init__(
            records=[
                {
                    "parent_asin": "A",
                    "title": "Explicit preference item",
                    "categories": ["Root", "Items"],
                },
                {
                    "parent_asin": "B",
                    "title": "Inferred preference item",
                    "categories": ["Root", "Items"],
                },
            ]
        )
        self.calls: list[tuple[str, str]] = []

    def search_with_scores(
        self,
        query: object,
        limit: int = 100,
        *,
        source: str = "keyword",
    ) -> list[RetrievedProduct]:
        query_text = str(query)
        self.calls.append((query_text, source))
        parent_asin = "A" if "explicit" in query_text else "B"
        return [RetrievedProduct(self.products[parent_asin], 1.0, source, 1)]


class SourceWeightedPreferenceTests(unittest.TestCase):
    def test_title_token_feature_scores_match_without_pipeline_type_error(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "MATCH",
                    "title": "Mojo walking clog",
                    "categories": ["Root", "Items"],
                },
                {
                    "parent_asin": "MISS",
                    "title": "Plain walking clog",
                    "categories": ["Root", "Items"],
                },
            ]
        )
        config = AgentConfig(
            ranking_bm25_enabled=False,
            ranking_title_coverage_enabled=False,
            ranking_popularity_enabled=False,
            ranking_rating_enabled=False,
            ranking_profile_enabled=False,
        )
        agent = Agent(repository=repository, config=config)
        state = SessionState("title-token")
        state.constraints.append(
            Constraint(
                attribute="feature",
                value="title contains: mojo",
                source="rule",
            )
        )

        ranked = agent._feature_rank(
            state,
            [
                RetrievedProduct(repository.products["MISS"]),
                RetrievedProduct(repository.products["MATCH"]),
            ],
            state.runtime_context(turn=1, remaining_turns=9),
        )

        self.assertEqual([item.parent_asin for item in ranked], ["MATCH", "MISS"])
        self.assertEqual([item.score for item in ranked], [1.6, 0.0])

    def test_canonical_rating_feature_uses_numeric_catalog_rating(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "HIGH",
                    "title": "Plain item",
                    "categories": ["Root", "Items"],
                    "average_rating": 5.0,
                },
                {
                    "parent_asin": "LOW",
                    "title": "Plain item",
                    "categories": ["Root", "Items"],
                    "average_rating": 4.0,
                },
            ]
        )
        config = AgentConfig(
            ranking_bm25_enabled=False,
            ranking_title_coverage_enabled=False,
            ranking_popularity_enabled=False,
            ranking_rating_enabled=False,
            ranking_profile_enabled=False,
        )
        agent = Agent(repository=repository, config=config)
        state = SessionState("rating")
        state.constraints.append(
            Constraint(
                attribute="feature",
                value="rating: 5.0 stars or higher",
                hardness="hard",
                source="rule",
            )
        )
        ranked = agent._feature_rank(
            state,
            [
                RetrievedProduct(repository.products["LOW"]),
                RetrievedProduct(repository.products["HIGH"]),
            ],
            state.runtime_context(turn=1, remaining_turns=9),
        )
        self.assertEqual([item.parent_asin for item in ranked], ["HIGH", "LOW"])
        self.assertEqual([item.score for item in ranked], [5.0, 0.0])

    def test_model_soft_preference_has_reduced_ranking_boost(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "MATCH",
                    "title": "Waterproof item",
                    "categories": ["Root", "Items"],
                }
            ]
        )
        config = AgentConfig(
            ranking_bm25_enabled=False,
            ranking_title_coverage_enabled=False,
            ranking_popularity_enabled=False,
            ranking_rating_enabled=False,
            ranking_profile_enabled=False,
        )
        agent = Agent(repository=repository, config=config)

        rule_state = SessionState("rule")
        rule_state.constraints.append(
            Constraint(attribute="feature", value="waterproof", source="rule")
        )
        rule_score = agent._feature_rank(
            rule_state,
            [RetrievedProduct(repository.products["MATCH"])],
            rule_state.runtime_context(turn=1, remaining_turns=9),
        )[0].score

        model_state = SessionState("model")
        model_state.constraints.append(
            Constraint(attribute="feature", value="waterproof", source="model")
        )
        model_score = agent._feature_rank(
            model_state,
            [RetrievedProduct(repository.products["MATCH"])],
            model_state.runtime_context(turn=1, remaining_turns=9),
        )[0].score

        self.assertAlmostEqual(rule_score, 1.6)
        self.assertAlmostEqual(model_score, 0.64)
        self.assertAlmostEqual(model_score, rule_score * 0.4)

    def test_browsing_preferences_use_separate_source_weighted_queries(self) -> None:
        repository = _RecordingRepository()
        engine = RetrievalEngine(repository, AgentConfig(retrieval_limit=40))
        state = SessionState("s")
        state.constraints.extend(
            [
                Constraint(attribute="feature", value="explicit", source="rule"),
                Constraint(attribute="feature", value="inferred", source="model"),
            ]
        )
        route = RouteDecision(
            mode="browsing",
            buying_weight=0.0,
            browsing_weight=1.0,
            retrieval_budget=40,
            reason_code="test",
        )

        candidates = engine.retrieve(state, "", 40, route=route)

        self.assertEqual(
            repository.calls,
            [
                ("explicit", "browsing:preferences"),
                ("inferred", "browsing:model_preferences"),
            ],
        )
        self.assertNotIn(("explicit inferred", "browsing:preferences"), repository.calls)
        by_id = {item.parent_asin: item for item in candidates}
        self.assertAlmostEqual(by_id["A"].score / by_id["B"].score, 0.75 / 0.30)
        self.assertEqual(
            engine.last_diagnostics["browsing_preference_query_weights"],
            {"non_model": 0.75, "model": 0.30},
        )

    def test_model_only_slot_remains_askable_and_unanswered(self) -> None:
        state = SessionState("s")
        state.constraints.append(
            Constraint(attribute="feature", value="waterproof", source="model")
        )
        state.attribute_exhausted.update(
            {"other", "material", "color", "size", "style", "brand", "budget"}
        )
        context = state.runtime_context(turn=2, remaining_turns=8)

        self.assertIn("feature", context.unanswered_attributes)
        self.assertEqual(
            ClarificationPolicy().choose_attribute(
                state,
                [repository_product(state, "Waterproof feature item")],
                turn=2,
                remaining_turns=8,
            ),
            "feature",
        )

    def test_model_preference_does_not_enter_structured_filter(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "MATCH",
                    "title": "Shoes waterproof",
                    "categories": ["Root", "Shoes"],
                },
                {
                    "parent_asin": "OTHER",
                    "title": "Shoes everyday",
                    "categories": ["Root", "Shoes"],
                },
            ]
        )
        state = SessionState("s", category_anchor="Shoes")
        state.constraints.append(
            Constraint(
                attribute="feature",
                value="waterproof",
                source="model",
                hardness="hard",
            )
        )

        result = build_structured_pool(repository, state)

        self.assertEqual(set(result.ids), {"MATCH", "OTHER"})
        self.assertEqual(result.applied_constraints, ())


def repository_product(_state: SessionState, title: str):
    """Return a tiny candidate fixture without coupling policy to a catalog."""

    return {
        "title": title,
        "feature": "waterproof",
    }


if __name__ == "__main__":
    unittest.main()
