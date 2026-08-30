from __future__ import annotations

import unittest

from starter.shopping_agent.catalog import CatalogRepository
from starter.agent import Agent
from starter.shopping_agent.config import AgentConfig
from starter.shopping_agent.catalog import RetrievedProduct
from starter.shopping_agent.policy import (
    CommitDecision,
    ClarificationPolicy,
    RankEvidence,
    RecommendationCommitPolicy,
)
from starter.shopping_agent.state import (
    CandidateStats,
    Constraint,
    ConstraintMutation,
    IntentUpdate,
    QueryEvidence,
    SessionState,
    StateReducer,
    parse_intent_update,
)
from starter.shopping_agent.structured_pool import build_structured_pool


def _repository() -> CatalogRepository:
    return CatalogRepository(
        records=[
            {
                "parent_asin": "A",
                "title": "Large Category popular standard",
                "categories": ["Root", "Large Category"],
                "features": ["standard"],
                "price": 25,
                "rating_number": 10000,
            },
            {
                "parent_asin": "B",
                "title": "Large Category low popularity cotton",
                "categories": ["Root", "Large Category"],
                "features": ["cotton"],
                "price": 30,
                "rating_number": 0,
            },
            {
                "parent_asin": "C",
                "title": "Large Category black cotton",
                "categories": ["Root", "Large Category"],
                "features": ["cotton", "black"],
                "price": 40,
                "rating_number": 1,
            },
        ]
    )


class ExhaustionStateTests(unittest.TestCase):
    def test_attribute_exhaustion_does_not_mean_global_exhaustion(self) -> None:
        update = parse_intent_update("I don't have a preference for material; use your judgment.")
        state = SessionState("s")
        StateReducer().apply(state, update, turn=1)
        self.assertEqual(state.attribute_exhausted, {"material"})
        self.assertFalse(state.global_exhausted)
        self.assertTrue(state.boundary_seen)

    def test_boundary_other_refusal_is_not_global_exhaustion(self) -> None:
        update = parse_intent_update("I don't have a preference for other; please use your judgment.")
        self.assertEqual(update.no_preference, frozenset({"other"}))
        self.assertFalse(update.global_exhausted)
        self.assertTrue(update.boundary_signal)
        state = SessionState("s")
        StateReducer().apply(state, update, turn=1)
        self.assertNotIn("other", state.attribute_exhausted)
        from starter.shopping_agent.policy import ClarificationPolicy
        self.assertEqual(ClarificationPolicy().choose_attribute(state, turn=2, remaining_turns=8), "other")

        exhausted = parse_intent_update("I don't have an additional preference for other.")
        self.assertTrue(exhausted.global_exhausted)

    def test_global_exhaustion_stops_all_questions_and_override_clears_it(self) -> None:
        reducer = StateReducer()
        state = SessionState("s")
        reducer.apply(state, parse_intent_update("I have no additional preferences."), turn=1)
        self.assertTrue(state.global_exhausted)
        reducer.apply(
            state,
            parse_intent_update("Actually, ignore my earlier preference. What I need is: black."),
            turn=2,
        )
        self.assertFalse(state.global_exhausted)
        self.assertEqual(state.intent_epoch, 1)

    def test_global_exhaustion_preserves_existing_query_evidence(self) -> None:
        reducer = StateReducer()
        state = SessionState("s")
        reducer.apply(
            state,
            parse_intent_update("Please look for ZYJLM.", turn=1),
            turn=1,
        )
        before_terms = list(state.active_query_terms)
        # Preference boilerplate is removed before lexical evidence enters
        # state; exhaustion must preserve the precise payload itself.
        self.assertEqual(before_terms, ["ZYJLM"])
        self.assertEqual(state.active_query_evidence[0].attribute_hint, "feature")

        exhausted = parse_intent_update(
            "I have no additional preferences to add.",
            turn=2,
            asked_attribute="other",
        )
        self.assertTrue(exhausted.global_exhausted)
        self.assertEqual(exhausted.no_preference, frozenset({"other"}))
        reducer.apply(state, exhausted, turn=2)

        self.assertTrue(state.global_exhausted)
        self.assertEqual(state.active_query_terms, before_terms)
        self.assertNotIn("feature", state.no_preference)
        self.assertNotIn("feature", state.attribute_exhausted)

    def test_other_is_asked_once_then_policy_chooses_highest_information_slot(self) -> None:
        from starter.shopping_agent.policy import ClarificationPolicy

        policy = ClarificationPolicy(mode="catalog_entropy")
        state = SessionState("s")
        self.assertEqual(policy.choose_attribute(state, turn=1, remaining_turns=9), "other")
        state.record_asked("other")
        stats = CandidateStats(
            attribute_entropy={
                "material": 0.4,
                "color": 1.7,
                "size": 1.4,
                "style": 1.3,
                "brand": 4.1,
                "budget": 0.0,
                "feature": 1.0,
                "use_case": 1.5,
            }
        )
        self.assertEqual(
            policy.choose_attribute(
                state,
                stats=stats,
                turn=2,
                remaining_turns=8,
            ),
            "brand",
        )
        state.no_progress_streak = 2
        self.assertNotEqual(policy.choose_attribute(state, turn=3, remaining_turns=7), "other")
        state.global_exhausted = True
        self.assertIsNone(policy.choose_attribute(state, turn=4, remaining_turns=6))

    def test_boundary_can_reopen_other_once_even_after_initial_broad_question(self) -> None:
        policy = ClarificationPolicy()
        state = SessionState("s")
        state.record_asked("other")
        state.no_preference.add("other")
        state.boundary_seen = True

        self.assertEqual(
            policy.choose_attribute(
                state,
                [{"title": f"candidate {index}"} for index in range(6)],
                turn=2,
                remaining_turns=8,
            ),
            "other",
        )

    def test_reopen_requires_rejection_and_explicit_clarification_request(self) -> None:
        reopen = parse_intent_update(
            "Those options are not quite right yet. Ask me about one specific attribute."
        )
        self.assertTrue(reopen.reopen_clarification)
        self.assertEqual(reopen.query_terms, ())

        plain_rejection = parse_intent_update("Those options are not quite right yet.")
        self.assertFalse(plain_rejection.reopen_clarification)
        ask_without_rejection = parse_intent_update("Ask me about one specific attribute.")
        self.assertFalse(ask_without_rejection.reopen_clarification)

    def test_reopen_event_preserves_global_exhaustion_and_keeps_constraints(self) -> None:
        reducer = StateReducer()
        state = SessionState("s", category_anchor="Large Category")
        reducer.apply(
            state,
            parse_intent_update("A key requirement is: cotton."),
            turn=1,
        )
        state.attribute_exhausted.update({"other", "material"})
        state.no_preference.update({"other", "material"})
        state.global_exhausted = True
        state.exhaustion_reason = "global_no_more_preferences"
        state.intent_epoch = 2
        state.recommendations_by_epoch[2] = ["SEEN"]
        before_constraints = [item.as_dict() for item in state.active_constraints]

        update = parse_intent_update(
            "Those options are not quite right yet. Ask me about one specific attribute."
        )
        reducer.apply(state, update, turn=3)

        self.assertTrue(state.global_exhausted)
        self.assertEqual(state.exhaustion_reason, "global_no_more_preferences")
        self.assertEqual(state.intent_epoch, 2)
        self.assertEqual([item.as_dict() for item in state.active_constraints], before_constraints)
        self.assertEqual(state.recommendations_by_epoch[2], ["SEEN"])
        self.assertEqual(state.attribute_exhausted, {"other", "material"})
        self.assertIn("other", state.no_preference)

    def test_plain_rejection_does_not_reopen_global_exhaustion(self) -> None:
        state = SessionState("s")
        state.global_exhausted = True
        state.exhaustion_reason = "global_no_more_preferences"
        StateReducer().apply(
            state,
            parse_intent_update("Those options are not quite right yet."),
            turn=2,
        )
        self.assertTrue(state.global_exhausted)
        self.assertEqual(state.exhaustion_reason, "global_no_more_preferences")

    def test_reopen_is_one_turn_policy_bypass_and_excludes_other_and_active_slots(self) -> None:
        policy = ClarificationPolicy()
        state = SessionState("s")
        state.global_exhausted = True
        state.exhaustion_reason = "global_no_more_preferences"
        state.attribute_exhausted.add("other")
        state.no_preference.add("other")
        state.record_asked("other")
        state.constraints.append(Constraint(attribute="material", value="cotton"))
        candidates = [
            {"title": "black cotton jacket", "features": ["black", "cotton"]},
            {"title": "red cotton jacket", "features": ["red", "cotton"]},
        ]
        self.assertIsNone(policy.choose_attribute(state, candidates, turn=3, remaining_turns=7))
        self.assertEqual(
            policy.choose_attribute(
                state,
                candidates,
                turn=3,
                remaining_turns=7,
                reopen_clarification=True,
            ),
            "color",
        )
        # The event does not mutate global exhaustion; without a new event the
        # next turn is blocked again.
        self.assertTrue(state.global_exhausted)
        self.assertIsNone(policy.choose_attribute(state, candidates, turn=4, remaining_turns=6))


class ProvenanceOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reducer = StateReducer()

    def test_ordinary_initial_message_does_not_predict_override(self) -> None:
        update = parse_intent_update("I'm looking for shirts. I prefer a relaxed fit.", turn=1)
        self.assertFalse(update.override)
        self.assertEqual(update.scope, "none")
        self.assertEqual(update.query_evidence[0].kind, "initial")

    def test_matching_override_upgrades_existing_constraint_and_evidence(self) -> None:
        state = SessionState("s", category_anchor="shoes")
        self.reducer.apply(
            state,
            parse_intent_update("For that, what matters is: leather.", turn=2),
            turn=2,
        )
        self.assertEqual(state.active_constraints[0].hardness, "soft")

        self.reducer.apply(
            state,
            parse_intent_update(
                "Actually, ignore my earlier preference. What I need is: leather.",
                turn=3,
            ),
            turn=3,
        )

        constraint = state.active_constraints[0]
        self.assertEqual(constraint.normalized_value(), "leather")
        self.assertEqual(constraint.hardness, "hard")
        self.assertEqual(constraint.polarity, "prefer")
        self.assertEqual(constraint.source, "rule")
        self.assertEqual(constraint.disclosure_kind, "override")
        self.assertEqual(constraint.epoch, 1)
        evidence = state.active_query_evidence
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].text, "leather")
        self.assertEqual(evidence[0].kind, "override")
        self.assertEqual(evidence[0].epoch, 1)
        self.assertEqual(evidence[0].turn, 3)

    def test_payload_free_global_reset_does_not_create_feature_evidence(self) -> None:
        state = SessionState("s", category_anchor="shoes")
        self.reducer.apply(
            state,
            parse_intent_update("For that, what matters is: leather.", turn=1),
            turn=1,
        )
        update = parse_intent_update("Start over.", turn=2)
        self.assertEqual(update.scope, "global_reset")
        self.assertEqual(update.mutations, ())
        self.assertEqual(update.query_terms, ())
        self.assertEqual(update.query_evidence, ())
        self.reducer.apply(state, update, turn=2)
        self.assertEqual(state.active_constraints, [])
        self.assertEqual(state.active_query_evidence, [])
        self.assertEqual(state.active_query_terms, [])
        self.assertEqual(state.query_terms, [])

        second = parse_intent_update("Forget everything.", turn=3)
        self.assertEqual(second.scope, "global_reset")
        self.assertEqual(second.mutations, ())
        self.assertEqual(second.query_evidence, ())

    def test_global_reset_clears_legacy_query_projection_without_evidence(self) -> None:
        state = SessionState(
            "legacy",
            category_anchor="shoes",
            query_terms=["legacy cotton"],
        )
        self.assertEqual(state.active_query_terms, ["legacy cotton"])
        update = parse_intent_update("Start over.", turn=1)
        self.reducer.apply(state, update, turn=1)
        self.assertEqual(state.query_terms, [])
        self.assertEqual(state.active_query_terms, [])

    def test_actually_change_attribute_precedes_referenced_override(self) -> None:
        state = SessionState("s", category_anchor="shirts")
        # Use explicit initial evidence to verify that an unrelated provisional
        # constraint/evidence survives the scoped color replacement.
        self.reducer.apply(
            state,
            IntentUpdate(
                mutations=(
                    ConstraintMutation(
                        action="upsert",
                        attribute="material",
                        value="cotton",
                        hardness="soft",
                        disclosure_kind="initial",
                    ),
                ),
                query_evidence=(
                    QueryEvidence(
                        text="cotton",
                        turn=1,
                        kind="initial",
                        attribute_hint="material",
                    ),
                ),
            ),
            turn=1,
        )
        self.reducer.apply(
            state,
            parse_intent_update("I'm looking for shirts. in red", turn=1),
            turn=1,
        )
        update = parse_intent_update("Actually, change the color to blue.", turn=2)
        self.assertEqual(update.scope, "attribute_replace")
        self.reducer.apply(state, update, turn=2)
        active = {(item.attribute, item.normalized_value()) for item in state.active_constraints}
        self.assertIn(("material", "cotton"), active)
        self.assertIn(("color", "blue"), active)
        self.assertNotIn(("color", "red"), active)
        self.assertIn("cotton", state.active_query_terms)
        self.assertNotIn("red", " ".join(state.active_query_terms).lower())

    def test_referenced_override_retires_provisional_and_carries_clarification(self) -> None:
        state = SessionState("s", category_anchor="shirts")
        self.reducer.apply(
            state,
            parse_intent_update("I'm looking for shirts. in red", turn=1),
            turn=1,
        )
        self.reducer.apply(
            state,
            parse_intent_update("For that, what matters is: cotton; durable.", turn=2),
            turn=2,
        )
        state.no_progress_streak = 3
        state.ask_counts["color"] = 2
        state.asked_attributes.append("color")
        state.recommendations_by_epoch[0] = ["SEEN"]
        state.softened_constraint_keys.add(("feature", "durable"))

        self.reducer.apply(
            state,
            parse_intent_update(
                "Actually, ignore my earlier preference. What I need is: leather.",
                turn=3,
            ),
            turn=3,
        )

        active = {(item.attribute, item.normalized_value()) for item in state.active_constraints}
        self.assertEqual(state.intent_epoch, 1)
        self.assertIn(("material", "leather"), active)
        self.assertNotIn(("color", "red"), active)
        self.assertIn(("feature", "durable"), active)
        self.assertEqual({item.epoch for item in state.active_constraints}, {1})
        self.assertEqual(
            [item.text for item in state.active_query_evidence],
            ["durable", "leather"],
        )
        self.assertEqual(state.query_terms, ["durable", "leather"])
        self.assertEqual(state.no_progress_streak, 0)
        self.assertEqual(state.ask_counts, {})
        self.assertEqual(state.recommendations_by_epoch[1], [])
        self.assertEqual(
            state.last_diagnostics["intent_scope"],
            "referenced_preference_replace",
        )
        self.assertEqual(state.last_diagnostics["query_evidence_carry_forward_count"], 1)

    def test_attribute_replacement_only_supersedes_same_attribute(self) -> None:
        state = SessionState("s", category_anchor="shirts")
        self.reducer.apply(
            state,
            parse_intent_update("For that, what matters is: color: red; cotton.", turn=1),
            turn=1,
        )
        self.reducer.apply(
            state,
            parse_intent_update("Change the color to blue instead of red.", turn=2),
            turn=2,
        )
        active = {(item.attribute, item.normalized_value()) for item in state.active_constraints}
        self.assertEqual(state.intent_epoch, 1)
        self.assertIn(("color", "blue"), active)
        self.assertIn(("material", "cotton"), active)
        self.assertNotIn(("color", "red"), active)
        self.assertEqual(
            [item.text for item in state.active_query_evidence],
            ["cotton", "blue"],
        )
        self.assertEqual(state.last_diagnostics["intent_scope"], "attribute_replace")

    def test_global_reset_is_explicit_and_clears_active_evidence(self) -> None:
        state = SessionState("s", category_anchor="shirts")
        self.reducer.apply(
            state,
            parse_intent_update("For that, what matters is: cotton; durable.", turn=1),
            turn=1,
        )
        state.no_progress_streak = 2
        state.recommendations_by_epoch[0] = ["SEEN"]
        self.reducer.apply(
            state,
            parse_intent_update("Forget everything. What I need is: wool.", turn=2),
            turn=2,
        )
        self.assertEqual(state.intent_epoch, 1)
        self.assertEqual(state.category_anchor, "shirts")
        self.assertEqual(
            [(item.attribute, item.normalized_value()) for item in state.active_constraints],
            [("material", "wool")],
        )
        self.assertEqual([item.text for item in state.active_query_evidence], ["wool"])
        self.assertFalse(any(item.status == "active" for item in state.query_evidence if item.text in {"cotton", "durable"}))
        self.assertEqual(state.no_progress_streak, 0)
        self.assertEqual(state.recommendations_by_epoch[1], [])
        self.assertEqual(state.last_diagnostics["intent_scope"], "global_reset")

    def test_superseded_query_evidence_is_not_in_projection_or_fingerprint(self) -> None:
        state = SessionState("s", category_anchor="shirts")
        self.reducer.apply(
            state,
            parse_intent_update("I'm looking for shirts. old preference cotton", turn=1),
            turn=1,
        )
        before = state.fingerprint()
        self.reducer.apply(
            state,
            parse_intent_update(
                "Actually, ignore my earlier preference. What I need is: leather.",
                turn=2,
            ),
            turn=2,
        )
        self.assertNotEqual(before, state.fingerprint())
        self.assertEqual(state.active_query_terms, ["leather"])
        self.assertNotIn("old preference", " ".join(state.active_query_terms).lower())
        self.assertTrue(any(item.status == "superseded" for item in state.query_evidence))


class StructuredPoolTests(unittest.TestCase):
    def test_numeric_and_title_feature_constraints_filter_complete_category(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "MATCH",
                    "title": "Mojo walking shoe",
                    "categories": ["Root", "Shoes"],
                    "average_rating": 4.8,
                    "rating_number": 300,
                },
                {
                    "parent_asin": "LOW-COUNT",
                    "title": "Mojo casual shoe",
                    "categories": ["Root", "Shoes"],
                    "average_rating": 4.8,
                    "rating_number": 20,
                },
                {
                    "parent_asin": "OTHER-TITLE",
                    "title": "Plain walking shoe",
                    "categories": ["Root", "Shoes"],
                    "average_rating": 4.8,
                    "rating_number": 300,
                },
            ]
        )
        state = SessionState("s", category_anchor="Shoes")
        StateReducer().apply(
            state,
            parse_intent_update(
                "A key requirement is: rating_count: 250 or more.\n"
                "A key requirement is: feature: title contains: mojo.",
                turn=1,
            ),
            turn=1,
        )

        result = build_structured_pool(repository, state)

        self.assertEqual(result.ids, ("MATCH",))
        self.assertEqual(len(result.applied_constraints), 2)

    def test_full_category_is_filtered_before_any_budget(self) -> None:
        repository = _repository()
        state = SessionState("s", category_anchor="Large Category")
        StateReducer().apply(
            state,
            parse_intent_update("A key requirement is: cotton."),
            turn=1,
        )
        result = build_structured_pool(repository, state)
        self.assertEqual(result.category_resolution.category_size, 3)
        self.assertEqual(set(result.ids), {"B", "C"})
        self.assertEqual(result.softened_constraints, ())

    def test_zero_result_rolls_back_and_records_soft_constraint(self) -> None:
        repository = _repository()
        state = SessionState("s", category_anchor="Large Category")
        StateReducer().apply(
            state,
            parse_intent_update("A key requirement is: unobtainium."),
            turn=1,
        )
        result = build_structured_pool(repository, state)
        self.assertEqual(set(result.ids), {"A", "B", "C"})
        self.assertEqual(len(result.softened_constraints), 1)
        self.assertEqual(result.softened_constraints[0].reason, "zero_result_rollback")
        state.softened_constraint_keys.update(result.softened_constraint_keys)
        repeated = build_structured_pool(repository, state)
        self.assertEqual(repeated.softened_constraints[0].reason, "previously_softened")

        # The persisted key also changes the ranking treatment from hard to
        # soft; this keeps a failed filter from receiving a hard-match boost.
        candidate = RetrievedProduct(repository.products["A"], 0.0, "structured:category")
        hard_state = SessionState("hard", category_anchor="Large Category")
        StateReducer().apply(
            hard_state,
            parse_intent_update("A key requirement is: standard."),
            turn=1,
        )
        agent = Agent(repository=repository)
        hard_score = agent._feature_rank(
            hard_state,
            [candidate],
            hard_state.runtime_context(turn=1, remaining_turns=9),
        )[0].score
        hard_state.softened_constraint_keys.add(("feature", "standard"))
        soft_score = agent._feature_rank(
            hard_state,
            [candidate],
            hard_state.runtime_context(turn=1, remaining_turns=9),
        )[0].score
        self.assertLess(soft_score, hard_score)

    def test_prefixed_color_label_does_not_become_a_required_token(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "BLACK",
                    "title": "Large Category Black jacket",
                    "categories": ["Root", "Large Category"],
                },
                {
                    "parent_asin": "RED",
                    "title": "Large Category Red jacket",
                    "categories": ["Root", "Large Category"],
                },
            ]
        )
        state = SessionState("s", category_anchor="Large Category")
        StateReducer().apply(
            state,
            parse_intent_update("For that, what matters is: color: black."),
            turn=1,
        )
        result = build_structured_pool(repository, state)
        self.assertEqual(result.ids, ("BLACK",))
        self.assertEqual(result.softened_constraints, ())

    def test_structured_pool_is_independent_of_adaptive_recall_switch(self) -> None:
        from starter.shopping_agent.config import AgentConfig

        rows = [
            {
                "parent_asin": f"L{index:04d}",
                "title": f"Large Category item {index}",
                "categories": ["Root", "Large Category"],
            }
            for index in range(151)
        ]
        repository = CatalogRepository(records=rows)
        agent = Agent(
            repository=repository,
            config=AgentConfig(adaptive_category_recall_enabled=False),
        )
        agent.reset("s", {})
        agent.respond("s", "I'm looking for Large Category.", 1, 10)
        diagnostics = agent.last_diagnostics
        self.assertEqual(diagnostics["structured_pool_size"], 151)
        self.assertEqual(diagnostics["candidate_count"], 151)
        self.assertEqual(diagnostics["effective_budget"], 151)
        self.assertEqual(diagnostics["feature_input_count"], 151)


class CommitPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = RecommendationCommitPolicy()
        self.state = SessionState("s")

    def test_supports_clarify_partial_and_full_modes(self) -> None:
        broad = self.policy.decide(RankEvidence(pool_size=40), self.state, turn=1, top_k=10)
        self.assertEqual((broad.mode, broad.recommendation_limit), ("clarify_only", 0))
        unstable_top1 = self.policy.decide(
            RankEvidence(pool_size=20, top1_margin=0.9), self.state, turn=2, top_k=10
        )
        self.assertEqual(unstable_top1.mode, "clarify_only")
        partial = self.policy.decide(
            RankEvidence(pool_size=20, top1_margin=0.9, top1_stability=1.0),
            self.state,
            turn=2,
            top_k=10,
        )
        self.assertEqual((partial.mode, partial.recommendation_limit), ("partial", 1))
        top3 = RecommendationCommitPolicy(partial_limit=3).decide(
            RankEvidence(pool_size=20, top3_stability=0.8), self.state, turn=2, top_k=10
        )
        self.assertEqual((top3.mode, top3.recommendation_limit), ("partial", 3))
        self.state.global_exhausted = True
        full = self.policy.decide(RankEvidence(pool_size=4), self.state, turn=3, top_k=10)
        self.assertEqual((full.mode, full.recommendation_limit), ("forced", 4))

    def test_late_turn_and_no_progress_force_submission(self) -> None:
        late = self.policy.decide(RankEvidence(pool_size=40), self.state, turn=9, top_k=10)
        self.assertEqual(late.mode, "forced")
        self.state.no_progress_streak = 2
        forced = self.policy.decide(RankEvidence(pool_size=40), self.state, turn=2, top_k=10)
        self.assertEqual(forced.reason_code, "no_progress")


class ProgressTrackingTests(unittest.TestCase):
    def test_new_intent_progress_resets_same_pool_no_progress_guard(self) -> None:
        state = SessionState("s", category_anchor="Large Category")
        pool = ("A", "B")
        state.update_progress(pool, pool, intent_fingerprint=state.fingerprint())
        state.no_progress_streak = 1

        StateReducer().apply(
            state,
            parse_intent_update("A key requirement is: cotton."),
            turn=2,
        )
        state.update_progress(pool, pool, intent_fingerprint=state.fingerprint())
        self.assertEqual(state.no_progress_streak, 0)

        decision = RecommendationCommitPolicy(no_progress_force_commit=1).decide(
            RankEvidence(pool_size=40), state, turn=2, top_k=10
        )
        self.assertNotEqual(decision.reason_code, "no_progress")
        self.assertNotEqual(decision.mode, "forced")

        # Once the intent is unchanged as well, the guard resumes counting.
        state.update_progress(pool, pool, intent_fingerprint=state.fingerprint())
        self.assertEqual(state.no_progress_streak, 1)


class RankingAblationTests(unittest.TestCase):
    def test_lexical_and_profile_signals_are_independently_switchable(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "RELEVANT",
                    "title": "Large Category waterproof hiking jacket",
                    "categories": ["Root", "Large Category"],
                    "rating_number": 0,
                },
                {
                    "parent_asin": "POPULAR",
                    "title": "Large Category everyday jacket",
                    "categories": ["Root", "Large Category"],
                    "rating_number": 100000,
                },
            ]
        )
        state = SessionState("s", category_anchor="Large Category")
        state.query_terms = ["waterproof hiking"]
        candidates = [
            RetrievedProduct(repository.products["RELEVANT"], 0.0, "structured:category"),
            RetrievedProduct(repository.products["POPULAR"], 0.0, "structured:category"),
        ]
        enabled = Agent(repository=repository, config=AgentConfig())
        ranked = enabled._feature_rank(
            state,
            candidates,
            state.runtime_context(turn=1, remaining_turns=9),
        )
        self.assertEqual(ranked[0].parent_asin, "RELEVANT")
        disabled = Agent(
            repository=repository,
            config=AgentConfig(
                ranking_bm25_enabled=False,
                ranking_title_coverage_enabled=False,
                ranking_popularity_enabled=True,
                ranking_rating_enabled=False,
                ranking_profile_enabled=False,
            ),
        )
        popularity_ranked = disabled._feature_rank(
            state,
            candidates,
            state.runtime_context(turn=1, remaining_turns=9),
        )
        self.assertEqual(popularity_ranked[0].parent_asin, "POPULAR")

    def test_rank_evidence_uses_fused_feature_scores(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "RELEVANT",
                    "title": "Large Category waterproof jacket",
                    "categories": ["Root", "Large Category"],
                },
                {
                    "parent_asin": "OTHER",
                    "title": "Large Category everyday jacket",
                    "categories": ["Root", "Large Category"],
                },
            ]
        )
        state = SessionState("s", category_anchor="Large Category")
        state.query_terms = ["waterproof"]
        candidates = [
            RetrievedProduct(repository.products["RELEVANT"], 0.0, "structured:category"),
            RetrievedProduct(repository.products["OTHER"], 0.0, "structured:category"),
        ]
        agent = Agent(
            repository=repository,
            config=AgentConfig(
                ranking_popularity_enabled=False,
                ranking_rating_enabled=False,
                ranking_profile_enabled=False,
            ),
        )
        ranked = agent._feature_rank(
            state,
            candidates,
            state.runtime_context(turn=1, remaining_turns=9),
        )
        self.assertGreater(ranked[0].score, ranked[1].score)
        evidence = agent._rank_evidence(ranked, (), state)
        self.assertGreater(evidence.top1_margin, 0.0)

    def test_protocol_constraint_label_is_not_rank_evidence(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "LABELED",
                    "title": "Large Category jacket",
                    "categories": ["Root", "Large Category"],
                    "features": ["Color: Black"],
                },
                {
                    "parent_asin": "PLAIN",
                    "title": "Large Category jacket",
                    "categories": ["Root", "Large Category"],
                    "features": ["Black"],
                },
            ]
        )
        state = SessionState("s", category_anchor="Large Category")
        state.constraints.append(
            Constraint(attribute="color", value="Color: Black", hardness="soft")
        )
        candidates = [
            RetrievedProduct(repository.products["LABELED"], 0.0, "structured:category"),
            RetrievedProduct(repository.products["PLAIN"], 0.0, "structured:category"),
        ]
        agent = Agent(
            repository=repository,
            config=AgentConfig(
                ranking_bm25_enabled=False,
                ranking_title_coverage_enabled=False,
                ranking_popularity_enabled=False,
                ranking_rating_enabled=False,
                ranking_profile_enabled=False,
            ),
        )
        ranked = agent._feature_rank(
            state,
            candidates,
            state.runtime_context(turn=1, remaining_turns=9),
        )
        self.assertEqual(
            {item.parent_asin for item in ranked},
            {"LABELED", "PLAIN"},
        )
        self.assertAlmostEqual(ranked[0].score, ranked[1].score)


class AgentIntegrationTests(unittest.TestCase):
    def test_valid_ids_respects_zero_and_positive_limits(self) -> None:
        agent = Agent(repository=_repository())
        values = ["A", "B", "C"]
        self.assertEqual(agent._valid_ids(values, 0), [])
        self.assertEqual(agent._valid_ids(values, 1), ["A"])
        self.assertEqual(agent._valid_ids(values, 3), ["A", "B", "C"])

    def test_zero_commit_limit_stays_empty_after_response_guard(self) -> None:
        class NoCommit:
            def decide(self, *_args: object, **_kwargs: object) -> CommitDecision:
                return CommitDecision("clarify_only", 0, "test_zero_limit")

        agent = Agent(repository=_repository(), commit_policy=NoCommit())
        agent.reset("s", {})
        response = agent.respond("s", "I'm looking for Large Category.", 1, 10)
        self.assertEqual(response["recommendations"], [])
        self.assertEqual(agent.last_diagnostics["recommendation_count"], 0)

    def test_rejection_with_explicit_question_reopens_specific_clarification(self) -> None:
        agent = Agent(repository=_repository())
        agent.reset("reopen", {})
        agent.respond(
            "reopen",
            "I'm looking for Large Category. A key requirement is: material: cotton.",
            1,
            10,
        )
        agent.respond("reopen", "I don't have an additional preference for other.", 2, 10)
        state = agent.store.require("reopen")
        self.assertTrue(state.global_exhausted)
        self.assertIn("other", state.attribute_exhausted)
        self.assertEqual(agent.last_diagnostics["commit"]["mode"], "forced")

        response = agent.respond(
            "reopen",
            "Those options are not quite right yet. Ask me about one specific attribute.",
            3,
            10,
        )
        state = agent.store.require("reopen")
        self.assertTrue(state.global_exhausted)
        self.assertEqual(response["ask_attribute"], "color")
        self.assertNotEqual(response["ask_attribute"], "other")
        self.assertNotEqual(response["ask_attribute"], "material")
        self.assertEqual(agent.last_diagnostics["commit"]["mode"], "forced")
        self.assertGreater(len(response["recommendations"]), 0)

        next_response = agent.respond("reopen", "No additional preferences.", 4, 10)
        self.assertIsNone(next_response["ask_attribute"])

    def test_response_can_include_ask_and_partial_recommendation(self) -> None:
        class OneCommit:
            def decide(self, *_args: object, **_kwargs: object) -> CommitDecision:
                return CommitDecision("partial", 1, "test_top1")

        agent = Agent(repository=_repository(), commit_policy=OneCommit())
        agent.reset("s", {})
        response = agent.respond("s", "I'm looking for Large Category.", 1, 10)
        self.assertEqual(response["ask_attribute"], "other")
        self.assertIn("Is there anything else that matters to you?", response["message"])
        self.assertEqual(len(response["recommendations"]), 1)

    def test_response_can_include_ask_and_top_three_for_small_pool(self) -> None:
        agent = Agent(repository=_repository())
        agent.reset("s", {})
        response = agent.respond("s", "I'm looking for Large Category.", 1, 10)
        self.assertEqual(response["ask_attribute"], "other")
        self.assertEqual(len(response["recommendations"]), 3)

    def test_pool_and_rank_history_are_isolated_between_sessions(self) -> None:
        repository = CatalogRepository(
            records=[
                {
                    "parent_asin": "A1",
                    "title": "Alpha Category item",
                    "categories": ["Root", "Alpha Category"],
                },
                {
                    "parent_asin": "A2",
                    "title": "Alpha Category item two",
                    "categories": ["Root", "Alpha Category"],
                },
                {
                    "parent_asin": "B1",
                    "title": "Beta Category item",
                    "categories": ["Root", "Beta Category"],
                },
                {
                    "parent_asin": "B2",
                    "title": "Beta Category item two",
                    "categories": ["Root", "Beta Category"],
                },
            ]
        )
        agent = Agent(repository=repository)
        agent.reset("a", {})
        agent.reset("b", {})
        agent.respond("a", "I'm looking for Alpha Category.", 1, 10)
        agent.respond("b", "I'm looking for Beta Category.", 1, 10)
        state_a = agent.store.require("a")
        state_b = agent.store.require("b")
        self.assertEqual(set(state_a.previous_structured_pool_ids), {"A1", "A2"})
        self.assertEqual(set(state_b.previous_structured_pool_ids), {"B1", "B2"})
        self.assertNotEqual(state_a.previous_ranked_ids, state_b.previous_ranked_ids)
        self.assertEqual(state_a.ask_counts, {"other": 1})
        self.assertEqual(state_b.ask_counts, {"other": 1})


if __name__ == "__main__":
    unittest.main()
