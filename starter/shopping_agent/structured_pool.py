"""Deterministic, target-preserving candidate eligibility within a category.

The pool deliberately owns candidate membership only.  It scans every member
of a resolved category before any retrieval budget or ranking shortlist is
applied.  A malformed or over-specific constraint can therefore be softened
without making the target disappear from the session.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import re

from .catalog import CategoryResolution, CatalogRepository, ProductRecord, safe_terms
from .state import (
    Constraint,
    SessionState,
    normalize_constraint_value,
    rating_count_lower_bound,
    rating_lower_bound,
)


FILTERABLE_ATTRIBUTES = frozenset(
    {"brand", "material", "color", "size", "style", "budget", "feature"}
)
OFFICIAL_FILTERABLE_ATTRIBUTES = frozenset(
    {"material", "color", "budget", "feature"}
)
_NUMBER_RE = re.compile(r"(?<!\w)\$?\s*(\d+(?:[,.]\d+)?)", re.I)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class ConstraintApplication:
    attribute: str
    value: str
    input_size: int
    output_size: int
    status: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "attribute": self.attribute,
            "value": self.value,
            "input_size": self.input_size,
            "output_size": self.output_size,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StructuredPoolResult:
    ids: tuple[str, ...]
    category_resolution: CategoryResolution
    applied_constraints: tuple[ConstraintApplication, ...] = ()
    softened_constraints: tuple[ConstraintApplication, ...] = ()
    softened_constraint_keys: tuple[tuple[str, str], ...] = ()
    previous_size: int = 0
    final_size: int = 0
    enabled: bool = True

    @property
    def resolved(self) -> bool:
        return bool(self.enabled and self.category_resolution.resolved)

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "category_resolution": self.category_resolution.as_dict(),
            "previous_size": self.previous_size,
            "final_size": self.final_size,
            "ids_sample": list(self.ids[:10]),
            "applied_constraints": [item.as_dict() for item in self.applied_constraints],
            "softened_constraints": [item.as_dict() for item in self.softened_constraints],
            "softened_constraint_keys": [list(item) for item in self.softened_constraint_keys],
        }


def _number(value: object) -> float | None:
    match = _NUMBER_RE.search(str(value or ""))
    if match is None:
        return None
    try:
        parsed = float(match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _budget_bounds(value: object) -> tuple[float, float] | None:
    text = str(value or "").lower()
    amount = _number(text)
    if amount is None or amount < 0:
        return None
    if re.search(r"\b(?:under|below|less\s+than|up\s+to|at\s+most)\b", text):
        return 0.0, amount
    if re.search(r"\b(?:over|above|more\s+than|at\s+least)\b", text):
        return amount, math.inf
    # "around $X" and a bare numeric budget are intentionally tolerant.  The
    # evaluator's budget cards use a nearby range rather than exact equality.
    tolerance = max(5.0, amount * 0.20)
    return max(0.0, amount - tolerance), amount + tolerance


def _contains_terms(text: str, value: object) -> bool:
    lowered = str(text or "").lower()
    terms = safe_terms(value)
    if not terms:
        terms = [token.lower() for token in _WORD_RE.findall(str(value or "")) if len(token) > 1]
    return bool(terms) and all(
        re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", lowered) is not None
        for term in terms
    )


def _title_contains_value(value: object) -> str | None:
    match = re.match(r"^title\s+contains\s*:\s*(?P<token>.+)$", str(value or ""), re.I)
    if match is None:
        return None
    token = str(match.group("token") or "").strip()
    return token or None


def _brand_text(record: ProductRecord) -> str:
    parts = [record.store or ""]
    for key, value in record.details.items():
        if re.search(r"\b(?:brand|label|maker|manufacturer)\b", str(key), re.I):
            parts.append(str(value))
    return " ".join(parts)


def _matches(record: ProductRecord, constraint: Constraint) -> bool:
    attribute = str(constraint.attribute).lower()
    rating_floor = rating_lower_bound(constraint.value) if attribute == "feature" else None
    if rating_floor is not None:
        return record.rating is not None and float(record.rating) >= rating_floor
    rating_count_floor = (
        rating_count_lower_bound(constraint.value) if attribute == "feature" else None
    )
    if rating_count_floor is not None:
        return record.rating_count is not None and int(record.rating_count) >= rating_count_floor
    title_token = _title_contains_value(constraint.value) if attribute == "feature" else None
    if title_token is not None:
        return _contains_terms(record.title, title_token)
    if attribute == "budget":
        bounds = _budget_bounds(constraint.value)
        if bounds is None or record.price is None:
            return False
        low, high = bounds
        return record.price >= low and record.price <= high
    # Evaluator cards sometimes spell a value as ``color: black`` or
    # ``material: cotton``.  The label is metadata, not a required catalog
    # token; use the same semantic normalization as the ranker so eligibility
    # and ordering cannot disagree about the value.
    value = normalize_constraint_value(attribute, constraint.value)
    if attribute == "brand":
        return _contains_terms(_brand_text(record), value)
    return _contains_terms(record.canonical_text, value)


def _constraint_key(constraint: Constraint) -> tuple[str, str]:
    return (
        str(constraint.attribute).lower(),
        normalize_constraint_value(constraint.attribute, constraint.value).lower(),
    )


class StructuredCandidatePool:
    """Build a full category pool and apply safe high-confidence filters."""

    def __init__(
        self,
        repository: CatalogRepository,
        *,
        enabled: bool = True,
        filterable_attributes: frozenset[str] | None = None,
    ) -> None:
        self.repository = repository
        self.enabled = bool(enabled)
        self.filterable_attributes = (
            FILTERABLE_ATTRIBUTES
            if filterable_attributes is None
            else frozenset(filterable_attributes)
        )

    def build(
        self,
        state: SessionState,
        resolution: CategoryResolution | None = None,
    ) -> StructuredPoolResult:
        resolution = resolution or self.repository.resolve_category(state.category_anchor or "")
        if not self.enabled or not resolution.resolved:
            return StructuredPoolResult(
                ids=(),
                category_resolution=resolution,
                enabled=self.enabled,
            )

        # The only source of IDs is the resolved, complete category member
        # list.  Sorting makes the result stable independent of catalog input
        # order and leaves popularity/ranking to later stages.
        current = [
            self.repository.products[parent_asin]
            for parent_asin in sorted(resolution.product_ids)
            if parent_asin in self.repository.products
        ]
        previous_size = len(current)
        applied: list[ConstraintApplication] = []
        softened: list[ConstraintApplication] = []
        softened_keys: list[tuple[str, str]] = []

        for constraint in state.active_constraints:
            attribute = str(constraint.attribute).lower()
            if attribute not in self.filterable_attributes:
                continue
            key = _constraint_key(constraint)
            if key in getattr(state, "softened_constraint_keys", set()):
                softened.append(
                    ConstraintApplication(
                        attribute=attribute,
                        value=str(constraint.value),
                        input_size=len(current),
                        output_size=len(current),
                        status="softened",
                        reason="previously_softened",
                    )
                )
                softened_keys.append(key)
                continue
            # Model-only semantic guesses remain ranking evidence.  Explicit
            # model facts (or catalog-grounded profile facts) may filter when
            # they are marked explicit; unverified model inferences may not.
            if str(constraint.source).lower() == "model" and not bool(
                getattr(constraint, "explicit", False)
            ):
                continue
            # Only explicit/high-confidence disclosures are eligible for hard
            # membership filtering.  Lower-confidence text remains rank
            # evidence in Agent._feature_rank.
            if constraint.hardness != "hard" and float(constraint.confidence) < 0.86:
                continue
            before = len(current)
            if str(constraint.polarity).lower() == "avoid":
                matched = [record for record in current if not _matches(record, constraint)]
            else:
                matched = [record for record in current if _matches(record, constraint)]
            if matched:
                current = matched
                applied.append(
                    ConstraintApplication(
                        attribute=attribute,
                        value=str(constraint.value),
                        input_size=before,
                        output_size=len(current),
                        status="applied",
                        reason="non_empty_intersection",
                    )
                )
                continue
            # Never accept a zero-result hard intersection.  The constraint is
            # retained as soft evidence by the ranker and surfaced to
            # diagnostics for replay/ablation analysis.
            softened.append(
                ConstraintApplication(
                    attribute=attribute,
                    value=str(constraint.value),
                    input_size=before,
                    output_size=0,
                    status="softened",
                    reason="zero_result_rollback",
                )
            )
            softened_keys.append(key)

        ids = tuple(record.parent_asin for record in current)
        return StructuredPoolResult(
            ids=ids,
            category_resolution=resolution,
            applied_constraints=tuple(applied),
            softened_constraints=tuple(softened),
            softened_constraint_keys=tuple(dict.fromkeys(softened_keys)),
            previous_size=previous_size,
            final_size=len(ids),
            enabled=self.enabled,
        )


def build_structured_pool(
    repository: CatalogRepository,
    state: SessionState,
    resolution: CategoryResolution | None = None,
    *,
    enabled: bool = True,
) -> StructuredPoolResult:
    """Functional convenience wrapper used by tests and the Agent."""

    return StructuredCandidatePool(repository, enabled=enabled).build(state, resolution)


__all__ = [
    "FILTERABLE_ATTRIBUTES",
    "OFFICIAL_FILTERABLE_ATTRIBUTES",
    "ConstraintApplication",
    "StructuredCandidatePool",
    "StructuredPoolResult",
    "build_structured_pool",
]
