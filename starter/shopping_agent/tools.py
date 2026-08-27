"""Catalog-only tools available to the bounded shopping-agent loop."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping, Sequence

from .actions import (
    ActionObservation,
    ActionValidationError,
    PlannedAction,
    bounded_ids,
)
from .catalog import CatalogRepository, ProductRecord, text_value
from .state import ALLOWED_ATTRIBUTES, SessionState

MAX_SEARCH_RESULTS = 20
MAX_WORKING_CANDIDATES = 100
MAX_FILTER_INPUTS = 10
MAX_DETAIL_TEXT = 1200

_NUMERIC_ATTRIBUTES = {
    "budget": "price",
    "price": "price",
    "rating": "rating",
    "rating_count": "rating_count",
}
_TEXT_ATTRIBUTES = frozenset(
    {
        "category",
        "categories",
        "material",
        "color",
        "size",
        "style",
        "brand",
        "feature",
        "use_case",
        "other",
        "title",
        "store",
    }
)
_OPERATORS = frozenset(
    {"contains", "equals", "not_contains", "lt", "lte", "gt", "gte", "between"}
)


def _integer(value: object, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value) if isinstance(value, (int, float, str)) else default
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        match = re.search(r"-?\d+(?:[,.]\d+)*", str(value))
        if not match:
            return None
        try:
            parsed = float(match.group(0).replace(",", ""))
        except ValueError:
            return None
    return parsed if math.isfinite(parsed) else None


def _compact_product(record: ProductRecord) -> dict[str, object]:
    return {
        "parent_asin": record.parent_asin,
        "title": record.title[:320],
        "categories": list(record.categories[:6]),
        "price": record.price,
        "rating": record.rating,
        "rating_count": record.rating_count,
        "store": (record.store or "")[:160] or None,
    }


def _product_text(record: ProductRecord, attribute: str) -> tuple[str | None, bool]:
    """Return text plus whether the catalog exposes that field explicitly."""

    attribute = attribute.lower()
    if attribute in {"category", "categories"}:
        value = text_value(record.categories)
        return (value or None, bool(value))
    if attribute == "brand" or attribute == "store":
        return (record.store, bool(record.store))
    if attribute == "title":
        return (record.title or None, bool(record.title))
    if attribute in record.details:
        return (record.details[attribute], True)
    matching_details = [
        value for key, value in record.details.items() if attribute in key.lower().replace(" ", "_")
    ]
    if matching_details:
        return (" ".join(matching_details), True)
    if attribute == "feature":
        evidence = " ".join((*record.features, *record.description, record.title)).strip()
        return (evidence or None, bool(record.features))
    if attribute in {"material", "color", "size", "style", "feature", "use_case", "other"}:
        evidence = " ".join(
            part
            for part in (
                record.title,
                text_value(record.features),
                text_value(record.details),
                text_value(record.description),
            )
            if part
        )
        # Free text can prove a positive mention, but its silence is not an
        # explicit statement that an attribute is absent.
        return (evidence or None, False)
    return (None, False)


class ShoppingToolbox:
    """Execute validated actions against one repository and one session."""

    def __init__(
        self,
        repository: CatalogRepository,
        state: SessionState,
        *,
        top_k: int = 10,
        deterministic_candidate_ids: Sequence[object] = (),
    ) -> None:
        self.repository = repository
        self.state = state
        self.top_k = min(max(int(top_k), 0), 10)
        self.deterministic_candidate_ids = {
            str(item).strip() for item in deterministic_candidate_ids if str(item).strip()
        }

    @property
    def observed_candidate_ids(self) -> set[str]:
        """IDs that retrieval has already exposed to this session/planner."""

        return set(self.state.tool_candidate_ids) | self.deterministic_candidate_ids

    def _require_observed(self, ids: Sequence[str], *, action: str) -> None:
        unobserved = [item for item in ids if item not in self.observed_candidate_ids]
        if unobserved:
            # Do not reveal whether a guessed ID happens to exist in the
            # catalog.  The planner must discover IDs through search first.
            raise ActionValidationError(
                f"{action} requires product IDs returned by an earlier catalog action"
            )

    def execute(self, action: PlannedAction) -> ActionObservation:
        started = time.perf_counter()
        try:
            handler = getattr(self, action.name, None)
            if not callable(handler):
                raise ActionValidationError(f"unsupported action: {action.name}")
            payload = handler(action.arguments)
            return ActionObservation(
                action=action.name,
                ok=True,
                payload=payload,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
        except (ActionValidationError, ValueError, TypeError) as exc:
            return ActionObservation(
                action=action.name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:500],
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

    def _remember(self, ids: Sequence[object]) -> list[str]:
        output: list[str] = []
        seen = set(self.state.tool_candidate_ids)
        for raw in ids:
            item = str(raw).strip()
            if not item or item not in self.repository.ids:
                continue
            output.append(item)
            if item not in seen and len(self.state.tool_candidate_ids) < MAX_WORKING_CANDIDATES:
                self.state.tool_candidate_ids.append(item)
                seen.add(item)
        return output

    def search_products(self, arguments: Mapping[str, object]) -> dict[str, object]:
        query = str(arguments.get("query") or "").strip()[:800]
        if not query:
            raise ActionValidationError("search query must not be empty")
        limit = _integer(arguments.get("limit"), 10, minimum=1, maximum=MAX_SEARCH_RESULTS)
        category = str(arguments.get("category") or "").strip()[:160]
        found = self.repository.search_with_scores(query, limit, source="tool:search")
        if category:
            resolution, category_found = self.repository.category_with_scores(category, limit)
            if resolution.resolved:
                category_ids = {item.parent_asin for item in category_found}
                narrowed = [item for item in found if item.parent_asin in category_ids]
                found = narrowed or category_found
        found = found[:limit]
        self._remember([item.parent_asin for item in found])
        return {
            "query": query,
            "category": category or None,
            "count": len(found),
            "products": [_compact_product(item.product) for item in found],
        }

    def filter_products(self, arguments: Mapping[str, object]) -> dict[str, object]:
        ids = bounded_ids(arguments.get("product_ids"), limit=MAX_FILTER_INPUTS)
        if not ids:
            raise ActionValidationError("filter requires at least one product ID")
        self._require_observed(ids, action="filter_products")
        attribute = str(arguments.get("attribute") or "").strip().lower().replace(" ", "_")
        if attribute not in _TEXT_ATTRIBUTES and attribute not in _NUMERIC_ATTRIBUTES:
            raise ActionValidationError(f"unsupported filter attribute: {attribute or '<empty>'}")
        operator = str(arguments.get("operator") or "contains").strip().lower()
        if operator not in _OPERATORS:
            raise ActionValidationError(f"unsupported filter operator: {operator}")
        value = arguments.get("value")
        second_value = arguments.get("max_value")
        records = self.repository.materialize(ids, MAX_FILTER_INPUTS)
        matched: list[ProductRecord] = []
        unknown_ids: list[str] = []
        for record in records:
            if attribute in _NUMERIC_ATTRIBUTES:
                field = _NUMERIC_ATTRIBUTES[attribute]
                actual = getattr(record, field)
                if actual is None:
                    unknown_ids.append(record.parent_asin)
                    continue
                expected = _number(value)
                if expected is None:
                    raise ActionValidationError("numeric filter value is invalid")
                other = _number(second_value)
                checks = {
                    "lt": actual < expected,
                    "lte": actual <= expected,
                    "gt": actual > expected,
                    "gte": actual >= expected,
                    "equals": math.isclose(float(actual), expected, rel_tol=0.0, abs_tol=1e-9),
                    "between": other is not None and min(expected, other) <= actual <= max(expected, other),
                }
                if operator not in checks:
                    raise ActionValidationError(f"operator {operator} is not numeric")
                is_match = bool(checks[operator])
            else:
                actual_text, explicit_field = _product_text(record, attribute)
                if not actual_text:
                    unknown_ids.append(record.parent_asin)
                    continue
                expected_text = str(value or "").strip().lower()
                if not expected_text:
                    raise ActionValidationError("text filter value must not be empty")
                normalized = actual_text.lower()
                if operator == "contains":
                    is_match = expected_text in normalized
                    if not is_match and not explicit_field:
                        unknown_ids.append(record.parent_asin)
                        continue
                elif operator == "not_contains":
                    if not explicit_field:
                        unknown_ids.append(record.parent_asin)
                        continue
                    is_match = expected_text not in normalized
                elif operator == "equals":
                    if not explicit_field:
                        unknown_ids.append(record.parent_asin)
                        continue
                    is_match = normalized.strip() == expected_text
                else:
                    raise ActionValidationError(f"operator {operator} is not textual")
            if is_match:
                matched.append(record)
        matched_ids = self._remember([record.parent_asin for record in matched])
        return {
            "attribute": attribute,
            "operator": operator,
            "input_count": len(records),
            "matched_count": len(matched_ids),
            "unknown_count": len(unknown_ids),
            "unknown_ids": unknown_ids[:20],
            "products": [_compact_product(record) for record in matched[:MAX_SEARCH_RESULTS]],
        }

    def get_product_details(self, arguments: Mapping[str, object]) -> dict[str, object]:
        parent_asin = str(arguments.get("parent_asin") or "").strip()[:80]
        if not parent_asin:
            raise ActionValidationError("parent_asin must not be empty")
        self._require_observed([parent_asin], action="get_product_details")
        record = self.repository.get(parent_asin)
        if record is None:
            raise ActionValidationError("unknown parent_asin")
        self._remember([parent_asin])
        details = {
            str(key)[:120]: str(value)[:500]
            for key, value in list(record.details.items())[:20]
        }
        return {
            **_compact_product(record),
            "features": [str(item)[:400] for item in record.features[:10]],
            "description": [str(item)[:MAX_DETAIL_TEXT] for item in record.description[:3]],
            "details": details,
        }

    def get_user_profile(self, arguments: Mapping[str, object]) -> dict[str, object]:
        if arguments:
            # Extra fields are ignored, but the action remains deterministic.
            pass
        self.state.profile_loaded = True
        allowed = (
            "purchase_frequency",
            "average_prior_rating",
            "rating_style",
            "preference_tags",
            "summary",
        )
        profile: dict[str, object] = {}
        for key in allowed:
            value = self.state.profile.get(key)
            if isinstance(value, str):
                profile[key] = value[:500]
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                profile[key] = [str(item)[:100] for item in list(value)[:20]]
            elif isinstance(value, (int, float, bool)) or value is None:
                profile[key] = value
        self.state.session_profile = dict(profile)
        return {"profile_loaded": True, "profile": profile}

    def ask_user(self, arguments: Mapping[str, object]) -> dict[str, object]:
        question = str(arguments.get("question") or "").strip()[:500]
        attribute = str(arguments.get("ask_attribute") or arguments.get("attribute") or "").strip().lower()
        if not question:
            raise ActionValidationError("ask_user question must not be empty")
        if attribute not in ALLOWED_ATTRIBUTES:
            raise ActionValidationError("ask_user attribute is not allowed")
        return {
            "pause": True,
            "question": question,
            "ask_attribute": attribute,
            "candidate_ids": list(self.state.tool_candidate_ids[:20]),
        }

    def recommend_products(self, arguments: Mapping[str, object]) -> dict[str, object]:
        ids = bounded_ids(
            arguments.get("parent_asins", arguments.get("product_ids")),
            limit=max(self.top_k, 1),
        )
        allowed = self.observed_candidate_ids
        valid = [item for item in ids if item in allowed and item in self.repository.ids]
        valid = list(dict.fromkeys(valid))[: self.top_k]
        if not valid:
            raise ActionValidationError("recommendation contains no observed catalog product")
        # A planner-provided sentence is untrusted free text and could assert
        # product facts that are absent from the frozen catalog.  Keep final
        # wording deterministic until a catalog-grounded response composer is
        # introduced.
        message = "Here are the closest catalog matches I found."
        return {"terminal": True, "message": message, "parent_asins": valid}


__all__ = [
    "MAX_DETAIL_TEXT",
    "MAX_FILTER_INPUTS",
    "MAX_SEARCH_RESULTS",
    "MAX_WORKING_CANDIDATES",
    "ShoppingToolbox",
]
