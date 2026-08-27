"""Semantic adapter and evaluator-facing response boundary helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .catalog import CatalogRepository, RetrievedProduct
from .config import AgentConfig
from .state import ALLOWED_ATTRIBUTES, RuntimeContext


FALLBACK_MESSAGE = "Here are the closest matches I found."


def normalize_limit(value: object) -> int:
    """Clamp a caller-provided recommendation count to the protocol limit."""

    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 10
    return max(min(value, 10), 0)


def valid_ids(
    repository: CatalogRepository, values: Sequence[object], limit: int
) -> list[str]:
    """Filter, deduplicate, and bound IDs against the frozen catalog."""

    if limit <= 0:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip() if value is not None else ""
        if item and item not in seen and item in repository.ids:
            seen.add(item)
            result.append(item)
            if len(result) >= limit:
                break
    return result


def apply_semantic(
    candidates: Sequence[RetrievedProduct], result: object
) -> list[RetrievedProduct]:
    """Apply a model-provided ordering while repairing unknown/duplicate IDs."""

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


def extract_usage(result: object | None) -> dict[str, int] | None:
    """Extract usage only from a successful model-backed result."""

    if result is None or not getattr(result, "backend", None):
        return None
    usage = getattr(result, "usage", None)
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


def extract_failures(result: object | None) -> list[object]:
    """Serialize model/backend failures for stable diagnostics."""

    failures = getattr(result, "failures", ()) if result is not None else ()
    output: list[object] = []
    for failure in failures or ():
        try:
            output.append(failure.as_dict())
        except Exception:
            output.append(str(failure)[:500])
    return output


def fallback_response(
    repository: CatalogRepository,
    top_k: object,
    *,
    valid_id_fn: Callable[[Sequence[object], int], list[str]] | None = None,
    limit_fn: Callable[[object], int] | None = None,
) -> dict[str, object]:
    """Construct a valid offline fallback response from popular products."""

    limit = limit_fn(top_k) if callable(limit_fn) else normalize_limit(top_k)
    validator = valid_id_fn or (
        lambda values, output_limit: valid_ids(repository, values, output_limit)
    )
    ids = validator(
        [item.parent_asin for item in repository.popular(limit)],
        limit,
    )
    return {
        "message": FALLBACK_MESSAGE,
        "ask_attribute": None,
        "recommendations": [{"parent_asin": item} for item in ids],
    }


def guard_response(
    repository: CatalogRepository,
    payload: object,
    top_k: object,
    *,
    valid_id_fn: Callable[[Sequence[object], int], list[str]] | None = None,
    fallback_fn: Callable[[object], dict[str, object]] | None = None,
    limit_fn: Callable[[object], int] | None = None,
) -> dict[str, object]:
    """Normalize an arbitrary pipeline result to the response contract."""

    if not isinstance(payload, Mapping):
        if fallback_fn is not None:
            return fallback_fn(top_k)
        return fallback_response(
            repository,
            top_k,
            valid_id_fn=valid_id_fn,
            limit_fn=limit_fn,
        )
    message = (
        payload.get("message")
        if isinstance(payload.get("message"), str)
        else FALLBACK_MESSAGE
    )
    ask = (
        payload.get("ask_attribute")
        if payload.get("ask_attribute") in ALLOWED_ATTRIBUTES
        else None
    )
    raw = payload.get("recommendations")
    values: list[object] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = [
            item.get("parent_asin") if isinstance(item, Mapping) else item
            for item in raw
        ]
    limit = limit_fn(top_k) if callable(limit_fn) else normalize_limit(top_k)
    validator = valid_id_fn or (
        lambda candidate_values, output_limit: valid_ids(
            repository, candidate_values, output_limit
        )
    )
    response: dict[str, object] = {
        "message": message,
        "ask_attribute": ask,
        "recommendations": [
            {"parent_asin": item} for item in validator(values, limit)
        ],
    }
    usage = payload.get("usage")
    # Keep the existing public guard semantics: bool is accepted here because
    # this is a final schema guard, while extract_usage is stricter for model
    # telemetry and diagnostics.
    if (
        isinstance(usage, Mapping)
        and isinstance(usage.get("prompt_tokens"), int)
        and isinstance(usage.get("completion_tokens"), int)
        and usage["prompt_tokens"] >= 0
        and usage["completion_tokens"] >= 0
    ):
        response["usage"] = {
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
        }
    return response


def response_message(attribute: str | None, over_general: bool) -> str:
    """Build the stable clarification copy used by the current policy."""

    if not attribute:
        return FALLBACK_MESSAGE
    label = {
        "use_case": "intended use",
        "other": "anything else that matters",
    }.get(attribute, attribute)
    prefix = (
        "I found several possibilities."
        if over_general
        else "Here are the closest matches."
    )
    return f"{prefix} Do you have a {label} preference?"


class SemanticResponseAdapter:
    """Invoke an optional semantic ranker and expose failures to the facade."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.last_failure: str | None = None

    def rank(
        self,
        semantic_ranker: object,
        context: RuntimeContext,
        candidates: Sequence[RetrievedProduct],
        *,
        limit: int | None = None,
    ) -> object | None:
        self.last_failure = None
        summary = {
            "category_anchor": context.category_anchor,
            "hard_constraints": [
                item.as_dict() for item in context.hard_constraints
            ],
            "soft_preferences": [
                item.as_dict() for item in context.soft_preferences
            ],
            "avoided_values": [item.as_dict() for item in context.avoided_values],
            "profile_priors": list(context.profile_priors),
            "epoch": context.intent_epoch,
            "turn": context.turn,
        }
        try:
            candidate_limit = self.config.candidate_limit
            if limit is not None:
                candidate_limit = min(candidate_limit, max(int(limit), 0))
            return semantic_ranker.rank(
                summary, list(candidates[:candidate_limit])
            )
        except Exception as exc:
            self.last_failure = f"{type(exc).__name__}: {exc}"[:500]
            return None


__all__ = [
    "FALLBACK_MESSAGE",
    "SemanticResponseAdapter",
    "apply_semantic",
    "extract_failures",
    "extract_usage",
    "fallback_response",
    "guard_response",
    "normalize_limit",
    "response_message",
    "valid_ids",
]
