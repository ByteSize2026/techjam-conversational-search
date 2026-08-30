from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import evaluator.local_evaluator as local_evaluator
from starter.agent import Agent
from starter.shopping_agent.catalog import CatalogRepository
from starter.shopping_agent.config import AgentConfig
from starter.shopping_agent.intent_interpreter import IntentInterpreter
from starter.shopping_agent.official_intent import parse_official_intent_update
from starter.shopping_agent.policy import ClarificationPolicy
from starter.shopping_agent.structured_pool import (
    FILTERABLE_ATTRIBUTES,
    OFFICIAL_FILTERABLE_ATTRIBUTES,
)


class ProtocolProfileTests(unittest.TestCase):
    @staticmethod
    def _agent(**kwargs: object) -> Agent:
        return Agent(
            repository=CatalogRepository(records=[]),
            config=AgentConfig(),
            **kwargs,
        )

    def test_default_is_explicit_official_profile(self) -> None:
        default = self._agent()
        explicit = self._agent(protocol_profile="official")

        for agent in (default, explicit):
            self.assertEqual(agent.protocol_profile, "official")
            self.assertIs(
                agent.intent_interpreter, parse_official_intent_update
            )
            self.assertEqual(agent.clarification_policy.mode, "protocol_aware")
            self.assertEqual(agent.last_diagnostics["protocol_profile"], "official")

    def test_profiles_select_protocol_boundary_configuration(self) -> None:
        official = self._agent(protocol_profile="official")
        natural = self._agent(protocol_profile="natural_language")

        self.assertIsInstance(natural.intent_interpreter, IntentInterpreter)
        self.assertEqual(natural.clarification_policy.mode, "catalog_entropy")
        self.assertIs(type(official.retrieval), type(natural.retrieval))
        self.assertIs(type(official.ranking), type(natural.ranking))
        self.assertIs(type(official.commit_policy), type(natural.commit_policy))
        self.assertEqual(
            official.commit_policy.__dict__, natural.commit_policy.__dict__
        )
        self.assertEqual(
            official.structured_pool.filterable_attributes,
            OFFICIAL_FILTERABLE_ATTRIBUTES,
        )
        self.assertEqual(
            natural.structured_pool.filterable_attributes,
            FILTERABLE_ATTRIBUTES,
        )

    def test_profile_grounding_is_natural_language_only(self) -> None:
        records = [
            {
                "parent_asin": "P1",
                "title": "Everyday jacket",
                "store": "Roamans",
            }
        ]
        official = Agent(
            repository=CatalogRepository(records=records),
            config=AgentConfig(),
            protocol_profile="official",
        )
        natural = Agent(
            repository=CatalogRepository(records=records),
            config=AgentConfig(),
            protocol_profile="natural_language",
        )
        profile = {
            "preference_tags": ["Please prioritize the Roamans label"]
        }

        official.reset("official-profile", profile)
        natural.reset("natural-profile", profile)

        self.assertEqual(official.last_diagnostics["profile_intent_mutations"], [])
        self.assertNotEqual(
            natural.last_diagnostics["profile_intent_mutations"], []
        )

    def test_explicit_dependencies_override_profile_defaults(self) -> None:
        interpreter = object()
        policy = ClarificationPolicy(mode="protocol_aware")
        agent = self._agent(
            protocol_profile="natural_language",
            intent_interpreter=interpreter,
            clarification_policy=policy,
        )

        self.assertIs(agent.intent_interpreter, interpreter)
        self.assertIs(agent.clarification_policy, policy)

    def test_config_and_environment_profile_resolution(self) -> None:
        configured = Agent(
            repository=CatalogRepository(records=[]),
            config=AgentConfig(protocol_profile="natural_language"),
        )
        self.assertEqual(configured.protocol_profile, "natural_language")

        with patch.dict(
            os.environ,
            {"SHOPPING_AGENT_PROTOCOL_PROFILE": "natural_language"},
            clear=False,
        ):
            self.assertEqual(
                AgentConfig.from_env().protocol_profile, "natural_language"
            )
        with patch.dict(
            os.environ,
            {"SHOPPING_AGENT_PROTOCOL_PROFILE": "typo"},
            clear=False,
        ):
            self.assertEqual(AgentConfig.from_env().protocol_profile, "official")

        with self.assertRaisesRegex(ValueError, "protocol_profile"):
            AgentConfig(protocol_profile="typo")
        with self.assertRaisesRegex(ValueError, "protocol_profile"):
            self._agent(protocol_profile="typo")
        self.assertEqual(
            AgentConfig(protocol_profile=" Natural_Language ").protocol_profile,
            "natural_language",
        )

    def test_diagnostics_keep_resolved_profile(self) -> None:
        agent = self._agent(protocol_profile="natural_language")
        agent.reset("profile-session", {})
        response = agent.respond("profile-session", "I need something blue", 1, 10)

        self.assertEqual(
            agent.last_diagnostics["protocol_profile"], "natural_language"
        )
        self.assertEqual(
            agent.sessions["profile-session"].last_diagnostics["protocol_profile"],
            "natural_language",
        )
        self.assertEqual(
            set(response), {"message", "ask_attribute", "recommendations"}
        )

    def test_official_cli_accepts_protocol_profile(self) -> None:
        args = local_evaluator._parser().parse_args(
            [
                "--protocol-profile",
                "natural_language",
                "--output",
                "/tmp/result.json",
            ]
        )
        self.assertEqual(args.protocol_profile, "natural_language")

    def test_official_cli_forwards_protocol_profile_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            argv = [
                "local_evaluator",
                "--catalog",
                "catalog.jsonl",
                "--dataset",
                "dataset.jsonl",
                "--output",
                str(output),
                "--protocol-profile",
                "natural_language",
            ]
            agent_instance = object()
            with (
                patch.object(sys, "argv", argv),
                patch.object(local_evaluator, "load_jsonl", return_value=[]),
                patch.object(
                    local_evaluator,
                    "catalog_index",
                    return_value=(set(), {}, {}),
                ),
                patch.object(
                    local_evaluator,
                    "Agent",
                    return_value=agent_instance,
                ) as agent_class,
                patch.object(
                    local_evaluator,
                    "evaluate",
                    return_value={"sessions": []},
                ) as evaluate,
                patch("builtins.print"),
            ):
                local_evaluator.main()

            agent_class.assert_called_once_with(
                "catalog.jsonl", protocol_profile="natural_language"
            )
            evaluate.assert_called_once_with(agent_instance, [], set(), {}, {})


if __name__ == "__main__":
    unittest.main()
