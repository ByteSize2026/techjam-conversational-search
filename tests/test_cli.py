from __future__ import annotations

from io import StringIO
import unittest
from unittest.mock import patch

from starter.cli import build_parser, format_response, main, run_repl


class _FakeAgent:
    def __init__(self) -> None:
        self.resets: list[tuple[str, dict[str, object]]] = []
        self.calls: list[tuple[str, str, int, int]] = []
        self.last_diagnostics = {
            "protocol_profile": "official",
            "intent_path": "rules",
            "route": "buying",
            "target_parent_asin": "SHOULD_NOT_PRINT",
        }

    def reset(self, session_id: str, user_profile: dict[str, object]) -> None:
        self.resets.append((session_id, user_profile))

    def respond(self, session_id: str, message: str, turn: int, top_k: int) -> dict[str, object]:
        self.calls.append((session_id, message, turn, top_k))
        return {
            "message": f"reply to {message}",
            "ask_attribute": "color",
            "recommendations": [{"parent_asin": "P1"}, "P2"],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }


class InteractiveCliTests(unittest.TestCase):
    def test_parser_exposes_both_profiles(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args([]).protocol_profile, "official")
        self.assertEqual(
            parser.parse_args(["--protocol-profile", "natural_language"]).protocol_profile,
            "natural_language",
        )

    def test_format_response_is_readable(self) -> None:
        rendered = format_response(
            {
                "message": "Here are options.",
                "ask_attribute": None,
                "recommendations": [{"parent_asin": "P1"}, "P2"],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        )
        self.assertIn("recommendations (2):", rendered)
        self.assertIn("1. P1", rendered)
        self.assertIn("usage: prompt=3, completion=2, total=5", rendered)

    def test_repl_handles_turns_reset_and_exit(self) -> None:
        agent = _FakeAgent()
        stdin = StringIO("first\n:reset\nsecond\n:exit\n")
        stdout = StringIO()
        result = run_repl(
            agent,
            session_id="demo",
            user_profile={"preference_tags": ["comfort"]},
            top_k=2,
            max_turns=10,
            stdin=stdin,
            stdout=stdout,
            show_diagnostics=True,
            prompt=False,
        )
        self.assertEqual(result, 0)
        self.assertEqual(len(agent.resets), 2)
        self.assertEqual(agent.calls, [("demo", "first", 1, 2), ("demo", "second", 1, 2)])
        self.assertIn("intent_path: \"rules\"", stdout.getvalue())
        self.assertNotIn("SHOULD_NOT_PRINT", stdout.getvalue())

    def test_repl_stops_at_max_turns(self) -> None:
        agent = _FakeAgent()
        stdout = StringIO()
        run_repl(
            agent,
            session_id="demo",
            user_profile={},
            top_k=10,
            max_turns=1,
            stdin=StringIO("first\nsecond\n"),
            stdout=stdout,
            prompt=False,
        )
        self.assertEqual(len(agent.calls), 1)
        self.assertIn("ended after 1 turns", stdout.getvalue())

    def test_repl_ends_cleanly_on_eof(self) -> None:
        agent = _FakeAgent()
        stdout = StringIO()
        result = run_repl(
            agent,
            session_id="demo",
            user_profile={},
            top_k=10,
            max_turns=10,
            stdin=StringIO(""),
            stdout=stdout,
            prompt=False,
        )
        self.assertEqual(result, 0)
        self.assertEqual(agent.calls, [])
        self.assertIn("Session ended (EOF)", stdout.getvalue())

    @patch("starter.cli.run_repl", return_value=0)
    @patch("starter.cli.Agent")
    def test_main_passes_natural_language_profile_to_agent(
        self, agent_class: object, repl: object
    ) -> None:
        result = main(
            [
                "--protocol-profile",
                "natural_language",
                "--catalog",
                "/tmp/catalog.jsonl",
                "--no-prompt",
            ]
        )
        self.assertEqual(result, 0)
        agent_class.assert_called_once_with(  # type: ignore[attr-defined]
            "/tmp/catalog.jsonl", protocol_profile="natural_language"
        )
        self.assertTrue(repl.called)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
