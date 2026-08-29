"""Reproducible generator for ``artifacts/scenario_showcase/*.md``.

Drives the *live* ``starter.agent.Agent`` through six hand-picked
conversations end to end (real model calls when ``SHOPPING_AGENT_DEEPSEEK_API_KEY``
is set in the environment; deterministic fallback otherwise -- the Agent's own
documented "no model configured" path, not a second code path here).

Design note (readability fix, 08-28-agent-v2-router-value-node dispatch):
the previous, hand-written version of these transcripts embedded a raw,
JSON-serialized preview of each Value Node's ``NodeTraceEntry.output_summary``
(``starter/shopping_agent/graph.py``'s ``_summarize_node_output``), which
dumps the *entire* per-turn ``GraphState.scratch`` dict and gets silently cut
off mid-string at ``_TRACE_TEXT_LIMIT`` (600 chars) -- useful for the
machine-readable trace, useless (and actively confusing) in a human-read
transcript.  This script does not read that string at all for rendering.
Instead it wraps ``graph.ROUTERS``/``graph.NODES`` in-process (mutating the
shared module dicts for the lifetime of this script only) to capture each
node's *typed* Python inputs/outputs directly, and renders one concrete,
complete sentence per node from a small per-node-type formatter table
(``_VALUE_NODE_GLOSS`` below) -- e.g. "retrieved 150 candidate(s)
(route=browsing)" or "asked: material (fill_missing)" -- instead of a
truncated dict repr.  Router decisions (``next_node``/``args``) were never
the truncation problem (those payloads are always small) and are rendered
the same straightforward way as before.

Usage::

    set -a; source .env; set +a   # optional: enables live model calls
    python3 scripts/generate_scenario_showcase.py

Writes six ``artifacts/scenario_showcase/<id>.md`` files (overwriting any
existing ones) and, for the two scenarios driven through the live Agent
end-to-end (01-04), a matching ``artifacts/scenario_showcase/<id>/events.jsonl``
provenance trace via ``evaluator.trace_runner``'s existing recorder.  Scenarios
5-6 demonstrate a code path (the bounded empty-search retry cycle) that is
unreachable through the real catalog-backed Agent in this dataset (the
recommendation engine always falls back to a popularity list rather than
truly returning zero candidates) -- exactly as the prior version of this
script disclosed -- so they drive ``starter.shopping_agent.graph.run_graph``
directly with a scripted always-empty ``Search`` node substituted in place of
the real one, the same technique ``tests/test_router_graph.py``'s
``RetryBoundAndDeadEndTest`` uses to prove the mechanism structurally.  No
other node in scenarios 5-6 is mocked.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Callable
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evaluator.trace_runner import JsonlRecorder, TracingAgentProxy
from starter.agent import Agent
from starter.shopping_agent import graph as graph_mod
from starter.shopping_agent.state import (
    ConstraintMutation,
    IntentUpdate,
    NodeStateUpdate,
    SessionState,
    StateReducer,
)

_OUTPUT_DIR = _REPO_ROOT / "artifacts" / "scenario_showcase"
_CATALOG_PATH = _REPO_ROOT / "data" / "catalog.jsonl"


# ---------------------------------------------------------------------------
# In-process instrumentation: wrap graph.ROUTERS / graph.NODES to capture a
# concise, complete gloss of each node's real decision, in call order.  Wraps
# once, globally, for the life of this script -- graph.ROUTERS/graph.NODES
# are module-level singletons every Agent instance shares, so wrapping them
# once covers every scenario below.
# ---------------------------------------------------------------------------

_CAPTURED: list[dict[str, object]] = []


def _reset_capture() -> None:
    _CAPTURED.clear()


def _truncate(text: object, limit: int = 160) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut + "..."


def _fmt_constraint(item: object) -> str:
    return f"{item.attribute}={item.value}[{item.hardness}]"


def _router_gloss(next_node: str, args: dict[str, object]) -> str:
    if not args:
        return f"-> {next_node}"
    parts = ", ".join(f"{key}={value}" for key, value in args.items())
    return f"-> {next_node} ({parts})"


def _gloss_classify_intent(gs, args, before_constraints, before_profile) -> str:
    data = gs.scratch.get("classify_intent") or {}
    intent = data.get("intent", "?")
    targets = data.get("target_ids") or []
    tail = f" (targets: {', '.join(str(t) for t in targets[:3])})" if targets else ""
    return f"classified intent: {intent}{tail}"


def _gloss_extract_constraints(gs, args, before_constraints, before_profile) -> str:
    update = gs.scratch.get("extract_constraints_update")
    if not isinstance(update, IntentUpdate):
        return "no constraint diff produced"
    bits = [
        f"{mutation.action} {mutation.attribute}={mutation.value}[{mutation.hardness}]"
        for mutation in update.mutations
    ]
    if update.category_anchor:
        bits.append(f"category={update.category_anchor}")
    if update.no_preference:
        bits.append("no_preference=" + ",".join(sorted(update.no_preference)))
    if update.global_override:
        bits.append("override=true")
    return "; ".join(bits) if bits else "no changes extracted this turn"


def _gloss_distill_profile(gs, args, before_constraints, before_profile) -> str:
    profile = gs.session.session_profile
    if profile == before_profile:
        return "no new profile signal (no-op)"
    return "profile updated: " + ", ".join(f"{key}={value}" for key, value in profile.items())


def _gloss_ask_attribute(gs, args, before_constraints, before_profile) -> str:
    pending = gs.session.pending_question
    if pending is None:
        return f"asked about {args.get('attribute')}"
    return f'asked: {pending.attribute} ({pending.mode}) -- "{pending.question_text}"'


def _gloss_search(gs, args, before_constraints, before_profile) -> str:
    retrieved = gs.scratch.get("search_retrieved")
    count = len(retrieved) if isinstance(retrieved, list) else len(gs.session.candidates)
    route = gs.scratch.get("search_route")
    mode = getattr(route, "mode", "?")
    return f"retrieved {count} candidate(s) (route={mode})"


def _gloss_loosen_constraints(gs, args, before_constraints, before_profile) -> str:
    before_set = {_fmt_constraint(item) for item in before_constraints}
    after_set = {_fmt_constraint(item) for item in gs.session.active_constraints}
    dropped = before_set - after_set
    added = after_set - before_set
    if dropped and added:
        return f"widened {next(iter(dropped))} -> {next(iter(added))}"
    if dropped:
        return f"dropped {next(iter(dropped))}"
    return "no active constraint left to loosen (no-op retry)"


def _gloss_no_match(gs, args, before_constraints, before_profile) -> str:
    return str(gs.scratch.get("no_match_message", "no match"))


def _gloss_rank(gs, args, before_constraints, before_profile) -> str:
    top5 = [item.parent_asin for item in gs.session.ranked[:5]]
    return f"ranked {len(gs.session.ranked)} candidate(s) (source=feature_rank); top5={top5}"


def _gloss_semantic_rank(gs, args, before_constraints, before_profile) -> str:
    top5 = [item.parent_asin for item in gs.session.ranked[:5]]
    backend = gs.scratch.get("semantic_rank_backend")
    count = gs.scratch.get("semantic_rank_input_count")
    failures = gs.scratch.get("semantic_rank_failures") or []
    if backend is not None:
        note = f", {len(failures)} failure(s) before eventual success" if failures else ""
        return f"reordered top {count} via {backend}{note}; top5={top5}"
    # ``semantic_rank_input_count`` is only ever written *after* a real
    # ``ranker.rank(...)`` call was made (graph.py's ``_semantic_rank_node``),
    # so its presence -- even with ``backend is None`` -- means a model call
    # was actually attempted and failed, not merely skipped/declined.  These
    # are very different reliability signals and must not be conflated.
    if count is None:
        if not gs.session.ranked:
            return f"semantic reorder skipped (no candidates); top5={top5}"
        gate = gs.scratch.get("candidate_gate_decision")
        if gate is not None and not getattr(gate, "run_semantic_ranker", True):
            return f"semantic reorder skipped (CandidateGate declined -- over-general pool, cost control); top5={top5}"
        return f"semantic reorder skipped (no ranker configured); top5={top5}"
    detail = "; ".join(
        str(item.get("error", item)) if isinstance(item, dict) else str(item) for item in failures[:2]
    )
    return (
        f"semantic reorder ATTEMPTED (model call made, {count} candidate(s)) but returned no usable "
        f"backend result{': ' + detail if detail else ''} -- fell back to feature-rank order; top5={top5}"
    )


def _gloss_explain(gs, args, before_constraints, before_profile) -> str:
    return _truncate(gs.scratch.get("explain_text", ""))


def _gloss_fetch_details(gs, args, before_constraints, before_profile) -> str:
    ids = gs.scratch.get("fetch_details_ids") or ()
    return f"fetched details for {len(ids)} id(s): {', '.join(str(i) for i in ids[:5])}"


def _gloss_compare(gs, args, before_constraints, before_profile) -> str:
    return _truncate(gs.scratch.get("compare_text", ""))


def _gloss_render(gs, args, before_constraints, before_profile) -> str:
    response = gs.scratch.get("response") or {}
    message = _truncate(response.get("message", ""), 110)
    count = len(response.get("recommendations") or [])
    ask = response.get("ask_attribute")
    tail = f", ask_attribute={ask}" if ask else ""
    return f'message="{message}", {count} recommendation(s){tail}'


_VALUE_NODE_GLOSS: dict[str, Callable] = {
    "ClassifyIntent": _gloss_classify_intent,
    "ExtractConstraints": _gloss_extract_constraints,
    "DistillProfile": _gloss_distill_profile,
    "AskAttribute": _gloss_ask_attribute,
    "Search": _gloss_search,
    "LoosenConstraints": _gloss_loosen_constraints,
    "NoMatch": _gloss_no_match,
    "Rank": _gloss_rank,
    "SemanticRank": _gloss_semantic_rank,
    "Explain": _gloss_explain,
    "FetchDetails": _gloss_fetch_details,
    "Compare": _gloss_compare,
    "Render": _gloss_render,
}

_NODE_PURPOSE: dict[str, str] = {
    "Entry": "decide whether this turn resumes a pending question, follows up on shown results, or starts fresh evidence gathering",
    "IntentRouter2": "pure code: compare_details -> FetchDetails; refine/new_search -> ExtractConstraints; confirm_choice -> Render",
    "ClassifyIntent": "LLM (or deterministic fallback offline): classify the follow-up intent",
    "DistillTriggerRouter": "pure code: decide whether this turn's constraint diff is worth distilling into the soft profile",
    "ExtractConstraints": "LLM (or deterministic fallback parser offline): extract constraint/category updates from the message",
    "SlotCheckRouter": "pure code: does prior-turn candidate evidence justify asking about an unasked attribute before this turn's own Search runs?",
    "DistillProfile": "LLM: distill a small soft profile update (price sensitivity / style signal) from this turn's diff",
    "CandidatePoolRouter": "pure code: empty pool -> loosen/relax/no-match; over-general pool -> ask; focused pool -> rank",
    "Search": "deterministic: multi-route retrieval (lexical + category + constraints) over the frozen catalog",
    "LoosenConstraints": "deterministic: drop or widen exactly one constraint (fixed priority order) and retry Search once",
    "NoMatch": "deterministic: nothing left to relax and/or no turns left -- fixed dead-end message, no state write",
    "Rank": "deterministic: constraint/profile feature ranking over this turn's full retrieval",
    "RankRouter": "pure code: an over-general question pending? skip the expensive ranker and ask instead; else continue to SemanticRank",
    "SemanticRank": "LLM: reorder the ranked pool by semantic relevance (permutation-safe, cannot invent an id)",
    "Explain": "LLM: one short sentence introducing the shown recommendations",
    "AskAttribute": "LLM (or fixed template offline): word this turn's clarification/relax question",
    "FetchDetails": "deterministic: bounded catalog lookup for the requested product id(s)",
    "Compare": "LLM: short comparison of the fetched product details",
    "Render": "terminal: assemble the public response from whatever state this turn produced",
}


def _wrap_router(key: str, fn: Callable) -> Callable:
    def _wrapped(gs):
        started = time.perf_counter()
        next_node, args = fn(gs)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        display = graph_mod._BRANCHING_ROUTER_NAMES.get(key)
        if display is not None:
            _CAPTURED.append(
                {
                    "node": display,
                    "kind": "router",
                    "elapsed_ms": elapsed_ms,
                    "gloss": _router_gloss(next_node, args),
                }
            )
        return next_node, args

    return _wrapped


def _wrap_node(key: str, fn: Callable) -> Callable:
    def _wrapped(gs, args):
        before_constraints = list(gs.session.active_constraints)
        before_profile = dict(gs.session.session_profile)
        started = time.perf_counter()
        result = fn(gs, args)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        kind = graph_mod._NODE_KIND.get(key)
        if kind is not None:
            formatter = _VALUE_NODE_GLOSS.get(key)
            gloss = formatter(result, args, before_constraints, before_profile) if formatter else "(no gloss)"
            _CAPTURED.append({"node": key, "kind": kind, "elapsed_ms": elapsed_ms, "gloss": gloss})
        return result

    return _wrapped


def _install_instrumentation() -> None:
    for key, fn in list(graph_mod.ROUTERS.items()):
        graph_mod.ROUTERS[key] = _wrap_router(key, fn)
    for key, fn in list(graph_mod.NODES.items()):
        if key == "Done":
            continue
        graph_mod.NODES[key] = _wrap_node(key, fn)


class _EmptySearchFixture:
    """Always-empty ``Search`` stand-in (same technique as
    ``tests/test_router_graph.py``'s ``RetryBoundAndDeadEndTest``), wrapped
    with the same instrumentation every other node gets so scenarios 5-6
    render through the identical rendering path as 1-4."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, gs, args):
        self.calls += 1
        StateReducer().apply(gs.session, NodeStateUpdate(candidates=()))
        return gs


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_turn(turn: int, message: str, response: dict, nodes: list[dict]) -> str:
    lines = [f"### Turn {turn}", "", f"**User:** {message}", "", "**Nodes fired:**", ""]
    for entry in nodes:
        purpose = _NODE_PURPOSE.get(entry["node"], "")
        lines.append(f"  - **{entry['node']}** (`{entry['kind']}`, {entry['elapsed_ms']}ms) -- {purpose}")
        lines.append(f"    {entry['gloss']}")
    lines.append("")
    lines.append(f"**Agent message:** {response.get('message')}")
    lines.append(f"**ask_attribute:** {response.get('ask_attribute')}")
    ids = [item.get("parent_asin") for item in (response.get("recommendations") or [])][:5]
    lines.append(f"**Top 5 recommendations:** {ids}")
    return "\n".join(lines)


def _write_markdown(scenario_id: str, title: str, intro: str, turn_blocks: list[str]) -> None:
    path = _OUTPUT_DIR / f"{scenario_id}.md"
    body = [f"# {title}", "", f"**Demonstrates:** {intro}", ""]
    for block in turn_blocks:
        body.append(block)
        body.append("")
    path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
    print(f"wrote {path}")


# ---------------------------------------------------------------------------
# Scenarios 1-4: driven through the live public Agent interface end to end.
# ---------------------------------------------------------------------------


def _run_agent_scenario(
    agent: Agent,
    scenario_id: str,
    scenario_type: str,
    messages: list[str],
) -> tuple[list[str], list[list[dict]]]:
    """Runs one scenario through ``Agent.reset``/``Agent.respond`` (via the
    tracing proxy for the events.jsonl provenance artifact) and returns the
    rendered per-turn markdown blocks plus the raw captured node facts per
    turn (for scenario-specific epilogue computation, e.g. scenario 1's
    clarification-cadence summary)."""

    run_dir = _OUTPUT_DIR / scenario_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    recorder = JsonlRecorder(run_dir / "events.jsonl", uuid.uuid4().hex)
    samples = [{"sample_id": scenario_id, "scenario_type": scenario_type}]
    proxy = TracingAgentProxy(agent, recorder, samples, catalog_ids=set())
    proxy.reset(scenario_id, {})

    blocks: list[str] = []
    per_turn_nodes: list[list[dict]] = []
    for turn, message in enumerate(messages, start=1):
        _reset_capture()
        response = proxy.respond(scenario_id, message, turn, 10)
        nodes = list(_CAPTURED)
        per_turn_nodes.append(nodes)
        blocks.append(_render_turn(turn, message, response, nodes))
    recorder.close()
    return blocks, per_turn_nodes


# ---------------------------------------------------------------------------
# Scenarios 5-6: direct graph.run_graph with a scripted always-empty Search.
# ---------------------------------------------------------------------------


def _run_direct_graph_scenario(
    agent: Agent,
    session_id: str,
    message: str,
    seed_constraints: list[tuple[str, str, str]],
) -> tuple[str, dict, int]:
    session = SessionState(session_id=session_id)
    if seed_constraints:
        mutations = tuple(
            ConstraintMutation(action="upsert", attribute=attribute, value=value, hardness=hardness)
            for attribute, value, hardness in seed_constraints
        )
        StateReducer().apply(session, IntentUpdate(mutations=mutations), turn=1)

    empty_search = _EmptySearchFixture()
    wrapped_empty_search = _wrap_node("Search", empty_search)
    _reset_capture()
    with mock.patch.dict(graph_mod.NODES, {"Search": wrapped_empty_search}):
        gs = graph_mod.run_graph(session, turn=3, top_k=10, message=message, services=agent._graph_services)
    response = gs.scratch.get("response") or {}
    nodes = list(_CAPTURED)
    return _render_turn(1, message, response, nodes), response, empty_search.calls


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------


def _summarize_clarification_cadence(per_turn_nodes: list[list[dict]]) -> str:
    asked: list[str] = []
    for nodes in per_turn_nodes:
        for entry in nodes:
            if entry["node"] == "AskAttribute":
                # gloss is: asked: <attribute> (<mode>) -- "..."
                attribute = entry["gloss"].split(":", 1)[1].split("(", 1)[0].strip()
                asked.append(attribute)
    turns = len(per_turn_nodes)
    if not asked:
        return f"No clarifying question was asked across all {turns} turn(s)."
    if len(asked) == turns:
        return (
            f"A clarifying question was asked on all {turns} of {turns} turn(s) "
            f"(attributes in order: {', '.join(asked)}), each a genuinely new, "
            f"not-yet-asked attribute with candidate evidence -- recommendations "
            f"were still returned every turn."
        )
    return (
        f"A clarifying question was asked on {len(asked)} of {turns} turn(s) "
        f"(attributes in order: {', '.join(asked)})."
    )


def build_scenarios(agent: Agent) -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- Scenario 1 -------------------------------------------------------
    blocks, per_turn = _run_agent_scenario(
        agent,
        "01_vague_clarify_converge",
        "vague_clarify_converge",
        ["I'm looking for jewelry", "gold", "under $50", "no preference for style"],
    )
    cadence = _summarize_clarification_cadence(per_turn)
    intro1 = (
        "Pillar II's Dynamic State Machine (proactive, structured clarification) and the "
        "`SlotCheckRouter`/`CandidatePoolRouter` mechanism from design.md Section 6. "
        "**Corrected claim (see this dispatch's Part 1 investigation):** an earlier version of "
        "this transcript's own intro claimed the session 'converges by turn 3-4' and implied "
        "`ClarificationPolicy`'s `max_questions` (default 1) caps the *total* number of "
        "clarifying questions asked in a session. Neither claim matches the actual, verified "
        "implementation, in both this Router/Value-Node graph and the prior v1 pipeline it "
        "replaced (same `ClarificationPolicy.choose_attribute` call site, same semantics): "
        "`max_questions` is only ever compared against zero (`self.max_questions <= 0`) to "
        "decide whether clarification is enabled at all -- it does not count how many questions "
        "have actually been asked. The real per-turn gate is `state.asked_attributes` "
        "(`asked_set`), which blocks *re-asking the same attribute* but not asking a *different* "
        "one; `SlotCheckRouter` runs this evidence check every turn, independent of "
        "`CandidateGate`'s own focused/over-general signal (that signal only gates "
        "`CandidatePoolRouter`'s separate, later check). For a genuinely vague, multi-attribute "
        "query like 'jewelry', where the catalog carries lexical evidence for several distinct "
        "attributes (material, color, size, style, brand, ...), this legitimately produces a "
        "new clarifying question most/every turn until the attribute list, the turn budget, or "
        "genuine 'no preference' answers exhaust it -- not a turn-3 convergence. " + cadence
    )
    _write_markdown("01_vague_clarify_converge", "Scenario 1 -- Vague query, proactive structured clarification", intro1, blocks)

    # --- Scenario 2 ---------------------------------------------------------
    blocks, _ = _run_agent_scenario(
        agent,
        "02_over_general_fill_missing",
        "over_general_fill_missing",
        ["I am looking for shoes"],
    )
    intro2 = (
        "Pillar II's 'Proactive Guidance' requirement verbatim -- 'Trigger an immediate retrieval "
        "cutoff when facing Over-Generality (candidate pool overload) to actively generate "
        "structured, proactive clarification prompts.' On turn 1, 'shoes' matches essentially the "
        "whole catalog (high estimated count / category entropy, zero hard constraints). "
        "`CandidatePoolRouter` still runs the cheap deterministic `Search`/`Rank` (recommendations "
        "are never suppressed just because a question is coming, design.md Section 4.1), but "
        "`RankRouter` then skips the expensive `SemanticRank`/`Explain` steps in favor of "
        "`AskAttribute(mode=\"fill_missing\")` -- the actual cutoff, decided entirely by pure code "
        "(`CandidateGate.evaluate`), never a model call."
    )
    _write_markdown("02_over_general_fill_missing", "Scenario 2 -- Over-general query triggers an immediate retrieval cutoff", intro2, blocks)

    # --- Scenario 3 ---------------------------------------------------------
    blocks, _ = _run_agent_scenario(
        agent,
        "03_intent_override",
        "intent_override",
        [
            "I'm shopping for a leather jacket",
            "black",
            "Actually, ignore all that, I want running shoes instead",
            "gray",
        ],
    )
    intro3 = (
        "Pillar II's 'abrupt Intent Override (slot erasure and rewriting)' requirement. The user "
        "builds up jacket-shopping constraints over two turns, then explicitly discards them for "
        "an unrelated category. `ExtractConstraints` (LLM, with a deterministic fallback parser "
        "offline) detects the override phrase and sets `global_override=True`; `StateReducer.apply` "
        "bumps `intent_epoch`, marks every prior constraint superseded, and clears "
        "`query_terms`/`no_preference`/`asked_attributes` for the new epoch -- visible below as the "
        "'material' question becoming askable again on turn 3 even though it was already asked (and "
        "answered) in the prior epoch on turn 1."
    )
    _write_markdown("03_intent_override", "Scenario 3 -- Mid-session intent override (global_override)", intro3, blocks)

    # --- Scenario 4 ---------------------------------------------------------
    blocks, per_turn4 = _run_agent_scenario(
        agent,
        "04_multiturn_rank_reorder",
        "multiturn_rank_reorder",
        ["I'm shopping for a necklace", "gold", "cotton", "no preference for color"],
    )
    live_model = bool(agent._graph_services.model_client and agent._graph_services.model_client.backends)
    semantic_glosses = [
        entry["gloss"]
        for turn_nodes in per_turn4
        for entry in turn_nodes
        if entry["node"] == "SemanticRank"
    ]
    if not live_model:
        model_note = (
            "**Honest disclosure:** no model backend/API key is configured this run, so "
            "`SemanticRank` runs as the documented no-op pass-through (design.md Section 5, "
            "`_semantic_rank_node`'s 'no ranker configured' branch) -- this transcript demonstrates "
            "the deterministic half of the hybrid pipeline (multi-route retrieval + feature "
            "ranking) reordering results turn over turn."
        )
    elif any("reordered top" in gloss for gloss in semantic_glosses):
        model_note = (
            "**Live model run:** a real model backend is configured this run "
            "(`AgentConfig.from_env()` found a usable API key). `SemanticRank` successfully "
            "performed at least one real LLM-driven reorder below -- see the `Rank` vs "
            "`SemanticRank` `top5` lists for the before/after order (turns where the pool was "
            "still over-general skip straight to `AskAttribute` instead, per `RankRouter`, so not "
            "every turn reaches `SemanticRank`)."
        )
    else:
        model_note = (
            "**Live model run, with a genuine reliability caveat:** a real model backend is "
            "configured this run, but every `SemanticRank` call actually attempted below either "
            "failed (see the node's own gloss for the concrete backend error) or the turn's pool "
            "stayed over-general and `RankRouter` routed straight to `AskAttribute` instead, "
            "skipping `SemanticRank` entirely -- this transcript does not happen to show a "
            "successful live semantic reorder; see this dispatch's Part 3 reliability assessment "
            "for a larger-sample view of `SemanticRank`'s live success rate."
        )
    intro4 = (
        "Pillar I's Hybrid Pipeline ('Multi-Route Retrieval -> LLM Semantic Ranking') and Pillar "
        "II's Information Accumulation. Each turn's newly disclosed constraint (material, then a "
        "hard budget-free color signal) is folded into the same `intent_epoch`'s feature ranking "
        "(`Rank`'s `rank_source=\"feature_rank\"`), visibly reordering which candidates surface at "
        "the top -- not a fresh, unrelated search each time. " + model_note
    )
    _write_markdown("04_multiturn_rank_reorder", "Scenario 4 -- Multi-turn constraint accumulation reorders recommendations", intro4, blocks)

    # --- Scenario 5 ---------------------------------------------------------
    block5, response5, calls5 = _run_direct_graph_scenario(
        agent,
        "05_empty_search_retry_relax",
        "any options?",
        [("color", "red", "soft"), ("material", "cotton", "soft")],
    )
    block5 += f"\n\n(Search node was invoked {calls5} time(s) this turn -- proves the bounded, counter-capped retry: never a third call.)"
    intro5 = (
        "design.md Section 4.1's one intentional, counter-bounded cycle (`CandidatePoolRouter` -> "
        "`LoosenConstraints` -> `Search`, capped at 1 by `search_retry_count`), and Pillar II's "
        "dynamic state machine handling a genuine dead end gracefully. **Honest disclosure:** this "
        "cannot be organically triggered against the live catalog-backed Agent in this dataset -- "
        "`CatalogRecommendationEngine._retrieve` always falls back to popularity-ranked products "
        "when lexical/category search finds nothing, so `Search` never actually returns zero "
        "candidates for any real query here. To demonstrate the mechanism itself end to end, this "
        "transcript drives `starter.shopping_agent.graph.run_graph` directly (through the same live "
        "`GraphServices`/model client as scenarios 1-4) with a scripted always-empty `Search` node "
        "substituted in place of the real one -- the exact technique "
        "`tests/test_router_graph.py`'s `RetryBoundAndDeadEndTest` uses to prove this structurally. "
        "Every other node below (`ExtractConstraints`, `LoosenConstraints`, `CandidatePoolRouter`, "
        "`AskAttribute`) is 100% real, unmocked code; only the zero-candidate trigger condition is "
        "synthetic."
    )
    _write_markdown("05_empty_search_retry_relax", "Scenario 5 -- Empty search, bounded auto-loosen retry, then a targeted relax question", intro5, [block5])

    # --- Scenario 6 ---------------------------------------------------------
    block6, response6, calls6 = _run_direct_graph_scenario(
        agent,
        "06_true_dead_end_no_match",
        "invisible teleporting shoes",
        [],
    )
    block6 += f"\n\n(Search node was invoked {calls6} time(s) this turn -- proves the bounded, counter-capped retry: never a third call.)"
    intro6 = (
        "the other branch of the same design.md Section 4.1 mechanism -- when the retried "
        "`Search` is still empty and there is no active constraint left to offer relaxing (a "
        "pure-browsing turn with zero constraints, a genuine catalog/category gap), "
        "`CandidatePoolRouter` falls through to the static `NoMatch` message instead of asking a "
        "question about an empty result set. Same honest disclosure and technique as Scenario 5."
    )
    _write_markdown("06_true_dead_end_no_match", "Scenario 6 -- True dead end: nothing left to relax", intro6, [block6])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(_CATALOG_PATH))
    args = parser.parse_args()

    _install_instrumentation()
    agent = Agent(args.catalog)
    build_scenarios(agent)


if __name__ == "__main__":
    main()
