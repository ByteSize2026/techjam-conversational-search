"""Route-aware recall and feature-ranking Services shared by every caller.

This module is a mechanical extraction of the former ``Agent._retrieve`` and
``Agent._feature_rank`` private methods (plus their shared ``_diversify``
helper) into a typed ``RecommendationEngine``.  Every weight, constant,
tie-break and behavior is preserved exactly; only the ownership boundary
changed so both the DeepSeek/Runtime ``search_products``/``rank_products``
tool actions and the deterministic facade can call the same strong recall
and ranking logic instead of maintaining two implementations that could
silently drift apart.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .catalog import CatalogRepository, ProductRecord, RetrievedProduct, safe_terms
from .config import AgentConfig
from .policy import (
    IntentRouter,
    RouteDecision,
    adaptive_category_budget,
)
from .state import CandidateStats, RuntimeContext, SessionState

# Preserved verbatim from the former ``starter/agent.py`` module-level
# constants: the absolute cap on the cheap candidate pool and the bounded
# tail reserved for non-category (lexical/constraint/profile-only)
# candidates.
ABSOLUTE_CAP = 600
NON_CATEGORY_TAIL = 100


def _float_field(value: object) -> float:
    """Read a numeric accumulator field from an untyped merge-entry dict."""

    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _source_list(value: object) -> list[str]:
    """Read a source-list field from an untyped merge-entry dict."""

    return value if isinstance(value, list) else []


# Word lists and helpers backing ``CatalogRecommendationEngine.candidate_stats``
# -- a mechanical port of the former ``Agent._attribute_values``/``_entropy``.
_STATS_WORDS: dict[str, tuple[str, ...]] = {
    "material": ("cotton", "polyester", "nylon", "leather", "wool", "silk", "linen", "denim"),
    "color": ("black", "white", "blue", "red", "pink", "green", "brown", "gray", "grey", "navy"),
    "size": ("size", "sizing", "small", "medium", "large", "wide", "narrow"),
    "style": ("fit", "comfort", "style", "casual", "formal", "warmth", "durable"),
    "feature": ("waterproof", "pocket", "hood", "durable", "feature"),
    "use_case": ("hiking", "running", "gym", "work", "winter", "outdoor", "travel", "walking"),
}


def _stats_entropy(values: Sequence[str]) -> float:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    total = sum(counts.values())
    if total <= 1 or len(counts) <= 1:
        return 0.0
    return -sum((count / total) * math.log(count / total) for count in counts.values())


def _stats_attribute_values(candidates: Sequence[RetrievedProduct], attribute: str) -> list[str]:
    output: list[str] = []
    for item in candidates:
        product = item.product
        if attribute == "budget" and product.price is not None:
            output.append(str(round(product.price / 25.0)))
        elif attribute == "brand" and product.store:
            output.append(product.store.lower())
        else:
            text = product.canonical_text.lower()
            words = _STATS_WORDS.get(attribute, ())
            match = next((word for word in words if re.search(rf"\b{re.escape(word)}\b", text)), None)
            if match:
                output.append(match)
    return output


@dataclass(frozen=True)
class RecommendationRequestContext:
    """Bounded per-call context threaded from the Runtime/Agent facade.

    ``decision`` is the ``RouteDecision`` produced by ``IntentRouter.decide``
    for the current turn; it is the primary route source for ``search`` and
    its ``route`` (mode) property is the route hint consumed by ``rank``'s
    diversification step.
    """

    state: SessionState
    decision: RouteDecision
    user_message: str
    turn: int
    top_k: int


@dataclass(frozen=True)
class SearchRequest:
    """What the ``search_products`` tool call already carries as arguments."""

    query: str
    category: str | None = None
    limit: int | None = None


@dataclass(frozen=True)
class RankRequest:
    """What the ``rank_products`` tool call already carries as arguments."""

    product_ids: tuple[str, ...]
    limit: int | None = None


@dataclass(frozen=True)
class CandidateSearchResult:
    """Internal typed result.  Route/source/score provenance stays here;
    the Node-facing projection only ever sees the existing bounded compact
    product summary built from ``candidates``."""

    candidates: tuple[RetrievedProduct, ...]
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateRankResult:
    """``product_ids`` is always an ordered subset of the request IDs."""

    product_ids: tuple[str, ...]


class RecommendationEngine(Protocol):
    def search(
        self, request: SearchRequest, context: RecommendationRequestContext
    ) -> CandidateSearchResult: ...

    def rank(
        self, request: RankRequest, context: RecommendationRequestContext
    ) -> CandidateRankResult: ...


class CatalogRecommendationEngine:
    """Route-aware multi-source recall plus constraint/profile feature rank.

    ``search``/``rank`` are the typed Service entry points Runtime calls for
    the ``search_products``/``rank_products`` actions.  ``_retrieve`` and
    ``_feature_rank`` are the extracted implementations (unchanged from the
    former ``Agent`` private methods) and remain available for the
    deterministic facade to call directly with its own already-computed
    ``RouteDecision``/``RuntimeContext``, eliminating duplicated logic.
    """

    def __init__(
        self,
        repository: CatalogRepository,
        config: AgentConfig,
        router: IntentRouter,
    ) -> None:
        self.repository = repository
        self.config = config
        self.router = router
        self._last_retrieval_diagnostics: dict[str, object] = {}

    def search(
        self, request: SearchRequest, context: RecommendationRequestContext
    ) -> CandidateSearchResult:
        state = context.state
        route = context.decision if context.decision is not None else None
        budget = int(
            getattr(route, "retrieval_budget", getattr(self.config, "retrieval_limit", 100))
        )
        candidates = self._retrieve(state, request.query, budget, route=route)
        diagnostics = dict(self._last_retrieval_diagnostics)
        category = (request.category or "").strip()
        if category:
            # Preserve the existing ``search_products`` category-narrowing
            # behavior: intersect the strong recall output with a resolved
            # category rather than replacing recall with a single query.
            resolution, category_found = self.repository.category_with_scores(
                category, max(len(candidates), 1)
            )
            if resolution.resolved:
                category_ids = {item.parent_asin for item in category_found}
                narrowed = [item for item in candidates if item.parent_asin in category_ids]
                candidates = narrowed or category_found
        limit = int(request.limit) if request.limit is not None else 10
        limit = max(min(limit, 20), 0)
        return CandidateSearchResult(candidates=tuple(candidates[:limit]), diagnostics=diagnostics)

    def rank(
        self, request: RankRequest, context: RecommendationRequestContext
    ) -> CandidateRankResult:
        state = context.state
        wanted = max(len(request.product_ids), 1)
        records = {
            item.parent_asin: item
            for item in self.repository.materialize(request.product_ids, wanted)
        }
        retrieved = [
            RetrievedProduct(records[parent_asin], 0.0, "tool:observed", index)
            for index, parent_asin in enumerate(request.product_ids)
            if parent_asin in records
        ]
        route_hint = context.decision.route if context.decision is not None else None
        runtime_context = state.runtime_context(
            turn=context.turn,
            remaining_turns=max(10 - int(context.turn), 0),
            route_hint=route_hint,
        )
        ranked = self._feature_rank(state, retrieved, runtime_context, limit=len(retrieved))
        limit = int(request.limit) if request.limit is not None else len(ranked)
        limit = max(min(limit, 10), 0)
        return CandidateRankResult(product_ids=tuple(item.parent_asin for item in ranked[:limit]))

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
            except Exception:  # noqa: BLE001 - retrieval source is optional
                found = []
            for rank, item in enumerate(found, 1):
                entry = route_merged[route_name].setdefault(
                    item.parent_asin,
                    {"product": item.product, "score": 0.0, "sources": [], "best": 0.0},
                )
                weighted = route_weight * source_weight
                entry["score"] = _float_field(entry["score"]) + weighted * 100.0 / (60.0 + rank)
                entry["best"] = max(
                    _float_field(entry["best"]), weighted * max(float(item.score), 0.0)
                )
                if isinstance(entry["sources"], list) and labeled_source not in entry["sources"]:
                    entry["sources"].append(labeled_source)

        route_outputs: dict[str, list[RetrievedProduct]] = {"buying": [], "browsing": []}
        for route_name, merged_route in route_merged.items():
            output_route: list[RetrievedProduct] = []
            for entry in merged_route.values():
                product = entry["product"]
                if isinstance(product, ProductRecord):
                    source = "+".join(str(value) for value in _source_list(entry["sources"]))
                    output_route.append(
                        RetrievedProduct(
                            product,
                            _float_field(entry["score"]) + _float_field(entry["best"]),
                            source,
                            0,
                        )
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
            entry["score"] = _float_field(entry["score"]) + item.score
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
                -_float_field(entry["score"]),
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
                        _float_field(entry["score"]),
                        "+".join(str(value) for value in _source_list(entry["sources"])),
                        0,
                    )
                )
        for entry in remaining:
            if isinstance(entry["product"], ProductRecord):
                output.append(
                    RetrievedProduct(
                        entry["product"],
                        _float_field(entry["score"]),
                        "+".join(str(value) for value in _source_list(entry["sources"])),
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

    def candidate_stats(
        self,
        state: SessionState,
        candidates: Sequence[RetrievedProduct],
        latest: str,
    ) -> CandidateStats:
        """Cheap over-generality signal consumed by ``CandidateGate.evaluate``
        (design.md Section 5.3) -- the input the ``Search`` Value Node
        produces and ``CandidatePoolRouter`` reads.  Mechanical port of the
        former ``Agent._stats``/``_entropy``/``_attribute_values`` helpers:
        unchanged behavior, only the ownership boundary moved to the Service
        both the graph node and, previously, the fixed pipeline can share.
        """

        query = " ".join(
            (
                latest,
                state.category_anchor or "",
                " ".join(item.value for item in state.active_constraints),
            )
        )
        try:
            estimate = self.repository.estimate_count(query)
        except Exception:  # noqa: BLE001 - estimate is a best-effort probe
            estimate = len(candidates)
        sample = candidates[:100]
        categories = [
            value.lower() for item in sample for value in item.product.categories[:2] if value
        ]
        entropy: dict[str, float] = {
            attribute: _stats_entropy(_stats_attribute_values(sample, attribute))
            for attribute in (
                "material",
                "color",
                "size",
                "style",
                "brand",
                "budget",
                "feature",
                "use_case",
            )
        }
        return CandidateStats(
            estimated_count=max(int(estimate), len(candidates)),
            category_entropy=_stats_entropy(categories),
            attribute_entropy=entropy,
            active_hard_constraint_count=sum(
                item.hardness == "hard" for item in state.active_constraints
            ),
        )

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


__all__ = [
    "ABSOLUTE_CAP",
    "NON_CATEGORY_TAIL",
    "CandidateRankResult",
    "CandidateSearchResult",
    "CatalogRecommendationEngine",
    "RankRequest",
    "RecommendationEngine",
    "RecommendationRequestContext",
    "SearchRequest",
]
