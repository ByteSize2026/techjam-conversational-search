"""Phase 5 tests for the Router/Value-Node graph's LLM Value Nodes.

Per implement.md's explicit "reduce unnecessary unit tests" direction
(design.md Section 9), this file is deliberately small: one test of the
shared retry-once-then-fallback helper (design.md Section 5.1) -- the single
mechanism every LLM Value Node routes through, so this is the only
per-node-shaped contract test needed -- plus the two Router-level model-call
gating claims design.md Section 9 calls out explicitly:
``DistillTriggerRouter`` (a no-op turn never reaches ``DistillProfile``) and
``CandidatePoolRouter`` (this path never touches a model client, for any
``CandidateStats`` input).  Behavioral quality of the 7 wired nodes is
judged from the Phase 8 scenario showcase, not from a per-node test matrix
here.
"""

from __future__ import annotations

import unittest

from starter.shopping_agent import graph
from starter.shopping_agent.llm_nodes import AskAttributeOutput, call_llm_value_node
from starter.shopping_agent.model import BackendFailure, ModelCompletionResult
from starter.shopping_agent.state import (
    CandidateRef,
    CandidateStats,
    ConstraintMutation,
    IntentUpdate,
    NodeStateUpdate,
    SessionState,
    StateReducer,
)


class _ScriptedClient:
    """A ``TieredModelClient``-shaped fake: returns each entry of
    ``payloads`` in call order (repeating the last one past the end), and
    runs the caller-supplied validator itself so a validation failure
    surfaces exactly the way ``TieredModelClient.complete_json`` does.
    """

    def __init__(self, payloads: list[object]) -> None:
        self.backends = ("fake",)
        self._payloads = list(payloads)
        self.calls = 0

    def complete_json(self, messages, validator=None) -> ModelCompletionResult:
        self.calls += 1
        index = min(self.calls - 1, len(self._payloads) - 1)
        payload = self._payloads[index]
        if validator is None:
            return ModelCompletionResult(value=payload, backend="fake", usage=None)
        try:
            value = validator(payload)
        except Exception as exc:  # mirrors TieredModelClient's own catch
            return ModelCompletionResult(
                value=None,
                backend=None,
                usage=None,
                failures=(BackendFailure("fake", "validator", str(exc)),),
            )
        return ModelCompletionResult(value=value, backend="fake", usage=None)


class _CountingClient:
    """Records how many times a call was attempted; never returns success.

    Used only to prove a code path does or does not touch a model client at
    all -- the *value* of a successful call is irrelevant to these tests.
    """

    def __init__(self) -> None:
        self.backends = ("fake",)
        self.calls = 0

    def complete_json(self, messages, validator=None) -> ModelCompletionResult:
        self.calls += 1
        return ModelCompletionResult(value=None, backend=None, usage=None)


class SharedRetryFallbackHelperTest(unittest.TestCase):
    """design.md Section 5.1/Section 9: the one mechanism every LLM Value
    Node routes through -- proven once here, not duplicated per node."""

    def test_success_retry_and_fallback_paths(self) -> None:
        with self.subTest("first attempt succeeds"):
            client = _ScriptedClient([{"question_text": "Color preference?"}])
            result = call_llm_value_node(
                client,
                task_prompt="ask",
                user_payload={"attribute": "color"},
                output_model=AskAttributeOutput,
            )
            self.assertIsInstance(result, AskAttributeOutput)
            self.assertEqual(result.question_text, "Color preference?")
            self.assertEqual(client.calls, 1)

        with self.subTest("first attempt invalid, retry succeeds"):
            client = _ScriptedClient([{"bad": "shape"}, {"question_text": "Size preference?"}])
            result = call_llm_value_node(
                client,
                task_prompt="ask",
                user_payload={"attribute": "size"},
                output_model=AskAttributeOutput,
            )
            self.assertIsInstance(result, AskAttributeOutput)
            self.assertEqual(result.question_text, "Size preference?")
            self.assertEqual(client.calls, 2)

        with self.subTest("both attempts invalid falls back to None, never a third call"):
            client = _ScriptedClient([{"bad": "shape"}, {"still": "bad"}])
            result = call_llm_value_node(
                client,
                task_prompt="ask",
                user_payload={"attribute": "brand"},
                output_model=AskAttributeOutput,
            )
            self.assertIsNone(result)
            self.assertEqual(client.calls, 2)

        with self.subTest("no client configured is the same fallback, zero calls"):
            client = _CountingClient()
            result = call_llm_value_node(
                None,
                task_prompt="ask",
                user_payload={"attribute": "brand"},
                output_model=AskAttributeOutput,
            )
            self.assertIsNone(result)
            self.assertEqual(client.calls, 0)  # never touched -- client wasn't even passed in


class DistillTriggerRouterGatingTest(unittest.TestCase):
    """design.md Section 9: a no-op turn must produce zero model calls."""

    def test_no_op_turn_skips_distill_profile_and_touches_no_model(self) -> None:
        counting_client = _CountingClient()
        services = graph.GraphServices(model_client=counting_client)

        gs_no_op = graph.GraphState(
            session=SessionState(session_id="distill-gate-no-op"), turn=1, top_k=10, services=services
        )
        gs_no_op.scratch["extract_constraints_changed"] = False
        gs_no_op.scratch["extract_constraints_override"] = False
        next_node, _args = graph.ROUTERS["ExtractConstraints"](gs_no_op)
        self.assertNotEqual(next_node, "DistillProfile")

        gs_changed = graph.GraphState(
            session=SessionState(session_id="distill-gate-changed"), turn=1, top_k=10, services=services
        )
        gs_changed.scratch["extract_constraints_changed"] = True
        gs_changed.scratch["extract_constraints_override"] = False
        next_node_changed, _args = graph.ROUTERS["ExtractConstraints"](gs_changed)
        self.assertEqual(next_node_changed, "DistillProfile")

        # The router itself is pure code regardless of which branch it took.
        self.assertEqual(counting_client.calls, 0)


class CandidatePoolRouterNoModelTest(unittest.TestCase):
    """design.md Section 5.3/Section 9: CandidatePoolRouter never touches a
    model client, for any CandidateStats input."""

    def _router_call(self, gs: graph.GraphState) -> str:
        next_node, _args = graph.ROUTERS["Search"](gs)
        return next_node

    def test_empty_pool_first_pass_never_touches_model(self) -> None:
        counting_client = _CountingClient()
        services = graph.GraphServices(model_client=counting_client)
        session = SessionState(session_id="pool-empty-first")
        gs = graph.GraphState(session=session, turn=1, top_k=10, services=services)

        self.assertEqual(self._router_call(gs), "LoosenConstraints")
        self.assertEqual(counting_client.calls, 0)

    def test_empty_pool_after_retry_with_constraint_left_never_touches_model(self) -> None:
        counting_client = _CountingClient()
        services = graph.GraphServices(model_client=counting_client)
        session = SessionState(session_id="pool-empty-retry")
        StateReducer().apply(
            session,
            IntentUpdate(
                mutations=(
                    ConstraintMutation(action="upsert", attribute="color", value="red", hardness="soft"),
                )
            ),
            turn=1,
        )
        StateReducer().apply(session, NodeStateUpdate(increment_search_retry=True))
        gs = graph.GraphState(session=session, turn=3, top_k=10, services=services)

        self.assertEqual(self._router_call(gs), "AskAttribute")
        self.assertEqual(counting_client.calls, 0)

    def test_true_dead_end_never_touches_model(self) -> None:
        counting_client = _CountingClient()
        services = graph.GraphServices(model_client=counting_client)
        session = SessionState(session_id="pool-dead-end")
        StateReducer().apply(session, NodeStateUpdate(increment_search_retry=True))
        gs = graph.GraphState(session=session, turn=3, top_k=10, services=services)

        self.assertEqual(self._router_call(gs), "NoMatch")
        self.assertEqual(counting_client.calls, 0)

    def test_over_general_pool_never_touches_model(self) -> None:
        """CandidatePoolRouter always sends an over-general, non-empty pool
        to ``Rank`` (never straight to ``AskAttribute`` -- a question must
        never suppress this turn's own recommendations, see
        ``_candidate_pool_router``'s docstring); the "ask instead of
        SemanticRank" decision is made one step later by ``RankRouter``
        (``ROUTERS["Rank"]``), reading the same attribute
        ``CandidatePoolRouter`` already stashed in scratch.  No model call on
        either step.
        """

        counting_client = _CountingClient()
        services = graph.GraphServices(model_client=counting_client)
        session = SessionState(session_id="pool-over-general")
        StateReducer().apply(
            session,
            NodeStateUpdate(candidates=(CandidateRef(parent_asin="B00000001", summary="a shoe", score=1.0),)),
        )
        session.last_candidate_stats = CandidateStats(estimated_count=5000, category_entropy=3.0)
        gs = graph.GraphState(session=session, turn=1, top_k=10, services=services)

        self.assertEqual(self._router_call(gs), "Rank")
        pending = gs.scratch.get("rank_ask_attribute")
        self.assertIsInstance(pending, dict)
        self.assertTrue(pending.get("attribute"))
        next_node, _args = graph.ROUTERS["Rank"](gs)
        self.assertEqual(next_node, "AskAttribute")
        self.assertEqual(counting_client.calls, 0)

    def test_focused_pool_never_touches_model(self) -> None:
        counting_client = _CountingClient()
        services = graph.GraphServices(model_client=counting_client)
        session = SessionState(session_id="pool-focused")
        StateReducer().apply(
            session,
            NodeStateUpdate(candidates=(CandidateRef(parent_asin="B00000001", summary="a shoe", score=1.0),)),
        )
        session.last_candidate_stats = CandidateStats(estimated_count=5, category_entropy=0.1)
        gs = graph.GraphState(session=session, turn=1, top_k=10, services=services)

        self.assertEqual(self._router_call(gs), "Rank")
        self.assertEqual(counting_client.calls, 0)


if __name__ == "__main__":
    unittest.main()
