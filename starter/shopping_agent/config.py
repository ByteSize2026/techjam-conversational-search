"""Configuration for the optional shopping-agent model backends.

The model layer is deliberately configuration-only.  Constructing an
``AgentConfig`` never performs I/O, and an empty environment produces an
offline configuration with no model backends enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
import os


def _optional_text(value: str | None) -> str | None:
    """Return a stripped non-empty environment value, or ``None``."""

    if value is None:
        return None
    value = value.strip()
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
        )

    @property
    def deepseek_enabled(self) -> bool:
        """Whether the explicitly configured DeepSeek API backend is usable."""

        return bool(self.deepseek_api_key)

    @property
    def local_model_enabled(self) -> bool:
        """Whether the local endpoint has the two required settings."""

        return bool(self.local_base_url and self.local_model)

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
