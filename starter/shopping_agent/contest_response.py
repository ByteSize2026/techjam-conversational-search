"""Contract guard for ContestAgent responses.

Missing or illegal fields are repaired here so the evaluator never sees a
non-string message, an out-of-enum ask_attribute, or a parent_asin outside
the frozen catalog. Usage is kept only when both token counts are
non-negative ints. MiniLM/FlashRank already no-op when weights are missing;
this module does not load models.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .contest_index import ContestIndex
from .contest_text import ALLOWED_ATTRIBUTES

FALLBACK_MESSAGE = "Let me try again. Which detail matters most to you?"


def normalize_limit(top_k: object) -> int:
    try:
        limit = int(top_k)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        limit = 10
    return min(max(limit, 0), 10)


def valid_ids(index: ContestIndex, values: Sequence[object], limit: int) -> list[str]:
    allowed = getattr(index, "id_set", None)
    if not isinstance(allowed, set):
        allowed = set(index.ids)
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        parent = str(value or "").strip()
        if not parent or parent in seen or parent not in allowed:
            continue
        seen.add(parent)
        output.append(parent)
        if len(output) >= limit:
            break
    return output


def guard_response(
    index: ContestIndex,
    payload: object,
    top_k: object,
    *,
    fallback_fn: Callable[[object], dict[str, object]] | None = None,
) -> dict[str, object]:
    """Normalize an arbitrary pipeline result to the evaluator contract."""

    limit = normalize_limit(top_k)
    if not isinstance(payload, Mapping):
        if fallback_fn is not None:
            return fallback_fn(top_k)
        return {
            "message": FALLBACK_MESSAGE,
            "ask_attribute": "other",
            "recommendations": [
                {"parent_asin": index.ids[idx]} for idx in index.popular(limit)
            ],
        }
    message = (
        payload.get("message")
        if isinstance(payload.get("message"), str)
        else FALLBACK_MESSAGE
    )
    ask = payload.get("ask_attribute")
    if ask not in ALLOWED_ATTRIBUTES:
        ask = None
    raw = payload.get("recommendations")
    values: list[object] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = [
            item.get("parent_asin") if isinstance(item, Mapping) else item
            for item in raw
        ]
    response: dict[str, object] = {
        "message": message,
        "ask_attribute": ask,
        "recommendations": [
            {"parent_asin": item} for item in valid_ids(index, values, limit)
        ],
    }
    usage = payload.get("usage")
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
