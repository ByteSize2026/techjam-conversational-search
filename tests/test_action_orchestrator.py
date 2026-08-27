from __future__ import annotations

import json
import unittest

from starter.shopping_agent.catalog import CatalogRepository
from starter.shopping_agent.model import BackendResponse, TieredModelClient
from starter.shopping_agent.orchestrator import ActionOrchestrator
from starter.shopping_agent.planner import (
    ModelActionPlanner,
    PlannerContext,
    ScriptedActionPlanner,
)
from starter.shopping_agent.state import SessionStore
from starter.shopping_agent.tools import ShoppingToolbox
from tests.test_agent_tools import PRODUCTS


class ActionOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = CatalogRepository(catalog_path=None, records=PRODUCTS)
        self.state = SessionStore().reset(
            "session", {"preference_tags": ["comfort"], "summary": "likes comfort"}
        )

    def _run(self, actions: list[object], *, max_steps: int = 4):
        planner = ScriptedActionPlanner(actions)
        loop = ActionOrchestrator(planner, max_steps=max_steps, timeout_seconds=2.0)
        toolbox = ShoppingToolbox(self.repository, self.state, top_k=2)
        result = loop.run(
            self.state,
            toolbox,
            user_message="I want a cotton running shirt",
            turn=1,
        )
        return result, planner

    def test_multiple_nonterminal_actions_then_recommend(self) -> None:
        result, planner = self._run(
            [
                {"name": "get_user_profile", "arguments": {}},
                {"name": "search_products", "arguments": {"query": "cotton running", "limit": 2}},
                {"name": "get_product_details", "arguments": {"parent_asin": "A"}},
                {"name": "recommend_products", "arguments": {"parent_asins": ["A"], "message": "Choose A"}},
            ]
        )
        self.assertFalse(result.fallback_needed)
        self.assertEqual(result.reason, "recommend_products")
        self.assertEqual(result.action_count, 4)
        self.assertEqual(result.response["recommendations"], [{"parent_asin": "A"}])
        self.assertFalse(planner.contexts[0].profile_loaded)
        self.assertIsNone(planner.contexts[0].profile)
        self.assertTrue(planner.contexts[1].profile_loaded)
        self.assertIn("summary", planner.contexts[1].profile)
        self.assertEqual(
            [entry.action for entry in self.state.tool_trajectory],
            ["get_user_profile", "search_products", "get_product_details", "recommend_products"],
        )

    def test_ask_user_pauses_without_waiting(self) -> None:
        result, _planner = self._run(
            [
                {"name": "search_products", "arguments": {"query": "shirts", "limit": 2}},
                {
                    "name": "ask_user",
                    "arguments": {"question": "What material do you prefer?", "ask_attribute": "material"},
                },
            ]
        )
        self.assertEqual(result.reason, "ask_user")
        self.assertEqual(result.response["ask_attribute"], "material")
        self.assertIsNotNone(self.state.pending_task)
        self.assertEqual(self.state.pending_task.intent_epoch, 0)

    def test_invalid_actions_and_step_exhaustion_request_fallback(self) -> None:
        result, _planner = self._run(
            [
                {"name": "unknown", "arguments": {}},
                {"name": "still_unknown", "arguments": {}},
            ]
        )
        self.assertTrue(result.fallback_needed)
        self.assertEqual(result.reason, "invalid_action_limit")
        self.assertEqual(len(self.state.tool_trajectory), 3)
        self.assertEqual(self.state.tool_trajectory[-1].status, "fallback")
        self.assertEqual(
            self.state.tool_trajectory[-1].observation_summary["reason"],
            "invalid_action_limit",
        )

        self.state.tool_trajectory.clear()
        result, _planner = self._run(
            [{"name": "search_products", "arguments": {"query": "shirt"}}],
            max_steps=1,
        )
        self.assertTrue(result.fallback_needed)
        self.assertEqual(result.reason, "step_budget_exhausted")

    def test_model_planner_uses_existing_json_client_and_gates_profile(self) -> None:
        class Backend:
            name = "fake-planner"

            def __init__(self) -> None:
                self.messages = None

            def complete(self, messages, **_kwargs):
                self.messages = messages
                return BackendResponse(
                    content={
                        "name": "search_products",
                        "arguments": {"query": "cotton shirt", "limit": 2},
                        "rationale": "find candidates",
                    },
                    usage={"prompt_tokens": 11, "completion_tokens": 7},
                )

        backend = Backend()
        planner = ModelActionPlanner(TieredModelClient([backend]))
        context = PlannerContext.from_state(
            self.state,
            user_message="shirt",
            turn=1,
            remaining_steps=4,
        )
        result = planner.next_action(context)
        action = result.action
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.name, "search_products")
        self.assertEqual(result.backend, "fake-planner")
        self.assertEqual(result.usage, {"prompt_tokens": 11, "completion_tokens": 7})
        assert backend.messages is not None
        prompt_payload = json.loads(backend.messages[1]["content"])
        self.assertFalse(prompt_payload["state"]["profile_loaded"])
        self.assertNotIn("profile", prompt_payload["state"])
        self.assertNotIn("likes comfort", backend.messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
