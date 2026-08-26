"""Offline-first public facade for the shopping agent."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import math
from pathlib import Path
import re
import time

from .shopping_agent.catalog import CatalogRepository, ProductRecord, RetrievedProduct, safe_terms
from .shopping_agent.config import AgentConfig
from .shopping_agent.policy import (
    CandidateGate,
    ClarificationPolicy,
    CommitDecision,
    IntentRouter,
    RankEvidence,
    RecommendationCommitPolicy,
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
    normalize_constraint_value,
    parse_intent_update,
)
from .shopping_agent.structured_pool import StructuredCandidatePool, StructuredPoolResult


ABSOLUTE_CAP = 600
NON_CATEGORY_TAIL = 100


class Agent:
    """Stateful, deterministic retrieval pipeline with optional LLM reranking."""

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
        commit_policy: RecommendationCommitPolicy | None = None,
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
        self.structured_pool = StructuredCandidatePool(
            self.repository,
            enabled=bool(getattr(self.config, "structured_pool_enabled", True)),
        )
        self.commit_policy = commit_policy or RecommendationCommitPolicy(
            commit_all_threshold=getattr(self.config, "commit_all_threshold", 5),
            partial_threshold=getattr(self.config, "partial_commit_threshold", 25),
            partial_limit=getattr(self.config, "partial_commit_limit", 1),
            no_progress_force_commit=getattr(self.config, "no_progress_force_commit", 2),
            top1_margin_threshold=getattr(self.config, "commit_top1_margin_threshold", 0.30),
            top3_stability_threshold=getattr(self.config, "commit_top3_stability_threshold", 0.66),
        )
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
        self.last_diagnostics: dict[str, object] = {
            "event": "initialized",
            "catalog_size": len(self.repository),
            "model_backends": self._model_backends(),
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
        }

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        started = time.perf_counter()
        state = self.store.get(session_id)
        missing_reset = state is None
        if state is None:
            state = self.store.reset(session_id, {})
        try:
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

    def _respond_impl(self, state: SessionState, user_message: object, turn: int, top_k: int) -> dict:
        turn = max(int(turn), 1)
        limit = self._limit(top_k)
        message = str(user_message or "")
        old_epoch = state.intent_epoch
        update = parse_intent_update(message, turn=turn)
        self.reducer.apply(state, update, turn=turn)
        # Capture the post-reducer intent before later diagnostics or ranking
        # stages mutate derived state (for example a softened pool key).
        intent_fingerprint = state.fingerprint()
        state.record_message(turn, message)
        context = state.runtime_context(turn=turn, remaining_turns=max(10 - turn, 0))
        route = self.router.decide(context)
        state.active_route = route.mode
        candidates = self._retrieve(state, message, route.retrieval_budget, route=route)
        retrieval_diagnostics = dict(self._last_retrieval_diagnostics)
        structured_pool = self.structured_pool.build(state)
        if structured_pool.softened_constraint_keys:
            state.softened_constraint_keys.update(structured_pool.softened_constraint_keys)
        if structured_pool.resolved and structured_pool.ids:
            # Rehydrate the complete category pool from catalog records.  IDs
            # already present in lexical/category retrieval retain provenance;
            # the remaining members are intentionally admitted with a neutral
            # score so later ranking, rather than recall budget, decides them.
            retrieved_by_id = {item.parent_asin: item for item in candidates}
            candidates = [
                retrieved_by_id.get(
                    parent_asin,
                    RetrievedProduct(
                        self.repository.products[parent_asin],
                        0.0,
                        "structured:category",
                        0,
                    ),
                )
                for parent_asin in structured_pool.ids
                if parent_asin in self.repository.products
            ]
            # Retrieval diagnostics describe the pool handed to the feature
            # ranker.  Once the full structured category has replaced the
            # bounded route union, expose that larger deterministic pool.
            retrieval_diagnostics["cheap_candidate_pool_size"] = len(candidates)
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
        legacy_category_resolved = retrieval_diagnostics.get(
            "category_resolution"
        ) in {"resolved", "resolved_union"}
        adaptive_enabled = bool(
            getattr(self.config, "adaptive_category_recall_enabled", True)
        )
        if structured_pool.resolved or (legacy_category_resolved and adaptive_enabled):
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
        previous_ranked_ids = tuple(state.previous_ranked_ids)
        state.update_progress(
            structured_pool.ids if structured_pool.resolved else [item.parent_asin for item in candidates],
            [item.parent_asin for item in ranked],
            intent_fingerprint=intent_fingerprint,
        )
        rank_evidence = self._rank_evidence(ranked, previous_ranked_ids, state)
        records = [item.product for item in ranked]
        ask_attribute = self.clarification_policy.choose_attribute(
            state,
            records,
            stats,
            turn=turn,
            remaining_turns=max(10 - turn, 0),
            reopen_clarification=update.reopen_clarification,
        )
        if ask_attribute:
            state.record_asked(ask_attribute)
        if bool(getattr(self.config, "commit_policy_enabled", True)):
            commit_decision = self.commit_policy.decide(
                rank_evidence,
                state,
                turn=turn,
                top_k=limit,
            )
        else:
            commit_decision = CommitDecision("full", limit, "commit_policy_disabled")
        recommendation_limit = min(limit, max(int(commit_decision.recommendation_limit), 0))
        ids = self._valid_ids([item.parent_asin for item in ranked], recommendation_limit)
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
            "structured_pool": structured_pool.as_dict(),
            "structured_pool_size": structured_pool.final_size,
            "structured_pool_applied_constraints": [
                item.as_dict() for item in structured_pool.applied_constraints
            ],
            "structured_pool_softened_constraints": [
                item.as_dict() for item in structured_pool.softened_constraints
            ],
            "rank_evidence": rank_evidence.as_dict(),
            "commit": commit_decision.as_dict(),
            "recommendation_count": len(ids),
            "asked_attribute": ask_attribute,
            "no_preference": sorted(state.no_preference),
            "attribute_exhausted": sorted(state.attribute_exhausted),
            "global_exhausted": state.global_exhausted,
            "boundary_seen": state.boundary_seen,
            "exhaustion_reason": state.exhaustion_reason,
            "ask_counts": dict(state.ask_counts),
            "no_progress_streak": state.no_progress_streak,
            "softened_constraint_keys": [list(item) for item in sorted(state.softened_constraint_keys)],
            "seen_in_epoch": len(state.seen_recommendations),
            "model_backend": getattr(semantic, "backend", None),
            "model_failures": self._failures(semantic),
            "usage": usage,
            **retrieval_diagnostics,
        }
        return result

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
    def _rank_evidence(
        ranked: Sequence[RetrievedProduct],
        previous_ranked_ids: Sequence[str] = (),
        state: SessionState | None = None,
    ) -> RankEvidence:
        """Derive relative, target-free confidence signals for CommitGate."""

        values: list[float] = []
        for item in ranked:
            try:
                value = float(item.score)
            except (TypeError, ValueError):
                value = 0.0
            values.append(value if math.isfinite(value) else 0.0)
        margin = 0.0
        if len(values) >= 2:
            span = values[0] - values[-1]
            if span > 1e-12:
                margin = min(max((values[0] - values[1]) / span, 0.0), 1.0)
        current_top = {item.parent_asin for item in ranked[:3]}
        previous_top = {str(value).strip() for value in previous_ranked_ids[:3] if str(value).strip()}
        top1_stability = (
            1.0
            if ranked and previous_ranked_ids and ranked[0].parent_asin == str(previous_ranked_ids[0]).strip()
            else 0.0
        )
        stability = (
            len(current_top & previous_top) / len(current_top | previous_top)
            if current_top and previous_top
            else 0.0
        )
        hard_count = len(state.active_constraints) if state is not None else 0
        return RankEvidence(
            pool_size=len(ranked),
            top1_margin=margin,
            top1_stability=top1_stability,
            top3_stability=stability,
            hard_constraint_count=sum(
                item.hardness == "hard" for item in state.active_constraints
            ) if state is not None else hard_count,
            no_progress_streak=getattr(state, "no_progress_streak", 0) if state is not None else 0,
            ranked_ids=tuple(item.parent_asin for item in ranked[:10]),
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
        query_terms = list(safe_terms(" ".join(state.query_terms)))
        bm25_scores, title_scores = self._lexical_rank_scores(input_candidates, query_terms)
        scored: list[tuple[float, int, RetrievedProduct]] = []
        for index, item in enumerate(input_candidates):
            product = item.product
            text = product.canonical_text.lower()
            score = item.score
            if bool(getattr(self.config, "ranking_bm25_enabled", True)):
                score += float(getattr(self.config, "ranking_bm25_weight", 0.18)) * bm25_scores.get(
                    item.parent_asin, 0.0
                )
            if bool(getattr(self.config, "ranking_title_coverage_enabled", True)):
                score += float(
                    getattr(self.config, "ranking_title_coverage_weight", 0.12)
                ) * title_scores.get(item.parent_asin, 0.0)
            # Category membership is established by the exact category route
            # before this stage.  Give that route a relevance-first boost so a
            # globally popular but unrelated lexical hit cannot displace a
            # valid member merely because it has more ratings.
            if any(source.endswith(":category_exact") for source in item.source.split("+")):
                score += 6.0
            if context.category_anchor:
                score += 1.8 * sum(term in text for term in safe_terms(context.category_anchor))
            for constraint in state.active_constraints:
                terms = safe_terms(
                    normalize_constraint_value(constraint.attribute, constraint.value)
                )
                if not terms:
                    continue
                ratio = sum(term in text for term in terms) / len(terms)
                if constraint.polarity == "avoid":
                    score -= 3.0 * ratio
                elif constraint.hardness == "hard" and (
                    str(constraint.attribute).lower(),
                    normalize_constraint_value(
                        constraint.attribute, constraint.value
                    ).lower(),
                ) not in state.softened_constraint_keys:
                    score += 5.0 * ratio
                else:
                    score += 1.6 * ratio
            tags = state.profile.get("preference_tags", ())
            if isinstance(tags, str):
                tags = (tags,)
            if bool(getattr(self.config, "ranking_profile_enabled", True)) and isinstance(
                tags, (list, tuple, set)
            ):
                score += float(getattr(self.config, "ranking_profile_weight", 0.08)) * sum(
                    bool(safe_terms(tag)) and safe_terms(tag)[0] in text for tag in tags
                )
            if bool(getattr(self.config, "ranking_rating_enabled", True)) and product.rating is not None:
                score += float(getattr(self.config, "ranking_rating_weight", 0.05)) * max(
                    0.0, min(float(product.rating), 5.0)
                )
            if bool(getattr(self.config, "ranking_popularity_enabled", True)) and product.rating_count is not None:
                score += float(getattr(self.config, "ranking_popularity_weight", 0.03)) * math.log1p(
                    max(product.rating_count, 0)
                )
            if item.parent_asin in state.seen_recommendations:
                score -= 8.0
            scored.append((score, index, item))
        scored.sort(key=lambda value: (-value[0], value[1], value[2].parent_asin))
        # Carry the fused deterministic score forward.  Commit confidence
        # must reflect the score that actually produced the order, rather
        # than the earlier retrieval-only score on RetrievedProduct.
        ordered = [
            RetrievedProduct(item.product, float(score), item.source, rank)
            for rank, (score, _index, item) in enumerate(scored, 1)
        ]
        unseen = [item for item in ordered if item.parent_asin not in state.seen_recommendations]
        ordered = unseen + [item for item in ordered if item.parent_asin in state.seen_recommendations] if unseen else ordered
        if context.route_hint == "browsing":
            ordered = self._diversify(ordered)
        output_limit = max(int(limit), 0) if limit is not None else max(self.config.retrieval_limit, self.config.candidate_limit)
        return ordered[:output_limit]

    @staticmethod
    def _lexical_rank_scores(
        candidates: Sequence[RetrievedProduct],
        query_terms: Sequence[str],
    ) -> tuple[dict[str, float], dict[str, float]]:
        """Compute normalized BM25-like and title-coverage evidence locally."""

        if not candidates or not query_terms:
            return {}, {}
        token_re = re.compile(r"[^\W_]+", re.UNICODE)
        documents: dict[str, list[str]] = {
            item.parent_asin: token_re.findall(item.product.canonical_text.lower())
            for item in candidates
        }
        titles: dict[str, set[str]] = {
            item.parent_asin: set(token_re.findall(item.product.title.lower()))
            for item in candidates
        }
        unique_terms = list(dict.fromkeys(str(term).lower() for term in query_terms if str(term).strip()))
        if not unique_terms:
            return {}, {}
        document_frequency = {
            term: sum(term in set(tokens) for tokens in documents.values())
            for term in unique_terms
        }
        average_length = sum(len(tokens) for tokens in documents.values()) / max(len(documents), 1)
        average_length = max(average_length, 1.0)
        bm25: dict[str, float] = {}
        title_coverage: dict[str, float] = {}
        for parent_asin, tokens in documents.items():
            counts = Counter(tokens)
            document_length = max(len(tokens), 1)
            value = 0.0
            for term in unique_terms:
                frequency = counts.get(term, 0)
                if frequency <= 0:
                    continue
                df = document_frequency[term]
                idf = math.log1p((len(documents) - df + 0.5) / (df + 0.5))
                denominator = frequency + 1.5 * (
                    1.0 - 0.75 + 0.75 * document_length / average_length
                )
                value += idf * (frequency * 2.5 / max(denominator, 1e-12))
            bm25[parent_asin] = value
            title_coverage[parent_asin] = sum(
                term in titles[parent_asin] for term in unique_terms
            ) / len(unique_terms)
        maximum = max(bm25.values(), default=0.0)
        if maximum > 0.0:
            bm25 = {key: value / maximum for key, value in bm25.items()}
        return bm25, title_coverage

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
        if limit <= 0:
            return []
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
