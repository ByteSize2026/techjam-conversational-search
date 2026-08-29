"""Deterministic routing, candidate gating, and bounded clarification policy."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import math
import re
from typing import Any

from .catalog import ProductRecord
from .state import ALLOWED_ATTRIBUTES, CandidateStats, RuntimeContext, SessionState


CATEGORY_RECALL_SMALL_LIMIT = 500
CATEGORY_RECALL_MIN_LIMIT = 100
CATEGORY_RECALL_MAX_LIMIT = 400


def category_recall_ratio(
    active_hard_constraint_count: object = 0,
    *,
    browsing: float = 0.50,
    one_hard: float = 0.35,
    many_hard: float = 0.20,
) -> float:
    """Return the deterministic recall ratio for conversational specificity."""

    def _ratio(value: object, fallback: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = fallback
        return min(max(parsed, 0.0), 1.0) if math.isfinite(parsed) else fallback

    browsing_ratio = _ratio(browsing, 0.50)
    one_hard_ratio = _ratio(one_hard, 0.35)
    many_hard_ratio = _ratio(many_hard, 0.20)
    try:
        count = int(active_hard_constraint_count)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return browsing_ratio
    if count == 1:
        return one_hard_ratio
    return many_hard_ratio


def adaptive_category_budget(
    category_size: object,
    active_hard_constraint_count: object = 0,
    *,
    small_category_limit: int = CATEGORY_RECALL_SMALL_LIMIT,
    minimum: int = CATEGORY_RECALL_MIN_LIMIT,
    maximum: int = CATEGORY_RECALL_MAX_LIMIT,
    browsing_ratio: float = 0.50,
    one_hard_ratio: float = 0.35,
    many_hard_ratio: float = 0.20,
) -> int:
    """Calculate a bounded category-route budget.

    Small categories are cheap to scan completely.  Larger categories use a
    specificity-aware proportion and a hard upper bound so an uncertain
    anchor cannot trigger an unbounded catalog read.  Invalid or negative
    sizes safely produce zero.
    """

    try:
        size = max(int(category_size), 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return 0
    try:
        small_limit = max(int(small_category_limit), 0)
    except (TypeError, ValueError):
        small_limit = CATEGORY_RECALL_SMALL_LIMIT
    try:
        lower = max(int(minimum), 0)
    except (TypeError, ValueError):
        lower = CATEGORY_RECALL_MIN_LIMIT
    try:
        upper = max(int(maximum), lower)
    except (TypeError, ValueError):
        upper = max(CATEGORY_RECALL_MAX_LIMIT, lower)
    if size <= small_limit:
        return size
    proportional = math.ceil(
        size
        * category_recall_ratio(
            active_hard_constraint_count,
            browsing=browsing_ratio,
            one_hard=one_hard_ratio,
            many_hard=many_hard_ratio,
        )
    )
    return min(size, max(lower, min(proportional, upper)))


# Readable alias for callers that use the noun-first naming convention.
category_recall_budget = adaptive_category_budget


@dataclass(frozen=True)
class RouteDecision:
    mode: str
    buying_weight: float
    browsing_weight: float
    retrieval_budget: int
    reason_code: str

    @property
    def route(self) -> str:
        return self.mode


class IntentRouter:
    """Choose a route from distilled state on every turn.

    This is intentionally a small policy controller.  It can transition from
    browsing to buying after a newly disclosed requirement and never relies on
    scenario labels or hidden evaluator fields.
    """

    def __init__(
        self,
        *,
        retrieval_budget: int = 100,
        browsing_threshold: int = 0,
    ) -> None:
        self.retrieval_budget = max(int(retrieval_budget), 20)
        self.browsing_threshold = max(int(browsing_threshold), 0)

    def decide(self, context: RuntimeContext) -> RouteDecision:
        hard_count = len(context.hard_constraints)
        active_count = hard_count + len(context.soft_preferences)
        has_category = bool(context.category_anchor)
        has_specific_query = bool(context.hard_constraints or context.soft_preferences)
        broad_terms = any(
            word in " ".join(context.profile_priors).lower()
            for word in ("explor", "anything", "general", "various")
        )
        if hard_count >= 2 or (hard_count >= 1 and has_category and not broad_terms):
            mode = "buying"
            buying_weight, browsing_weight = 0.82, 0.18
            reason = "hard_constraints"
        elif hard_count >= 1 or (has_category and has_specific_query):
            mode = "mixed"
            buying_weight, browsing_weight = 0.65, 0.35
            reason = "mixed_constraints"
        elif has_category and not broad_terms:
            mode = "mixed"
            buying_weight, browsing_weight = 0.45, 0.55
            reason = "category_exploration"
        else:
            mode = "browsing"
            buying_weight, browsing_weight = 0.20, 0.80
            reason = "open_exploration"
        return RouteDecision(
            mode=mode,
            buying_weight=buying_weight,
            browsing_weight=browsing_weight,
            retrieval_budget=self.retrieval_budget,
            reason_code=reason,
        )

    route = decide


@dataclass(frozen=True)
class CandidateGateDecision:
    mode: str
    over_general: bool
    run_semantic_ranker: bool
    retrieval_limit: int
    reason_code: str

    @property
    def focused(self) -> bool:
        return not self.over_general


class CandidateGate:
    """Cheap cutoff before semantic ranking.

    Count and entropy thresholds are deliberately conservative: a low count
    from a truncated probe should not suppress the ranker, while an enormous
    unconstrained candidate set gets a capped diverse response and one useful
    question.
    """

    def __init__(
        self,
        *,
        count_threshold: int = 4000,
        entropy_threshold: float = 2.25,
        cutoff_limit: int = 60,
    ) -> None:
        self.count_threshold = max(int(count_threshold), 1)
        self.entropy_threshold = max(float(entropy_threshold), 0.0)
        self.cutoff_limit = max(int(cutoff_limit), 10)

    def evaluate(
        self,
        stats: CandidateStats | None,
        context: RuntimeContext | None = None,
        *,
        turn: int = 1,
    ) -> CandidateGateDecision:
        stats = stats or CandidateStats()
        hard_count = stats.active_hard_constraint_count
        if context is not None:
            hard_count = max(hard_count, len(context.hard_constraints))
        count_general = stats.estimated_count >= self.count_threshold
        entropy_general = (
            stats.category_entropy >= self.entropy_threshold
            and stats.estimated_count >= max(100, self.count_threshold // 10)
        )
        no_specificity = hard_count == 0 and context is not None and not (
            context.soft_preferences or context.category_anchor
        )
        over_general = bool((count_general or entropy_general) and (no_specificity or hard_count == 0))
        # Preserve a focused path late in a session when candidates have been
        # narrowed by the user's prior answers, even if the cheap estimate is
        # noisy.
        if context is not None and context.hard_constraints:
            over_general = False
        if over_general:
            return CandidateGateDecision(
                mode="over_general",
                over_general=True,
                run_semantic_ranker=False,
                retrieval_limit=self.cutoff_limit,
                reason_code="count_or_entropy_cutoff",
            )
        return CandidateGateDecision(
            mode="focused",
            over_general=False,
            run_semantic_ranker=True,
            retrieval_limit=max(self.cutoff_limit, 100),
            reason_code="focused_candidates",
        )

    gate = evaluate


def _entropy(values: Iterable[str]) -> float:
    counts = Counter(value for value in values if value)
    total = sum(counts.values())
    if total <= 1 or len(counts) <= 1:
        return 0.0
    return -sum((count / total) * math.log(count / total) for count in counts.values())


def _contains_any(text: str, words: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words)


class ClarificationPolicy:
    """Select at most one not-yet-exhausted structured question per turn."""

    ATTRIBUTE_ORDER = (
        "material",
        "color",
        "size",
        "style",
        "brand",
        "budget",
        "feature",
        "use_case",
        "category",
        "other",
    )

    _WORDS: dict[str, tuple[str, ...]] = {
        "material": ("cotton", "polyester", "nylon", "leather", "wool", "silk", "linen", "denim"),
        "color": ("black", "white", "blue", "red", "pink", "green", "brown", "gray", "grey", "navy"),
        "size": ("size", "sizing", "width", "wide", "narrow", "small", "medium", "large"),
        "style": ("fit", "comfort", "style", "sleeve", "neck", "casual", "formal", "warmth"),
        "brand": ("brand",),
        "budget": ("price", "budget", "under", "$"),
        "feature": ("waterproof", "pocket", "hood", "durable", "feature"),
        "use_case": ("hiking", "running", "gym", "work", "winter", "outdoor", "travel", "walking"),
        "category": ("shoe", "boot", "shirt", "jacket", "dress", "pants", "clothing"),
        "other": (),
    }

    def __init__(self, mode: str = "protocol_aware", *, max_questions: int = 1) -> None:
        self.mode = mode if mode in {"protocol_aware", "catalog_entropy"} else "protocol_aware"
        self.max_questions = max(int(max_questions), 1)

    def _candidate_values(self, candidates: Sequence[object], attribute: str) -> list[str]:
        values: list[str] = []
        words = self._WORDS.get(attribute, ())
        for candidate in candidates:
            if isinstance(candidate, ProductRecord):
                text = candidate.canonical_text
                if attribute == "brand" and candidate.store:
                    values.append(candidate.store.lower())
                elif attribute == "budget" and candidate.price is not None:
                    values.append(str(round(candidate.price, 2)))
                elif attribute == "category":
                    values.append((candidate.categories[0] if candidate.categories else "").lower())
                else:
                    matched = next((word for word in words if re.search(rf"\b{re.escape(word)}\b", text, re.I)), "")
                    if matched:
                        values.append(matched.lower())
                continue
            if isinstance(candidate, dict):
                text = " ".join(str(value) for value in candidate.values())
            else:
                text = str(candidate)
            matched = next((word for word in words if re.search(rf"\b{re.escape(word)}\b", text, re.I)), "")
            if matched:
                values.append(matched.lower())
        return values

    def _utility(
        self,
        attribute: str,
        state: SessionState,
        candidates: Sequence[object],
        stats: CandidateStats | None,
    ) -> float:
        if attribute in state.no_preference:
            return float("-inf")
        if attribute in state.asked_set:
            # Repeating is only useful when no untouched attribute has any
            # candidate evidence; protocol-aware remains bounded to once.
            return float("-inf")
        if attribute in {item.attribute for item in state.active_constraints}:
            # Already known from a source other than this policy asking for
            # it (an override, an unprompted statement) -- re-asking reads as
            # not having listened. asked_set only tracks what *this policy*
            # asked, so it misses that case on its own.
            return float("-inf")
        entropy = 0.0
        if stats is not None:
            entropy = float(stats.attribute_entropy.get(attribute, 0.0))
        if entropy <= 0.0:
            entropy = _entropy(self._candidate_values(candidates, attribute))
        evidence = len(self._candidate_values(candidates, attribute))
        base = 0.18 * self.ATTRIBUTE_ORDER.index(attribute)
        # ``other`` is a protocol fallback, not a preferred first question.
        if attribute == "other":
            base -= 0.4
        return entropy + min(evidence, 10) * 0.02 - base

    def choose_attribute(
        self,
        state: SessionState,
        candidates: Sequence[object] = (),
        stats: CandidateStats | None = None,
        *,
        turn: int = 1,
        remaining_turns: int | None = None,
    ) -> str | None:
        if remaining_turns is None:
            remaining_turns = max(10 - int(turn), 0)
        if remaining_turns <= 0 or self.max_questions <= 0:
            return None
        known_attributes = {item.attribute for item in state.active_constraints}
        available = [
            attribute
            for attribute in self.ATTRIBUTE_ORDER
            if attribute in ALLOWED_ATTRIBUTES
            and attribute not in state.no_preference
            and attribute not in state.asked_set
            # A hard/soft value can arrive without this policy ever having
            # asked for it (override, unprompted statement) -- asked_set
            # alone misses that, so re-ask the exact thing the customer
            # just told us (showcase b, artifacts/full_live_test/
            # final_check_20_20260830_010957, public_0002/public_0013).
            and attribute not in known_attributes
        ]
        if not available:
            return None
        if self.mode == "protocol_aware":
            # Prefer attributes with actual candidate evidence.  No evidence
            # at all means there is nothing yet to justify a question about
            # any specific attribute -- return None (the caller falls
            # through to Search/Rank) rather than guessing the first
            # not-yet-excluded attribute in a fixed order.  08-28's
            # Router/Value-Node graph (design.md Section 6) relies on this:
            # both ``SlotCheckRouter`` and ``CandidatePoolRouter`` call this
            # method to decide whether to *ask at all*, not only what to ask
            # once asking is already decided -- unlike this method's v1 call
            # site, which only ever ran after a full Search/Rank pass had
            # already produced real per-turn evidence, so an evidence-free
            # call was never actually possible there.
            with_evidence = [
                attribute
                for attribute in available
                if self._candidate_values(candidates, attribute)
                or (stats is not None and stats.attribute_entropy.get(attribute, 0.0) > 0)
            ]
            return with_evidence[0] if with_evidence else None
        return max(available, key=lambda attribute: self._utility(attribute, state, candidates, stats))

    def choose(
        self,
        state: SessionState,
        candidates: Sequence[object] = (),
        stats: CandidateStats | None = None,
        *,
        turn: int = 1,
        remaining_turns: int | None = None,
    ) -> str | None:
        return self.choose_attribute(
            state,
            candidates,
            stats,
            turn=turn,
            remaining_turns=remaining_turns,
        )


__all__ = [
    "CATEGORY_RECALL_MAX_LIMIT",
    "CATEGORY_RECALL_MIN_LIMIT",
    "CATEGORY_RECALL_SMALL_LIMIT",
    "CandidateGate",
    "CandidateGateDecision",
    "ClarificationPolicy",
    "IntentRouter",
    "RouteDecision",
    "adaptive_category_budget",
    "category_recall_budget",
    "category_recall_ratio",
]
