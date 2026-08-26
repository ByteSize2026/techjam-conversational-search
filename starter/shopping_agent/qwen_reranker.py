"""Optional, offline-first Qwen3 cross-encoder reranking.

The shopping agent keeps retrieval and candidate identity deterministic.  This
module is deliberately a small adapter around the official
``sentence_transformers.CrossEncoder`` interface: it only scores the bounded
candidate list handed to it and can never introduce a new catalog ID.

Importantly, :mod:`sentence_transformers` is imported inside the model factory
instead of at module import time.  The normal (no local model configured) path
therefore remains dependency-free and cannot download a checkpoint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
import inspect
import json
import math
import os
from pathlib import Path
import time
from typing import Callable

from .config import AgentConfig
from .model import BackendFailure
from .semantic_ranking import SemanticRankingResult


BACKEND_NAME = "qwen3-reranker-0.6b"
DEFAULT_MAX_LENGTH = 8192
DEFAULT_DOCUMENT_LIMIT = 1800
DEFAULT_INTENT_LIMIT = 4000

# Qwen's reranker format uses an instruction-prefixed query.  Keeping the
# instruction here (rather than in an unbounded prompt assembled by Agent)
# makes every score comparable and makes the adapter easy to benchmark.
SHOPPING_RELEVANCE_INSTRUCTION = (
    "Given a shopping request, retrieve products that best satisfy the request. "
    "Consider the product category, hard requirements, intended use, and stated "
    "preferences; avoid products that conflict with explicit constraints."
)


def _candidate_id(candidate: object) -> str | None:
    if isinstance(candidate, Mapping):
        value = candidate.get("parent_asin")
    else:
        value = getattr(candidate, "parent_asin", None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _bounded_text(value: object, limit: int = DEFAULT_DOCUMENT_LIMIT) -> str:
    """Serialize a candidate while placing a hard bound on document text."""

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
    return text[: max(int(limit), 1)]


def _candidate_payload(candidate: object, parent_asin: str) -> object:
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
        payload = dict(value)
        # The whitelist is sourced from the actual candidate, never from a
        # stale or model-generated compressed representation.
        payload["parent_asin"] = parent_asin
        return payload
    return {"parent_asin": parent_asin, "text": value}


def _intent_text(intent_summary: object) -> str:
    if isinstance(intent_summary, Mapping):
        try:
            text = json.dumps(intent_summary, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(dict(intent_summary))
    elif is_dataclass(intent_summary) and not isinstance(intent_summary, type):
        try:
            text = json.dumps(asdict(intent_summary), ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(intent_summary)
    else:
        text = str(intent_summary)
    return text[:DEFAULT_INTENT_LIMIT]


def _query_text(intent_summary: object) -> str:
    # The angle-bracket labels are the format used by the Qwen reranker model
    # card.  The instruction itself is shopping-specific for this benchmark.
    return (
        f"<Instruct>: {SHOPPING_RELEVANCE_INSTRUCTION}\n"
        f"<Query>: {_intent_text(intent_summary)}\n"
    )


def _invoke_with_supported_kwargs(method: Callable[..., object], *args: object, **kwargs: object) -> object:
    """Call a real or fake CrossEncoder while tolerating older signatures."""

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(*args, **kwargs)
    parameters = signature.parameters
    if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return method(*args, **kwargs)
    filtered = {key: value for key, value in kwargs.items() if key in parameters}
    return method(*args, **filtered)


def _default_model_factory(
    model_path: str,
    *,
    revision: str | None = None,
    device: str = "mps",
    max_length: int = DEFAULT_MAX_LENGTH,
) -> object:
    """Construct CrossEncoder lazily with all model reads forced offline."""

    # This is intentionally the only import of sentence_transformers in the
    # project.  A default Agent never reaches this function.
    from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]

    kwargs: dict[str, object] = {
        "device": device,
        "max_length": max_length,
        "trust_remote_code": True,
        "local_files_only": True,
        # CrossEncoder forwards these dictionaries to Transformers.  Supplying
        # local_files_only to both model and tokenizer prevents an accidental
        # Hub fetch when a path is incomplete or revision metadata is absent.
        "model_kwargs": {"local_files_only": True},
        "tokenizer_kwargs": {"local_files_only": True},
    }
    if revision:
        kwargs["revision"] = revision
    return _invoke_with_supported_kwargs(CrossEncoder, model_path, **kwargs)


def _normalise_scores(raw_scores: object, count: int) -> list[float]:
    """Validate the score vector and return finite Python floats only."""

    value = raw_scores
    # numpy arrays and torch tensors expose one of these conversion methods;
    # keeping this duck-typed avoids making numpy/torch runtime dependencies.
    detach = getattr(value, "detach", None)
    if callable(detach):
        value = detach()
    cpu = getattr(value, "cpu", None)
    if callable(cpu):
        value = cpu()
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("reranker returned a non-sequence score vector")
    values = list(value)
    # Some CrossEncoder versions return shape (n, 1).  A singleton dimension
    # is equivalent to the documented one-score-per-pair interface.
    flattened: list[object] = []
    for item in values:
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            item_values = list(item)
            if len(item_values) != 1:
                raise ValueError("reranker returned more than one score per candidate")
            flattened.append(item_values[0])
        else:
            flattened.append(item)
    if len(flattened) != count:
        raise ValueError(f"reranker returned {len(flattened)} scores for {count} candidates")

    scores: list[float] = []
    for item in flattened:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("reranker returned a non-numeric score")
        score = float(item)
        if not math.isfinite(score):
            raise ValueError("reranker returned a non-finite score")
        scores.append(score)
    return scores


def _feature_score(candidate: object) -> float:
    """Read the deterministic ranker's score without trusting bad values."""

    if isinstance(candidate, Mapping):
        value = candidate.get("score", 0.0)
    else:
        value = getattr(candidate, "score", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    score = float(value)
    return score if math.isfinite(score) else 0.0


def _minmax(values: Sequence[float]) -> list[float]:
    """Normalize scores to [0, 1], retaining deterministic constant ties."""

    if not values:
        return []
    low = min(values)
    high = max(values)
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        return [0.0] * len(values)
    scale = high - low
    return [(float(value) - low) / scale for value in values]


class QwenCrossEncoderReranker:
    """Bounded Qwen3-Reranker adapter implementing ``SemanticRankingResult``.

    ``model_factory`` and ``scorer`` are deliberately injectable so contract
    tests can exercise ordering and failure repair without downloading model
    assets.  Production construction remains lazy until the first focused
    turn reaches :meth:`rank`.  The latency setting is a soft synchronous
    budget: a call is allowed to return, then its elapsed time is checked and
    the original feature order is restored if the budget was exceeded.  The
    adapter does not spawn a thread or rebuild an MPS model after an inference
    failure; callers should use ``device=cpu`` for a correctness fallback.
    """

    backend_name = BACKEND_NAME

    def __init__(
        self,
        config: AgentConfig | None = None,
        *,
        model_path: str | Path | None = None,
        revision: str | None = None,
        device: str | None = None,
        batch_size: int | None = None,
        candidate_limit: int | None = None,
        timeout_seconds: float | None = None,
        fusion_weight: float | None = None,
        model_factory: Callable[..., object] | None = None,
        scorer: object | None = None,
        model: object | None = None,
    ) -> None:
        self.config = config or AgentConfig.from_env()
        configured_path = model_path
        if configured_path is None:
            configured_path = getattr(self.config, "qwen_reranker_model_path", None)
        self.model_path = self._resolve_path(configured_path)
        self.revision = revision if revision is not None else getattr(self.config, "qwen_reranker_revision", None)
        configured_device = device if device is not None else getattr(self.config, "qwen_reranker_device", "mps")
        configured_device = str(configured_device or "mps").strip().lower()
        self.device = configured_device if configured_device in {"mps", "cuda", "cpu"} else "cpu"
        self.batch_size = max(
            int(batch_size if batch_size is not None else getattr(self.config, "qwen_reranker_batch_size", 8)),
            1,
        )
        self.candidate_limit = max(
            int(
                candidate_limit
                if candidate_limit is not None
                else getattr(self.config, "qwen_reranker_candidate_limit", 30)
            ),
            1,
        )
        self.candidate_limit = min(self.candidate_limit, 30)
        configured_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else getattr(self.config, "qwen_reranker_timeout_seconds", 15.0)
        )
        try:
            self.timeout_seconds = max(float(configured_timeout), 0.001)
        except (TypeError, ValueError):
            self.timeout_seconds = 15.0
        configured_fusion = (
            fusion_weight
            if fusion_weight is not None
            else getattr(self.config, "qwen_reranker_fusion_weight", 1.0)
        )
        try:
            configured_fusion = float(configured_fusion)
        except (TypeError, ValueError):
            configured_fusion = 1.0
        if not math.isfinite(configured_fusion):
            configured_fusion = 1.0
        self.fusion_weight = min(max(configured_fusion, 0.0), 1.0)

        self._model_factory = model_factory or _default_model_factory
        self._model = model if model is not None else scorer
        self._model_loaded = self._model is not None
        self._load_attempted = self._model_loaded
        self._load_failures: tuple[BackendFailure, ...] = ()
        self.active_device: str | None = None

    @staticmethod
    def _resolve_path(value: str | Path | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        try:
            path = Path(str(value).strip()).expanduser()
            # A resolved absolute path is required for the production opt-in.
            # ``strict=False`` keeps construction side-effect free and lets the
            # later offline loader report a useful missing-assets diagnostic.
            return str(path.resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return None

    @property
    def enabled(self) -> bool:
        """Whether a local path was explicitly supplied."""

        return bool(self.model_path)

    @property
    def model(self) -> object | None:
        return self._model

    def _load(self) -> tuple[object | None, tuple[BackendFailure, ...]]:
        if self._model_loaded:
            return self._model, self._load_failures
        if self._load_attempted:
            return None, self._load_failures
        self._load_attempted = True

        if not self.model_path:
            self._load_failures = (
                BackendFailure(
                    backend=BACKEND_NAME,
                    stage="configuration",
                    error="local model path is not configured",
                ),
            )
            return None, self._load_failures

        failures: list[BackendFailure] = []
        # Keep model construction offline even when an incomplete local
        # checkpoint would otherwise cause Transformers to consult the Hub.
        # These flags are set immediately before the factory call and remain
        # set for the process lifetime of this opt-in local backend.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_DATASETS_OFFLINE"] = "1"

        devices = [self.device] if self.device == "cpu" else [self.device, "cpu"]
        for device in devices:
            try:
                kwargs: dict[str, object] = {
                    "revision": self.revision,
                    "device": device,
                    "max_length": DEFAULT_MAX_LENGTH,
                    "local_files_only": True,
                    "trust_remote_code": True,
                }
                model = _invoke_with_supported_kwargs(self._model_factory, self.model_path, **kwargs)
                if model is None or not callable(getattr(model, "predict", None)):
                    raise TypeError("CrossEncoder factory returned an object without predict()")
                self._model = model
                self._model_loaded = True
                self.active_device = device
                self._load_failures = tuple(failures)
                return model, self._load_failures
            except Exception as exc:
                failures.append(
                    BackendFailure(
                        backend=BACKEND_NAME,
                        stage="load",
                        error=f"{device}: {type(exc).__name__}: {exc}"[:500],
                    )
                )
        self._load_failures = tuple(failures)
        return None, self._load_failures

    def _predict(self, model: object, pairs: Sequence[Sequence[str]]) -> object:
        predict = getattr(model, "predict", None)
        if callable(predict):
            return _invoke_with_supported_kwargs(
                predict,
                pairs,
                batch_size=self.batch_size,
                show_progress_bar=False,
            )
        # A callable scorer is useful for a dependency-free benchmark fake;
        # production objects still follow the official CrossEncoder.predict
        # contract and are validated during loading.
        if callable(model):
            return _invoke_with_supported_kwargs(
                model,
                pairs,
                batch_size=self.batch_size,
                show_progress_bar=False,
            )
        raise TypeError("CrossEncoder object has no predict()")

    def rank(
        self,
        intent_summary: object,
        candidates: Sequence[object],
    ) -> SemanticRankingResult:
        # Preserve first occurrence and use this bounded list as both fallback
        # order and the complete whitelist for the model result.
        bounded: list[object] = []
        candidate_ids: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            parent_asin = _candidate_id(candidate)
            if parent_asin is None or parent_asin in seen:
                continue
            seen.add(parent_asin)
            bounded.append(candidate)
            candidate_ids.append(parent_asin)
            if len(candidate_ids) >= self.candidate_limit:
                break
        fallback = tuple(candidate_ids)
        if not candidate_ids:
            return SemanticRankingResult(ordered_parent_asins=(), scores={})

        model, load_failures = self._load()
        if model is None:
            return SemanticRankingResult(
                ordered_parent_asins=fallback,
                scores={},
                backend=None,
                usage=None,
                failures=load_failures,
            )

        query = _query_text(intent_summary)
        pairs: list[tuple[str, str]] = []
        for parent_asin, candidate in zip(candidate_ids, bounded):
            document = _bounded_text(_candidate_payload(candidate, parent_asin))
            pairs.append((query, document))

        started = time.perf_counter()
        try:
            raw_scores = self._predict(model, pairs)
            elapsed = time.perf_counter() - started
            if elapsed > self.timeout_seconds:
                raise TimeoutError(
                    f"reranker scoring exceeded {self.timeout_seconds:.3f}s timeout"
                )
            qwen_scores = _normalise_scores(raw_scores, len(candidate_ids))
            feature_scores = [_feature_score(candidate) for candidate in bounded]
            qwen_normalized = _minmax(qwen_scores)
            feature_normalized = _minmax(feature_scores)
            weight = self.fusion_weight
            final_scores = [
                (1.0 - weight) * feature_score + weight * qwen_score
                for feature_score, qwen_score in zip(feature_normalized, qwen_normalized)
            ]
        except Exception as exc:
            failure = BackendFailure(
                backend=BACKEND_NAME,
                stage="scores",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            return SemanticRankingResult(
                ordered_parent_asins=fallback,
                scores={},
                backend=None,
                usage=None,
                failures=load_failures + (failure,),
            )

        # At zero Qwen weight, the incoming order is already the deterministic
        # Feature Ranker order.  Keep it exactly, even if a caller supplied
        # feature scores that do not happen to be monotonic.  For positive
        # weights, stable input-index tie-breaking makes fusion reproducible.
        if self.fusion_weight <= 0.0:
            order = list(range(len(candidate_ids)))
        else:
            order = sorted(range(len(candidate_ids)), key=lambda index: (-final_scores[index], index))
        ordered = tuple(candidate_ids[index] for index in order)
        score_map = {
            parent_asin: float(score)
            for parent_asin, score in zip(candidate_ids, final_scores)
        }
        return SemanticRankingResult(
            ordered_parent_asins=ordered,
            scores=score_map,
            backend=BACKEND_NAME,
            # CrossEncoder does not expose chat token accounting.  Do not fake
            # usage values for the public evaluator.
            usage=None,
            failures=load_failures,
        )


__all__ = [
    "BACKEND_NAME",
    "DEFAULT_MAX_LENGTH",
    "SHOPPING_RELEVANCE_INSTRUCTION",
    "QwenCrossEncoderReranker",
]
