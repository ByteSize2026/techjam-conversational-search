"""Phase 4 smoke test for the deterministic Router/Value-Node bodies.

Not a scenario matrix (that is the Phase 8 scenario showcase, per
implement.md's explicit instruction): a single buying-style conversation run
end-to-end through ``run_graph`` with a real (tiny fixture) catalog wired in,
proving ``Search``/``CandidatePoolRouter``/``Rank``/``SlotCheckRouter``
actually cooperate -- not just that the stubbed graph shape terminates
(``tests/test_router_graph.py`` already proves that with every node still
returning a fixed placeholder).  LLM Value Nodes (``ClassifyIntent``,
``ExtractConstraints``, ``AskAttribute`` wording, ``SemanticRank``,
``Explain``) stay exactly as Phase 3 left them -- fixed dummy output, no
model wired -- per this phase's scope.
"""

from __future__ import annotations

import unittest

from starter.shopping_agent import graph
from starter.shopping_agent.catalog import CatalogRepository
from starter.shopping_agent.config import AgentConfig
from starter.shopping_agent.policy import ClarificationPolicy, IntentRouter
from starter.shopping_agent.recommendation import CatalogRecommendationEngine
from starter.shopping_agent.state import SessionState

_CATALOG_RECORDS = [
    {
        "parent_asin": "B00000001",
        "title": "Red Running Shoes",
        "categories": ["Clothing, Shoes & Jewelry", "Shoes", "Running"],
        "price": 45.0,
        "rating": 4.5,
        "rating_count": 120,
    },
    {
        "parent_asin": "B00000002",
        "title": "Blue Running Shoes",
        "categories": ["Clothing, Shoes & Jewelry", "Shoes", "Running"],
        "price": 55.0,
        "rating": 4.2,
        "rating_count": 80,
    },
    {
        "parent_asin": "B00000003",
        "title": "Black Casual Sneakers",
        "categories": ["Clothing, Shoes & Jewelry", "Shoes", "Casual"],
        "price": 35.0,
        "rating": 4.0,
        "rating_count": 50,
    },
]


class DeterministicNodesSmokeTest(unittest.TestCase):
    def test_buying_style_scenario_runs_entry_to_render(self) -> None:
        repository = CatalogRepository(catalog_path=None, records=_CATALOG_RECORDS)
        config = AgentConfig()
        engine = CatalogRecommendationEngine(repository, config, IntentRouter())
        services = graph.GraphServices(
            recommendation_engine=engine,
            clarification_policy=ClarificationPolicy(),
        )
        session = SessionState(session_id="smoke-buying")
        # ``ClarificationPolicy`` always asks at least one question
        # (``max_questions`` is clamped to a minimum of 1) -- pre-mark every
        # attribute as already asked so SlotCheckRouter's ``choose_attribute``
        # call finds nothing left to ask and this turn actually reaches
        # Search -> CandidatePoolRouter -> Rank instead of stopping at
        # AskAttribute on the very first turn of a fresh session.
        session.asked_attributes = list(ClarificationPolicy.ATTRIBUTE_ORDER)

        gs = graph.run_graph(
            session,
            turn=1,
            top_k=10,
            message="red running shoes",
            services=services,
        )

        self.assertIn("response", gs.scratch)
        response = gs.scratch["response"]
        self.assertIsInstance(response["message"], str)
        self.assertIsInstance(response["recommendations"], list)

        # Real Search/Rank actually ran (not the engine-less stub path).
        self.assertTrue(session.candidates)
        self.assertTrue(session.ranked)
        self.assertIsNotNone(session.last_candidate_stats)

        known_ids = {record["parent_asin"] for record in _CATALOG_RECORDS}
        for item in response["recommendations"]:
            self.assertIn(item["parent_asin"], known_ids)


if __name__ == "__main__":
    unittest.main()
