"""Official evaluator entry and stateful shopping-agent pipeline.

``python -m evaluator.local_evaluator`` loads this ``Agent``.  The same class
is also the dependency-injection boundary used by the offline tests and
Qwen/adaptive-recall benchmarks.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import time

from .shopping_agent.catalog import (
    CatalogRepository,
    RetrievedProduct,
)
from .shopping_agent.config import AgentConfig
from .shopping_agent.policy import (
    CandidateGate,
    ClarificationPolicy,
    CommitDecision,
    IntentRouter,
    RankEvidence,
    RecommendationCommitPolicy,
    RouteDecision,
)
from .shopping_agent.qwen_reranker import QwenCrossEncoderReranker
from .shopping_agent.ranking import (
    RankingEngine,
    attribute_values,
    entropy,
    lexical_rank_scores,
    rank_evidence,
)
from .shopping_agent.response import (
    SemanticResponseAdapter,
    apply_semantic,
    extract_failures,
    extract_usage,
    fallback_response,
    guard_response,
    normalize_limit,
    response_message,
    valid_ids,
)
from .shopping_agent.retrieval import (
    ABSOLUTE_CAP,
    NON_CATEGORY_TAIL,
    RetrievalEngine,
    diversify_candidates,
    source_counts,
)
from .shopping_agent.semantic_ranking import LLMSemanticRanker
from .shopping_agent.state import (
    CandidateStats,
    RuntimeContext,
    SessionState,
    SessionStore,
    StateReducer,
    parse_intent_update,
)
from .shopping_agent.structured_pool import StructuredCandidatePool


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
        self.router = router or IntentRouter(
            retrieval_budget=self.config.retrieval_limit
        )
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
            no_progress_force_commit=getattr(
                self.config, "no_progress_force_commit", 2
            ),
            top1_margin_threshold=getattr(
                self.config, "commit_top1_margin_threshold", 0.30
            ),
            top3_stability_threshold=getattr(
                self.config, "commit_top3_stability_threshold", 0.66
            ),
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
        self.retrieval = RetrievalEngine(
            self.repository,
            self.config,
            router=self.router,
        )
        self.ranking = RankingEngine(self.repository, self.config)
        self.response_helpers = SemanticResponseAdapter(self.config)
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

    def _respond_impl(
        self, state: SessionState, user_message: object, turn: int, top_k: int
    ) -> dict:
        turn = max(int(turn), 1)
        limit = self._limit(top_k)
        message = str(user_message or "")
        old_epoch = state.intent_epoch
        update = parse_intent_update(message, turn=turn)
        self.reducer.apply(state, update, turn=turn)
        intent_diagnostics = dict(state.last_diagnostics)
        # Capture the post-reducer intent before later diagnostics or ranking
        # stages mutate derived state (for example a softened pool key).
        intent_fingerprint = state.fingerprint()
        state.record_message(turn, message)
        context = state.runtime_context(
            turn=turn, remaining_turns=max(10 - turn, 0)
        )
        route = self.router.decide(context)
        state.active_route = route.mode
        candidates = self._retrieve(
            state, message, route.retrieval_budget, route=route
        )
        retrieval_diagnostics = dict(self._last_retrieval_diagnostics)
        structured_pool = self.structured_pool.build(state)
        if structured_pool.softened_constraint_keys:
            state.softened_constraint_keys.update(
                structured_pool.softened_constraint_keys
            )
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
                        self.repository.products[parent_asin], 0.0, "structured:category", 0
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
        route_budget = max(
            int(getattr(route, "retrieval_budget", self.config.retrieval_limit)), 0
        )
        gate_budget = max(int(getattr(gate, "retrieval_limit", route_budget)), 0)
        legacy_category_resolved = retrieval_diagnostics.get(
            "category_resolution"
        ) in {"resolved", "resolved_union"}
        adaptive_enabled = bool(
            getattr(self.config, "adaptive_category_recall_enabled", True)
        )
        if structured_pool.resolved or (
            legacy_category_resolved and adaptive_enabled
        ):
            # The category route already applied its own bounded quota.  The
            # gate decides only whether semantic ranking runs; it must not
            # discard the cheap category/feature pool before ranking.
            effective_budget = len(candidates)
        else:
            # Preserve the bounded fixed-budget fallback for unknown or
            # ambiguous anchors and for an explicit rollback configuration.
            effective_budget = min(route_budget, gate_budget)
        ranked = self._feature_rank(
            state, candidates, context, limit=effective_budget
        )
        semantic = None
        semantic_input_count = 0
        if gate.run_semantic_ranker and ranked:
            ranker_limit = getattr(
                self.semantic_ranker, "candidate_limit", self.config.candidate_limit
            )
            try:
                ranker_limit = max(int(ranker_limit), 1)
            except (TypeError, ValueError):
                ranker_limit = self.config.candidate_limit
            # The semantic stage is a separate cost boundary from cheap
            # feature recall.  Keep it at Top-30 even if a caller supplies a
            # larger general candidate_limit or an injected ranker advertises
            # a wider input capacity.
            semantic_limit = min(
                30, effective_budget, self.config.candidate_limit, ranker_limit
            )
            semantic_candidates = ranked[: max(semantic_limit, 0)]
            semantic_input_count = len(semantic_candidates)
            semantic = self._semantic_rank(
                context, semantic_candidates, limit=effective_budget
            )
            if semantic is not None:
                ranked = self._apply_semantic(ranked, semantic)
        previous_ranked_ids = tuple(state.previous_ranked_ids)
        state.update_progress(
            structured_pool.ids
            if structured_pool.resolved
            else [item.parent_asin for item in candidates],
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
        recommendation_limit = min(
            limit, max(int(commit_decision.recommendation_limit), 0)
        )
        ids = self._valid_ids(
            [item.parent_asin for item in ranked], recommendation_limit
        )
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
            "intent_scope": intent_diagnostics.get("intent_scope", "none"),
            "scope": intent_diagnostics.get("scope", "none"),
            "added_constraint_keys": intent_diagnostics.get(
                "added_constraint_keys", []
            ),
            "retained_constraint_keys": intent_diagnostics.get(
                "retained_constraint_keys", []
            ),
            "superseded_constraint_keys": intent_diagnostics.get(
                "superseded_constraint_keys", []
            ),
            "query_evidence_count": intent_diagnostics.get(
                "query_evidence_count", len(state.query_evidence)
            ),
            "active_query_evidence_count": intent_diagnostics.get(
                "active_query_evidence_count", len(state.active_query_evidence)
            ),
            "superseded_query_evidence_count": intent_diagnostics.get(
                "superseded_query_evidence_count", 0
            ),
            "query_evidence_carry_forward_count": intent_diagnostics.get(
                "query_evidence_carry_forward_count", 0
            ),
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
            "softened_constraint_keys": [
                list(item) for item in sorted(state.softened_constraint_keys)
            ],
            "seen_in_epoch": len(state.seen_recommendations),
            "model_backend": getattr(semantic, "backend", None),
            "model_failures": self._failures(semantic),
            "usage": usage,
            **retrieval_diagnostics,
        }
        return result

    # Compatibility delegates -------------------------------------------------
    # Keep these names stable for direct tests, diagnostics scripts, and the
    # frozen-trace benchmark wrapper.  The turn pipeline above intentionally
    # calls self._feature_rank so replacing that instance attribute continues
    # to intercept live feature ranking.

    @staticmethod
    def _limit(value: object) -> int:
        return normalize_limit(value)

    def _retrieve(
        self,
        state: SessionState,
        latest: str,
        budget: int,
        *,
        route: RouteDecision | None = None,
    ) -> list[RetrievedProduct]:
        candidates = self.retrieval.retrieve(
            state,
            latest,
            budget,
            route=route,
            router=self.router,
            diversify=self._diversify,
        )
        self._last_retrieval_diagnostics = dict(self.retrieval.last_diagnostics)
        return candidates

    @staticmethod
    def _diversify(
        candidates: Sequence[RetrievedProduct], limit: int | None = None
    ) -> list[RetrievedProduct]:
        return diversify_candidates(candidates, limit)

    @staticmethod
    def _source_counts(candidates: Sequence[RetrievedProduct]) -> dict[str, int]:
        return source_counts(candidates)

    def _stats(
        self,
        state: SessionState,
        candidates: Sequence[RetrievedProduct],
        latest: str,
    ) -> CandidateStats:
        return self.ranking.stats(
            state,
            candidates,
            latest,
            entropy_fn=self._entropy,
            attribute_values_fn=self._attribute_values,
        )

    @staticmethod
    def _rank_evidence(
        ranked: Sequence[RetrievedProduct],
        previous_ranked_ids: Sequence[str] = (),
        state: SessionState | None = None,
    ) -> RankEvidence:
        return rank_evidence(ranked, previous_ranked_ids, state)

    def _feature_rank(
        self,
        state: SessionState,
        candidates: Sequence[RetrievedProduct],
        context: RuntimeContext,
        *,
        limit: int | None = None,
    ) -> list[RetrievedProduct]:
        return self.ranking.feature_rank(
            state,
            candidates,
            context,
            limit=limit,
            diversify=self._diversify,
            lexical_rank=self._lexical_rank_scores,
        )

    @staticmethod
    def _lexical_rank_scores(
        candidates: Sequence[RetrievedProduct], query_terms: Sequence[str]
    ) -> tuple[dict[str, float], dict[str, float]]:
        return lexical_rank_scores(candidates, query_terms)

    @staticmethod
    def _entropy(values: Sequence[str]) -> float:
        return entropy(values)

    @staticmethod
    def _attribute_values(
        candidates: Sequence[RetrievedProduct], attribute: str
    ) -> list[str]:
        return attribute_values(candidates, attribute)

    def _semantic_rank(
        self,
        context: RuntimeContext,
        candidates: Sequence[RetrievedProduct],
        *,
        limit: int | None = None,
    ) -> object | None:
        result = self.response_helpers.rank(
            self.semantic_ranker,
            context,
            candidates,
            limit=limit,
        )
        if result is None and self.response_helpers.last_failure:
            self.last_diagnostics = {
                **self.last_diagnostics,
                "model_failures": [self.response_helpers.last_failure],
            }
        return result

    @staticmethod
    def _apply_semantic(
        candidates: Sequence[RetrievedProduct], result: object
    ) -> list[RetrievedProduct]:
        return apply_semantic(candidates, result)

    @staticmethod
    def _usage(result: object | None) -> dict[str, int] | None:
        return extract_usage(result)

    @staticmethod
    def _failures(result: object | None) -> list[object]:
        return extract_failures(result)

    def _fallback_response(self, top_k: object) -> dict[str, object]:
        return fallback_response(
            self.repository,
            top_k,
            valid_id_fn=self._valid_ids,
            limit_fn=self._limit,
        )

    def _guard(self, payload: object, top_k: object) -> dict[str, object]:
        return guard_response(
            self.repository,
            payload,
            top_k,
            valid_id_fn=self._valid_ids,
            fallback_fn=self._fallback_response,
            limit_fn=self._limit,
        )

    def _valid_ids(self, values: Sequence[object], limit: int) -> list[str]:
        return valid_ids(self.repository, values, limit)

    @staticmethod
    def _message(attribute: str | None, over_general: bool) -> str:
        return response_message(attribute, over_general)

__all__ = ["Agent"]
