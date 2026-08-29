"""One-off qualitative probes of the live LLM Value Nodes (Part 3 spot-check
material -- not a regression test, no assertions, just printed evidence).

Runs a handful of hand-picked, deliberately indirect/ambiguous messages
directly through the live Agent/graph and prints the concrete structured
output each node produced, for direct quoting in the reliability report.

Usage::

    set -a; source .env; set +a
    python3 scripts/probe_llm_node_quality.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from starter.agent import Agent
from starter.shopping_agent import llm_nodes


def _probe_extract_constraints(agent: Agent) -> None:
    print("\n=== ExtractConstraints: indirect/non-templated phrasing ===")
    messages = [
        "nothing too flashy, and I really can't spend a fortune on this",
        "it's for my mom, something she could wear to a nice dinner without it being itchy",
        "I don't care about the brand at all, just make sure it'll survive the washing machine",
    ]
    for message in messages:
        payload = {
            "message": message,
            "recent_turns": [],
            "known_constraints": [],
        }
        output = llm_nodes.call_llm_value_node(
            agent._graph_services.llm_client(),
            task_prompt=llm_nodes.EXTRACT_CONSTRAINTS_PROMPT,
            user_payload=payload,
            output_model=llm_nodes.ExtractConstraintsOutput,
        )
        print(f"\n--- message: {message!r}")
        print(json.dumps(output.model_dump() if output else None, indent=2))


def _probe_classify_intent(agent: Agent) -> None:
    print("\n=== ClassifyIntent: deliberately ambiguous follow-ups ===")
    last_ids = ["B0BNP1RZ2W", "B08B62ZW7H", "B08HCP9YTV"]
    messages = [
        "tell me more about the second one",
        "actually do you have anything cheaper",
        "yeah that first one looks perfect, I'll go with that",
        "never mind, let's look at watches instead",
    ]
    for message in messages:
        payload = {"message": message, "last_candidate_ids": last_ids}
        output = llm_nodes.call_llm_value_node(
            agent._graph_services.llm_client(),
            task_prompt=llm_nodes.CLASSIFY_INTENT_PROMPT,
            user_payload=payload,
            output_model=llm_nodes.ClassifyIntentOutput,
        )
        print(f"\n--- message: {message!r}")
        print(json.dumps(output.model_dump() if output else None, indent=2))


def _probe_semantic_rank(agent: Agent) -> None:
    print("\n=== SemanticRank: repeated attempts to capture a side-by-side reorder ===")
    session_id = "probe-semantic-rank"
    agent.reset(session_id, {})
    agent.respond(session_id, "I need a durable phone case for my teenager, nothing too expensive", 1, 10)
    from starter.shopping_agent import graph as graph_mod

    state = agent.store.get(session_id)
    if state is None or not state.candidates:
        print("(no candidates available for this probe)")
        return
    for attempt in range(3):
        before_ids = [item.parent_asin for item in state.ranked[:5]]
        gs = graph_mod.GraphState(session=state, turn=2, top_k=10, message="", services=agent._graph_services)
        gs = graph_mod.NODES["SemanticRank"](gs, {})
        after_ids = [item.parent_asin for item in state.ranked[:5]]
        backend = gs.scratch.get("semantic_rank_backend")
        print(f"\n--- attempt {attempt + 1}: backend={backend}")
        print(f"before (feature_rank) top5: {before_ids}")
        print(f"after  (SemanticRank)  top5: {after_ids}")
        if backend is not None:
            break


def _probe_hallucination(agent: Agent) -> None:
    print("\n=== Explain/Compare: hallucination spot-check against real catalog fields ===")
    session_id = "probe-hallucination"
    agent.reset(session_id, {})
    response = agent.respond(session_id, "I'm looking for a leather wallet for men", 1, 10)
    print(f"\nExplain output: {response.get('message')!r}")
    ids = [item["parent_asin"] for item in response.get("recommendations", [])[:2]]
    for parent_asin in ids:
        records = agent.repository.materialize([parent_asin], 1)
        if records:
            record = records[0]
            print(
                f"  catalog fact check {parent_asin}: title={record.title!r}, "
                f"price={record.price}, rating={record.rating}"
            )
    if len(ids) >= 2:
        response2 = agent.respond(session_id, f"compare {ids[0]} and {ids[1]}", 2, 10)
        print(f"\nCompare output: {response2.get('message')!r}")


def main() -> None:
    agent = Agent(str(_REPO_ROOT / "data" / "catalog.jsonl"))
    live = bool(agent._graph_services.model_client and agent._graph_services.model_client.backends)
    print(f"live model configured: {live}")
    _probe_extract_constraints(agent)
    _probe_classify_intent(agent)
    _probe_semantic_rank(agent)
    _probe_hallucination(agent)


if __name__ == "__main__":
    main()
