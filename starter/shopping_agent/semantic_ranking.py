"""Bounded listwise semantic ranking over an already validated candidate set."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
import json
import math
from typing import Any

from .config import AgentConfig
from .model import (
    BackendFailure,
    ModelCompletionResult,
    TieredModelClient,
    TokenUsage,
)


@dataclass(frozen=True)
class SemanticRankingResult:
    """The safe output of :class:`LLMSemanticRanker`.

    ``ordered_parent_asins`` is always a duplicate-free permutation of the
    candidate IDs supplied to ``rank`` (after the configured bound is
    applied).  A model can reorder these IDs, but can never add a new one.
    """

    ordered_parent_asins: tuple[str, ...]
    scores: dict[str, float]
    backend: str | None = None
    usage: TokenUsage | None = None
    failures: tuple[BackendFailure, ...] = ()

    @property
    def parent_asins(self) -> tuple[str, ...]:
        return self.ordered_parent_asins

    @property
    def ordered_ids(self) -> tuple[str, ...]:
        return self.ordered_parent_asins

    @property
    def used_model(self) -> bool:
        return self.backend is not None

    @property
    def backend_name(self) -> str | None:
        return self.backend

    @property
    def diagnostics(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "failures": [failure.as_dict() for failure in self.failures],
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "ordered_parent_asins": list(self.ordered_parent_asins),
            "scores": dict(self.scores),
            "backend": self.backend,
            "usage": self.usage.as_dict() if self.usage else None,
            "diagnostics": self.diagnostics,
        }


def _candidate_id(candidate: object) -> str | None:
    if isinstance(candidate, Mapping):
        value = candidate.get("parent_asin")
    else:
        value = getattr(candidate, "parent_asin", None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _bounded_text(value: object, limit: int = 1800) -> str:
    """Serialize one compressed candidate without allowing prompt growth."""

    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(dict(value))
    elif isinstance(value, (list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    return text[:limit]


def _candidate_payload(candidate: object, parent_asin: str) -> dict[str, object]:
    compressed = getattr(candidate, "compressed", None)
    if callable(compressed):
        try:
            value = compressed()
        except Exception:
            value = {}
    elif isinstance(candidate, Mapping):
        value = candidate
    else:
        value = getattr(candidate, "text", None) or str(candidate)

    if isinstance(value, Mapping):
        payload: dict[str, object] = dict(value)
        # The ID used for the whitelist always comes from the candidate
        # object, never from a potentially stale compressed representation.
        payload["parent_asin"] = parent_asin
        return payload
    return {"parent_asin": parent_asin, "text": value}


def _as_rank_entries(value: object) -> object:
    if isinstance(value, list):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("ranking response must be an object or list")
    for key in (
        "ranked_parent_asins",
        "ordered_parent_asins",
        "parent_asins",
        "ranked_ids",
        "ordered_ids",
        "ranking",
        "items",
        "results",
    ):
        if key in value:
            entries = value[key]
            if not isinstance(entries, list):
                raise ValueError(f"{key} must be a list")
            return value
    # A dict with only score values is still a useful bounded response; the
    # normalizer will sort known IDs by score and append all others.
    if isinstance(value.get("scores"), Mapping):
        return value
    raise ValueError("ranking response has no ranking list")


def _validate_ranking_json(value: object) -> object:
    """Validate shape only; unknown/duplicate IDs are repaired, not trusted."""

    return _as_rank_entries(value)


def _entry_id_and_score(entry: object) -> tuple[str | None, float | None]:
    if isinstance(entry, str):
        return entry.strip() or None, None
    if isinstance(entry, Mapping):
        value = (
            entry.get("parent_asin")
            or entry.get("id")
            or entry.get("product_id")
            or entry.get("asin")
        )
        score = entry.get("score")
        if value is None:
            return None, None
        parsed_score: float | None = None
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            if math.isfinite(float(score)):
                parsed_score = float(score)
        return str(value).strip() or None, parsed_score
    return None, None


def _extract_entries(value: object) -> tuple[list[object], Mapping[str, object] | None]:
    if isinstance(value, list):
        return value, None
    if not isinstance(value, Mapping):
        return [], None
    for key in (
        "ranked_parent_asins",
        "ordered_parent_asins",
        "parent_asins",
        "ranked_ids",
        "ordered_ids",
        "ranking",
        "items",
        "results",
    ):
        entries = value.get(key)
        if isinstance(entries, list):
            scores = value.get("scores")
            return entries, scores if isinstance(scores, Mapping) else None
    scores = value.get("scores")
    if isinstance(scores, Mapping):
        # Score-only output is normalized in deterministic score-descending,
        # input-order tie-break order by the caller.
        return [], scores
    return [], None


def _repair_order(
    value: object,
    candidate_ids: Sequence[str],
) -> tuple[tuple[str, ...], dict[str, float]]:
    whitelist = set(candidate_ids)
    entries, score_map = _extract_entries(value)
    ordered: list[str] = []
    scores: dict[str, float] = {}

    for entry in entries:
        parent_asin, score = _entry_id_and_score(entry)
        if parent_asin not in whitelist or parent_asin in ordered:
            continue
        ordered.append(parent_asin)
        if score is not None:
            scores[parent_asin] = score

    score_only: dict[str, float] = {}
    if score_map is not None:
        for key, raw_score in score_map.items():
            parent_asin = str(key).strip()
            if parent_asin not in whitelist:
                continue
            if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
                parsed = float(raw_score)
                if math.isfinite(parsed):
                    score_only[parent_asin] = parsed
                    scores.setdefault(parent_asin, parsed)

    if not entries and score_only:
        # Score-only responses can reorder known IDs, but ties retain the
        # original feature-ranker order for reproducibility.
        ordered.extend(
            sorted(
                (candidate_id for candidate_id in candidate_ids if candidate_id in score_only),
                key=lambda candidate_id: (-score_only[candidate_id], candidate_ids.index(candidate_id)),
            )
        )

    # Repair omissions with the deterministic original order.  This also
    # handles a response containing only unknown IDs.
    ordered.extend(candidate_id for candidate_id in candidate_ids if candidate_id not in ordered)
    return tuple(ordered), scores


class LLMSemanticRanker:
    """Execute at most one bounded listwise call per ``rank`` invocation."""

    def __init__(
        self,
        client: TieredModelClient | object | None = None,
        config: AgentConfig | None = None,
        candidate_limit: int | None = None,
        *,
        model_client: TieredModelClient | object | None = None,
    ) -> None:
        if client is not None and model_client is not None:
            raise ValueError("pass either client or model_client, not both")
        self.client = model_client if model_client is not None else client
        if self.client is None:
            self.client = TieredModelClient.from_config(config or AgentConfig.from_env())
        configured_limit = getattr(config, "candidate_limit", 30) if config else 30
        self.candidate_limit = max(
            int(candidate_limit if candidate_limit is not None else configured_limit),
            1,
        )

    def rank(
        self,
        intent_summary: object,
        candidates: Sequence[object],
    ) -> SemanticRankingResult:
        # Preserve first occurrence of each ID; the candidate list itself is
        # the deterministic fallback order and the only legal output universe.
        bounded_candidates: list[object] = []
        candidate_ids: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            parent_asin = _candidate_id(candidate)
            if parent_asin is None or parent_asin in seen:
                continue
            seen.add(parent_asin)
            bounded_candidates.append(candidate)
            candidate_ids.append(parent_asin)
            if len(candidate_ids) >= self.candidate_limit:
                break

        fallback = tuple(candidate_ids)
        if not candidate_ids:
            return SemanticRankingResult(ordered_parent_asins=(), scores={})

        if isinstance(intent_summary, Mapping):
            try:
                intent_text = json.dumps(intent_summary, ensure_ascii=False, sort_keys=True, default=str)
            except (TypeError, ValueError):
                intent_text = str(dict(intent_summary))
        elif is_dataclass(intent_summary) and not isinstance(intent_summary, type):
            try:
                intent_text = json.dumps(asdict(intent_summary), ensure_ascii=False, sort_keys=True, default=str)
            except (TypeError, ValueError):
                intent_text = str(intent_summary)
        else:
            intent_text = str(intent_summary)
        intent_text = intent_text[:4000]

        prompt_candidates: list[dict[str, object]] = []
        for parent_asin, candidate in zip(candidate_ids, bounded_candidates):
            payload = _candidate_payload(candidate, parent_asin)
            prompt_candidates.append(
                {
                    "parent_asin": parent_asin,
                    "product": _bounded_text(payload),
                }
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a constrained product relevance ranker. Return JSON only, "
                    "with schema {\"ranked_parent_asins\":[string],\"scores\":{string:number}}. "
                    "Use only the supplied parent_asin values. Do not invent products. "
                    "The list may be partial; the caller will repair omissions."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "intent": intent_text,
                        "candidates": prompt_candidates,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]

        try:
            completion = self.client.complete_json(messages, _validate_ranking_json)
        except Exception as exc:
            # Custom clients are allowed, but a broken adapter must never
            # break the public Agent response contract.
            completion = ModelCompletionResult(
                value=None,
                backend=None,
                usage=None,
                failures=(
                    BackendFailure(
                        backend="model-client",
                        stage="request",
                        error=f"{type(exc).__name__}: {exc}"[:500],
                    ),
                ),
            )

        if not isinstance(completion, ModelCompletionResult):
            # Permit a small duck-typed fake client in tests/integrations.
            value = getattr(completion, "value", None)
            backend = getattr(completion, "backend", None)
            usage = getattr(completion, "usage", None)
            failures = getattr(completion, "failures", ())
            success = bool(getattr(completion, "success", backend is not None))
            completion = ModelCompletionResult(
                value=value,
                backend=backend if success else None,
                usage=usage if success else None,
                failures=tuple(failures or ()),
            )

        if not completion.success:
            return SemanticRankingResult(
                ordered_parent_asins=fallback,
                scores={},
                backend=None,
                usage=None,
                failures=completion.failures,
            )

        try:
            ordered, scores = _repair_order(completion.value, candidate_ids)
        except Exception as exc:
            return SemanticRankingResult(
                ordered_parent_asins=fallback,
                scores={},
                backend=None,
                usage=None,
                failures=completion.failures
                + (
                    BackendFailure(
                        backend=completion.backend or "model",
                        stage="repair",
                        error=f"{type(exc).__name__}: {exc}"[:500],
                    ),
                ),
            )

        # Usage is deliberately copied only from a successful completion.  A
        # failed API tier's usage is never surfaced after local fallback.
        usage = completion.usage if completion.success else None
        return SemanticRankingResult(
            ordered_parent_asins=ordered,
            scores=scores,
            backend=completion.backend,
            usage=usage,
            failures=completion.failures,
        )


__all__ = ["LLMSemanticRanker", "SemanticRankingResult"]
