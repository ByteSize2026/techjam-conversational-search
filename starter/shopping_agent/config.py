"""Configuration for the optional shopping-agent model backends.

The model layer is deliberately configuration-only.  Constructing an
``AgentConfig`` never performs I/O, and an empty environment produces an
offline configuration with no model backends enabled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import os
from pathlib import Path


def _optional_text(value: object) -> str | None:
    """Return a stripped non-empty environment value, or ``None``."""

    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _positive_float(value: str | None, default: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_float(value: str | None, default: float) -> float:
    """Parse a finite non-negative float, preserving a safe default."""

    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 and math.isfinite(parsed) else default


def _bounded_unit_float(value: str | None, default: float) -> float:
    """Parse a score-fusion weight and clamp it to the closed unit interval."""

    parsed = _nonnegative_float(value, default)
    return min(max(parsed, 0.0), 1.0)


def _boolean(value: object, default: bool) -> bool:
    """Parse an opt-in/opt-out environment flag without surprising truthiness."""

    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _reranker_device_from_env(env: Mapping[str, str]) -> str:
    """Read the supported MPS/CUDA/CPU device setting with a safe fallback."""

    value = (
        _optional_text(env.get("SHOPPING_AGENT_QWEN_RERANKER_DEVICE"))
        or _optional_text(env.get("SHOPPING_AGENT_RERANKER_DEVICE"))
        or "mps"
    ).lower()
    return value if value in {"mps", "cuda", "cpu"} else "cpu"


@dataclass(frozen=True)
class AgentConfig:
    """Runtime settings shared by the model and ranking layers.

    The API key is intentionally represented only as a value read from the
    process environment.  No default key, network client, or model process is
    created by this class.
    """

    # DeepSeek is enabled only when ``deepseek_api_key`` is non-empty.
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # A local OpenAI-compatible endpoint is enabled only when both values are
    # present.  Local servers commonly do not require an Authorization header.
    local_base_url: str | None = None
    local_model: str | None = None
    local_api_key: str | None = None

    # Every request receives the same hard timeout.  Limits keep prompts and
    # retrieval work bounded even when a caller supplies a large candidate set.
    model_timeout_seconds: float = 8.0
    candidate_limit: int = 30
    retrieval_limit: int = 100
    model_max_tokens: int = 512
    temperature: float = 0.0

    # Optional rule-first intent interpretation.  It is disabled by default
    # so an existing deterministic deployment remains exactly offline until
    # the operator explicitly opts in.  Supplying a model client directly to
    # ``Agent``/``IntentInterpreter`` is an explicit integration opt-in.
    intent_model_enabled: bool = False
    intent_model_mode: str = "rules_first"
    intent_model_trigger_threshold: float = 0.72
    intent_model_accept_threshold: float = 0.65
    intent_model_recent_turns: int = 4

    # Optional local Qwen3-Reranker.  A path is intentionally the opt-in
    # switch: model IDs are not accepted as a default because the evaluator
    # may run offline and must never download a checkpoint implicitly.
    qwen_reranker_model_path: str | None = None
    qwen_reranker_revision: str | None = None
    qwen_reranker_device: str = "mps"
    qwen_reranker_batch_size: int = 8
    qwen_reranker_candidate_limit: int = 30
    # M1 warm Top-30 scoring is typically ~12s; keep the opt-in default above
    # that runtime while callers can tighten it for latency-sensitive runs.
    # This is a soft synchronous budget: the adapter checks elapsed time only
    # after predict() returns.
    qwen_reranker_timeout_seconds: float = 15.0
    qwen_reranker_fusion_weight: float = 1.0

    # Adaptive category recall is deterministic and enabled by default.  The
    # explicit switch and bounds keep the previous fixed-budget path available
    # for rollback/ablation without touching evaluator semantics.
    adaptive_category_recall_enabled: bool = True
    category_recall_small_category_limit: int = 500
    category_recall_min_budget: int = 100
    category_recall_max_budget: int = 400
    category_recall_browsing_ratio: float = 0.50
    category_recall_one_hard_ratio: float = 0.35
    category_recall_many_hard_ratio: float = 0.20

    # Full-category structured eligibility and dynamic recommendation commit
    # are deterministic policy layers.  Their bounds are configurable so
    # benchmark ablations do not require code edits.
    structured_pool_enabled: bool = True
    commit_policy_enabled: bool = True
    commit_all_threshold: int = 5
    partial_commit_threshold: int = 25
    partial_commit_limit: int = 1
    no_progress_force_commit: int = 2
    commit_top1_margin_threshold: float = 0.30
    commit_top3_stability_threshold: float = 0.66

    # Deterministic ranking signal switches/weights.  Keeping these knobs
    # explicit makes ranking ablations reproducible after dialogue policy is
    # fixed, without coupling them to the commit decision.
    ranking_bm25_enabled: bool = True
    ranking_title_coverage_enabled: bool = True
    ranking_popularity_enabled: bool = True
    ranking_rating_enabled: bool = True
    ranking_profile_enabled: bool = True
    ranking_bm25_weight: float = 0.36
    ranking_title_coverage_weight: float = 0.12
    ranking_popularity_weight: float = 0.03
    ranking_rating_weight: float = 0.05
    ranking_profile_weight: float = 0.08

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Build a configuration from explicitly named environment values.

        With no relevant environment variables this returns a fully offline
        configuration.  In particular, the DeepSeek endpoint being present as
        a default URL does *not* enable a network backend; the API key is the
        opt-in switch.
        """

        env = os.environ
        return cls(
            deepseek_api_key=_optional_text(
                env.get("SHOPPING_AGENT_DEEPSEEK_API_KEY")
            ),
            deepseek_base_url=(
                _optional_text(env.get("SHOPPING_AGENT_DEEPSEEK_BASE_URL"))
                or "https://api.deepseek.com"
            ),
            deepseek_model=(
                _optional_text(env.get("SHOPPING_AGENT_DEEPSEEK_MODEL"))
                or "deepseek-v4-flash"
            ),
            local_base_url=_optional_text(
                env.get("SHOPPING_AGENT_LOCAL_BASE_URL")
            ),
            local_model=_optional_text(env.get("SHOPPING_AGENT_LOCAL_MODEL")),
            local_api_key=_optional_text(
                env.get("SHOPPING_AGENT_LOCAL_API_KEY")
            ),
            model_timeout_seconds=_positive_float(
                env.get("SHOPPING_AGENT_MODEL_TIMEOUT_SECONDS"), 8.0
            ),
            candidate_limit=_positive_int(
                env.get("SHOPPING_AGENT_MODEL_CANDIDATE_LIMIT")
                or env.get("SHOPPING_AGENT_CANDIDATE_LIMIT"),
                30,
            ),
            retrieval_limit=_positive_int(
                env.get("SHOPPING_AGENT_RETRIEVAL_LIMIT"), 100
            ),
            model_max_tokens=_positive_int(
                env.get("SHOPPING_AGENT_MODEL_MAX_TOKENS"), 512
            ),
            temperature=_positive_float(
                env.get("SHOPPING_AGENT_MODEL_TEMPERATURE"), 0.0
            )
            if env.get("SHOPPING_AGENT_MODEL_TEMPERATURE") is not None
            else 0.0,
            intent_model_enabled=_boolean(
                env.get("SHOPPING_AGENT_INTENT_MODEL_ENABLED")
                or env.get("SHOPPING_AGENT_INTENT_INTERPRETER_ENABLED"),
                False,
            ),
            intent_model_mode=(
                _optional_text(env.get("SHOPPING_AGENT_INTENT_MODEL_MODE"))
                or "rules_first"
            ).lower(),
            intent_model_trigger_threshold=_bounded_unit_float(
                env.get("SHOPPING_AGENT_INTENT_MODEL_TRIGGER_THRESHOLD")
                or env.get("SHOPPING_AGENT_INTENT_TRIGGER_THRESHOLD"),
                0.72,
            ),
            intent_model_accept_threshold=_bounded_unit_float(
                env.get("SHOPPING_AGENT_INTENT_MODEL_ACCEPT_THRESHOLD")
                or env.get("SHOPPING_AGENT_INTENT_ACCEPT_THRESHOLD"),
                0.65,
            ),
            intent_model_recent_turns=_positive_int(
                env.get("SHOPPING_AGENT_INTENT_MODEL_RECENT_TURNS"),
                4,
            ),
            qwen_reranker_model_path=_optional_text(
                env.get("SHOPPING_AGENT_QWEN_RERANKER_MODEL_PATH")
                or env.get("SHOPPING_AGENT_QWEN_RERANKER_PATH")
                or env.get("SHOPPING_AGENT_RERANKER_MODEL_PATH")
                or env.get("SHOPPING_AGENT_RERANKER_PATH")
            ),
            qwen_reranker_revision=_optional_text(
                env.get("SHOPPING_AGENT_QWEN_RERANKER_REVISION")
                or env.get("SHOPPING_AGENT_RERANKER_REVISION")
            ),
            qwen_reranker_device=_reranker_device_from_env(env),
            qwen_reranker_batch_size=_positive_int(
                env.get("SHOPPING_AGENT_QWEN_RERANKER_BATCH_SIZE")
                or env.get("SHOPPING_AGENT_RERANKER_BATCH_SIZE"),
                8,
            ),
            qwen_reranker_candidate_limit=_positive_int(
                env.get("SHOPPING_AGENT_QWEN_RERANKER_CANDIDATE_LIMIT")
                or env.get("SHOPPING_AGENT_RERANKER_CANDIDATE_LIMIT")
                or env.get("SHOPPING_AGENT_MODEL_CANDIDATE_LIMIT"),
                30,
            ),
            qwen_reranker_timeout_seconds=_positive_float(
                env.get("SHOPPING_AGENT_QWEN_RERANKER_TIMEOUT_SECONDS")
                or env.get("SHOPPING_AGENT_RERANKER_TIMEOUT_SECONDS"),
                15.0,
            ),
            qwen_reranker_fusion_weight=_bounded_unit_float(
                env.get("SHOPPING_AGENT_QWEN_RERANKER_FUSION_WEIGHT")
                or env.get("SHOPPING_AGENT_RERANKER_FUSION_WEIGHT"),
                1.0,
            ),
            adaptive_category_recall_enabled=_boolean(
                env.get("SHOPPING_AGENT_ADAPTIVE_CATEGORY_RECALL")
                or env.get("SHOPPING_AGENT_ADAPTIVE_CATEGORY_RECALL_ENABLED"),
                True,
            ),
            category_recall_small_category_limit=_positive_int(
                env.get("SHOPPING_AGENT_CATEGORY_RECALL_SMALL_LIMIT"),
                500,
            ),
            category_recall_min_budget=_positive_int(
                env.get("SHOPPING_AGENT_CATEGORY_RECALL_MIN_BUDGET"),
                100,
            ),
            category_recall_max_budget=_positive_int(
                env.get("SHOPPING_AGENT_CATEGORY_RECALL_MAX_BUDGET"),
                400,
            ),
            category_recall_browsing_ratio=_bounded_unit_float(
                env.get("SHOPPING_AGENT_CATEGORY_RECALL_BROWSING_RATIO"),
                0.50,
            ),
            category_recall_one_hard_ratio=_bounded_unit_float(
                env.get("SHOPPING_AGENT_CATEGORY_RECALL_ONE_HARD_RATIO"),
                0.35,
            ),
            category_recall_many_hard_ratio=_bounded_unit_float(
                env.get("SHOPPING_AGENT_CATEGORY_RECALL_MANY_HARD_RATIO"),
                0.20,
            ),
            structured_pool_enabled=_boolean(
                env.get("SHOPPING_AGENT_STRUCTURED_POOL")
                or env.get("SHOPPING_AGENT_STRUCTURED_POOL_ENABLED"),
                True,
            ),
            commit_policy_enabled=_boolean(
                env.get("SHOPPING_AGENT_COMMIT_POLICY")
                or env.get("SHOPPING_AGENT_COMMIT_POLICY_ENABLED"),
                True,
            ),
            commit_all_threshold=_positive_int(
                env.get("SHOPPING_AGENT_COMMIT_ALL_THRESHOLD"), 5
            ),
            partial_commit_threshold=_positive_int(
                env.get("SHOPPING_AGENT_PARTIAL_COMMIT_THRESHOLD"), 25
            ),
            partial_commit_limit=min(
                3,
                _positive_int(env.get("SHOPPING_AGENT_PARTIAL_COMMIT_LIMIT"), 1),
            ),
            no_progress_force_commit=_positive_int(
                env.get("SHOPPING_AGENT_NO_PROGRESS_FORCE_COMMIT"), 2
            ),
            commit_top1_margin_threshold=_bounded_unit_float(
                env.get("SHOPPING_AGENT_COMMIT_TOP1_MARGIN_THRESHOLD"), 0.30
            ),
            commit_top3_stability_threshold=_bounded_unit_float(
                env.get("SHOPPING_AGENT_COMMIT_TOP3_STABILITY_THRESHOLD"), 0.66
            ),
            ranking_bm25_enabled=_boolean(
                env.get("SHOPPING_AGENT_RANKING_BM25")
                or env.get("SHOPPING_AGENT_RANKING_BM25_ENABLED"),
                True,
            ),
            ranking_title_coverage_enabled=_boolean(
                env.get("SHOPPING_AGENT_RANKING_TITLE_COVERAGE")
                or env.get("SHOPPING_AGENT_RANKING_TITLE_COVERAGE_ENABLED"),
                True,
            ),
            ranking_popularity_enabled=_boolean(
                env.get("SHOPPING_AGENT_RANKING_POPULARITY")
                or env.get("SHOPPING_AGENT_RANKING_POPULARITY_ENABLED"),
                True,
            ),
            ranking_rating_enabled=_boolean(
                env.get("SHOPPING_AGENT_RANKING_RATING")
                or env.get("SHOPPING_AGENT_RANKING_RATING_ENABLED"),
                True,
            ),
            ranking_profile_enabled=_boolean(
                env.get("SHOPPING_AGENT_RANKING_PROFILE")
                or env.get("SHOPPING_AGENT_RANKING_PROFILE_ENABLED"),
                True,
            ),
            ranking_bm25_weight=_nonnegative_float(
                env.get("SHOPPING_AGENT_RANKING_BM25_WEIGHT"), 0.36
            ),
            ranking_title_coverage_weight=_nonnegative_float(
                env.get("SHOPPING_AGENT_RANKING_TITLE_WEIGHT"), 0.12
            ),
            ranking_popularity_weight=_nonnegative_float(
                env.get("SHOPPING_AGENT_RANKING_POPULARITY_WEIGHT"), 0.03
            ),
            ranking_rating_weight=_nonnegative_float(
                env.get("SHOPPING_AGENT_RANKING_RATING_WEIGHT"), 0.05
            ),
            ranking_profile_weight=_nonnegative_float(
                env.get("SHOPPING_AGENT_RANKING_PROFILE_WEIGHT"), 0.08
            ),
        )

    @property
    def deepseek_enabled(self) -> bool:
        """Whether the explicitly configured DeepSeek API backend is usable."""

        return bool(self.deepseek_api_key)

    @property
    def local_model_enabled(self) -> bool:
        """Whether the local endpoint has the two required settings."""

        return bool(self.local_base_url and self.local_model)

    @property
    def intent_interpreter_enabled(self) -> bool:
        """Compatibility alias for the optional intent model switch."""

        return self.intent_model_enabled

    @property
    def qwen_reranker_model_path_resolved(self) -> str | None:
        """Return an absolute local path without touching or creating it."""

        value = _optional_text(self.qwen_reranker_model_path)
        if not value:
            return None
        # Absolute paths are required by the production opt-in contract.  A
        # relative value is retained for direct adapter tests but cannot
        # enable Qwen through the Agent environment path.
        path = Path(value).expanduser()
        if not path.is_absolute():
            return None
        try:
            return str(path.resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return None

    @property
    def qwen_reranker_enabled(self) -> bool:
        """Whether an explicit absolute local checkpoint path was supplied."""

        return bool(self.qwen_reranker_model_path_resolved)

    # Short aliases make the settings convenient for benchmark code while the
    # qwen-prefixed fields remain the canonical environment-facing names.
    @property
    def reranker_model_path(self) -> str | None:
        return self.qwen_reranker_model_path

    @property
    def reranker_revision(self) -> str | None:
        return self.qwen_reranker_revision

    @property
    def reranker_device(self) -> str:
        return self.qwen_reranker_device

    @property
    def reranker_batch_size(self) -> int:
        return self.qwen_reranker_batch_size

    @property
    def reranker_candidate_limit(self) -> int:
        return self.qwen_reranker_candidate_limit

    @property
    def reranker_timeout_seconds(self) -> float:
        return self.qwen_reranker_timeout_seconds

    @property
    def reranker_fusion_weight(self) -> float:
        return self.qwen_reranker_fusion_weight

    # Small compatibility aliases keep callers from having to know the
    # internal field naming while retaining one source of truth.
    @property
    def timeout_seconds(self) -> float:
        return self.model_timeout_seconds

    @property
    def hard_timeout_seconds(self) -> float:
        return self.model_timeout_seconds

    @property
    def llm_candidate_limit(self) -> int:
        return self.candidate_limit

    @property
    def model_candidate_limit(self) -> int:
        return self.candidate_limit

    @property
    def deepseek_endpoint(self) -> str:
        return self.deepseek_base_url

    @property
    def local_endpoint(self) -> str | None:
        return self.local_base_url

    @property
    def adaptive_category_recall(self) -> bool:
        """Compatibility alias for the adaptive category feature switch."""

        return self.adaptive_category_recall_enabled
