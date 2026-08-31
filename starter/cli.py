"""Small interactive terminal for demonstrating :class:`starter.agent.Agent`.

This is deliberately a thin presentation layer.  It does not implement a
second conversation engine: every turn goes through the same ``reset`` and
``respond`` methods used by the evaluators.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import TextIO

from .agent import Agent


EXIT_COMMANDS = frozenset({":exit", ":quit", "exit", "quit"})
HELP_COMMANDS = frozenset({":help", "help"})
RESET_COMMANDS = frozenset({":reset", "reset"})
SAFE_DIAGNOSTIC_KEYS = (
    "protocol_profile",
    "intent_path",
    "intent_trigger_reason",
    "intent_accepted",
    "intent_rejected",
    "route",
    "gate",
    "commit",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interactive terminal for the TechJam shopping Agent"
    )
    parser.add_argument(
        "--protocol-profile",
        choices=("official", "natural_language"),
        default="official",
        help="Agent protocol profile (default: official)",
    )
    parser.add_argument(
        "--catalog",
        default="data/catalog.jsonl",
        help="Path to the frozen catalog JSONL (default: data/catalog.jsonl)",
    )
    parser.add_argument(
        "--user-profile-json",
        default="{}",
        help="Optional JSON object passed to Agent.reset (default: {})",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Session ID for diagnostics (default: generated locally)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Number of recommendations to display (default: 10)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=10,
        help="Maximum turns in this demo session (default: 10)",
    )
    parser.add_argument(
        "--show-diagnostics",
        action="store_true",
        help="Show safe, target-free intent and policy diagnostics after each turn",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Do not print the You> input prompt (useful for piped stdin)",
    )
    return parser


def parse_profile(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--user-profile-json must be valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("--user-profile-json must contain a JSON object")
    return value


def _recommendation_id(item: object) -> str:
    if isinstance(item, Mapping):
        value = item.get("parent_asin", "")
    else:
        value = item
    return str(value).strip()


def format_response(response: object) -> str:
    """Render only user-facing response fields and reported usage."""

    if not isinstance(response, Mapping):
        return "Agent response: <invalid response>"
    lines = [f"Agent: {str(response.get('message', '')).strip()}"]
    ask_attribute = response.get("ask_attribute")
    lines.append(f"ask_attribute: {ask_attribute if ask_attribute else 'none'}")
    raw_recommendations = response.get("recommendations", [])
    recommendations = (
        raw_recommendations if isinstance(raw_recommendations, Iterable) and not isinstance(raw_recommendations, (str, bytes, Mapping)) else ()
    )
    ids = [_recommendation_id(item) for item in recommendations]
    ids = [value for value in ids if value]
    lines.append(f"recommendations ({len(ids)}):")
    lines.extend(f"  {index}. {value}" for index, value in enumerate(ids, 1))
    usage = response.get("usage")
    if isinstance(usage, Mapping):
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        total = usage.get("total_tokens")
        if total is None and isinstance(prompt, int) and isinstance(completion, int):
            total = prompt + completion
        lines.append(
            f"usage: prompt={prompt}, completion={completion}, total={total if total is not None else 'unknown'}"
        )
    return "\n".join(lines)


def format_safe_diagnostics(agent: object) -> str:
    diagnostics = getattr(agent, "last_diagnostics", {})
    if not isinstance(diagnostics, Mapping):
        return "diagnostics: unavailable"
    lines = ["diagnostics (safe summary):"]
    for key in SAFE_DIAGNOSTIC_KEYS:
        if key not in diagnostics:
            continue
        value = diagnostics[key]
        # Avoid dumping arbitrary nested state while retaining the useful
        # model/policy path for a demo recording.
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        lines.append(f"  {key}: {rendered[:500]}")
    return "\n".join(lines)


def run_repl(
    agent: Agent,
    *,
    session_id: str,
    user_profile: dict[str, object],
    top_k: int,
    max_turns: int,
    stdin: TextIO,
    stdout: TextIO,
    show_diagnostics: bool = False,
    prompt: bool = True,
) -> int:
    agent.reset(session_id, user_profile)
    turn = 1
    stdout.write("Interactive shopping Agent. Type :help for commands.\n")
    while turn <= max_turns:
        if prompt:
            stdout.write("You> ")
            stdout.flush()
        raw = stdin.readline()
        if raw == "":
            stdout.write("\nSession ended (EOF).\n")
            return 0
        message = raw.strip()
        if not message:
            continue
        command = message.casefold()
        if command in EXIT_COMMANDS:
            stdout.write("Session ended.\n")
            return 0
        if command in HELP_COMMANDS:
            stdout.write("Commands: :help, :reset, :exit (or quit).\n")
            continue
        if command in RESET_COMMANDS:
            agent.reset(session_id, user_profile)
            turn = 1
            stdout.write("Session reset; turn counter is back to 1.\n")
            continue
        try:
            response = agent.respond(session_id, message, turn, top_k)
        except Exception as exc:  # pragma: no cover - defensive demo boundary
            stdout.write(f"Agent error on turn {turn}: {exc.__class__.__name__}\n")
            turn += 1
            continue
        stdout.write(f"\nTurn {turn}\n{format_response(response)}\n")
        if show_diagnostics:
            stdout.write(format_safe_diagnostics(agent) + "\n")
        turn += 1
    stdout.write(f"Session ended after {max_turns} turns.\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.top_k <= 100:
        parser.error("--top-k must be between 1 and 100")
    if not 1 <= args.max_turns <= 10:
        parser.error("--max-turns must be between 1 and 10")
    try:
        user_profile = parse_profile(args.user_profile_json)
    except ValueError as exc:
        parser.error(str(exc))
    agent = Agent(args.catalog, protocol_profile=args.protocol_profile)
    session_id = args.session_id or f"cli-{uuid.uuid4().hex}"
    return run_repl(
        agent,
        session_id=session_id,
        user_profile=user_profile,
        top_k=args.top_k,
        max_turns=args.max_turns,
        stdin=sys.stdin,
        stdout=sys.stdout,
        show_diagnostics=args.show_diagnostics,
        prompt=sys.stdin.isatty() and not args.no_prompt,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
