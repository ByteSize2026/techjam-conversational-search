"""Deterministic candidate statistics and feature ranking.

This module contains the inexpensive, target-free evidence used by the
shopping agent before an optional semantic reranker.  It owns no session
store; all conversational state and runtime context are passed explicitly.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
import math
import re

from .catalog import CatalogRepository, RetrievedProduct, safe_terms
from .config import AgentConfig
from .policy import RankEvidence
from .state import (
    CandidateStats,
    RuntimeContext,
    SessionState,
    normalize_constraint_value,
)


def entropy(values: Sequence[str]) -> float:
    """Return Shannon entropy for non-empty categorical values."""

    counts = Counter(value for value in values if value)
    total = sum(counts.values())
    return (
        -sum((count / total) * math.log(count / total) for count in counts.values())
        if total and len(counts) > 1
        else 0.0
    )


def attribute_values(
    candidates: Sequence[RetrievedProduct], attribute: str
) -> list[str]:
    """Extract coarse deterministic evidence for a clarification attribute."""

    words = {
        "material": (
            "cotton",
            "polyester",
            "nylon",
            "leather",
            "wool",
            "silk",
            "linen",
            "denim",
        ),
        "color": (
            "black",
            "white",
            "blue",
            "red",
            "pink",
            "green",
            "brown",
            "gray",
            "grey",
            "navy",
        ),
        "size": ("size", "sizing", "small", "medium", "large", "wide", "narrow"),
        "style": (
            "fit",
            "comfort",
            "style",
            "casual",
            "formal",
            "warmth",
            "durable",
        ),
        "feature": ("waterproof", "pocket", "hood", "durable", "feature"),
        "use_case": (
            "hiking",
            "running",
            "gym",
            "work",
            "winter",
            "outdoor",
            "travel",
            "walking",
        ),
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
            match = next(
                (
                    word
                    for word in words.get(attribute, ())
                    if re.search(rf"\b{re.escape(word)}\b", text)
                ),
                None,
            )
            if match:
                output.append(match)
    return output


def candidate_stats(
    repository: CatalogRepository,
    state: SessionState,
    candidates: Sequence[RetrievedProduct],
    latest: str,
) -> CandidateStats:
    """Compute bounded candidate-count and attribute-diversity evidence."""

    query = " ".join(
        (
            latest,
            state.category_anchor or "",
            " ".join(item.value for item in state.active_constraints),
        )
    )
    try:
        estimate = repository.estimate_count(query)
    except Exception:
        estimate = len(candidates)
    categories = [
        value.lower()
        for item in candidates[:100]
        for value in item.product.categories[:2]
        if value
    ]
    attribute_entropy = {
        attribute: entropy(attribute_values(candidates[:100], attribute))
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
        category_entropy=entropy(categories),
        attribute_entropy=attribute_entropy,
        active_hard_constraint_count=sum(
            item.hardness == "hard" for item in state.active_constraints
        ),
    )


def rank_evidence(
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
    previous_top = {
        str(value).strip() for value in previous_ranked_ids[:3] if str(value).strip()
    }
    top1_stability = (
        1.0
        if ranked
        and previous_ranked_ids
        and ranked[0].parent_asin == str(previous_ranked_ids[0]).strip()
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
        hard_constraint_count=(
            sum(item.hardness == "hard" for item in state.active_constraints)
            if state is not None
            else hard_count
        ),
        no_progress_streak=(
            getattr(state, "no_progress_streak", 0) if state is not None else 0
        ),
        ranked_ids=tuple(item.parent_asin for item in ranked[:10]),
    )


def lexical_rank_scores(
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
    unique_terms = list(
        dict.fromkeys(str(term).lower() for term in query_terms if str(term).strip())
    )
    if not unique_terms:
        return {}, {}
    document_frequency = {
        term: sum(term in set(tokens) for tokens in documents.values())
        for term in unique_terms
    }
    average_length = sum(len(tokens) for tokens in documents.values()) / max(
        len(documents), 1
    )
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


class RankingEngine:
    """Apply the existing deterministic feature fusion and ordering policy."""

    def __init__(self, repository: CatalogRepository, config: AgentConfig) -> None:
        self.repository = repository
        self.config = config

    def stats(
        self,
        state: SessionState,
        candidates: Sequence[RetrievedProduct],
        latest: str,
        *,
        entropy_fn: object | None = None,
        attribute_values_fn: object | None = None,
    ) -> CandidateStats:
        if callable(entropy_fn) and callable(attribute_values_fn):
            query = " ".join(
                (
                    latest,
                    state.category_anchor or "",
                    " ".join(item.value for item in state.active_constraints),
                )
            )
            try:
                estimate = self.repository.estimate_count(query)
            except Exception:
                estimate = len(candidates)
            bounded = candidates[:100]
            categories = [
                value.lower()
                for item in bounded
                for value in item.product.categories[:2]
                if value
            ]
            attributes = {
                attribute: entropy_fn(attribute_values_fn(bounded, attribute))
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
                category_entropy=entropy_fn(categories),
                attribute_entropy=attributes,
                active_hard_constraint_count=sum(
                    item.hardness == "hard" for item in state.active_constraints
                ),
            )
        return candidate_stats(self.repository, state, candidates, latest)

    def feature_rank(
        self,
        state: SessionState,
        candidates: Sequence[RetrievedProduct],
        context: RuntimeContext,
        *,
        limit: int | None = None,
        diversify: object | None = None,
        lexical_rank: object | None = None,
    ) -> list[RetrievedProduct]:
        input_candidates = list(candidates)
        if limit is not None:
            input_candidates = input_candidates[: max(int(limit), 0)]
        query_terms = list(safe_terms(" ".join(state.active_query_terms)))
        score_fn = lexical_rank if callable(lexical_rank) else lexical_rank_scores
        bm25_scores, title_scores = score_fn(input_candidates, query_terms)
        scored: list[tuple[float, int, RetrievedProduct]] = []
        for index, item in enumerate(input_candidates):
            product = item.product
            text = product.canonical_text.lower()
            score = item.score
            if bool(getattr(self.config, "ranking_bm25_enabled", True)):
                score += float(
                    getattr(self.config, "ranking_bm25_weight", 0.18)
                ) * bm25_scores.get(item.parent_asin, 0.0)
            if bool(getattr(self.config, "ranking_title_coverage_enabled", True)):
                score += float(
                    getattr(self.config, "ranking_title_coverage_weight", 0.12)
                ) * title_scores.get(item.parent_asin, 0.0)
            # Category membership is established by the exact category route
            # before this stage.  Give that route a relevance-first boost so a
            # globally popular but unrelated lexical hit cannot displace a
            # valid member merely because it has more ratings.
            if any(
                source.endswith(":category_exact")
                for source in item.source.split("+")
            ):
                score += 6.0
            if context.category_anchor:
                score += 1.8 * sum(
                    term in text for term in safe_terms(context.category_anchor)
                )
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
                score += float(
                    getattr(self.config, "ranking_profile_weight", 0.08)
                ) * sum(
                    bool(safe_terms(tag)) and safe_terms(tag)[0] in text
                    for tag in tags
                )
            if (
                bool(getattr(self.config, "ranking_rating_enabled", True))
                and product.rating is not None
            ):
                score += float(
                    getattr(self.config, "ranking_rating_weight", 0.05)
                ) * max(0.0, min(float(product.rating), 5.0))
            if (
                bool(getattr(self.config, "ranking_popularity_enabled", True))
                and product.rating_count is not None
            ):
                score += float(
                    getattr(self.config, "ranking_popularity_weight", 0.03)
                ) * math.log1p(max(product.rating_count, 0))
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
        unseen = [
            item for item in ordered if item.parent_asin not in state.seen_recommendations
        ]
        ordered = (
            unseen + [
                item
                for item in ordered
                if item.parent_asin in state.seen_recommendations
            ]
            if unseen
            else ordered
        )
        diversify_fn = diversify if callable(diversify) else _default_diversify
        if context.route_hint == "browsing":
            ordered = diversify_fn(ordered)
        output_limit = (
            max(int(limit), 0)
            if limit is not None
            else max(self.config.retrieval_limit, self.config.candidate_limit)
        )
        return ordered[:output_limit]


def _default_diversify(
    candidates: Sequence[RetrievedProduct], limit: int | None = None
) -> list[RetrievedProduct]:
    """Small local fallback to avoid coupling ranking to the facade."""

    target = len(candidates) if limit is None else max(int(limit), 0)
    if target <= 0:
        return []
    buckets: dict[str, list[RetrievedProduct]] = {}
    order: list[str] = []
    for item in candidates:
        key = next(
            (
                str(value).strip().lower()
                for value in reversed(item.product.categories)
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
            if buckets[key]:
                output.append(buckets[key].pop(0))
                progressed = True
                if len(output) >= target:
                    break
        if not progressed:
            break
    return output


__all__ = [
    "RankingEngine",
    "attribute_values",
    "candidate_stats",
    "entropy",
    "lexical_rank_scores",
    "rank_evidence",
]
