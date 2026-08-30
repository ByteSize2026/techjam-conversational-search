"""Router/Value-Node graph scaffold (design.md Sections 1, 4, 6, 7, 9).

``08-28-agent-v2-router-value-node``: this module defines the static
``ROUTERS``/``NODES`` tables and the main loop shape.  Phase 3 proved the
graph *shape* is sound -- reachable, terminating, and matching the
Router/Value-Node discipline -- with every node stubbed to a fixed
placeholder.  Phase 4 wires the *deterministic* nodes/routers to their real
implementations (``ClarificationPolicy.choose_attribute`` in
``SlotCheckRouter``, ``CandidateGate.evaluate`` in ``CandidatePoolRouter``,
``CatalogRecommendationEngine._retrieve``/``_feature_rank`` in
``Search``/``Rank``, the catalog lookup in ``FetchDetails``, and constraint
loosening in ``LoosenConstraints``) via the new ``GraphServices`` bundle on
``GraphState`` -- see each node's docstring below.  Phase 5 wires the seven
LLM Value Nodes (``ClassifyIntent``, ``ExtractConstraints``,
``DistillProfile``, ``AskAttribute`` wording, ``SemanticRank``, ``Explain``,
``Compare``) to real model calls, all but ``SemanticRank`` routing through
the single shared retry-once-then-fallback helper in ``llm_nodes.py``
(design.md Section 5, Section 5.1) via the new ``GraphServices.model_client``
/``llm_nodes_enabled`` fields; ``SemanticRank`` reuses ``LLMSemanticRanker``
(``semantic_ranking.py``) directly through ``GraphServices.semantic_ranker``,
since that class already implements an equivalent always-safe
fallback-on-failure contract of its own (see ``_semantic_rank_node``).  See
design.md Section 1 for the core discipline both Router and Value-Node kinds
must hold to regardless of phase:

    A Router is pure code: it reads state and decides the next node's name
    plus its arguments.  It never calls a model and never writes a fact.
    A Value Node produces exactly one fact and writes it back through the
    single state-commit path (``StateReducer``).  It never decides what
    runs next.

``starter/agent.py``'s ``Agent.respond()`` calls ``run_graph`` (below) as its
only implementation (Phase 7); the pre-08-28 fixed pipeline this replaced
(formerly ``Agent._respond_impl``) was removed once Phase 8's regression
suite and scenario showcase confirmed the graph path stands on its own --
see ``AGENT_ARCHITECTURE.md`` for the full picture.

Node-naming note (design.md Section 1 pseudocode)
--------------------------------------------------
The main loop's exact shape is::

    node = "Entry"
    while node != "Done":
        node, args = ROUTERS[node](state)
        state = NODES[node](state, args)

Because ``ROUTERS`` is indexed by the *current* ``node`` value (the name of
whatever just ran, or the ``"Entry"`` sentinel) and immediately re-assigns
``node`` to the chosen destination, every node name that can ever be
executed needs a ``ROUTERS`` entry describing what follows it -- even value
nodes with only one possible successor (``SemanticRank`` always leads to
``Explain``, etc.).  Design.md Section 4 names five *branching* routers
(``Entry``, ``IntentRouter2``, ``DistillTriggerRouter``,
``SlotCheckRouter``, ``CandidatePoolRouter``); ``ROUTERS["Rank"]``
(``_rank_router``) is a sixth, added during implementation once acceptance
evidence showed ``AskAttribute`` cannot simply replace ``Rank`` (a question
must never suppress this turn's recommendations -- see
``_rank_router``/``_candidate_pool_router`` docstrings).  The remaining
entries are single-branch pass-throughs, kept as real ``ROUTERS`` entries
for uniformity rather than special-cased in the loop.

``IntentRouter2``, ``DistillTriggerRouter``, ``SlotCheckRouter``, and
``CandidatePoolRouter`` are the human-readable names design.md gives these
decisions.  Mechanically they live at ``ROUTERS["ClassifyIntent"]``,
``ROUTERS["ExtractConstraints"]``, ``ROUTERS["DistillProfile"]`` (its second
entry point; see ``_distill_trigger_router``'s "skip" branch for the first),
and ``ROUTERS["Search"]`` respectively -- the loop only ever indexes
``ROUTERS`` by the name of the node that just executed, never by a router's
own name.  The four names are additionally registered as aliases (pointing
at the exact same callables) purely so every one of design.md Section 4's
18 node-table rows has a literal, discoverable ``ROUTERS``/``NODES`` entry;
the main loop never looks them up under those aliases.

``NODE_TRANSITIONS`` is a separate, declarative edge table used only for
testing/documentation (design.md Section 9's "static graph test"): it lists
every ``ROUTERS[key]`` callable's *possible* outputs so the graph's shape
can be walked and asserted on without executing any node body.  ``run_graph``
cross-checks every real router decision against this table at runtime, so
the declarative table and the executable routers cannot silently drift
apart.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import Literal

from . import llm_nodes
from .model import TieredModelClient
from .policy import CandidateGate, ClarificationPolicy
from .state import (
    CandidateRef,
    CandidateStats,
    Constraint,
    ConstraintMutation,
    IntentUpdate,
    NodeStateUpdate,
    NodeTraceEntry,
    PendingQuestion,
    ProductDetail,
    RankedRef,
    SessionState,
    StateReducer,
    bind_clarification_answer,
    extract_category_hint,
    parse_intent_update,
)

# Generously above the longest legitimate path (Entry -> ExtractConstraints
# -> DistillProfile -> AskAttribute/Search -> ... -> Render is ~6-9 steps,
# even counting the one bounded LoosenConstraints<->Search retry).  This is
# a defensive fuse for a future bug that adds an unbounded cycle by mistake
# (design.md Section 7), not a tuned budget -- termination is actually
# guaranteed by ``search_retry_count`` capping the graph's one declared
# back-edge, checked independently below and in ``tests/test_router_graph``.
MAX_INTERNAL_STEPS = len(
    {
        "Entry", "ClassifyIntent", "ExtractConstraints", "DistillProfile",
        "AskAttribute", "Search", "LoosenConstraints", "NoMatch", "Rank",
        "SemanticRank", "Explain", "FetchDetails", "Compare", "Render",
    }
) * 3  # == 42


class GraphStepLimitExceeded(RuntimeError):
    """Raised when the defensive step fuse trips (design.md Section 7)."""


class UnexpectedGraphTransition(RuntimeError):
    """Raised when a Router returns a destination outside its declared set.

    ``NODE_TRANSITIONS`` is the static, testable edge table; this guard
    keeps runtime router behavior from silently drifting away from it.
    """


@dataclass(frozen=True)
class GraphServices:
    """External/catalog-backed dependencies the deterministic nodes need.

    Kept separate from ``GraphState`` (rather than module-level singletons)
    so ``Agent`` (Phase 7) can construct one ``GraphServices`` per catalog
    load and pass it into every ``run_graph`` call, while tests can inject a
    fixture catalog/tuned policy without monkeypatching module globals.
    ``recommendation_engine`` is ``None`` in every test that does not need
    real retrieval (design.md's node table stubs, the Phase 3 structural
    tests) -- ``Search``/``Rank``/``FetchDetails`` fall back to their
    Phase 3 placeholder behavior in that case, exactly as before.

    ``model_client``/``semantic_ranker`` back the seven LLM Value Nodes
    (Phase 5, design.md Section 5).  Both default to ``None``: an unset
    ``model_client`` is exactly the "no model configured" case
    ``llm_nodes.call_llm_value_node`` already treats identically to a
    schema-validation failure (design.md Section 10 -- "one fallback
    mechanism, not two"), and an unset ``semantic_ranker`` leaves
    ``SemanticRank`` a no-op reorder, matching its Phase 3 stub exactly.
    ``llm_nodes_enabled`` is the single flag design.md Section 10 asks for:
    set it ``False`` to force every LLM Value Node onto its deterministic
    fallback branch even when a real ``model_client``/``semantic_ranker`` is
    configured (ablation/offline-mode testing), without needing two
    different "no model" code paths.
    """

    recommendation_engine: object | None = None  # CatalogRecommendationEngine | None
    candidate_gate: CandidateGate = field(default_factory=CandidateGate)
    clarification_policy: ClarificationPolicy = field(default_factory=ClarificationPolicy)
    model_client: TieredModelClient | None = None
    semantic_ranker: object | None = None  # LLMSemanticRanker | None
    llm_nodes_enabled: bool = True

    def llm_client(self) -> TieredModelClient | None:
        """The model client every LLM Value Node (except ``SemanticRank``)
        calls through -- respects ``llm_nodes_enabled`` uniformly."""

        return self.model_client if self.llm_nodes_enabled else None

    def llm_semantic_ranker(self) -> object | None:
        """``SemanticRank``'s own model-backed dependency, gated the same
        way as ``llm_client()`` so the one flag covers both call paths."""

        return self.semantic_ranker if self.llm_nodes_enabled else None


DEFAULT_SERVICES = GraphServices()


@dataclass
class GraphState:
    """Bundles ``SessionState`` with this turn's transient, non-persisted
    inputs and scratch space.

    ``SessionState`` intentionally has no ``turn``/``top_k`` fields -- those
    are per-``respond()``-call arguments (design.md Section 3), not durable
    session facts.  ``scratch`` carries Value-Node outputs that the very
    next Router needs to read (e.g. ``ClassifyIntent``'s parsed intent for
    ``IntentRouter2``) but that do not belong in ``SessionState`` because
    they are single-turn-only and are never committed through
    ``StateReducer``.  Nothing here is a protected ``SessionState`` field,
    so writing to ``scratch`` does not need to go through the reducer.
    """

    session: SessionState
    turn: int
    top_k: int
    message: str = ""
    services: GraphServices = field(default_factory=lambda: DEFAULT_SERVICES)
    scratch: dict[str, object] = field(default_factory=dict)

    @property
    def remaining_turns(self) -> int:
        return max(10 - int(self.turn), 0)


Router = Callable[[GraphState], tuple[str, dict[str, object]]]
Node = Callable[[GraphState, dict[str, object]], GraphState]

_REDUCER = StateReducer()

# ---------------------------------------------------------------------------
# Declarative edge table (design.md Section 4/Section 9's "static graph
# test").  Every tuple lists the full set of destinations the corresponding
# ROUTERS[key] callable may return.  "Done" is the loop's terminal sentinel,
# not a node in NODES.
# ---------------------------------------------------------------------------
NODE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "Entry": ("ExtractConstraints", "ClassifyIntent"),
    # IntentRouter2's branches: compare_details / refine|new_search / confirm_choice
    "ClassifyIntent": ("FetchDetails", "ExtractConstraints", "Render"),
    # DistillTriggerRouter's branches, chained into SlotCheckRouter's own
    # (single) destination when distillation is skipped -- see
    # _distill_trigger_router.  SlotCheckRouter never routes directly to
    # AskAttribute (see _slot_check_router's docstring): a found attribute
    # is stashed in scratch for RankRouter, not used to skip Search.
    "ExtractConstraints": ("DistillProfile", "Search"),
    # SlotCheckRouter's second entry point, after distillation.
    "DistillProfile": ("Search",),
    "AskAttribute": ("Render",),
    # CandidatePoolRouter's branches (design.md Section 4.1).
    "Search": ("LoosenConstraints", "AskAttribute", "NoMatch", "Rank"),
    "LoosenConstraints": ("Search",),  # the one declared, counter-bounded back-edge
    "NoMatch": ("Render",),
    # RankRouter's branches (see ``_rank_router``): AskAttribute when
    # CandidatePoolRouter flagged an over-general question, else SemanticRank.
    "Rank": ("AskAttribute", "SemanticRank"),
    "SemanticRank": ("Explain",),
    "Explain": ("Render",),
    "FetchDetails": ("Compare",),
    "Compare": ("Render",),
    "Render": ("Done",),
}

# The one back-edge the static graph test allows a repeated node through.
DECLARED_BACK_EDGE: tuple[str, str] = ("LoosenConstraints", "Search")


# ---------------------------------------------------------------------------
# Node trace (design.md Section 8).  Every node execution appends one bounded
# ``NodeTraceEntry`` to ``state.node_trace`` through ``StateReducer`` -- see
# ``run_graph``'s main loop.  ``kind`` is knowable statically from which of
# the 18 design-table nodes fired:
#
# - The 5 design.md-named branching routers (``Entry``, ``IntentRouter2``,
#   ``DistillTriggerRouter``, ``SlotCheckRouter``, ``CandidatePoolRouter``)
#   plus ``RankRouter`` (``ROUTERS["Rank"]``, added during implementation --
#   see the module docstring's "Node-naming note") get a ``"router"`` entry,
#   keyed here by the ``ROUTERS`` dict key the main loop actually calls and
#   mapped to the human-readable name for the decision.  The other ``ROUTERS``
#   entries are trivial single-branch pass-throughs, so they are not traced
#   as a separate ``"router"`` step.
# - The 5 deterministic Value Nodes (``Search``, ``LoosenConstraints``,
#   ``NoMatch``, ``Rank``, ``FetchDetails``) plus the terminal ``Render``
#   node (deterministic code, no model call -- ``NodeTraceEntry.kind`` has no
#   separate "terminal" literal) get ``"value_node_deterministic"``.
# - The 7 LLM Value Nodes get ``"value_node_llm"``.
# ---------------------------------------------------------------------------
_BRANCHING_ROUTER_NAMES: dict[str, str] = {
    "Entry": "Entry",
    "ClassifyIntent": "IntentRouter2",
    "ExtractConstraints": "DistillTriggerRouter",
    "DistillProfile": "SlotCheckRouter",
    "Search": "CandidatePoolRouter",
    "Rank": "RankRouter",
}

_NODE_KIND: dict[str, Literal["value_node_deterministic", "value_node_llm"]] = {
    "ClassifyIntent": "value_node_llm",
    "ExtractConstraints": "value_node_llm",
    "DistillProfile": "value_node_llm",
    "AskAttribute": "value_node_llm",
    "Search": "value_node_deterministic",
    "LoosenConstraints": "value_node_deterministic",
    "NoMatch": "value_node_deterministic",
    "Rank": "value_node_deterministic",
    "SemanticRank": "value_node_llm",
    "Explain": "value_node_llm",
    "FetchDetails": "value_node_deterministic",
    "Compare": "value_node_llm",
    "Render": "value_node_deterministic",
}

# Bounded, matching the spirit of trace_runner._MAX_TEXT (2_000) but smaller
# since each turn can log up to ~2x the node count worth of entries and
# ``node_trace`` itself is also bounded (state.NODE_TRACE_LIMIT).
_TRACE_TEXT_LIMIT = 600


def _sanitize_for_trace(value: object) -> object:
    """Bound/redact a trace payload the same way ``evaluator.trace_runner``'s
    ``sanitize()`` already does (design.md Section 8) -- reused rather than
    reimplemented.

    Imported lazily (call-time, not module-level) so this starter-side
    module does not pick up a module-load-time dependency on the evaluator
    package: ``evaluator.trace_runner`` already imports ``starter.agent``,
    and once Phase 7 wires this module into ``starter.agent``, a
    module-level import here in the reverse direction would risk a circular
    import at import time.  A call-time import does not: by the time a node
    actually executes, both modules have already finished loading.
    """

    try:
        from evaluator.trace_runner import sanitize
    except ImportError:  # pragma: no cover - evaluator package always ships alongside starter
        return value
    return sanitize(value)


def _trace_text(value: object) -> str:
    sanitized = _sanitize_for_trace(value)
    try:
        text = json.dumps(sanitized, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = str(sanitized)
    return text[:_TRACE_TEXT_LIMIT]


def _state_signal(gs: GraphState) -> dict[str, object]:
    """Small, cheap-to-compute snapshot of the session facts most Router
    branches and Value Nodes actually read/write -- shared by both the
    input and output summaries below rather than re-derived per node.
    """

    session = gs.session
    return {
        "constraint_count": len(session.active_constraints),
        "candidate_count": len(session.candidates),
        "ranked_count": len(session.ranked),
        "search_retry_count": session.search_retry_count,
        "pending_attribute": session.pending_question.attribute if session.pending_question else None,
        "intent_epoch": session.intent_epoch,
    }


def _summarize_router_input(router_key: str, gs: GraphState) -> str:
    payload = {
        "router_key": router_key,
        "turn": gs.turn,
        "remaining_turns": gs.remaining_turns,
        "message": gs.message,
        "scratch": dict(gs.scratch),
        **_state_signal(gs),
    }
    return _trace_text(payload)


def _summarize_router_output(next_node: str, args: dict[str, object]) -> str:
    return _trace_text({"next_node": next_node, "args": args})


def _summarize_node_input(node_name: str, gs: GraphState, args: dict[str, object]) -> str:
    payload = {"node": node_name, "args": args, "message": gs.message}
    return _trace_text(payload)


def _summarize_node_output(node_name: str, gs: GraphState) -> str:
    payload = {"node": node_name, "scratch": dict(gs.scratch), **_state_signal(gs)}
    return _trace_text(payload)


def _checked(current: str, decision: tuple[str, str, dict[str, object]]) -> tuple[str, dict[str, object]]:
    """Guard a Router's runtime decision against its declared transition set."""

    router_name, next_node, args = decision
    allowed = NODE_TRANSITIONS.get(current, ())
    if next_node not in allowed:
        raise UnexpectedGraphTransition(
            f"{router_name} (ROUTERS[{current!r}]) returned {next_node!r}, "
            f"not one of the declared destinations {allowed!r}"
        )
    return next_node, args


# ---------------------------------------------------------------------------
# Routers -- pure code, no model calls, no state writes.
# ---------------------------------------------------------------------------

def _entry_router(gs: GraphState) -> tuple[str, dict[str, object]]:
    """design.md Section 4 (``Entry``) / Section 6's resume-after-question note.

    A turn that resumes after ``pending_question`` was asked bypasses
    ``ClassifyIntent`` and goes straight to ``ExtractConstraints`` -- the
    reply answers *that* question, not comments on shown results, so intent
    classification would be a redundant model call.
    """

    session = gs.session
    if session.pending_question is not None:
        next_node = "ExtractConstraints"
    elif session.seen_recommendations:
        next_node = "ClassifyIntent"
    else:
        next_node = "ExtractConstraints"
    return _checked("Entry", ("Entry", next_node, {}))


def _intent_router_2(gs: GraphState) -> tuple[str, dict[str, object]]:
    """design.md Section 4/Section 6 (``IntentRouter2``), lives at ROUTERS["ClassifyIntent"].

    Reads the ``ClassifyIntent`` output from ``gs.scratch`` -- still a fixed
    stub value until Phase 5 wires a real model call, but this Router's own
    branch dispatch is real: ``compare_details`` -> ``FetchDetails``;
    ``refine_search``/``new_search`` -> ``ExtractConstraints`` (with the
    override flag set for ``new_search`` so the following
    ``ExtractConstraints`` node -- once real, Phase 5 -- knows to treat this
    as ``IntentUpdate.global_override``); ``confirm_choice`` -> ``Render``.

    Design.md Section 6's ID-safety rule: never trust model-supplied ids
    blindly.  ``target_ids`` are dropped unless present in
    ``last_candidate_ids`` (this session's last shown recommendations) or
    already in ``details_cache`` (already-fetched detail entries) -- the
    same discipline v1 applied to tool-loop terminal IDs.
    """

    output = gs.scratch.get("classify_intent", {})
    intent = output.get("intent", "new_search") if isinstance(output, dict) else "new_search"
    args: dict[str, object] = {}
    if intent == "compare_details":
        raw_ids = output.get("target_ids", []) if isinstance(output, dict) else []
        known_ids = set(gs.session.last_candidate_ids) | set(gs.session.details_cache.keys())
        target_ids = tuple(
            str(parent_asin)
            for parent_asin in (raw_ids if isinstance(raw_ids, (list, tuple)) else ())
            if str(parent_asin) in known_ids
        )
        args = {"target_ids": target_ids}
        next_node = "FetchDetails"
    elif intent == "confirm_choice":
        next_node = "Render"
    else:  # refine_search, new_search
        next_node = "ExtractConstraints"
        if intent == "new_search":
            args = {"override": True}
    return _checked("ClassifyIntent", ("IntentRouter2", next_node, args))


def _slot_check_router(gs: GraphState) -> tuple[str, dict[str, object]]:
    """design.md Section 4/Section 6 (``SlotCheckRouter``).

    Reachable from two predecessors: directly after ``ExtractConstraints``
    when distillation is skipped, and after ``DistillProfile`` runs.  Kept
    as one shared function so both ``ROUTERS`` entries route through
    identical logic rather than duplicating it.

    This is the primary early-convergence path, not a turn-10 fallback
    (design.md Section 6): ``ClarificationPolicy.choose_attribute`` reused
    unchanged.  It returns ``None`` as soon as either the one-question budget
    is spent or no remaining attribute has evidence worth asking about --
    both can happen as early as turn 2-3.  Passing
    ``remaining_turns=gs.remaining_turns`` also applies design.md Section
    6's separate backstop (never ask a question that can't be answered in
    time) *inside* the same call, since ``choose_attribute`` already treats
    ``remaining_turns <= 0`` as an unconditional ``None`` -- no separate
    check is layered on top here.  Candidate evidence is whatever the
    *previous* turn's ``Search``/``Rank`` left in ``session.candidates`` /
    ``session.last_candidate_stats``; this turn's own ``Search`` has not run
    yet at this point in the graph.

    Always routes on to ``Search`` -- never directly to ``AskAttribute``.  A
    found attribute is stashed in ``gs.scratch["rank_ask_attribute"]`` (the
    same scratch key ``CandidatePoolRouter``'s over-general branch uses,
    read by ``RankRouter`` below) rather than short-circuiting the turn:
    asking a clarifying question must never suppress this turn's own fresh
    ``Search``/``Rank`` (and therefore recommendations/diagnostics) -- the
    former fixed pipeline always ranked *and* separately decided whether to
    also ask (see ``_candidate_pool_router``'s docstring for the fuller
    rationale and the acceptance evidence:
    ``test_agent_contract.test_no_preference_attribute_is_not_asked_again``,
    ``test_adaptive_category_recall.test_small_category_is_complete_and_large_category_uses_specificity_ratios``).
    ``CandidatePoolRouter`` runs after this turn's own ``Search`` and only
    *adds* its own (fresher-evidence) attribute on top; it never clears one
    already stashed here.

    ``session.candidates`` empty is treated as "no evidence exists yet" and
    skips the ``choose_attribute`` call entirely -- caught in Phase 7's
    facade integration (implement.md), the first point this router is
    exercised on a session's actual first turn (no prior ``Search`` has ever
    run).  ``choose_attribute``'s own "no evidence" behavior is to fall back
    to the first not-yet-excluded attribute in ``ATTRIBUTE_ORDER`` rather
    than ``None`` (it was written for its v1 call site, always *after* a
    ``Search``/``Rank`` had already run every turn, so a truly evidence-free
    call was never possible there).  Called here with an empty candidate
    pool, that fallback would ask a fixed, query-blind question -- e.g.
    "material" -- before this turn's own ``Search`` has even run,
    contradicting this router's own "no remaining attribute has enough
    evidence... to be worth asking" rule stated above.  Skipping the call on
    an empty pool keeps that rule true rather than silently asking a blind
    question on turn 1 of every session.
    """

    session = gs.session
    if session.candidates:
        attribute = gs.services.clarification_policy.choose_attribute(
            session,
            session.candidates,
            session.last_candidate_stats,
            turn=gs.turn,
            remaining_turns=gs.remaining_turns,
        )
        if attribute:
            gs.scratch["rank_ask_attribute"] = {"attribute": attribute, "mode": "fill_missing"}
    return "Search", {}


def _distill_trigger_router(gs: GraphState) -> tuple[str, dict[str, object]]:
    """design.md Section 4/Section 6 (``DistillTriggerRouter``), lives at ROUTERS["ExtractConstraints"].

    On the "skip distillation" branch, control passes straight to
    ``SlotCheckRouter`` without an intervening Value Node execution
    (design.md Section 4's "(commit)" row has no node of its own) -- so this
    function calls ``_slot_check_router`` directly, a plain pure-code call,
    not a model call or a state write.
    """

    changed = bool(gs.scratch.get("extract_constraints_changed", False))
    override = bool(gs.scratch.get("extract_constraints_override", False))
    if changed or override:
        next_node, args = "DistillProfile", {}
    else:
        next_node, args = _slot_check_router(gs)
    return _checked("ExtractConstraints", ("DistillTriggerRouter", next_node, args))


def _distill_profile_router(gs: GraphState) -> tuple[str, dict[str, object]]:
    """``ROUTERS["DistillProfile"]``: SlotCheckRouter's second entry point."""

    next_node, args = _slot_check_router(gs)
    return _checked("DistillProfile", ("SlotCheckRouter", next_node, args))


def _candidate_pool_router(gs: GraphState) -> tuple[str, dict[str, object]]:
    """design.md Section 4.1/Section 6 (``CandidatePoolRouter``), lives at ROUTERS["Search"].

    Branches (no model call on any path -- design.md Section 5.3):

    - empty pool, first attempt this turn (``search_retry_count == 0``) ->
      ``LoosenConstraints`` (bounded retry, design.md Section 4.1).
    - empty pool, already retried once, an active constraint remains (the
      one ``LoosenConstraints`` just touched is already gone from
      ``state.constraints``, so a plain scan over what's still active
      naturally skips it) and turns are left -> ``AskAttribute`` with
      ``mode="relax_conflict"``.
    - empty pool, nothing left to relax or no turns left -> ``NoMatch``.
    - non-empty pool -> always ``Rank`` (feature ranking, and therefore a
      populated ``session.ranked``/recommendations, never depends on whether
      a question ends up being asked -- the former fixed pipeline always
      ranked *and* separately decided whether to also ask; see
      ``test_qwen_reranker.test_candidate_gate_over_general_skips_qwen`` and
      ``test_agent_contract.test_no_preference_attribute_is_not_asked_again``
      for the acceptance evidence this mirrors).  When
      ``CandidateGate.evaluate`` reports over-general and a question is
      still affordable and useful (``ClarificationPolicy`` finds a real
      attribute to ask), that attribute/mode is stashed in
      ``gs.scratch["rank_ask_attribute"]`` for ``RankRouter`` (``ROUTERS["Rank"]``,
      below) to read *after* ``Rank`` has produced its ranked pool -- this is
      the "immediate retrieval cutoff" the problem statement asks for: the
      *expensive* ``SemanticRank``/``Explain`` steps are skipped in favor of
      ``AskAttribute``, not the cheap feature ranking that recommendations
      are built from.
    """

    session = gs.session
    if not session.candidates:
        if session.search_retry_count < 1:
            next_node, args = "LoosenConstraints", {}
        else:
            relax_attribute = next(
                (item.attribute for item in session.active_constraints), None
            )
            if relax_attribute and gs.remaining_turns > 0:
                next_node, args = "AskAttribute", {
                    "attribute": relax_attribute,
                    "mode": "relax_conflict",
                }
            else:
                next_node, args = "NoMatch", {}
    else:
        stats = session.last_candidate_stats
        context = session.runtime_context(
            turn=gs.turn,
            remaining_turns=gs.remaining_turns,
            route_hint=session.active_route,
            candidate_stats=stats,
        )
        gate = gs.services.candidate_gate.evaluate(stats, context, turn=gs.turn)
        # Diagnostics-only: read by the facade (Phase 7) to report the gate's
        # decision, exactly as the former fixed pipeline's ``gate``/
        # ``gate_reason``/``gate_retrieval_limit`` diagnostics did.  Not a
        # ``SessionState`` write and not consumed by any later Router/Node's
        # control-flow decision -- ``Rank`` receives the one field it
        # actually needs (``retrieval_limit``) through ``args`` below, same
        # as every other Router-to-Node argument.
        gs.scratch["candidate_gate_decision"] = gate
        attribute = None
        if gate.over_general and gs.remaining_turns > 0:
            attribute = gs.services.clarification_policy.choose_attribute(
                session,
                session.candidates,
                stats,
                turn=gs.turn,
                remaining_turns=gs.remaining_turns,
            )
        if attribute:
            # Fresher (this turn's own) evidence than whatever
            # SlotCheckRouter may have already stashed pre-Search -- always
            # takes priority, matching this branch's own over-general signal
            # having just been (re)computed.  When ``attribute`` is ``None``
            # here, deliberately leave any pre-Search ``SlotCheckRouter``
            # decision in place rather than clearing it (see
            # ``_slot_check_router``'s docstring): this router only ever
            # *adds* a reason to ask, never removes one.
            gs.scratch["rank_ask_attribute"] = {"attribute": attribute, "mode": "fill_missing"}
        effective_budget = _effective_budget(gs, gate)
        gs.scratch["rank_effective_budget"] = effective_budget
        next_node, args = "Rank", {"retrieval_limit": effective_budget}
    return _checked("Search", ("CandidatePoolRouter", next_node, args))


def _rank_router(gs: GraphState) -> tuple[str, dict[str, object]]:
    """``RankRouter``, lives at ``ROUTERS["Rank"]``.

    Not one of design.md Section 4's five named branching routers, but a
    genuine code-only decision all the same (design.md Section 1's
    discipline applies to any Router, named or not): reads the attribute
    ``CandidatePoolRouter`` already chose (``gs.scratch["rank_ask_attribute"]``,
    set purely from ``CandidateGate``/``ClarificationPolicy`` output, no
    model call here) and either sends control to ``AskAttribute`` -- skipping
    the model-backed ``SemanticRank``/``Explain`` steps, the actual "cutoff"
    the problem statement's Over-Generality guidance asks for -- or continues
    to ``SemanticRank`` as normal.  ``Rank`` itself (the Value Node) never
    makes this decision; it only ever produces the ranked pool.
    """

    pending = gs.scratch.get("rank_ask_attribute")
    if isinstance(pending, dict) and pending.get("attribute"):
        return _checked("Rank", ("RankRouter", "AskAttribute", dict(pending)))
    return _checked("Rank", ("RankRouter", "SemanticRank", {}))


def _effective_budget(gs: GraphState, gate: object) -> int:
    """``CandidatePoolRouter``'s cost boundary for ``Rank`` (design.md
    Section 5.3), mirroring the former fixed pipeline's ``effective_budget``
    exactly: a resolved category route already applied its own bounded
    quota (``adaptive_category_budget``), so the gate's cutoff must not
    additionally discard that already-quota'd pool before feature ranking
    even runs -- it only decides whether the *semantic* reorder step spends
    a model call (``_semantic_rank_node``'s own ``run_semantic_ranker``
    check).  Falls back to ``min(route_budget, gate_budget)`` -- the gate's
    own cutoff, capped by this turn's route budget -- for every other case
    (unresolved/ambiguous/unknown category anchor, or the adaptive recall
    feature disabled).
    """

    retrieval_diagnostics = gs.scratch.get("search_retrieval_diagnostics")
    category_resolved = (
        isinstance(retrieval_diagnostics, dict)
        and retrieval_diagnostics.get("category_resolution") in {"resolved", "resolved_union"}
    )
    engine = gs.services.recommendation_engine
    adaptive_enabled = bool(getattr(getattr(engine, "config", None), "adaptive_category_recall_enabled", True))
    retrieved = gs.scratch.get("search_retrieved")
    if category_resolved and adaptive_enabled and isinstance(retrieved, list):
        return len(retrieved)
    route = gs.scratch.get("search_route")
    route_budget = max(int(getattr(route, "retrieval_budget", 0)), 0) if route is not None else 0
    gate_budget = max(int(getattr(gate, "retrieval_limit", route_budget)), 0)
    return min(route_budget, gate_budget) if route is not None else gate_budget


def _fixed(current: str, router_name: str, next_node: str) -> Router:
    """Build a trivial, single-branch Router (still pure code, still checked
    against ``NODE_TRANSITIONS``) for the 8 Value-Node successors that have
    only one possible destination (``Rank`` is the ninth candidate but is a
    real, two-branch decision -- see ``_rank_router``).
    """

    def _router(gs: GraphState) -> tuple[str, dict[str, object]]:
        return _checked(current, (router_name, next_node, {}))

    _router.__name__ = f"router_{current}"
    return _router


ROUTERS: dict[str, Router] = {
    "Entry": _entry_router,
    "ClassifyIntent": _intent_router_2,
    "ExtractConstraints": _distill_trigger_router,
    "DistillProfile": _distill_profile_router,
    "AskAttribute": _fixed("AskAttribute", "AskAttribute", "Render"),
    "Search": _candidate_pool_router,
    "LoosenConstraints": _fixed("LoosenConstraints", "LoosenConstraints", "Search"),
    "NoMatch": _fixed("NoMatch", "NoMatch", "Render"),
    "Rank": _rank_router,
    "SemanticRank": _fixed("SemanticRank", "SemanticRank", "Explain"),
    "Explain": _fixed("Explain", "Explain", "Render"),
    "FetchDetails": _fixed("FetchDetails", "FetchDetails", "Compare"),
    "Compare": _fixed("Compare", "Compare", "Render"),
    "Render": _fixed("Render", "Render", "Done"),
    # Aliases: design.md Section 4 names these four routers directly.  Same
    # callables as their predecessor-keyed entries above; never looked up by
    # name in the main loop itself (see module docstring).
    "IntentRouter2": _intent_router_2,
    "DistillTriggerRouter": _distill_trigger_router,
    "SlotCheckRouter": _distill_profile_router,
    "CandidatePoolRouter": _candidate_pool_router,
}


# ---------------------------------------------------------------------------
# Value Nodes -- deterministic and LLM, all stubbed to fixed placeholder
# outputs (design.md Section 4).  Every write to a SessionState protected
# field goes through StateReducer.apply, matching state.py's single-writer
# contract (see tests/test_state_reducer.py).
# ---------------------------------------------------------------------------

# Conservative fallback when no model is usable or both attempts fail
# validation (design.md Section 5, item 2): "refine_search" is deliberately
# *not* "new_search" here -- IntentRouter2 sends "new_search" on to
# ExtractConstraints with ``global_override=True``, which would wipe every
# accumulated constraint on a plain model outage.  "refine_search" reaches
# the same ExtractConstraints node without that override, so a
# fallback-classified turn degrades to "reparse this message as more
# detail" rather than "silently discard everything the user already told
# us" -- the smaller, safer failure.
def _classify_intent_fallback() -> dict[str, object]:
    return {"intent": "refine_search", "target_ids": []}


def _classify_intent_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """LLM Value Node (design.md Section 5.5).  Single closed-enum
    classification; writes nothing to ``SessionState`` -- ``ClassifyIntent``'s
    only output is transient scratch for ``IntentRouter2`` (design.md
    Section 4/Section 6's model-supplied-ID distrust: ``IntentRouter2``, not
    this node, filters ``target_ids`` against known IDs).
    """

    session = gs.session
    payload = {
        "message": gs.message,
        "last_candidate_ids": list(session.last_candidate_ids)[:20],
    }
    output = llm_nodes.call_llm_value_node(
        gs.services.llm_client(),
        task_prompt=llm_nodes.CLASSIFY_INTENT_PROMPT,
        user_payload=payload,
        output_model=llm_nodes.ClassifyIntentOutput,
    )
    if output is not None:
        gs.scratch["classify_intent"] = {
            "intent": output.intent,
            "target_ids": list(output.target_ids),
        }
    else:
        gs.scratch["classify_intent"] = _classify_intent_fallback()
    return gs


def _extract_constraints_input(gs: GraphState, pending: PendingQuestion | None) -> dict[str, object]:
    """design.md Section 4's ``ExtractConstraints`` row: user message + last
    ~3 turns + current constraints -- never the full ``SessionState``.
    ``answering_attribute`` is included only on a resume-after-question turn
    (design.md Section 6's ``Entry`` note) so the model knows a bare reply
    like "black" is answering *that* attribute, not a fresh, unscoped claim.
    """

    session = gs.session
    payload: dict[str, object] = {
        "message": gs.message,
        "recent_turns": [item.user_message for item in session.messages[-3:]],
        "known_constraints": [
            {"attribute": item.attribute, "value": item.value, "hardness": item.hardness}
            for item in session.active_constraints[:10]
        ],
    }
    if pending is not None:
        payload["answering_attribute"] = pending.attribute
    return payload


def _extract_constraints_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """LLM Value Node (design.md Section 5.4).  Reuses ``IntentUpdate`` as
    both the LLM's output shape and the fallback parser's output shape, so
    exactly one code path (below) ever calls ``StateReducer.apply`` with it
    regardless of which produced it.

    Fallback on failure (design.md Section 5, item 2): the same keyword-only
    parser v1 used (``parse_intent_update``), plus ``bind_clarification_answer``
    when this turn is a resume-after-question turn -- a bare reply like
    "black" only becomes a ``color`` constraint if the parser is told which
    attribute it is answering.

    ``args["override"]`` (set by ``IntentRouter2`` on its ``new_search``
    branch, design.md Section 4/Section 6) is a Router decision, not
    something either the model or the fallback parser gets to make --
    forced onto whichever ``IntentUpdate`` resulted, exactly matching the
    Router/Value-Node discipline (design.md Section 1): a Value Node never
    decides control flow, but a Router-supplied argument may still shape a
    Value Node's *output*.
    """

    session = gs.session
    pending = session.pending_question
    override_forced = bool(args.get("override"))

    output = llm_nodes.call_llm_value_node(
        gs.services.llm_client(),
        task_prompt=llm_nodes.EXTRACT_CONSTRAINTS_PROMPT,
        user_payload=_extract_constraints_input(gs, pending),
        output_model=llm_nodes.ExtractConstraintsOutput,
    )
    if output is not None:
        update = llm_nodes.extract_constraints_output_to_intent_update(output, session=session)
        # The model's own field can come back null even on an otherwise
        # successful call (see ``extract_category_hint``'s docstring). Only
        # patch it in when the session has no anchor yet and the model
        # didn't supply one -- never overrides a real model answer. Mirrors
        # ``parse_intent_update``'s own ``if not override`` guard (state.py):
        # an override turn's phrasing is a swap, not a fresh category claim,
        # and StateReducer.apply already keeps whatever anchor is set through
        # an override -- this must not invent one from override wording.
        # ``override_forced`` is checked too since it flips ``global_override``
        # to true a few lines below regardless of what the model itself said.
        if (
            update.category_anchor is None
            and session.category_anchor is None
            and not update.global_override
            and not override_forced
        ):
            hint = extract_category_hint(gs.message)
            if hint:
                update = dataclass_replace(update, category_anchor=hint)
    else:
        update = parse_intent_update(gs.message, turn=gs.turn)
        if pending is not None:
            update = bind_clarification_answer(update, attribute=pending.attribute, message=gs.message)

    if override_forced and not update.global_override:
        update = dataclass_replace(update, global_override=True)

    # "Changed" gates DistillTriggerRouter (design.md Section 4/Section 6):
    # a turn that added/changed a constraint or recorded an explicit
    # rejection is informative; a turn that only re-affirmed existing
    # evidence or produced nothing new is not.
    changed = bool(update.mutations) or bool(update.no_preference) or bool(update.category_anchor)

    _REDUCER.apply(session, update, turn=gs.turn)
    if pending is not None:
        # design.md Section 3: cleared once the following turn's
        # ExtractConstraints runs -- this is that turn.
        _REDUCER.apply(session, NodeStateUpdate(pending_question=None))

    gs.scratch["extract_constraints_update"] = update
    gs.scratch["extract_constraints_changed"] = changed
    gs.scratch["extract_constraints_override"] = update.global_override
    return gs


def _distill_profile_input(gs: GraphState) -> dict[str, object]:
    """design.md Section 5.6's input triple: previous ``session_profile`` +
    this turn's ``IntentUpdate`` diff (from ``ExtractConstraints``'s scratch,
    not re-derived) + any rejection signal.  Never the full constraint set
    (already redundant with ``state.constraints``, per design.md Section
    5.6).  ``rejection_signal`` is inferred from ``ClassifyIntent``'s intent
    enum -- ``new_search`` is the one value that means "discard what I was
    just shown", the closest thing to an explicit rejection this graph's
    schemas carry (``ClassifyIntent``'s own schema, design.md Section 5.5,
    has no dedicated rejection field).
    """

    session = gs.session
    update = gs.scratch.get("extract_constraints_update")
    diff: dict[str, object] = {}
    if isinstance(update, IntentUpdate):
        diff = {
            "mutations": [
                {"attribute": item.attribute, "value": item.value, "action": item.action}
                for item in update.mutations
            ],
            "no_preference": sorted(update.no_preference),
            "category_anchor": update.category_anchor,
            "global_override": update.global_override,
        }
    classify_output = gs.scratch.get("classify_intent")
    rejection_signal = (
        isinstance(classify_output, dict) and classify_output.get("intent") == "new_search"
    )
    return {
        "previous_profile": dict(session.session_profile),
        "diff": diff,
        "rejection_signal": bool(rejection_signal),
    }


def _distill_profile_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """LLM Value Node (design.md Section 5.6).  Only reached when
    ``DistillTriggerRouter`` decided this turn actually changed something
    (see ``_distill_trigger_router``), so no separate no-op gate is needed
    here.  Deliberately shallow: two soft fields, not designed for
    extension (``prd.md`` Out of Scope).

    Fallback on failure (design.md Section 5, item 2): "nothing to
    distill" -- a no-op, same as the Phase 3 placeholder.  ``session_profile``
    is written through ``StateReducer`` only, and only when the merged
    result actually differs from what is already stored, so an all-``None``
    model response never becomes a spurious write.
    """

    output = llm_nodes.call_llm_value_node(
        gs.services.llm_client(),
        task_prompt=llm_nodes.DISTILL_PROFILE_PROMPT,
        user_payload=_distill_profile_input(gs),
        output_model=llm_nodes.DistillProfileOutput,
    )
    if output is None:
        return gs

    session = gs.session
    merged = dict(session.session_profile)
    if output.price_sensitivity:
        merged["price_sensitivity"] = output.price_sensitivity
    if output.style_signal:
        merged["style_signal"] = output.style_signal
    if merged != session.session_profile:
        _REDUCER.apply(session, NodeStateUpdate(session_profile=merged))
    return gs


# Attribute label overrides for a more natural sentence -- same convention
# ``starter/agent.py``'s v1 ``_message`` uses for its own template.
_ATTRIBUTE_LABELS: dict[str, str] = {
    "use_case": "intended use",
    "other": "anything else that matters",
}


def _attribute_label(attribute: str) -> str:
    return _ATTRIBUTE_LABELS.get(attribute, attribute.replace("_", " "))


def _ask_attribute_template(attribute: str, mode: str) -> str:
    """Deterministic fallback wording (design.md Section 4/Section 5, item
    2).  On the critical response path: must always succeed even with zero
    model configured, so this never itself calls out to anything that can
    fail.
    """

    label = _attribute_label(attribute)
    if mode == "relax_conflict":
        return f"I couldn't find a match with your current {label} requirement -- want to adjust it?"
    return f"Do you have a {label} preference?"


def _ask_attribute_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """LLM Value Node (design.md Section 4, wording-only).  The attribute and
    ``mode`` are already chosen by the calling Router (``SlotCheckRouter`` or
    ``CandidatePoolRouter``); this node's only job is the sentence itself.

    Fallback on failure (design.md Section 5, item 2): a fixed template per
    ``mode`` -- this node is on the critical response path (every
    ``AskAttribute`` turn needs *a* question), so it must always produce a
    usable ``question_text`` with zero model configured.
    """

    attribute = str(args.get("attribute") or "other")
    mode = "relax_conflict" if args.get("mode") == "relax_conflict" else "fill_missing"
    payload = {
        "attribute": attribute,
        "mode": mode,
        "known_constraints": [
            {"attribute": item.attribute, "value": item.value}
            for item in gs.session.active_constraints[:6]
        ],
    }
    task_prompt = (
        llm_nodes.ASK_ATTRIBUTE_RELAX_CONFLICT_PROMPT
        if mode == "relax_conflict"
        else llm_nodes.ASK_ATTRIBUTE_FILL_MISSING_PROMPT
    )
    output = llm_nodes.call_llm_value_node(
        gs.services.llm_client(),
        task_prompt=task_prompt,
        user_payload=payload,
        output_model=llm_nodes.AskAttributeOutput,
    )
    question_text = output.question_text.strip() if output is not None else ""
    if not question_text:
        question_text = _ask_attribute_template(attribute, mode)
    question = PendingQuestion(
        attribute=attribute,
        mode=mode,
        question_text=question_text,
        asked_turn=gs.turn,
    )
    _REDUCER.apply(gs.session, NodeStateUpdate(pending_question=question))
    # ``asked_attributes`` is not one of ``StateReducer``'s protected fields
    # (design.md's single-writer contract only covers the fields listed in
    # ``tests/test_state_reducer.py``'s ``PROTECTED_FIELDS``) -- the former
    # fixed pipeline (``Agent._respond_impl``) recorded this the same way,
    # directly on ``SessionState``, via ``state.record_asked(ask_attribute)``.
    # Recording it here (once per question actually asked, regardless of
    # ``mode``) is what lets ``ClarificationPolicy.choose_attribute``'s
    # ``attribute not in state.asked_set`` bound ever take effect for the
    # graph path -- without it, ``SlotCheckRouter``/``CandidatePoolRouter``
    # can re-select the same already-asked attribute every subsequent turn,
    # since evidence for it never disappears just because it was asked once.
    gs.session.record_asked(attribute)
    return gs


# ``state.candidates`` is deliberately compact evidence for Router judgment
# (design.md Section 3), not the full cheap-pool.  The full
# ``RetrievedProduct`` list ``_search_node`` retrieves stays in
# ``gs.scratch`` (single-turn-only) for ``Rank`` to feature-rank; only this
# many of the highest-scored candidates are also projected into the
# committed, cross-turn ``CandidateRef`` list.
_CANDIDATE_REF_LIMIT = 150


def _search_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """Deterministic Value Node (design.md Section 4).  Wraps
    ``CatalogRecommendationEngine._retrieve`` -- the extracted, unchanged
    multi-route recall implementation -- plus ``candidate_stats`` for
    ``CandidatePoolRouter``'s over-general judgment.

    Falls back to the Phase 3 placeholder (one fixed candidate, empty
    stats) when no ``recommendation_engine`` is configured (``services``
    default / catalog-free tests), keeping the graph runnable without a
    catalog exactly as before.
    """

    session = gs.session
    engine = gs.services.recommendation_engine
    if engine is None:
        candidate = CandidateRef(parent_asin="STUB0000001", summary="stub candidate", score=1.0)
        _REDUCER.apply(session, NodeStateUpdate(candidates=(candidate,)))
        session.last_candidate_stats = CandidateStats()
        session.active_route = "browsing"
        return gs

    context = session.runtime_context(turn=gs.turn, remaining_turns=gs.remaining_turns)
    route = engine.router.decide(context)
    session.active_route = route.mode
    retrieved = engine._retrieve(session, gs.message, route.retrieval_budget, route=route)
    stats = engine.candidate_stats(session, retrieved, gs.message)
    session.last_candidate_stats = stats
    # Transient: this turn's ``Rank`` reads the full objects; never
    # committed through StateReducer, cleared automatically next turn since
    # ``GraphState`` (and its ``scratch``) is rebuilt fresh per ``run_graph``
    # call.  ``search_route``/``search_retrieval_diagnostics`` are read only
    # by the facade (Phase 7, ``starter/agent.py``) to build its
    # ``last_diagnostics`` successor to the former fixed pipeline's route/
    # category-recall diagnostics -- the same data ``engine._retrieve`` has
    # always computed as a side effect (``_last_retrieval_diagnostics``),
    # just not previously surfaced from this node.
    gs.scratch["search_retrieved"] = retrieved
    gs.scratch["search_route"] = route
    gs.scratch["search_retrieval_diagnostics"] = dict(engine._last_retrieval_diagnostics)
    refs = tuple(
        CandidateRef(
            parent_asin=item.parent_asin,
            summary=item.product.title[:160],
            score=float(item.score),
        )
        for item in retrieved[:_CANDIDATE_REF_LIMIT]
    )
    _REDUCER.apply(session, NodeStateUpdate(candidates=refs))
    return gs


# Fixed, documented priority order for the one bounded auto-loosen attempt
# per turn (design.md Section 4.1): scanned when picking which *soft*
# preference to drop, or -- once no soft preference remains -- which *hard*
# constraint to drop.  ``budget`` is deliberately included here (a soft
# budget preference is exactly as droppable as any other soft attribute);
# a *hard* budget constraint is handled separately below by widening
# (downgrading to soft) rather than dropping, since a price band is more
# naturally "loosened" than an attribute like color or material.
_LOOSEN_ATTRIBUTE_ORDER = (
    "other",
    "feature",
    "style",
    "use_case",
    "brand",
    "material",
    "color",
    "size",
    "budget",
    "category",
)


def _pick_by_loosen_priority(items: Sequence[Constraint]) -> Constraint | None:
    if not items:
        return None
    return next(
        (item for attribute in _LOOSEN_ATTRIBUTE_ORDER for item in items if item.attribute == attribute),
        items[0],
    )


def _loosen_constraints_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """Deterministic Value Node (design.md Section 4.1).  Drops or widens
    exactly one constraint using the fixed priority above, then increments
    the bounded retry counter (capped at 1, enforced by
    ``StateReducer``/``CandidatePoolRouter`` -- see
    ``test_back_edge_guarded_by_retry_counter_cap``):

    1. If any *soft* preference is active, drop the highest-priority one
       (``ConstraintMutation(action="remove", ...)``).
    2. Else, if a *hard* budget constraint is active, widen it -- replace it
       with the same value at ``hardness="soft"`` rather than dropping it
       outright.
    3. Else, drop the highest-priority remaining *hard* constraint.
    4. If nothing is active at all, this is a no-op constraint-wise (still
       counts as the one bounded retry attempt).
    """

    session = gs.session
    active = session.active_constraints
    soft = [item for item in active if item.hardness == "soft"]
    mutation: ConstraintMutation | None = None
    target = _pick_by_loosen_priority(soft)
    if target is not None:
        # ``StateReducer.apply``'s mutation loop skips any mutation whose
        # ``value`` is empty (even for "remove", which otherwise only
        # matches by attribute) -- carry the constraint's own value along so
        # the drop is not silently ignored.
        mutation = ConstraintMutation(action="remove", attribute=target.attribute, value=target.value)
    else:
        hard = [item for item in active if item.hardness == "hard"]
        hard_budget = next((item for item in hard if item.attribute == "budget"), None)
        if hard_budget is not None:
            mutation = ConstraintMutation(
                action="replace",
                attribute="budget",
                value=hard_budget.value,
                hardness="soft",
                source="rule",
                confidence=hard_budget.confidence,
            )
        else:
            target = _pick_by_loosen_priority(hard)
            if target is not None:
                mutation = ConstraintMutation(action="remove", attribute=target.attribute, value=target.value)
    if mutation is not None:
        _REDUCER.apply(session, IntentUpdate(mutations=(mutation,)), turn=gs.turn)
    _REDUCER.apply(session, NodeStateUpdate(increment_search_retry=True))
    return gs


def _no_match_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """Deterministic Value Node (design.md Section 4.1).  Narrow true-dead-
    end fallback only -- reached when nothing is left to relax or turns are
    exhausted, not the default empty-result response.  Fixed message; no
    state write.
    """

    gs.scratch["no_match_message"] = (
        "No matches found for this search. Try a different category or rephrase your request."
    )
    return gs


def _rank_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """Deterministic Value Node (design.md Section 4).  Wraps
    ``CatalogRecommendationEngine._feature_rank`` -- the extracted,
    unchanged constraint/profile feature-ranking implementation -- over
    this turn's full retrieval (``gs.scratch["search_retrieved"]``, set by
    ``Search``).

    Falls back to a trivial rescore of the committed, compact
    ``session.candidates`` when no engine/full retrieval is available (no
    ``recommendation_engine`` configured, or a test that reached ``Rank``
    without going through the real ``Search`` node) -- the exact Phase 3
    placeholder behavior, so the default stub path is unchanged.

    ``args["retrieval_limit"]`` (``CandidatePoolRouter``'s forwarded
    ``CandidateGate`` cutoff, design.md Section 5.3) bounds how many of this
    turn's full retrieval enter feature ranking -- the same cost boundary
    the former fixed pipeline's ``effective_budget`` applied.  Falls back to
    ``_feature_rank``'s own default bound when absent (stub/test callers
    that reach ``Rank`` without going through ``CandidatePoolRouter``).
    """

    session = gs.session
    engine = gs.services.recommendation_engine
    retrieved = gs.scratch.get("search_retrieved")
    if engine is None or not isinstance(retrieved, list) or not retrieved:
        ranked = tuple(
            RankedRef(parent_asin=item.parent_asin, score=item.score, rank_source="stub_rank")
            for item in session.candidates
        )
        _REDUCER.apply(session, NodeStateUpdate(ranked=ranked))
        return gs

    context = session.runtime_context(
        turn=gs.turn,
        remaining_turns=gs.remaining_turns,
        route_hint=session.active_route,
        candidate_stats=session.last_candidate_stats,
    )
    raw_limit = args.get("retrieval_limit")
    limit = int(raw_limit) if isinstance(raw_limit, (int, float)) and not isinstance(raw_limit, bool) else None
    feature_ranked = engine._feature_rank(session, retrieved, context, limit=limit)
    ranked = tuple(
        RankedRef(parent_asin=item.parent_asin, score=float(item.score), rank_source="feature_rank")
        for item in feature_ranked
    )
    _REDUCER.apply(session, NodeStateUpdate(ranked=ranked))
    # Diagnostics-only, mirroring the former fixed pipeline's
    # ``feature_input_count`` (Phase 7 facade).
    gs.scratch["rank_feature_input_count"] = min(len(retrieved), limit) if limit is not None else len(retrieved)
    return gs


@dataclass(frozen=True)
class _SemanticRankCandidate:
    """Attribute-style view of one ``Rank`` output row, handed to
    ``ranker.rank(...)``.

    Every ranker this graph plugs in (``LLMSemanticRanker``,
    ``QwenCrossEncoderReranker``, and third-party test doubles matching the
    same convention the former fixed pipeline's ``_semantic_rank`` always
    used) expects attribute-style ``candidate.parent_asin`` access -- the
    same shape ``RetrievedProduct`` gave it pre-graph.  A plain dict payload
    still works for the two real rankers (their own ``_candidate_id``/
    ``_candidate_payload`` helpers are ``Mapping``-aware) but breaks any
    caller relying on plain attribute access, so this view keeps both
    working rather than silently narrowing the accepted candidate shape.
    ``compressed()`` gives the real rankers the same structured
    (parent_asin/score/summary) prompt payload a dict would have, instead of
    falling back to ``str(candidate)``.
    """

    parent_asin: str
    score: float
    summary: str

    def compressed(self) -> dict[str, object]:
        return {"parent_asin": self.parent_asin, "score": self.score, "summary": self.summary}


def _semantic_rank_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """LLM Value Node (design.md Section 5, reuses ``LLMSemanticRanker``
    directly rather than routing through ``llm_nodes.call_llm_value_node``:
    ``LLMSemanticRanker.rank`` already implements an equivalent
    always-succeeds contract of its own -- permutation-safe, cannot invent
    an id, and returns the original ``Rank`` order untouched on any model
    failure (``semantic_ranking.py``) -- so wrapping it in a second retry/
    fallback layer here would just duplicate that guarantee, not add one.

    No-op (``session.ranked`` left exactly as ``Rank`` produced it) when
    ``session.ranked`` is empty or no ``semantic_ranker`` is configured --
    matching the Phase 3/4 stub's behavior in both cases.

    ``LLMSemanticRanker.candidate_limit`` may be smaller than
    ``session.ranked``; any items past that bound are appended back in
    ``Rank``'s original order (``rank_source="rank"``) rather than silently
    dropped from the committed ordering.
    """

    session = gs.session
    if not session.ranked:
        return gs
    # ``CandidateGateDecision.run_semantic_ranker`` (design.md Section 5.3):
    # ``CandidatePoolRouter`` always routes ``Rank`` -> ``SemanticRank``
    # (design.md's static table has no branch here -- feature ranking always
    # runs even when the gate wants the model-backed reorder step skipped),
    # so this is where that cost-control decision is actually honored: a
    # no-op, matching the "no ranker configured" case exactly, when the gate
    # that already ran for this turn said not to spend a model call.  Read
    # from ``gs.scratch`` (stashed by ``CandidatePoolRouter``, not part of
    # ``SessionState``) rather than re-evaluating the gate here -- one
    # decision, one place it is made.
    gate_decision = gs.scratch.get("candidate_gate_decision")
    if gate_decision is not None and not getattr(gate_decision, "run_semantic_ranker", True):
        return gs
    ranker = gs.services.llm_semantic_ranker()
    if ranker is None:
        return gs

    # The semantic stage is a separate cost boundary from cheap feature
    # recall (design.md Section 5.3, mirroring the former fixed pipeline's
    # ``_semantic_rank`` exactly): keep it at Top-30 even if a caller
    # supplies a larger general ``candidate_limit`` or an injected ranker
    # advertises a wider input capacity.  Without this cutoff, a large
    # ``effective_budget`` (e.g. an adaptive-category-recall-resolved pool)
    # would otherwise be handed to the ranker in full.
    engine = gs.services.recommendation_engine
    config = getattr(engine, "config", None)
    config_candidate_limit = getattr(config, "candidate_limit", 30)
    try:
        config_candidate_limit = max(int(config_candidate_limit), 1)
    except (TypeError, ValueError):
        config_candidate_limit = 30
    ranker_limit = getattr(ranker, "candidate_limit", config_candidate_limit)
    try:
        ranker_limit = max(int(ranker_limit), 1)
    except (TypeError, ValueError):
        ranker_limit = config_candidate_limit
    effective_budget = gs.scratch.get("rank_effective_budget")
    try:
        effective_budget = int(effective_budget) if effective_budget is not None else len(session.ranked)
    except (TypeError, ValueError):
        effective_budget = len(session.ranked)
    semantic_limit = min(30, effective_budget, config_candidate_limit, ranker_limit)

    summary_by_id = {item.parent_asin: item.summary for item in session.candidates}
    score_by_id = {item.parent_asin: item.score for item in session.ranked}
    candidates_payload = [
        _SemanticRankCandidate(
            parent_asin=item.parent_asin,
            score=item.score,
            summary=summary_by_id.get(item.parent_asin, ""),
        )
        for item in session.ranked[: max(semantic_limit, 0)]
    ]
    intent_summary = {
        "message": gs.message,
        "constraints": [
            {"attribute": item.attribute, "value": item.value}
            for item in session.active_constraints[:10]
        ],
    }
    result = ranker.rank(intent_summary, candidates_payload)
    # Defensive ``getattr`` throughout (mirroring the former fixed
    # pipeline's ``_apply_semantic``/``_usage``/``_failures`` helpers):
    # ``SemanticRankingResult`` is the full/safe contract real rankers
    # return, but a caller-supplied ranker/test double is only required to
    # provide ``ordered_parent_asins`` -- the one field every ranking
    # contract in this codebase has always guaranteed.
    ordered_raw = getattr(result, "ordered_parent_asins", ())
    reordered_ids = list(ordered_raw) if isinstance(ordered_raw, (list, tuple)) else []
    seen_ids = set(reordered_ids)
    result_backend = getattr(result, "backend", None)
    result_scores = getattr(result, "scores", None)
    result_scores = result_scores if isinstance(result_scores, Mapping) else {}
    used_model = bool(getattr(result, "used_model", result_backend is not None))
    reordered = [
        RankedRef(
            parent_asin=parent_asin,
            score=float(result_scores.get(parent_asin, score_by_id.get(parent_asin, 0.0))),
            rank_source="semantic_rank" if used_model else "rank",
        )
        for parent_asin in reordered_ids
    ]
    reordered.extend(
        RankedRef(parent_asin=item.parent_asin, score=item.score, rank_source="rank")
        for item in session.ranked
        if item.parent_asin not in seen_ids
    )
    _REDUCER.apply(session, NodeStateUpdate(ranked=tuple(reordered)))
    # Transient, mirroring the "search_retrieved" convention above: the
    # facade (Phase 7, ``starter/agent.py``) reads this to populate the
    # public ``usage`` field and diagnostics exactly as the old fixed
    # pipeline's ``_semantic_rank``/``_usage`` did -- not part of
    # ``SessionState``, since it is single-turn-only and never committed
    # through ``StateReducer``.
    result_usage = getattr(result, "usage", None)
    if result_usage is not None and hasattr(result_usage, "as_dict"):
        result_usage = result_usage.as_dict()
    elif not isinstance(result_usage, Mapping):
        result_usage = None
    result_failures = getattr(result, "failures", ()) or ()
    gs.scratch["semantic_rank_backend"] = result_backend
    gs.scratch["semantic_rank_usage"] = result_usage
    gs.scratch["semantic_rank_failures"] = [
        failure.as_dict() if hasattr(failure, "as_dict") else failure for failure in result_failures
    ]
    gs.scratch["semantic_rank_input_count"] = len(candidates_payload)
    return gs


def _explain_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """LLM Value Node (generation only).  Writes transient scratch text,
    never a new product fact -- only titles ``Rank``/``SemanticRank`` already
    surfaced are supplied as evidence.

    Fallback on failure (design.md Section 5, item 2): the exact Phase 3
    placeholder sentence -- a safe, generic default that never claims
    anything the model would have had to invent.
    """

    session = gs.session
    summary_by_id = {item.parent_asin: item.summary for item in session.candidates}
    payload = {
        "message": gs.message,
        "results": [
            {"parent_asin": item.parent_asin, "title": summary_by_id.get(item.parent_asin, "")}
            for item in session.ranked[:5]
        ],
    }
    output = llm_nodes.call_llm_value_node(
        gs.services.llm_client(),
        task_prompt=llm_nodes.EXPLAIN_PROMPT,
        user_payload=payload,
        output_model=llm_nodes.ExplainOutput,
    )
    text = output.message.strip() if output is not None else ""
    gs.scratch["explain_text"] = text or "Here are some options that match what you're looking for."
    return gs


def _fetch_details_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """Deterministic Value Node (design.md Section 4).  Bounded projection:
    looks up only the (already ID-safety-filtered by ``IntentRouter2``)
    ``target_ids`` against the catalog and rejects any id the repository
    does not recognize.  Falls back to a placeholder detail entry when no
    ``recommendation_engine`` is configured, matching the Phase 3 stub.
    """

    target_ids = args.get("target_ids") or ()
    # Transient: ``Compare`` needs to know which of the (bounded,
    # cross-turn) ``details_cache`` entries belong to *this* turn's fetch --
    # ``ROUTERS["FetchDetails"]`` (a single-branch pass-through) does not
    # forward ``args`` on to ``Compare``, so this is scratch, not state.
    gs.scratch["fetch_details_ids"] = tuple(str(parent_asin) for parent_asin in target_ids)
    engine = gs.services.recommendation_engine
    if engine is None:
        details = tuple(
            ProductDetail(parent_asin=str(parent_asin), title="[stub] details unavailable")
            for parent_asin in target_ids
        )
    else:
        records = engine.repository.materialize(target_ids, max(len(target_ids), 1))
        details = tuple(
            ProductDetail(
                parent_asin=record.parent_asin,
                title=record.title,
                categories=record.categories,
                features=record.features,
                description=record.description,
                details=dict(record.details),
                store=record.store,
                price=record.price,
                rating=record.rating,
                rating_count=record.rating_count,
            )
            for record in records
        )
    if details:
        _REDUCER.apply(gs.session, NodeStateUpdate(details=details))
    return gs


def _compare_template(details: Sequence[ProductDetail]) -> str:
    """Deterministic fallback wording (design.md Section 5, item 2)."""

    if not details:
        return "I couldn't find details to compare right now."
    parts: list[str] = []
    for item in details[:3]:
        bits = [item.title or item.parent_asin]
        if item.price is not None:
            bits.append(f"${item.price:.2f}")
        if item.rating is not None:
            bits.append(f"{item.rating:.1f} stars")
        parts.append(" - ".join(bits))
    return "Here's what I found: " + "; ".join(parts)


def _compare_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """LLM Value Node (generation only).  Reads only the ``details_cache``
    entries ``FetchDetails`` fetched *this* turn (via
    ``gs.scratch["fetch_details_ids"]``), not the whole bounded cache, so a
    stale detail entry from an earlier turn's comparison never leaks in.
    """

    session = gs.session
    target_ids = gs.scratch.get("fetch_details_ids") or ()
    details = [session.details_cache[pid] for pid in target_ids if pid in session.details_cache]
    payload = {
        "message": gs.message,
        "products": [
            {
                "parent_asin": item.parent_asin,
                "title": item.title,
                "price": item.price,
                "rating": item.rating,
                "features": list(item.features[:5]),
            }
            for item in details
        ],
    }
    output = llm_nodes.call_llm_value_node(
        gs.services.llm_client(),
        task_prompt=llm_nodes.COMPARE_PROMPT,
        user_payload=payload,
        output_model=llm_nodes.CompareOutput,
    )
    text = output.message.strip() if output is not None else ""
    gs.scratch["compare_text"] = text or _compare_template(details)
    return gs


def _render_node(gs: GraphState, args: dict[str, object]) -> GraphState:
    """Terminal node.  Builds the turn's response from whatever state the
    graph accumulated; loosely mirrors the existing public response schema
    for forward compatibility, though nothing reads this yet (Phase 7 wires
    ``starter/agent.py``).
    """

    session = gs.session
    ids = [item.parent_asin for item in session.ranked][: max(gs.top_k, 0)]
    ask_attribute = session.pending_question.attribute if session.pending_question else None
    gs.scratch["response"] = {
        "message": (session.pending_question.question_text if session.pending_question else None)
        or gs.scratch.get("compare_text")
        or gs.scratch.get("explain_text")
        or gs.scratch.get("no_match_message")
        or "Here are the closest matches I found.",
        "ask_attribute": ask_attribute,
        "recommendations": [{"parent_asin": parent_asin} for parent_asin in ids],
    }
    # ``last_candidate_ids``/``recommendations_by_epoch`` (the latter via
    # ``record_recommendations``) are, like ``asked_attributes`` above,
    # outside ``StateReducer``'s protected-field scope -- the former fixed
    # pipeline (``Agent._respond_impl``) wrote them the same direct way.
    # Committing what this turn actually showed the user is what lets
    # ``session.seen_recommendations``/``last_candidate_ids`` mean anything
    # on the *next* turn: ``_entry_router``'s "recommendations already shown
    # this epoch -> ClassifyIntent" branch and ``_intent_router_2``'s
    # model-supplied-``target_ids`` safety filter (design.md Section 6) both
    # read these, and ``_feature_rank``'s already-seen penalty/diversify
    # step (``recommendation.py``) reads ``seen_recommendations`` too --
    # without this commit all three silently degrade to "nothing has ever
    # been shown," even on turn 5 of a session that has shown 40 products.
    session.last_candidate_ids = list(ids)
    session.record_recommendations(ids)
    return gs


NODES: dict[str, Node] = {
    "ClassifyIntent": _classify_intent_node,
    "ExtractConstraints": _extract_constraints_node,
    "DistillProfile": _distill_profile_node,
    "AskAttribute": _ask_attribute_node,
    "Search": _search_node,
    "LoosenConstraints": _loosen_constraints_node,
    "NoMatch": _no_match_node,
    "Rank": _rank_node,
    "SemanticRank": _semantic_rank_node,
    "Explain": _explain_node,
    "FetchDetails": _fetch_details_node,
    "Compare": _compare_node,
    "Render": _render_node,
    # Literal fidelity to design.md Section 1's pseudocode: after
    # ROUTERS["Render"] sets node = "Done", the loop body (as written)
    # unconditionally calls NODES["Done"] once before the while-condition
    # is re-checked.  A pure identity no-op keeps that call harmless.
    "Done": lambda gs, args: gs,
}


def run_graph(
    session: SessionState,
    *,
    turn: int,
    top_k: int,
    message: str = "",
    services: GraphServices | None = None,
) -> GraphState:
    """Execute one official turn's Router/Value-Node walk (design.md Section 1).

    Not yet called by ``starter/agent.py`` (Phase 7).  Mirrors the exact
    loop shape from design.md Section 1: a Router decides the next node's
    name and args; that node executes and returns updated state; the loop
    owns termination unconditionally.

    ``services`` defaults to ``DEFAULT_SERVICES`` (no catalog): every
    deterministic node that needs a catalog falls back to its Phase 3
    placeholder behavior in that case, so the graph remains runnable without
    one, exactly as before.
    """

    gs = GraphState(
        session=session, turn=turn, top_k=top_k, message=message, services=services or DEFAULT_SERVICES
    )
    # design.md Section 8: bounded per-turn trace, cleared at the start of
    # each official turn (this call), not mid-turn -- one reset, before any
    # Router/Value-Node has run.  ``search_retry_count`` is reset the same
    # way (design.md Section 3: "reset to 0 at the start of every official
    # turn") -- caught here in Phase 7, the first point at which
    # ``run_graph`` is exercised across more than one official turn on the
    # same session: without this, a turn that used its one bounded
    # LoosenConstraints retry would silently deny every later turn that
    # same retry, since Phase 3-6 tests only ever called ``NODES``/``ROUTERS``
    # directly (never ``run_graph`` twice on one session) and so never
    # observed the counter surviving across turns.
    _REDUCER.apply(gs.session, NodeStateUpdate(reset_node_trace=True, reset_search_retry=True))
    node = "Entry"
    steps = 0
    trace_step = 0
    while node != "Done":
        steps += 1
        if steps > MAX_INTERNAL_STEPS:
            raise GraphStepLimitExceeded(
                f"graph exceeded {MAX_INTERNAL_STEPS} internal steps without reaching Done "
                f"(design.md Section 7 defensive fuse; last node={node!r})"
            )
        current = node
        router_started = time.perf_counter()
        next_node, args = ROUTERS[current](gs)
        router_elapsed_ms = (time.perf_counter() - router_started) * 1000.0
        router_name = _BRANCHING_ROUTER_NAMES.get(current)
        if router_name is not None:
            trace_step += 1
            router_entry: NodeTraceEntry = {
                "step": trace_step,
                "node": router_name,
                "kind": "router",
                "input_summary": _summarize_router_input(current, gs),
                "output_summary": _summarize_router_output(next_node, args),
                "elapsed_ms": round(router_elapsed_ms, 3),
            }
            _REDUCER.apply(gs.session, NodeStateUpdate(append_node_trace=router_entry))

        node_kind = _NODE_KIND.get(next_node)
        node_input_summary = _summarize_node_input(next_node, gs, args) if node_kind is not None else ""
        node_started = time.perf_counter()
        gs = NODES[next_node](gs, args)
        node_elapsed_ms = (time.perf_counter() - node_started) * 1000.0
        if node_kind is not None:
            trace_step += 1
            node_entry: NodeTraceEntry = {
                "step": trace_step,
                "node": next_node,
                "kind": node_kind,
                "input_summary": node_input_summary,
                "output_summary": _summarize_node_output(next_node, gs),
                "elapsed_ms": round(node_elapsed_ms, 3),
            }
            _REDUCER.apply(gs.session, NodeStateUpdate(append_node_trace=node_entry))
        node = next_node
    return gs


__all__ = [
    "DECLARED_BACK_EDGE",
    "DEFAULT_SERVICES",
    "MAX_INTERNAL_STEPS",
    "NODES",
    "NODE_TRANSITIONS",
    "ROUTERS",
    "GraphServices",
    "GraphState",
    "GraphStepLimitExceeded",
    "Node",
    "Router",
    "UnexpectedGraphTransition",
    "run_graph",
]
