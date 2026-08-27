"""Offline-first public facade for the shopping agent."""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from .shopping_agent.catalog import (
    CatalogRepository,
    ProductRecord,
    RetrievedProduct,
    safe_terms,
)
from .shopping_agent.config import AgentConfig
from .shopping_agent.model import TieredModelClient
from .shopping_agent.orchestrator import ActionOrchestrator
from .shopping_agent.planner import ModelActionPlanner
from .shopping_agent.policy import (
    CandidateGate,
    ClarificationPolicy,
    IntentRouter,
    RouteDecision,
    adaptive_category_budget,
)
from .shopping_agent.qwen_reranker import QwenCrossEncoderReranker
from .shopping_agent.semantic_ranking import LLMSemanticRanker
from .shopping_agent.state import (
    ALLOWED_ATTRIBUTES,
    CandidateStats,
    RuntimeContext,
    SessionState,
    SessionStore,
    StateReducer,
    bind_clarification_answer,
    parse_intent_update,
)
from .shopping_agent.tools import ShoppingToolbox

ABSOLUTE_CAP = 600
NON_CATEGORY_TAIL = 100


class Agent:
    """Offline-first shopping Agent with an optional bounded action loop."""

    def __init__(
        self,
        catalog_path: str | Path = "data/catalog.jsonl",
        *,
        config: AgentConfig | None = None,
        repository: CatalogRepository | None = None,
        model_client: object | None = None,
        semantic_ranker: object | None = None,
        router: IntentRouter | None = None,
        candidate_gate: CandidateGate | None = None,
        clarification_policy: ClarificationPolicy | None = None,
        action_planner: object | None = None,
        action_orchestrator: ActionOrchestrator | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.config = config or AgentConfig.from_env()
        self.repository = repository or CatalogRepository(self.catalog_path)
        self.catalog = self.repository
        self.connection = self.repository.connection
        self.store = SessionStore()
        self.sessions = self.store.sessions
        self._sessions = self.sessions
        self.reducer = StateReducer()
        self.router = router or IntentRouter(retrieval_budget=self.config.retrieval_limit)
        self.candidate_gate = candidate_gate or CandidateGate()
        self.clarification_policy = clarification_policy or ClarificationPolicy()
        if semantic_ranker is not None:
            # Explicit injection remains the highest-priority integration hook
            # used by tests and benchmark replay.
            self.semantic_ranker = semantic_ranker
        elif model_client is not None:
            # Preserve the existing local/API model-client contract when a
            # caller supplies a client directly.
            self.semantic_ranker = LLMSemanticRanker(
                client=model_client,
                config=self.config,
                candidate_limit=self.config.candidate_limit,
            )
        elif getattr(self.config, "qwen_reranker_enabled", False):
            # Qwen is opt-in only when an explicit absolute local checkpoint
            # path is configured.  Construction is lazy and therefore does
            # not import sentence_transformers or load model assets here.
            self.semantic_ranker = QwenCrossEncoderReranker(config=self.config)
        else:
            self.semantic_ranker = LLMSemanticRanker(
                client=None,
                config=self.config,
                candidate_limit=self.config.candidate_limit,
            )
        self.action_planner = action_planner
        if action_orchestrator is not None:
            self.action_orchestrator = action_orchestrator
            if self.action_planner is None:
                self.action_planner = getattr(action_orchestrator, "planner", None)
        else:
            if self.action_planner is None and getattr(self.config, "tool_planning_enabled", False):
                planner_client = model_client or TieredModelClient.from_config(self.config)
                backend_names = getattr(planner_client, "backend_names", ())
                if model_client is not None or backend_names:
                    self.action_planner = ModelActionPlanner(planner_client)  # type: ignore[arg-type]
            self.action_orchestrator = (
                ActionOrchestrator(
                    self.action_planner,  # type: ignore[arg-type]
                    max_steps=getattr(self.config, "tool_max_steps", 4),
                    timeout_seconds=getattr(self.config, "tool_timeout_seconds", 8.0),
                )
                if self.action_planner is not None
                else None
            )
        self.last_diagnostics: dict[str, object] = {
            "event": "initialized",
            "catalog_size": len(self.repository),
            "model_backends": self._model_backends(),
            "tool_planning": self.action_orchestrator is not None,
        }
        self._last_retrieval_diagnostics: dict[str, object] = {}

    def _model_backends(self) -> list[str]:
        names = getattr(getattr(self.semantic_ranker, "client", None), "backend_names", ())
        if isinstance(names, (list, tuple)):
            return [str(name) for name in names]
        backend_name = getattr(self.semantic_ranker, "backend_name", None)
        return [str(backend_name)] if isinstance(backend_name, str) and backend_name else []

    def reset(self, session_id: str, user_profile: dict) -> None:
        """Replace any prior state for this session ID."""

        state = self.store.reset(session_id, user_profile)
        self.last_diagnostics = {
            "event": "reset",
            "session_id": state.session_id,
            "intent_epoch": state.intent_epoch,
            "catalog_size": len(self.repository),
            "model_backends": self._model_backends(),
            "tool_planning": self.action_orchestrator is not None,
        }

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        started = time.perf_counter()
        state = self.store.get(session_id)
        missing_reset = state is None
        if state is None:
            state = self.store.reset(session_id, {})
        try:
            if self.action_orchestrator is not None:
                response = self._respond_tool_impl(state, user_message, turn, top_k)
            else:
                response = self._respond_impl(state, user_message, turn, top_k)
        except Exception as exc:  # public boundary must not leak pipeline errors
            response = self._fallback_response(top_k)
            self.last_diagnostics = {
                "event": "pipeline_error",
                "session_id": state.session_id,
                "turn": int(turn),
                "intent_epoch": state.intent_epoch,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        self.last_diagnostics = {
            **self.last_diagnostics,
            "session_id": state.session_id,
            "turn": int(turn),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "missing_reset": missing_reset,
        }
        state.last_diagnostics = dict(self.last_diagnostics)
        return self._guard(response, top_k)

    def _respond_impl(
        self,
        state: SessionState,
        user_message: object,
        turn: int,
        top_k: int,
        *,
        _prepared: bool = False,
        _old_epoch: int | None = None,
    ) -> dict:
        turn = max(int(turn), 1)
        limit = self._limit(top_k)
        message = str(user_message or "")
        old_epoch = state.intent_epoch if _old_epoch is None else int(_old_epoch)
        if not _prepared:
            update = parse_intent_update(message, turn=turn)
            self.reducer.apply(state, update, turn=turn)
            state.record_message(turn, message)
        context = state.runtime_context(turn=turn, remaining_turns=max(10 - turn, 0))
        route = self.router.decide(context)
        state.active_route = route.mode
        candidates = self._retrieve(state, message, route.retrieval_budget, route=route)
        retrieval_diagnostics = dict(self._last_retrieval_diagnostics)
        stats = self._stats(state, candidates, message)
        state.last_candidate_stats = stats
        context = state.runtime_context(
            turn=turn,
            remaining_turns=max(10 - turn, 0),
            route_hint=route.mode,
            candidate_stats=stats,
        )
        gate = self.candidate_gate.evaluate(stats, context, turn=turn)
        # CandidateGate runs after the cheap probe/retrieval stage.  Its
        # retrieval_limit is therefore the last safe point at which we can
        # prevent an over-general turn from feeding an unnecessarily large
        # candidate set into feature ranking or the model backend.
        route_budget = max(int(getattr(route, "retrieval_budget", self.config.retrieval_limit)), 0)
        gate_budget = max(int(getattr(gate, "retrieval_limit", route_budget)), 0)
        category_resolved = retrieval_diagnostics.get("category_resolution") in {
            "resolved",
            "resolved_union",
        }
        adaptive_enabled = bool(
            getattr(self.config, "adaptive_category_recall_enabled", True)
        )
        if category_resolved and adaptive_enabled:
            # The category route already applied its own bounded quota.  The
            # gate decides only whether semantic ranking runs; it must not
            # discard the cheap category/feature pool before ranking.
            effective_budget = len(candidates)
        else:
            # Preserve the bounded fixed-budget fallback for unknown or
            # ambiguous anchors and for an explicit rollback configuration.
            effective_budget = min(route_budget, gate_budget)
        ranked = self._feature_rank(state, candidates, context, limit=effective_budget)
        semantic = None
        semantic_input_count = 0
        if gate.run_semantic_ranker and ranked:
            ranker_limit = getattr(self.semantic_ranker, "candidate_limit", self.config.candidate_limit)
            try:
                ranker_limit = max(int(ranker_limit), 1)
            except (TypeError, ValueError):
                ranker_limit = self.config.candidate_limit
            # The semantic stage is a separate cost boundary from cheap
            # feature recall.  Keep it at Top-30 even if a caller supplies a
            # larger general candidate_limit or an injected ranker advertises
            # a wider input capacity.
            semantic_limit = min(30, effective_budget, self.config.candidate_limit, ranker_limit)
            semantic_candidates = ranked[: max(semantic_limit, 0)]
            semantic_input_count = len(semantic_candidates)
            semantic = self._semantic_rank(context, semantic_candidates, limit=effective_budget)
            if semantic is not None:
                ranked = self._apply_semantic(ranked, semantic)
        records = [item.product for item in ranked]
        ask_attribute = self.clarification_policy.choose_attribute(
            state,
            records,
            stats,
            turn=turn,
            remaining_turns=max(10 - turn, 0),
        )
        if ask_attribute:
            state.record_asked(ask_attribute)
        ids = self._valid_ids([item.parent_asin for item in ranked], limit)
        state.last_candidate_ids = list(ids)
        state.record_recommendations(ids)
        result: dict[str, object] = {
            "message": self._message(ask_attribute, gate.over_general),
            "ask_attribute": ask_attribute,
            "recommendations": [{"parent_asin": item} for item in ids],
        }
        usage = self._usage(semantic)
        if usage is not None:
            result["usage"] = usage
        self.last_diagnostics = {
            "event": "respond",
            "execution_mode": "deterministic",
            "session_id": state.session_id,
            "turn": turn,
            "intent_epoch": state.intent_epoch,
            "epoch_changed": state.intent_epoch != old_epoch,
            "route": route.mode,
            "route_reason": route.reason_code,
            "route_weights": {
                "buying": float(route.buying_weight),
                "browsing": float(route.browsing_weight),
            },
            "retrieval_budget": route_budget,
            "gate": gate.mode,
            "gate_reason": gate.reason_code,
            "gate_retrieval_limit": gate_budget,
            "effective_budget": effective_budget,
            "feature_input_count": min(len(candidates), effective_budget),
            "semantic_input_count": semantic_input_count,
            "candidate_count": len(candidates),
            "retrieval_sources": self._source_counts(candidates),
            "candidate_stats": stats.as_dict(),
            "recommendation_count": len(ids),
            "asked_attribute": ask_attribute,
            "no_preference": sorted(state.no_preference),
            "seen_in_epoch": len(state.seen_recommendations),
            "model_backend": getattr(semantic, "backend", None),
            "model_failures": self._failures(semantic),
            "usage": usage,
            **retrieval_diagnostics,
        }
        return result

    def _respond_tool_impl(
        self,
        state: SessionState,
        user_message: object,
        turn: int,
        top_k: int,
    ) -> dict[str, object]:
        """Apply the turn once, then let the planner select bounded actions."""

        turn = max(int(turn), 1)
        limit = self._limit(top_k)
        message = str(user_message or "")
        old_epoch = state.intent_epoch
        pending = state.pending_task
        update = parse_intent_update(message, turn=turn)
        if pending is not None and pending.intent_epoch == state.intent_epoch:
            update = bind_clarification_answer(
                update,
                attribute=pending.ask_attribute,
                message=message,
            )
        self.reducer.apply(state, update, turn=turn)
        state.record_message(turn, message)
        resumed = bool(
            pending is not None
            and pending.intent_epoch == state.intent_epoch
            and not update.global_override
        )
        if resumed:
            state.pending_task = None

        toolbox = ShoppingToolbox(
            self.repository,
            state,
            top_k=limit,
            deterministic_candidate_ids=state.last_candidate_ids,
        )
        result = self.action_orchestrator.run(  # type: ignore[union-attr]
            state,
            toolbox,
            user_message=message,
            turn=turn,
            resumed_after_ask=resumed,
            resumed_task=pending if resumed else None,
        )
        if result.fallback_needed or result.response is None:
            response = self._respond_impl(
                state,
                message,
                turn,
                limit,
                _prepared=True,
                _old_epoch=old_epoch,
            )
            response = self._merge_usage(response, result.usage)
            response_usage = response.get("usage")
            merged_usage = (
                dict(response_usage)
                if isinstance(response_usage, Mapping)
                else None
            )
            self.last_diagnostics = {
                **self.last_diagnostics,
                "execution_mode": "fallback",
                "tool_fallback_reason": result.reason,
                "tool_action_count": result.action_count,
                "tool_backend": result.backend,
                "tool_failures": self._tool_failures(result.failures),
                "usage": merged_usage,
                "profile_loaded": state.profile_loaded,
                "resumed_after_ask": resumed,
                "tool_trajectory": self._turn_trajectory(state, turn),
            }
            return response

        response = dict(result.response)
        if result.reason == "recommend_products":
            response = self._fill_tool_recommendations(state, response, message, turn, limit)
        raw_recommendations = response.get("recommendations")
        recommendation_count = (
            len(raw_recommendations)
            if isinstance(raw_recommendations, Sequence)
            and not isinstance(raw_recommendations, (str, bytes))
            else 0
        )
        self.last_diagnostics = {
            "event": "respond",
            "execution_mode": "tool_loop",
            "session_id": state.session_id,
            "turn": turn,
            "intent_epoch": state.intent_epoch,
            "epoch_changed": state.intent_epoch != old_epoch,
            "tool_terminal_reason": result.reason,
            "tool_action_count": result.action_count,
            "tool_backend": result.backend,
            "tool_failures": self._tool_failures(result.failures),
            "usage": result.usage,
            "profile_loaded": state.profile_loaded,
            "pending_attribute": (
                state.pending_task.ask_attribute if state.pending_task is not None else None
            ),
            "resumed_after_ask": resumed,
            "candidate_count": len(state.tool_candidate_ids),
            "recommendation_count": recommendation_count,
            "tool_trajectory": self._turn_trajectory(state, turn),
        }
        return response

    def _fill_tool_recommendations(
        self,
        state: SessionState,
        response: dict[str, object],
        message: str,
        turn: int,
        limit: int,
    ) -> dict[str, object]:
        raw = response.get("recommendations")
        current = [
            item.get("parent_asin") if isinstance(item, Mapping) else item
            for item in raw
        ] if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else []
        ids = self._valid_ids(current, limit)
        if len(ids) < limit:
            context = state.runtime_context(turn=turn, remaining_turns=max(10 - turn, 0))
            route = self.router.decide(context)
            candidates = self._retrieve(state, message, route.retrieval_budget, route=route)
            ranked = self._feature_rank(state, candidates, context, limit=route.retrieval_budget)
            ids = self._valid_ids(
                [*ids, *(item.parent_asin for item in ranked), *(item.parent_asin for item in self.repository.popular(limit))],
                limit,
            )
        state.last_candidate_ids = list(ids)
        state.record_recommendations(ids)
        response["recommendations"] = [{"parent_asin": item} for item in ids]
        return response

    @staticmethod
    def _merge_usage(
        response: dict[str, object],
        additional: Mapping[str, object] | None,
    ) -> dict[str, object]:
        if not additional:
            return response
        raw_base = response.get("usage")
        base: Mapping[str, object] = raw_base if isinstance(raw_base, Mapping) else {}
        def valid_count(value: object) -> int | None:
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        prompt = valid_count(base.get("prompt_tokens", 0))
        completion = valid_count(base.get("completion_tokens", 0))
        extra_prompt = valid_count(additional.get("prompt_tokens", 0))
        extra_completion = valid_count(additional.get("completion_tokens", 0))
        if None not in (prompt, completion, extra_prompt, extra_completion):
            assert prompt is not None
            assert completion is not None
            assert extra_prompt is not None
            assert extra_completion is not None
            response["usage"] = {
                "prompt_tokens": prompt + extra_prompt,
                "completion_tokens": completion + extra_completion,
            }
        return response

    @staticmethod
    def _tool_failures(failures: Sequence[object]) -> list[object]:
        output: list[object] = []
        for failure in failures[:10]:
            as_dict = getattr(failure, "as_dict", None)
            try:
                output.append(as_dict() if callable(as_dict) else str(failure)[:500])
            except (AttributeError, TypeError, ValueError):
                output.append(str(failure)[:500])
        return output

    @staticmethod
    def _turn_trajectory(state: SessionState, turn: int) -> list[dict[str, object]]:
        return [
            item.as_dict()
            for item in state.tool_trajectory
            if item.turn == int(turn)
        ]

    @staticmethod
    def _limit(value: object) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 10
        return max(min(value, 10), 0)

    def _retrieve(
        self,
        state: SessionState,
        latest: str,
        budget: int,
        *,
        route: RouteDecision | None = None,
    ) -> list[RetrievedProduct]:
        """Retrieve through the route selected for this turn.

        The dense asset is intentionally not assumed here.  Browsing's
        Phase-1 fallback is broad lexical recall plus category interleaving;
        Buying is a smaller, precision-weighted lexical union.  Mixed mode
        keeps both route outputs and combines their weighted scores.
        """

        if route is None:
            route = self.router.decide(state.runtime_context(turn=1, remaining_turns=9))
        mode = str(getattr(route, "mode", "browsing")).lower()
        if mode not in {"buying", "browsing", "mixed"}:
            mode = "browsing"
        buying_weight = max(float(getattr(route, "buying_weight", 0.0)), 0.0)
        browsing_weight = max(float(getattr(route, "browsing_weight", 0.0)), 0.0)
        per_query = min(max(int(budget), 20), 120)
        adaptive_enabled = bool(getattr(self.config, "adaptive_category_recall_enabled", True))

        # Category retrieval is an independent route.  It is resolved before
        # lexical route construction and its quota is never competed away by
        # profile, constraint, or popularity candidates.
        category_resolution = self.repository.resolve_category(state.category_anchor or "")
        category_budget = 0
        category_ratio: float | None = None
        category_route: list[RetrievedProduct] = []
        if adaptive_enabled and category_resolution.resolved:
            hard_count = sum(item.hardness == "hard" for item in state.active_constraints)
            small_limit = getattr(self.config, "category_recall_small_category_limit", 500)
            minimum = getattr(self.config, "category_recall_min_budget", 100)
            maximum = getattr(self.config, "category_recall_max_budget", 400)
            category_budget = adaptive_category_budget(
                category_resolution.category_size,
                hard_count,
                small_category_limit=small_limit,
                minimum=minimum,
                maximum=maximum,
                browsing_ratio=getattr(self.config, "category_recall_browsing_ratio", 0.50),
                one_hard_ratio=getattr(self.config, "category_recall_one_hard_ratio", 0.35),
                many_hard_ratio=getattr(self.config, "category_recall_many_hard_ratio", 0.20),
            )
            if category_resolution.category_size <= int(small_limit):
                category_ratio = 1.0
            elif hard_count <= 0:
                category_ratio = float(getattr(self.config, "category_recall_browsing_ratio", 0.50))
            elif hard_count == 1:
                category_ratio = float(getattr(self.config, "category_recall_one_hard_ratio", 0.35))
            else:
                category_ratio = float(getattr(self.config, "category_recall_many_hard_ratio", 0.20))
            _, category_route = self.repository.category_with_scores(
                state.category_anchor or "",
                category_budget,
                hard_constraints=[
                    item
                    for item in state.active_constraints
                    if item.hardness == "hard" or item.polarity == "avoid"
                ],
                query_evidence=(
                    latest,
                    *state.query_terms,
                    *(item.value for item in state.active_preferences),
                ),
                resolution=category_resolution,
            )
            category_prefix = (
                "buying"
                if mode == "buying" or (mode == "mixed" and buying_weight >= browsing_weight)
                else "browsing"
            )
            category_route = [
                RetrievedProduct(item.product, item.score, f"{category_prefix}:category_exact", item.rank)
                for item in category_route
            ]

        # Each tuple is (query, source, route name, source weight).  Keeping
        # route and source in the provenance string makes the final candidate
        # diagnostics explainable without storing hidden evaluator data.
        specs: list[tuple[str, str, str, float, float]] = []
        if mode in {"buying", "mixed"} and buying_weight > 0.0:
            if latest.strip():
                specs.append((latest, "latest", "buying", buying_weight, 1.00))
            if state.category_anchor:
                specs.append((state.category_anchor, "category", "buying", buying_weight, 1.25))
            if state.active_constraints:
                specs.append(
                    (
                        " ".join(item.value for item in state.active_constraints),
                        "constraints",
                        "buying",
                        buying_weight,
                        1.50,
                    )
                )
        if mode in {"browsing", "mixed"} and browsing_weight > 0.0:
            if latest.strip():
                specs.append((latest, "latest", "browsing", browsing_weight, 0.65))
            if state.query_terms:
                specs.append(
                    (
                        " ".join(state.query_terms),
                        "accumulated",
                        "browsing",
                        browsing_weight,
                        1.00,
                    )
                )
            if state.active_preferences:
                specs.append(
                    (
                        " ".join(item.value for item in state.active_preferences),
                        "preferences",
                        "browsing",
                        browsing_weight,
                        0.75,
                    )
                )
            tags = state.profile.get("preference_tags", ())
            if isinstance(tags, str):
                tags = (tags,)
            if isinstance(tags, (list, tuple, set)) and tags:
                specs.append(
                    (
                        " ".join(str(tag) for tag in list(tags)[:6]),
                        "profile",
                        "browsing",
                        browsing_weight,
                        0.55,
                    )
                )
            if state.category_anchor:
                specs.append((state.category_anchor, "category", "browsing", browsing_weight, 0.45))

        unique: list[tuple[str, str, str, float, float]] = []
        seen_queries: set[tuple[str, str]] = set()
        for query, source, route_name, route_weight, source_weight in specs:
            key = (route_name, query.lower().strip())
            if key[1] and key not in seen_queries:
                seen_queries.add(key)
                unique.append((query, source, route_name, route_weight, source_weight))
        if not unique:
            # A route with zero weights (or an empty message/profile) still
            # returns valid products, but its fallback remains visible in
            # provenance rather than looking like a lexical hit.
            fallback_route = "buying" if mode == "buying" else "browsing"
            unique = [("", "popularity", fallback_route, 1.0, 0.25)]

        # Merge lexical routes separately so Browsing can diversify before
        # Mixed performs its union.  Scores are route_weight * source_weight
        # * RRF; category candidates are merged first below to preserve quota.
        route_merged: dict[str, dict[str, dict[str, object]]] = {"buying": {}, "browsing": {}}
        for query, source, route_name, route_weight, source_weight in unique[:8]:
            labeled_source = f"{route_name}:{source}"
            try:
                found = self.repository.search_with_scores(query, per_query, source=labeled_source)
            except Exception:
                found = []
            for rank, item in enumerate(found, 1):
                entry = route_merged[route_name].setdefault(
                    item.parent_asin,
                    {"product": item.product, "score": 0.0, "sources": [], "best": 0.0},
                )
                weighted = route_weight * source_weight
                entry["score"] = float(entry["score"]) + weighted * 100.0 / (60.0 + rank)
                entry["best"] = max(float(entry["best"]), weighted * max(float(item.score), 0.0))
                if isinstance(entry["sources"], list) and labeled_source not in entry["sources"]:
                    entry["sources"].append(labeled_source)

        route_outputs: dict[str, list[RetrievedProduct]] = {"buying": [], "browsing": []}
        for route_name, merged_route in route_merged.items():
            output_route: list[RetrievedProduct] = []
            for entry in merged_route.values():
                product = entry["product"]
                if isinstance(product, ProductRecord):
                    source = "+".join(str(value) for value in entry["sources"])
                    output_route.append(
                        RetrievedProduct(product, float(entry["score"]) + float(entry["best"]), source, 0)
                    )
            output_route.sort(key=lambda item: (-item.score, item.parent_asin))
            if route_name == "browsing":
                output_route = self._diversify(output_route)
            route_outputs[route_name] = output_route

        merged: dict[str, dict[str, object]] = {}
        category_ids = {item.parent_asin for item in category_route}

        def add_route_item(item: RetrievedProduct) -> None:
            entry = merged.setdefault(
                item.parent_asin,
                {"product": item.product, "score": 0.0, "sources": [], "category": False},
            )
            entry["score"] = float(entry["score"]) + item.score
            if item.parent_asin in category_ids:
                entry["category"] = True
            if isinstance(entry["sources"], list):
                for source in item.source.split("+"):
                    if source not in entry["sources"]:
                        entry["sources"].append(source)

        # Adding the exact category route first creates a stable protected
        # prefix.  Dedupe later routes into those entries without spending a
        # second category quota.
        for item in category_route:
            add_route_item(item)
        for route_name in ("buying", "browsing"):
            for item in route_outputs[route_name]:
                add_route_item(item)

        category_order = [item.parent_asin for item in category_route if item.parent_asin in merged]
        category_order_set = set(category_order)
        remaining = [
            entry
            for parent_asin, entry in merged.items()
            if parent_asin not in category_order_set
        ]
        remaining.sort(
            key=lambda entry: (
                -float(entry["score"]),
                str(entry["product"].parent_asin) if isinstance(entry["product"], ProductRecord) else "",
            )
        )
        output: list[RetrievedProduct] = []
        for parent_asin in category_order:
            entry = merged[parent_asin]
            if isinstance(entry["product"], ProductRecord):
                output.append(
                    RetrievedProduct(
                        entry["product"],
                        float(entry["score"]),
                        "+".join(str(value) for value in entry["sources"]),
                        0,
                    )
                )
        for entry in remaining:
            if isinstance(entry["product"], ProductRecord):
                output.append(
                    RetrievedProduct(
                        entry["product"],
                        float(entry["score"]),
                        "+".join(str(value) for value in entry["sources"]),
                        0,
                    )
                )
        if not output:
            output = [
                RetrievedProduct(product, 0.0, f"{mode}:popularity", index)
                for index, product in enumerate(self.repository.popular(per_query), 1)
            ]

        base_limit = max(per_query * 2, self.config.candidate_limit)
        if category_order:
            # Keep the category quota independent while reserving a bounded
            # tail for lexical/constraint/profile-only candidates.  The
            # absolute cap is deliberately finite even for large categories.
            non_category_tail = min(base_limit, NON_CATEGORY_TAIL)
            pool_limit = min(ABSOLUTE_CAP, len(category_order) + non_category_tail)
        else:
            non_category_tail = min(base_limit, ABSOLUTE_CAP)
            pool_limit = non_category_tail
        # Do not diversify the protected prefix: every category quota item
        # stays in the cheap feature pool.  Only the non-category tail is
        # diversified for Browsing.
        if mode == "browsing" and category_order:
            protected = output[: len(category_order)]
            tail = self._diversify(output[len(category_order) :])
            output = protected + tail
        elif mode == "browsing":
            output = self._diversify(output)
        output = output[:pool_limit]

        resolution_status = category_resolution.status if adaptive_enabled else "disabled"
        output_ids = {candidate.parent_asin for candidate in output}
        self._last_retrieval_diagnostics = {
            "category_resolution": resolution_status,
            "resolved_category": category_resolution.category if category_resolution.resolved else None,
            "category_resolution_reason": (
                "adaptive_disabled" if not adaptive_enabled else category_resolution.reason
            ),
            "category_size": category_resolution.category_size if adaptive_enabled else 0,
            "category_recall_ratio": category_ratio,
            "category_route_budget": category_budget,
            "category_route_candidate_count": len(category_route),
            "category_route_preserved_count": sum(item.parent_asin in output_ids for item in category_route),
            "route_candidate_counts": {
                "category": len(category_route),
                "buying": len(route_outputs["buying"]),
                "browsing": len(route_outputs["browsing"]),
            },
            "route_union_size": len(merged),
            "cheap_candidate_pool_limit": pool_limit,
            "cheap_candidate_pool_size": len(output),
            "cheap_candidate_pool_absolute_cap": ABSOLUTE_CAP,
            "non_category_tail_limit": non_category_tail,
            "non_category_tail_count": max(len(output) - len(category_order), 0),
        }
        return output

    @staticmethod
    def _diversify(candidates: Sequence[RetrievedProduct], limit: int | None = None) -> list[RetrievedProduct]:
        """Interleave leaf categories while retaining each route's score."""

        target = len(candidates) if limit is None else max(int(limit), 0)
        if target <= 0:
            return []
        buckets: dict[str, list[RetrievedProduct]] = {}
        order: list[str] = []
        for item in candidates:
            categories = item.product.categories
            key = next((str(value).strip().lower() for value in reversed(categories) if str(value).strip()), "__unknown__")
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(item)
        output: list[RetrievedProduct] = []
        while len(output) < target:
            progressed = False
            for key in order:
                bucket = buckets[key]
                if bucket:
                    output.append(bucket.pop(0))
                    progressed = True
                    if len(output) >= target:
                        break
            if not progressed:
                break
        return output

    @staticmethod
    def _source_counts(candidates: Sequence[RetrievedProduct]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for item in candidates:
            for source in item.source.split("+"):
                if source:
                    counts[source] += 1
        return dict(sorted(counts.items()))

    def _stats(self, state: SessionState, candidates: Sequence[RetrievedProduct], latest: str) -> CandidateStats:
        query = " ".join((latest, state.category_anchor or "", " ".join(item.value for item in state.active_constraints)))
        try:
            estimate = self.repository.estimate_count(query)
        except Exception:
            estimate = len(candidates)
        categories = [value.lower() for item in candidates[:100] for value in item.product.categories[:2] if value]
        entropy: dict[str, float] = {}
        for attribute in ("material", "color", "size", "style", "brand", "budget", "feature", "use_case"):
            entropy[attribute] = self._entropy(self._attribute_values(candidates[:100], attribute))
        return CandidateStats(
            estimated_count=max(int(estimate), len(candidates)),
            category_entropy=self._entropy(categories),
            attribute_entropy=entropy,
            active_hard_constraint_count=sum(item.hardness == "hard" for item in state.active_constraints),
        )

    @staticmethod
    def _entropy(values: Sequence[str]) -> float:
        counts = Counter(value for value in values if value)
        total = sum(counts.values())
        return -sum((count / total) * math.log(count / total) for count in counts.values()) if total and len(counts) > 1 else 0.0

    @staticmethod
    def _attribute_values(candidates: Sequence[RetrievedProduct], attribute: str) -> list[str]:
        words = {
            "material": ("cotton", "polyester", "nylon", "leather", "wool", "silk", "linen", "denim"),
            "color": ("black", "white", "blue", "red", "pink", "green", "brown", "gray", "grey", "navy"),
            "size": ("size", "sizing", "small", "medium", "large", "wide", "narrow"),
            "style": ("fit", "comfort", "style", "casual", "formal", "warmth", "durable"),
            "feature": ("waterproof", "pocket", "hood", "durable", "feature"),
            "use_case": ("hiking", "running", "gym", "work", "winter", "outdoor", "travel", "walking"),
        }
        output: list[str] = []
        for item in candidates:
            product = item.product
            if attribute == "budget" and product.price is not None:
                output.append(str(round(product.price / 25.0)))
            elif attribute == "brand" and product.store:
                output.append(product.store.lower())
            else:
                text = product.canonical_text.lower()
                match = next((word for word in words.get(attribute, ()) if re.search(rf"\b{re.escape(word)}\b", text)), None)
                if match:
                    output.append(match)
        return output

    def _feature_rank(
        self,
        state: SessionState,
        candidates: Sequence[RetrievedProduct],
        context: RuntimeContext,
        *,
        limit: int | None = None,
    ) -> list[RetrievedProduct]:
        input_candidates = list(candidates)
        if limit is not None:
            input_candidates = input_candidates[: max(int(limit), 0)]
        query_terms = set(safe_terms(" ".join(state.query_terms)))
        scored: list[tuple[float, int, RetrievedProduct]] = []
        for index, item in enumerate(input_candidates):
            product = item.product
            text = product.canonical_text.lower()
            score = item.score + 0.18 * sum(term in text for term in query_terms)
            # Category membership is established by the exact category route
            # before this stage.  Give that route a relevance-first boost so a
            # globally popular but unrelated lexical hit cannot displace a
            # valid member merely because it has more ratings.
            if any(source.endswith(":category_exact") for source in item.source.split("+")):
                score += 6.0
            if context.category_anchor:
                score += 1.8 * sum(term in text for term in safe_terms(context.category_anchor))
            for constraint in state.active_constraints:
                terms = safe_terms(constraint.value)
                if not terms:
                    continue
                ratio = sum(term in text for term in terms) / len(terms)
                if constraint.polarity == "avoid":
                    score -= 3.0 * ratio
                elif constraint.hardness == "hard":
                    score += 5.0 * ratio
                else:
                    score += 1.6 * ratio
            tags = state.profile.get("preference_tags", ())
            if isinstance(tags, str):
                tags = (tags,)
            if isinstance(tags, (list, tuple, set)):
                score += 0.08 * sum(bool(safe_terms(tag)) and safe_terms(tag)[0] in text for tag in tags)
            if product.rating is not None:
                score += 0.05 * max(0.0, min(float(product.rating), 5.0))
            if product.rating_count is not None:
                score += 0.03 * math.log1p(max(product.rating_count, 0))
            if item.parent_asin in state.seen_recommendations:
                score -= 8.0
            scored.append((score, index, item))
        scored.sort(key=lambda value: (-value[0], value[1], value[2].parent_asin))
        ordered = [value[2] for value in scored]
        unseen = [item for item in ordered if item.parent_asin not in state.seen_recommendations]
        ordered = unseen + [item for item in ordered if item.parent_asin in state.seen_recommendations] if unseen else ordered
        if context.route_hint == "browsing":
            ordered = self._diversify(ordered)
        output_limit = max(int(limit), 0) if limit is not None else max(self.config.retrieval_limit, self.config.candidate_limit)
        return ordered[:output_limit]

    def _semantic_rank(
        self,
        context: RuntimeContext,
        candidates: Sequence[RetrievedProduct],
        *,
        limit: int | None = None,
    ) -> object | None:
        summary = {
            "category_anchor": context.category_anchor,
            "hard_constraints": [item.as_dict() for item in context.hard_constraints],
            "soft_preferences": [item.as_dict() for item in context.soft_preferences],
            "avoided_values": [item.as_dict() for item in context.avoided_values],
            "profile_priors": list(context.profile_priors),
            "epoch": context.intent_epoch,
            "turn": context.turn,
        }
        try:
            candidate_limit = self.config.candidate_limit
            if limit is not None:
                candidate_limit = min(candidate_limit, max(int(limit), 0))
            return self.semantic_ranker.rank(summary, list(candidates[:candidate_limit]))
        except Exception as exc:
            self.last_diagnostics = {**self.last_diagnostics, "model_failures": [f"{type(exc).__name__}: {exc}"[:500]]}
            return None

    @staticmethod
    def _apply_semantic(candidates: Sequence[RetrievedProduct], result: object) -> list[RetrievedProduct]:
        ordered_ids = getattr(result, "ordered_parent_asins", ())
        if not isinstance(ordered_ids, (list, tuple)):
            return list(candidates)
        by_id = {item.parent_asin: item for item in candidates}
        output: list[RetrievedProduct] = []
        seen: set[str] = set()
        for value in ordered_ids:
            item = by_id.get(str(value).strip())
            if item is not None and item.parent_asin not in seen:
                output.append(item)
                seen.add(item.parent_asin)
        return output + [item for item in candidates if item.parent_asin not in seen]

    @staticmethod
    def _usage(result: object | None) -> dict[str, int] | None:
        if result is None or not getattr(result, "backend", None):
            return None
        usage = getattr(result, "usage", None)
        if not isinstance(usage, Mapping):
            return None
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0 and isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0:
            return {"prompt_tokens": prompt, "completion_tokens": completion}
        return None

    @staticmethod
    def _failures(result: object | None) -> list[object]:
        failures = getattr(result, "failures", ()) if result is not None else ()
        output: list[object] = []
        for failure in failures or ():
            try:
                output.append(failure.as_dict())
            except Exception:
                output.append(str(failure)[:500])
        return output

    def _fallback_response(self, top_k: object) -> dict[str, object]:
        ids = self._valid_ids([item.parent_asin for item in self.repository.popular(self._limit(top_k))], self._limit(top_k))
        return {"message": "Here are the closest matches I found.", "ask_attribute": None, "recommendations": [{"parent_asin": item} for item in ids]}

    def _guard(self, payload: object, top_k: object) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            return self._fallback_response(top_k)
        message = payload.get("message") if isinstance(payload.get("message"), str) else "Here are the closest matches I found."
        ask = payload.get("ask_attribute") if payload.get("ask_attribute") in ALLOWED_ATTRIBUTES else None
        raw = payload.get("recommendations")
        values: list[object] = []
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            values = [item.get("parent_asin") if isinstance(item, Mapping) else item for item in raw]
        response: dict[str, object] = {"message": message, "ask_attribute": ask, "recommendations": [{"parent_asin": item} for item in self._valid_ids(values, self._limit(top_k))]}
        usage = payload.get("usage")
        if isinstance(usage, Mapping) and isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int) and usage["prompt_tokens"] >= 0 and usage["completion_tokens"] >= 0:
            response["usage"] = {"prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"]}
        return response

    def _valid_ids(self, values: Sequence[object], limit: int) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = str(value).strip() if value is not None else ""
            if item and item not in seen and item in self.repository.ids:
                seen.add(item)
                result.append(item)
                if len(result) >= limit:
                    break
        return result

    @staticmethod
    def _message(attribute: str | None, over_general: bool) -> str:
        if not attribute:
            return "Here are the closest matches I found."
        label = {"use_case": "intended use", "other": "anything else that matters"}.get(attribute, attribute)
        prefix = "I found several possibilities." if over_general else "Here are the closest matches."
        return f"{prefix} Do you have a {label} preference?"


__all__ = ["Agent"]
