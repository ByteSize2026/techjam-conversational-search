from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from starter.agent import Agent
from starter.shopping_agent.config import AgentConfig
from starter.shopping_agent.policy import CandidateGateDecision
from starter.shopping_agent.state import parse_intent_update


def _write_catalog(root: Path) -> Path:
    rows = [
        {
            "parent_asin": "A",
            "title": "Blue cotton running shirt",
            "features": ["comfortable", "durable"],
            "description": ["lightweight walking top"],
            "categories": ["Clothing", "Shirts"],
            "details": {"department": "womens", "size": "medium"},
            "store": "Alpha",
            "price": 29.0,
            "average_rating": 4.8,
            "rating_number": 100,
        },
        {
            "parent_asin": "B",
            "title": "Black leather winter boot",
            "features": ["warm", "waterproof"],
            "description": ["outdoor hiking boot"],
            "categories": ["Clothing", "Boots"],
            "details": {"department": "mens", "size": "large"},
            "store": "Beta",
            "price": 89.0,
            "average_rating": 4.5,
            "rating_number": 80,
        },
        {
            "parent_asin": "C",
            "title": "White polyester casual jacket",
            "features": ["hood", "pockets"],
            "description": ["comfortable outdoor layer"],
            "categories": ["Clothing", "Jackets"],
            "details": {"department": "unisex", "size": "small"},
            "store": "Gamma",
            "price": 59.0,
            "average_rating": 4.2,
            "rating_number": 60,
        },
    ]
    path = root / "catalog.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


class AgentContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.catalog_path = _write_catalog(Path(self.tempdir.name))
        self.profile = {
            "purchase_frequency": "often",
            "average_prior_rating": 4.5,
            "rating_style": "positive",
            "preference_tags": ["comfort"],
            "summary": "comfort-focused",
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_default_offline_contract_and_diagnostics(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "SHOPPING_AGENT_DEEPSEEK_API_KEY": "",
                "SHOPPING_AGENT_LOCAL_BASE_URL": "",
                "SHOPPING_AGENT_LOCAL_MODEL": "",
            },
            clear=False,
        ):
            agent = Agent(self.catalog_path)
        agent.reset("session-a", self.profile)
        response = agent.respond("session-a", "I'm looking for shirts.", 1, 10)
        self.assertIsInstance(response["message"], str)
        self.assertIn(response["ask_attribute"], {None, "category", "material", "color", "size", "style", "brand", "budget", "feature", "use_case", "other"})
        ids = [item["parent_asin"] for item in response["recommendations"]]
        self.assertEqual(ids, list(dict.fromkeys(ids)))
        self.assertTrue(set(ids) <= {"A", "B", "C"})
        self.assertNotIn("usage", response)
        self.assertEqual(agent.last_diagnostics["event"], "respond")

    def test_reset_replaces_state_and_sessions_are_isolated(self) -> None:
        agent = Agent(self.catalog_path)
        agent.reset("a", self.profile)
        agent.reset("b", self.profile)
        agent.respond("a", "I'm looking for shirts. A key requirement is: cotton.", 1, 10)
        agent.respond("b", "I'm looking for boots. A key requirement is: leather.", 1, 10)
        self.assertEqual(agent.store.require("a").intent_epoch, 0)
        self.assertEqual(agent.store.require("b").intent_epoch, 0)
        self.assertNotEqual(
            [item.value for item in agent.store.require("a").active_constraints],
            [item.value for item in agent.store.require("b").active_constraints],
        )
        agent.reset("a", self.profile)
        state = agent.store.require("a")
        self.assertEqual(state.intent_epoch, 0)
        self.assertEqual(state.active_constraints, [])
        self.assertEqual(state.recommendations_by_epoch, {0: []})

    def test_override_starts_epoch_and_preserves_category_anchor(self) -> None:
        agent = Agent(self.catalog_path)
        agent.reset("override", self.profile)
        agent.respond("override", "I'm looking for shoes. A key requirement is: cotton.", 1, 10)
        state = agent.store.require("override")
        self.assertEqual(state.category_anchor, "shoes")
        agent.respond(
            "override",
            "Actually, ignore my earlier preference. What I need is: leather.",
            2,
            10,
        )
        state = agent.store.require("override")
        self.assertEqual(state.intent_epoch, 1)
        self.assertEqual(state.category_anchor, "shoes")
        self.assertTrue(any(item.value.lower() == "leather" for item in state.active_constraints))
        self.assertFalse(any(item.value.lower() == "cotton" for item in state.active_constraints))
        self.assertEqual(state.recommendations_by_epoch.get(1), state.last_candidate_ids)

    def test_no_preference_attribute_is_not_asked_again(self) -> None:
        # Router/Value-Node architecture note (08-28-agent-v2-router-value-node,
        # design.md Section 6): unlike the former fixed pipeline (which called
        # ``ClarificationPolicy.choose_attribute`` unconditionally after every
        # rank), ``SlotCheckRouter`` only has *evidence* to reason about once a
        # prior turn's ``Search`` has actually populated ``session.candidates``
        # -- it deliberately does not ask a blind, query-free question on a
        # session's very first turn.  ``CandidatePoolRouter``'s post-Search
        # branch is a separate, narrower trigger (a coarse "pool is over-general"
        # cutoff), not a catch-all -- so a small, focused catalog's first turn
        # (as here) legitimately asks nothing yet.  This test's first message
        # therefore only seeds evidence; the second (generic follow-up) message
        # is what actually exercises ``SlotCheckRouter``'s evidence-based
        # question, matching the "convergence by turn 2-3" story design.md
        # Section 9's scenario showcase describes.
        agent = Agent(self.catalog_path)
        agent.reset("boundary", self.profile)
        agent.respond("boundary", "I'm looking for clothing, but I'm still exploring.", 1, 10)
        second = agent.respond("boundary", "Show me more options.", 2, 10)
        first_attribute = second["ask_attribute"]
        self.assertIsNotNone(first_attribute)
        third = agent.respond(
            "boundary",
            f"I don't have a preference for {first_attribute}; please use your judgment.",
            3,
            10,
        )
        state = agent.store.require("boundary")
        self.assertIn(first_attribute, state.no_preference)
        self.assertNotEqual(third["ask_attribute"], first_attribute)

    def test_additional_no_preference_sentence_is_not_positive_evidence(self) -> None:
        update = parse_intent_update("I don't have an additional preference for material.")
        self.assertEqual(update.no_preference, frozenset({"material"}))
        self.assertEqual(update.mutations, ())
        self.assertEqual(update.query_terms, ())

    def test_negated_looking_for_is_not_a_category_claim(self) -> None:
        """ARCHITECTURE_IMPROVEMENT_PLAN.md item 2a: ``_extract_category``
        (reused by both the total-LLM-failure fallback and
        ``extract_category_hint``'s narrower backstop) must not read a
        negated clause as the customer naming a category."""

        update = parse_intent_update("I'm not looking for anything cheap; quality matters more.")
        self.assertIsNone(update.category_anchor)

    def test_route_changes_retrieval_sources_and_weights(self) -> None:
        agent = Agent(self.catalog_path)

        agent.reset("buying", self.profile)
        agent.respond("buying", "I'm looking for shirts. A key requirement is: cotton.", 1, 10)
        buying = agent.last_diagnostics
        self.assertEqual(buying["route"], "buying")
        self.assertTrue(all(str(source).startswith("buying:") for source in buying["retrieval_sources"]))

        agent.reset("browsing", self.profile)
        agent.respond("browsing", "Find me something.", 1, 10)
        browsing = agent.last_diagnostics
        self.assertEqual(browsing["route"], "browsing")
        self.assertTrue(any(str(source).startswith("browsing:") for source in browsing["retrieval_sources"]))
        self.assertNotEqual(buying["retrieval_sources"], browsing["retrieval_sources"])

    def test_gate_budget_caps_feature_and_semantic_inputs(self) -> None:
        class RecordingRanker:
            def __init__(self) -> None:
                self.input_sizes: list[int] = []

            def rank(self, _context: object, candidates: list[object]) -> object:
                self.input_sizes.append(len(candidates))
                return SimpleNamespace(
                    ordered_parent_asins=[item.parent_asin for item in candidates],
                    backend=None,
                    usage=None,
                    failures=(),
                )

        class FixedGate:
            def evaluate(self, *_args: object, **_kwargs: object) -> CandidateGateDecision:
                return CandidateGateDecision(
                    mode="focused",
                    over_general=False,
                    run_semantic_ranker=True,
                    retrieval_limit=2,
                    reason_code="test_budget",
                )

        ranker = RecordingRanker()
        agent = Agent(
            self.catalog_path,
            config=AgentConfig(retrieval_limit=20, candidate_limit=20),
            semantic_ranker=ranker,
            candidate_gate=FixedGate(),
        )
        agent.reset("budget", self.profile)
        agent.respond("budget", "Find me something.", 1, 10)
        self.assertEqual(ranker.input_sizes, [2])
        self.assertEqual(agent.last_diagnostics["effective_budget"], 2)
        self.assertEqual(agent.last_diagnostics["feature_input_count"], 2)
        self.assertEqual(agent.last_diagnostics["semantic_input_count"], 2)

    def test_recommendations_are_valid_unique_and_bounded(self) -> None:
        agent = Agent(self.catalog_path)
        agent.reset("bounded", self.profile)
        response = agent.respond("bounded", "Find me something.", 1, 100)
        ids = [item["parent_asin"] for item in response["recommendations"]]
        self.assertLessEqual(len(ids), 10)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(ids) <= {"A", "B", "C"})

    def test_ask_attribute_pause_resume_and_intent_override_in_one_session(self) -> None:
        """implement.md Phase 7: one end-to-end contract test proving both
        cycles work together within a single multi-turn session, on top of
        the graph facade (``_respond_impl_graph``) rather than testing each
        cycle in isolation.  ``test_no_preference_attribute_is_not_asked_again``
        already covers the ask/resume cycle alone and
        ``test_override_starts_epoch_and_preserves_category_anchor`` already
        covers the override cycle alone; this test is additive, not a
        replacement for either.
        """

        agent = Agent(self.catalog_path)
        agent.reset("combo", self.profile)

        # Turn 1: seed evidence (category + one hard constraint) -- no
        # clarification question yet (SlotCheckRouter has no prior-turn
        # evidence on a session's first turn; see design.md Section 6).
        first = agent.respond("combo", "I'm looking for shoes. A key requirement is: cotton.", 1, 10)
        self.assertIsNone(first["ask_attribute"])
        state = agent.store.require("combo")
        self.assertEqual(state.category_anchor, "shoes")

        # Turn 2: a generic follow-up lets SlotCheckRouter ask a real,
        # evidence-based question -- the "pause" half of the ask/resume
        # cycle.  Recommendations are still populated alongside the
        # question (the graph's Rank-always-runs guarantee).
        second = agent.respond("combo", "Show me more options.", 2, 10)
        asked_attribute = second["ask_attribute"]
        self.assertIsNotNone(asked_attribute)
        self.assertTrue(second["recommendations"])
        self.assertIsNotNone(state.pending_question)
        self.assertEqual(state.pending_question.attribute, asked_attribute)

        # Turn 3: answering that exact question is the "resume" half --
        # pending_question clears (bound to a fresh one, if any) and the
        # attribute is recorded as no-preference so it is never re-asked.
        agent.respond(
            "combo",
            f"I don't have a preference for {asked_attribute}; please use your judgment.",
            3,
            10,
        )
        self.assertIn(asked_attribute, state.no_preference)

        # Turn 4: an explicit override wipes the earlier "cotton" constraint
        # and bumps intent_epoch, while the category anchor established on
        # turn 1 survives the override (design.md's override semantics,
        # unchanged from v1 -- StateReducer.apply is reused as-is).
        old_epoch = state.intent_epoch
        agent.respond(
            "combo",
            "Actually, ignore my earlier preference. What I need is: leather.",
            4,
            10,
        )
        self.assertEqual(state.intent_epoch, old_epoch + 1)
        self.assertEqual(state.category_anchor, "shoes")
        self.assertFalse(any(item.value.lower() == "cotton" for item in state.active_constraints))
        self.assertTrue(any(item.value.lower() == "leather" for item in state.active_constraints))


if __name__ == "__main__":
    unittest.main()
