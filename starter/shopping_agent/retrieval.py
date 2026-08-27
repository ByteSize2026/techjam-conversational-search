"""Route-aware, deterministic candidate retrieval for the shopping agent.

The retrieval layer is deliberately independent of session state ownership and
response formatting.  It turns a :class:`SessionState` plus the current
message into a bounded, provenance-carrying candidate pool and exposes the
diagnostics needed by the Agent facade.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from .catalog import CatalogRepository, ProductRecord, RetrievedProduct
from .config import AgentConfig
from .policy import IntentRouter, RouteDecision, adaptive_category_budget
from .state import SessionState


ABSOLUTE_CAP = 600
NON_CATEGORY_TAIL = 100


def diversify_candidates(
    candidates: Sequence[RetrievedProduct], limit: int | None = None
) -> list[RetrievedProduct]:
    """Interleave leaf categories while retaining each candidate's score."""

    target = len(candidates) if limit is None else max(int(limit), 0)
    if target <= 0:
        return []
    buckets: dict[str, list[RetrievedProduct]] = {}
    order: list[str] = []
    for item in candidates:
        categories = item.product.categories
        key = next(
            (
                str(value).strip().lower()
                for value in reversed(categories)
                if str(value).strip()
            ),
            "__unknown__",
        )
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


def source_counts(candidates: Sequence[RetrievedProduct]) -> dict[str, int]:
    """Count retrieval provenance labels for diagnostics."""

    counts: Counter[str] = Counter()
    for item in candidates:
        for source in item.source.split("+"):
            if source:
                counts[source] += 1
    return dict(sorted(counts.items()))


class RetrievalEngine:
    """Build the bounded route union consumed by deterministic ranking."""

    def __init__(
        self,
        repository: CatalogRepository,
        config: AgentConfig,
        *,
        router: IntentRouter | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.router = router
        self.last_diagnostics: dict[str, object] = {}

    def retrieve(
        self,
        state: SessionState,
        latest: str,
        budget: int,
        *,
        route: RouteDecision | None = None,
        router: IntentRouter | None = None,
        diversify: object | None = None,
    ) -> list[RetrievedProduct]:
        """Retrieve through the route selected for this turn.

        The dense asset is intentionally not assumed here.  Browsing's
        Phase-1 fallback is broad lexical recall plus category interleaving;
        Buying is a smaller, precision-weighted lexical union.  Mixed mode
        keeps both route outputs and combines their weighted scores.
        """

        if route is None:
            active_router = router or self.router
            if active_router is None:
                active_router = IntentRouter(retrieval_budget=self.config.retrieval_limit)
            route = active_router.decide(
                state.runtime_context(turn=1, remaining_turns=9)
            )
        mode = str(getattr(route, "mode", "browsing")).lower()
        if mode not in {"buying", "browsing", "mixed"}:
            mode = "browsing"
        buying_weight = max(float(getattr(route, "buying_weight", 0.0)), 0.0)
        browsing_weight = max(float(getattr(route, "browsing_weight", 0.0)), 0.0)
        per_query = min(max(int(budget), 20), 120)
        adaptive_enabled = bool(
            getattr(self.config, "adaptive_category_recall_enabled", True)
        )
        diversify_fn = diversify if callable(diversify) else diversify_candidates

        # Category retrieval is an independent route.  It is resolved before
        # lexical route construction and its quota is never competed away by
        # profile, constraint, or popularity candidates.
        category_resolution = self.repository.resolve_category(
            state.category_anchor or ""
        )
        category_budget = 0
        category_ratio: float | None = None
        category_route: list[RetrievedProduct] = []
        if adaptive_enabled and category_resolution.resolved:
            hard_count = sum(
                item.hardness == "hard" for item in state.active_constraints
            )
            small_limit = getattr(
                self.config, "category_recall_small_category_limit", 500
            )
            minimum = getattr(self.config, "category_recall_min_budget", 100)
            maximum = getattr(self.config, "category_recall_max_budget", 400)
            category_budget = adaptive_category_budget(
                category_resolution.category_size,
                hard_count,
                small_category_limit=small_limit,
                minimum=minimum,
                maximum=maximum,
                browsing_ratio=getattr(
                    self.config, "category_recall_browsing_ratio", 0.50
                ),
                one_hard_ratio=getattr(
                    self.config, "category_recall_one_hard_ratio", 0.35
                ),
                many_hard_ratio=getattr(
                    self.config, "category_recall_many_hard_ratio", 0.20
                ),
            )
            if category_resolution.category_size <= int(small_limit):
                category_ratio = 1.0
            elif hard_count <= 0:
                category_ratio = float(
                    getattr(self.config, "category_recall_browsing_ratio", 0.50)
                )
            elif hard_count == 1:
                category_ratio = float(
                    getattr(self.config, "category_recall_one_hard_ratio", 0.35)
                )
            else:
                category_ratio = float(
                    getattr(self.config, "category_recall_many_hard_ratio", 0.20)
                )
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
                    *state.active_query_terms,
                    *(item.value for item in state.active_preferences),
                ),
                resolution=category_resolution,
            )
            category_prefix = (
                "buying"
                if mode == "buying"
                or (mode == "mixed" and buying_weight >= browsing_weight)
                else "browsing"
            )
            category_route = [
                RetrievedProduct(
                    item.product,
                    item.score,
                    f"{category_prefix}:category_exact",
                    item.rank,
                )
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
                specs.append(
                    (
                        state.category_anchor,
                        "category",
                        "buying",
                        buying_weight,
                        1.25,
                    )
                )
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
            if state.active_query_terms:
                specs.append(
                    (
                        " ".join(state.active_query_terms),
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
                specs.append(
                    (
                        state.category_anchor,
                        "category",
                        "browsing",
                        browsing_weight,
                        0.45,
                    )
                )

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
        route_merged: dict[str, dict[str, dict[str, object]]] = {
            "buying": {},
            "browsing": {},
        }
        for query, source, route_name, route_weight, source_weight in unique[:8]:
            labeled_source = f"{route_name}:{source}"
            try:
                found = self.repository.search_with_scores(
                    query, per_query, source=labeled_source
                )
            except Exception:
                found = []
            for rank, item in enumerate(found, 1):
                entry = route_merged[route_name].setdefault(
                    item.parent_asin,
                    {"product": item.product, "score": 0.0, "sources": [], "best": 0.0},
                )
                weighted = route_weight * source_weight
                entry["score"] = float(entry["score"]) + weighted * 100.0 / (60.0 + rank)
                entry["best"] = max(
                    float(entry["best"]), weighted * max(float(item.score), 0.0)
                )
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
                        RetrievedProduct(
                            product,
                            float(entry["score"]) + float(entry["best"]),
                            source,
                            0,
                        )
                    )
            output_route.sort(key=lambda item: (-item.score, item.parent_asin))
            if route_name == "browsing":
                output_route = diversify_fn(output_route)
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
                for item_source in item.source.split("+"):
                    if item_source not in entry["sources"]:
                        entry["sources"].append(item_source)

        # Adding the exact category route first creates a stable protected
        # prefix.  Dedupe later routes into those entries without spending a
        # second category quota.
        for item in category_route:
            add_route_item(item)
        for route_name in ("buying", "browsing"):
            for item in route_outputs[route_name]:
                add_route_item(item)

        category_order = [
            item.parent_asin for item in category_route if item.parent_asin in merged
        ]
        category_order_set = set(category_order)
        remaining = [
            entry
            for parent_asin, entry in merged.items()
            if parent_asin not in category_order_set
        ]
        remaining.sort(
            key=lambda entry: (
                -float(entry["score"]),
                str(entry["product"].parent_asin)
                if isinstance(entry["product"], ProductRecord)
                else "",
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
                for index, product in enumerate(
                    self.repository.popular(per_query), 1
                )
            ]

        base_limit = max(per_query * 2, self.config.candidate_limit)
        if category_order:
            # Keep the category quota independent while reserving a bounded
            # tail for lexical/constraint/profile-only candidates.  The
            # absolute cap is deliberately finite even for large categories.
            non_category_tail = min(base_limit, NON_CATEGORY_TAIL)
            pool_limit = min(
                ABSOLUTE_CAP, len(category_order) + non_category_tail
            )
        else:
            non_category_tail = min(base_limit, ABSOLUTE_CAP)
            pool_limit = non_category_tail
        # Do not diversify the protected prefix: every category quota item
        # stays in the cheap feature pool.  Only the non-category tail is
        # diversified for Browsing.
        if mode == "browsing" and category_order:
            protected = output[: len(category_order)]
            tail = diversify_fn(output[len(category_order) :])
            output = protected + tail
        elif mode == "browsing":
            output = diversify_fn(output)
        output = output[:pool_limit]

        resolution_status = category_resolution.status if adaptive_enabled else "disabled"
        output_ids = {candidate.parent_asin for candidate in output}
        self.last_diagnostics = {
            "category_resolution": resolution_status,
            "resolved_category": (
                category_resolution.category if category_resolution.resolved else None
            ),
            "category_resolution_reason": (
                "adaptive_disabled" if not adaptive_enabled else category_resolution.reason
            ),
            "category_size": category_resolution.category_size if adaptive_enabled else 0,
            "category_recall_ratio": category_ratio,
            "category_route_budget": category_budget,
            "category_route_candidate_count": len(category_route),
            "category_route_preserved_count": sum(
                item.parent_asin in output_ids for item in category_route
            ),
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


__all__ = [
    "ABSOLUTE_CAP",
    "NON_CATEGORY_TAIL",
    "RetrievalEngine",
    "diversify_candidates",
    "source_counts",
]
