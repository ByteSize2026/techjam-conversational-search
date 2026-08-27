from __future__ import annotations

import unittest

from starter.agent import Agent
from starter.shopping_agent.catalog import CatalogRepository
from starter.shopping_agent.config import AgentConfig
from starter.shopping_agent.planner import PlannerResult, ScriptedActionPlanner
from tests.test_agent_tools import PRODUCTS


class AgentToolLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = CatalogRepository(catalog_path=None, records=PRODUCTS)
        self.profile = {
            "purchase_frequency": "often",
            "preference_tags": ["comfort"],
            "summary": "comfort-focused",
        }

    def test_pause_resume_profile_gating_and_contract(self) -> None:
        planner = ScriptedActionPlanner(
            [
                {"name": "get_user_profile", "arguments": {}},
                {"name": "search_products", "arguments": {"query": "running shirts", "limit": 3}},
                {
                    "name": "ask_user",
                    "arguments": {"question": "Which material do you prefer?", "ask_attribute": "material"},
                },
                {
                    "name": "filter_products",
                    "arguments": {
                        "product_ids": ["A", "B"],
                        "attribute": "material",
                        "operator": "contains",
                        "value": "cotton",
                    },
                },
                {
                    "name": "recommend_products",
                    "arguments": {"parent_asins": ["A"], "message": "A is the best match."},
                },
            ]
        )
        agent = Agent(
            repository=self.repository,
            config=AgentConfig(tool_max_steps=4),
            action_planner=planner,
        )
        agent.reset("s", self.profile)
        first = agent.respond("s", "I'm looking for running shirts.", 1, 2)
        self.assertEqual(first["ask_attribute"], "material")
        self.assertIsNotNone(agent.store.require("s").pending_task)
        self.assertFalse(planner.contexts[0].profile_loaded)
        self.assertNotIn("profile", planner.contexts[0].as_dict())
        self.assertTrue(planner.contexts[1].profile_loaded)

        second = agent.respond("s", "cotton", 2, 2)
        ids = [item["parent_asin"] for item in second["recommendations"]]
        self.assertIsNone(second["ask_attribute"])
        self.assertEqual(ids[0], "A")
        self.assertLessEqual(len(ids), 2)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(ids) <= self.repository.ids)
        self.assertIsNone(agent.store.require("s").pending_task)
        self.assertTrue(agent.last_diagnostics["resumed_after_ask"])
        self.assertEqual(agent.last_diagnostics["execution_mode"], "tool_loop")
        answered = planner.contexts[-1].answered_clarification
        self.assertIsNotNone(answered)
        assert answered is not None
        self.assertEqual(
            answered["ask_attribute"], "material"
        )
        self.assertTrue(
            any(
                item.attribute == "material" and item.value.lower() == "cotton"
                for item in agent.store.require("s").active_constraints
            )
        )
        self.assertEqual(
            next(
                item.action
                for item in agent.store.require("s").tool_trajectory
                if item.turn == 2
            ),
            "resume_after_ask",
        )

    def test_pending_tasks_are_isolated_between_sessions(self) -> None:
        planner = ScriptedActionPlanner(
            [
                {"name": "search_products", "arguments": {"query": "cotton shirt", "limit": 2}},
                {"name": "ask_user", "arguments": {"question": "Which material?", "ask_attribute": "material"}},
                {"name": "search_products", "arguments": {"query": "hiking boot", "limit": 2}},
                {"name": "ask_user", "arguments": {"question": "Which size?", "ask_attribute": "size"}},
                {"name": "recommend_products", "arguments": {"parent_asins": ["A"]}},
            ]
        )
        agent = Agent(repository=self.repository, action_planner=planner)
        agent.reset("a", {"summary": "profile a"})
        agent.reset("b", {"summary": "profile b"})
        agent.respond("a", "shirt", 1, 2)
        agent.respond("b", "boot", 1, 2)
        self.assertEqual(agent.store.require("a").pending_task.ask_attribute, "material")
        self.assertEqual(agent.store.require("b").pending_task.ask_attribute, "size")

        agent.respond("a", "cotton", 2, 2)
        self.assertIsNone(agent.store.require("a").pending_task)
        self.assertEqual(agent.store.require("b").pending_task.ask_attribute, "size")
        self.assertNotEqual(
            agent.store.require("a").tool_candidate_ids,
            agent.store.require("b").tool_candidate_ids,
        )

    def test_override_invalidates_pending_work_and_candidates(self) -> None:
        planner = ScriptedActionPlanner(
            [
                {"name": "search_products", "arguments": {"query": "cotton shirt", "limit": 2}},
                {
                    "name": "ask_user",
                    "arguments": {"question": "Any material preference?", "ask_attribute": "material"},
                },
                {"name": "search_products", "arguments": {"query": "leather boot", "limit": 2}},
                {
                    "name": "recommend_products",
                    "arguments": {"parent_asins": ["B"], "message": "The boot matches."},
                },
            ]
        )
        agent = Agent(repository=self.repository, action_planner=planner)
        agent.reset("override", self.profile)
        agent.respond("override", "I'm looking for shirts. A key requirement is: cotton.", 1, 2)
        self.assertIsNotNone(agent.store.require("override").pending_task)

        response = agent.respond(
            "override",
            "Actually, ignore my earlier preference. What I need is: leather.",
            2,
            2,
        )
        state = agent.store.require("override")
        self.assertEqual(state.intent_epoch, 1)
        self.assertIsNone(state.pending_task)
        self.assertFalse(agent.last_diagnostics["resumed_after_ask"])
        self.assertEqual(response["recommendations"][0]["parent_asin"], "B")
        self.assertFalse(any(item.value.lower() == "cotton" for item in state.active_constraints))

    def test_bad_planner_falls_back_without_escaping_public_boundary(self) -> None:
        planner = ScriptedActionPlanner(
            [
                {"name": "made_up_tool", "arguments": {}},
                {"name": "another_bad_tool", "arguments": {}},
            ]
        )
        agent = Agent(repository=self.repository, action_planner=planner)
        agent.reset("fallback", self.profile)
        response = agent.respond("fallback", "I'm looking for shirts.", 1, 10)
        self.assertEqual(agent.last_diagnostics["execution_mode"], "fallback")
        self.assertEqual(agent.last_diagnostics["tool_fallback_reason"], "invalid_action_limit")
        self.assertIsInstance(response["message"], str)
        self.assertTrue(
            {item["parent_asin"] for item in response["recommendations"]}
            <= self.repository.ids
        )

    def test_fallback_aggregates_planner_usage_in_response_and_diagnostics(self) -> None:
        class InvalidPlanner:
            def next_action(self, _context):
                return PlannerResult(
                    action=None,
                    usage={"prompt_tokens": 3, "completion_tokens": 2},
                    error="invalid",
                )

        agent = Agent(repository=self.repository, action_planner=InvalidPlanner())
        agent.reset("usage", self.profile)
        response = agent.respond("usage", "I'm looking for shirts.", 1, 2)
        self.assertEqual(
            response["usage"],
            {"prompt_tokens": 6, "completion_tokens": 4},
        )
        self.assertEqual(agent.last_diagnostics["usage"], response["usage"])

    def test_tool_flag_without_model_backend_stays_offline_deterministic(self) -> None:
        agent = Agent(
            repository=self.repository,
            config=AgentConfig(tool_planning_enabled=True),
        )
        self.assertIsNone(agent.action_orchestrator)
        agent.reset("offline", self.profile)
        response = agent.respond("offline", "shirt", 1, 2)
        self.assertEqual(agent.last_diagnostics["execution_mode"], "deterministic")
        self.assertTrue(response["recommendations"])

    def test_reset_clears_all_tool_state(self) -> None:
        planner = ScriptedActionPlanner(
            [
                {"name": "search_products", "arguments": {"query": "shirts"}},
                {
                    "name": "ask_user",
                    "arguments": {"question": "Which size?", "ask_attribute": "size"},
                },
            ]
        )
        agent = Agent(repository=self.repository, action_planner=planner)
        agent.reset("same", self.profile)
        agent.respond("same", "shirts", 1, 2)
        self.assertTrue(agent.store.require("same").tool_trajectory)
        agent.reset("same", {"summary": "new profile"})
        state = agent.store.require("same")
        self.assertFalse(state.profile_loaded)
        self.assertEqual(state.tool_candidate_ids, [])
        self.assertEqual(state.tool_trajectory, [])
        self.assertIsNone(state.pending_task)


if __name__ == "__main__":
    unittest.main()
