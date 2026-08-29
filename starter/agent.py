"""Offline-first public facade for the shopping agent."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from .shopping_agent.catalog import (
    CatalogRepository,
    ProductRecord,
    RetrievedProduct,
)
from .shopping_agent.config import AgentConfig
from .shopping_agent.graph import GraphServices, GraphState, run_graph
from .shopping_agent.model import TieredModelClient
from .shopping_agent.policy import (
    CandidateGate,
    ClarificationPolicy,
    IntentRouter,
    RouteDecision,
    adaptive_category_budget,
)
from .shopping_agent.qwen_reranker import QwenCrossEncoderReranker
from .shopping_agent.recommendation import CatalogRecommendationEngine
from .shopping_agent.semantic_ranking import LLMSemanticRanker
from .shopping_agent.state import (
    ALLOWED_ATTRIBUTES,
    SessionState,
    SessionStore,
    StateReducer,
)

ABSOLUTE_CAP = 600
NON_CATEGORY_TAIL = 100


class Agent:
    """Offline-first shopping Agent with a fixed deterministic pipeline."""

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
        # Router/Value-Node graph wiring (design.md Section 2, Section 10;
        # 08-28-agent-v2-router-value-node Phase 7).  ``_llm_model_client``
        # backs every LLM Value Node except ``SemanticRank`` (design.md
        # Section 5).  Reusing the semantic ranker's own client when it is
        # already a ``TieredModelClient`` (the ``LLMSemanticRanker`` branch
        # above) avoids constructing a second client from the same env
        # config; Qwen's local cross-encoder client is not a
        # ``TieredModelClient``, so that branch builds one directly.  Either
        # way this performs no network call -- ``TieredModelClient.from_config``
        # only reads env-configured backend availability, exactly like the
        # ``LLMSemanticRanker(client=None, ...)`` branch above already did.
        existing_client = getattr(self.semantic_ranker, "client", None)
        self._llm_model_client: TieredModelClient | None = (
            existing_client
            if isinstance(existing_client, TieredModelClient)
            else TieredModelClient.from_config(self.config)
        )
        self.recommendation_engine = CatalogRecommendationEngine(
            self.repository, self.config, self.router
        )
        self._graph_services = GraphServices(
            recommendation_engine=self.recommendation_engine,
            candidate_gate=self.candidate_gate,
            clarification_policy=self.clarification_policy,
            model_client=self._llm_model_client,
            semantic_ranker=self.semantic_ranker,
            llm_nodes_enabled=True,
        )
        self.last_diagnostics: dict[str, object] = {
            "event": "initialized",
            "catalog_size": len(self.repository),
            "model_backends": self._model_backends(),
            "tool_planning": False,
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
            "tool_planning": False,
        }

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        started = time.perf_counter()
        state = self.store.get(session_id)
        missing_reset = state is None
        if state is None:
            state = self.store.reset(session_id, {})
        try:
            response = self._respond_impl_graph(state, user_message, turn, top_k)
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

    def _respond_impl_graph(
        self,
        state: SessionState,
        user_message: object,
        turn: int,
        top_k: int,
    ) -> dict:
        """Router/Value-Node graph implementation of ``respond()``
        (design.md Section 2, Section 10; 08-28-agent-v2-router-value-node
        Phase 7).  This is the only path ``respond()`` calls -- the legacy
        fixed pipeline this replaced (``_respond_impl``) was removed in
        Phase 8 once the full regression suite and the scenario showcase
        confirmed the graph path stands on its own (implement.md's Phase 7
        rollback point).

        Turn/top_k normalization, ``state.last_diagnostics``-compatible
        reporting through ``self.last_diagnostics``, and the public response
        keys (``message``, ``ask_attribute``, ``recommendations``, and an
        optional ``usage``) all mirror the former fixed pipeline's
        externally-visible shape.  Everything else -- routing, retrieval,
        ranking, clarification, constraint tracking -- is owned by
        ``run_graph``'s static Router/Node tables instead of an inline
        pipeline.
        """

        turn = max(int(turn), 1)
        limit = self._limit(top_k)
        message = str(user_message or "")
        old_epoch = state.intent_epoch
        gs = run_graph(
            state,
            turn=turn,
            top_k=limit,
            message=message,
            services=self._graph_services,
        )
        payload = gs.scratch.get("response")
        result: dict[str, object] = (
            dict(payload) if isinstance(payload, Mapping) else self._fallback_response(limit)
        )
        usage = self._graph_usage(gs)
        if usage is not None:
            result["usage"] = usage
        self.last_diagnostics = self._graph_diagnostics(state, gs, turn, old_epoch)
        return result

    @staticmethod
    def _graph_usage(gs: GraphState) -> dict[str, int] | None:
        """Successor to ``_usage`` for the graph path: reads the usage
        ``SemanticRank`` (the only LLM Value Node whose backend call can
        report real token counts against the public contract, design.md
        Section 5) stashed into transient scratch this turn."""

        usage = gs.scratch.get("semantic_rank_usage")
        if not isinstance(usage, Mapping):
            return None
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if (
            isinstance(prompt, int)
            and not isinstance(prompt, bool)
            and prompt >= 0
            and isinstance(completion, int)
            and not isinstance(completion, bool)
            and completion >= 0
        ):
            return {"prompt_tokens": prompt, "completion_tokens": completion}
        return None

    def _graph_diagnostics(
        self,
        state: SessionState,
        gs: GraphState,
        turn: int,
        old_epoch: int,
    ) -> dict[str, object]:
        """Successor to the old fixed pipeline's ``_respond_impl`` diagnostics
        dict (removed in Phase 8; see ``_respond_impl_graph``'s docstring),
        adapted to the graph's node names/structure (implement.md Phase 7,
        item 4).  Built from ``state.node_trace`` -- the bounded, per-turn
        trace design.md Section 8 already populates -- plus the same rich
        retrieval/gate/rank diagnostics ``CatalogRecommendationEngine.
        _retrieve``/``_feature_rank``/``CandidateGate.evaluate`` already
        computed as a side effect and the graph's ``Search``/``Rank``/
        ``CandidatePoolRouter`` nodes already stash into ``gs.scratch``
        (single-turn-only, never committed through ``StateReducer``) -- not a
        second bookkeeping path, just surfacing what those nodes already
        produce.  Field names/semantics were kept identical to the former
        fixed pipeline's diagnostics dict wherever the same fact was
        available, so existing diagnostics-consuming tests/tools do not need
        to know which pipeline produced a given turn.
        """

        trace = list(state.node_trace)
        kind_counts: Counter[str] = Counter(str(entry.get("kind")) for entry in trace)
        node_path = [str(entry.get("node")) for entry in trace]
        response = gs.scratch.get("response")
        recommendations = (
            response.get("recommendations") if isinstance(response, Mapping) else None
        )
        recommendation_count = (
            len(recommendations)
            if isinstance(recommendations, Sequence) and not isinstance(recommendations, (str, bytes))
            else 0
        )

        # Search/CandidatePoolRouter/Rank/SemanticRank transient scratch --
        # absent on turns that never ran ``Search`` this turn (e.g. a
        # ``compare_details``/``confirm_choice`` turn routed straight to
        # ``FetchDetails``/``Render``), in which case every field below
        # degrades to the same safe default the former fixed pipeline never
        # had to consider (it always retrieved once per turn).
        route = gs.scratch.get("search_route")
        route_budget = max(int(getattr(route, "retrieval_budget", 0)), 0) if route is not None else 0
        route_weights = (
            {
                "buying": float(getattr(route, "buying_weight", 0.0)),
                "browsing": float(getattr(route, "browsing_weight", 0.0)),
            }
            if route is not None
            else None
        )
        gate = gs.scratch.get("candidate_gate_decision")
        retrieved = gs.scratch.get("search_retrieved")
        retrieved_list = retrieved if isinstance(retrieved, list) else []
        candidate_count = len(retrieved_list) if retrieved is not None else len(state.candidates)
        retrieval_diagnostics = gs.scratch.get("search_retrieval_diagnostics")
        retrieval_diagnostics = (
            dict(retrieval_diagnostics) if isinstance(retrieval_diagnostics, dict) else {}
        )
        model_backend = gs.scratch.get("semantic_rank_backend")
        model_failures = gs.scratch.get("semantic_rank_failures") or []
        usage = self._graph_usage(gs)

        return {
            "event": "respond",
            "execution_mode": "graph_v2",
            "session_id": state.session_id,
            "turn": turn,
            "intent_epoch": state.intent_epoch,
            "epoch_changed": state.intent_epoch != old_epoch,
            "route": state.active_route,
            "route_reason": getattr(route, "reason_code", None),
            "route_weights": route_weights,
            "retrieval_budget": route_budget,
            "gate": getattr(gate, "mode", None),
            "gate_reason": getattr(gate, "reason_code", None),
            "gate_retrieval_limit": getattr(gate, "retrieval_limit", None),
            "effective_budget": gs.scratch.get("rank_effective_budget", 0),
            "feature_input_count": gs.scratch.get("rank_feature_input_count", 0),
            "semantic_input_count": gs.scratch.get("semantic_rank_input_count", 0),
            "candidate_count": candidate_count,
            "retrieval_sources": self._source_counts(retrieved_list),
            "candidate_stats": (
                state.last_candidate_stats.as_dict() if state.last_candidate_stats is not None else {}
            ),
            "node_count": len(trace),
            "node_path": node_path,
            "router_count": kind_counts.get("router", 0),
            "value_node_deterministic_count": kind_counts.get("value_node_deterministic", 0),
            "value_node_llm_count": kind_counts.get("value_node_llm", 0),
            "search_retry_count": state.search_retry_count,
            "ranked_count": len(state.ranked),
            "recommendation_count": recommendation_count,
            "asked_attribute": state.pending_question.attribute if state.pending_question else None,
            "no_preference": sorted(state.no_preference),
            "seen_in_epoch": len(state.seen_recommendations),
            "model_backends": self._model_backends(),
            "model_backend": model_backend,
            "model_failures": model_failures,
            "usage": usage,
            "semantic_backend": model_backend,
            "semantic_failures": model_failures,
            **retrieval_diagnostics,
        }

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


__all__ = ["Agent"]
