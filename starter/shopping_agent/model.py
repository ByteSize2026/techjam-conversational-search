"""Small, dependency-free model adapters and the tiered failover client.

The production agent only needs one OpenAI-compatible operation: submit a
bounded chat prompt and read a JSON object.  Keeping that operation here makes
the optional DeepSeek/local model path easy to test and leaves the default
execution path entirely offline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import inspect
import json
import re
from urllib import error as urllib_error
from urllib import request as urllib_request

from .config import AgentConfig


class ModelBackendError(RuntimeError):
    """Base class for errors raised while talking to one model backend."""


class BackendRequestError(ModelBackendError):
    """The HTTP request or response envelope could not be completed."""


class BackendJSONError(ModelBackendError):
    """The model's message content was not valid JSON."""


class BackendValidationError(ModelBackendError):
    """A decoded model response did not satisfy the caller's validator."""


class TokenUsage(dict[str, int]):
    """A dict-compatible token usage value with attribute access.

    The evaluator consumes ``prompt_tokens`` and ``completion_tokens`` as a
    mapping.  Attribute access is convenient for internal callers, while the
    dict base keeps this value compatible with the public response contract.
    """

    def __init__(self, prompt_tokens: int, completion_tokens: int, **extra: int):
        super().__init__(
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
        )
        for key, value in extra.items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self[key] = value

    @property
    def prompt_tokens(self) -> int:
        return int(self["prompt_tokens"])

    @property
    def completion_tokens(self) -> int:
        return int(self["completion_tokens"])

    @property
    def total_tokens(self) -> int:
        value = self.get("total_tokens")
        if isinstance(value, int):
            return value
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, int]:
        return dict(self)


def _coerce_usage(value: object) -> TokenUsage | None:
    """Keep only valid usage values from a real backend response."""

    if not isinstance(value, Mapping):
        return None

    def valid_int(item: object) -> int | None:
        # Providers generally return ints, but accepting digit strings makes
        # the adapter tolerant of small local-server implementations.
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            return item if item >= 0 else None
        if isinstance(item, str) and item.strip().isdigit():
            return int(item.strip())
        return None

    prompt = valid_int(value.get("prompt_tokens"))
    completion = valid_int(value.get("completion_tokens"))
    if prompt is None or completion is None:
        return None
    total = valid_int(value.get("total_tokens"))
    extras = {"total_tokens": total} if total is not None else {}
    return TokenUsage(prompt, completion, **extras)


@dataclass(frozen=True)
class BackendResponse:
    """Normalized response returned by an OpenAI-compatible backend."""

    content: object
    usage: TokenUsage | Mapping[str, object] | None = None
    raw: object | None = None


@dataclass(frozen=True)
class BackendFailure:
    """Non-sensitive diagnostics for one failed backend attempt."""

    backend: str
    stage: str
    error: str

    @property
    def backend_name(self) -> str:
        return self.backend

    def as_dict(self) -> dict[str, str]:
        return {
            "backend": self.backend,
            "stage": self.stage,
            "error": self.error,
        }

    # A little mapping compatibility makes diagnostics easy to consume from
    # both tests and JSON logging without sacrificing typed attributes.
    def __getitem__(self, key: str) -> str:
        return self.as_dict()[key]


@dataclass(frozen=True)
class ModelCompletionResult:
    """Result of the tiered JSON completion attempt."""

    value: object | None
    backend: str | None
    usage: TokenUsage | None
    failures: tuple[BackendFailure, ...] = ()
    attempted_backends: tuple[str, ...] = ()

    @property
    def success(self) -> bool:
        return self.backend is not None

    @property
    def ok(self) -> bool:
        return self.success

    @property
    def backend_name(self) -> str | None:
        return self.backend

    @property
    def data(self) -> object | None:
        return self.value

    @property
    def failure_diagnostics(self) -> list[dict[str, str]]:
        return [failure.as_dict() for failure in self.failures]

    @property
    def diagnostics(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "attempted_backends": list(self.attempted_backends),
            "failures": [failure.as_dict() for failure in self.failures],
        }

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "value": self.value,
            "backend": self.backend,
            "usage": self.usage.as_dict() if self.usage else None,
            "diagnostics": self.diagnostics,
        }
        return result


def _safe_error(error: BaseException, backend: object) -> str:
    """Format an exception while ensuring an API key cannot enter diagnostics."""

    message = f"{type(error).__name__}: {error}".strip()
    secret = getattr(backend, "api_key", None)
    if isinstance(secret, str) and secret:
        message = message.replace(secret, "<redacted>")
    return message[:500]


def _response_content(response: object) -> tuple[object, object | None]:
    """Extract message content and provider usage from common response shapes."""

    if isinstance(response, BackendResponse):
        return response.content, response.usage

    if isinstance(response, tuple) and len(response) == 2:
        return response[0], response[1]

    if isinstance(response, Mapping):
        usage = response.get("usage")
        choices = response.get("choices")
        if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)):
            if not choices:
                raise BackendRequestError("response choices is empty")
            choice = choices[0]
            if isinstance(choice, Mapping):
                message = choice.get("message")
                if isinstance(message, Mapping) and "content" in message:
                    return message["content"], usage
                if "text" in choice:
                    return choice["text"], usage
                if "content" in choice:
                    return choice["content"], usage
            elif isinstance(choice, str):
                return choice, usage
            raise BackendRequestError("response choice has no message content")

        # Simple fake/local-server response forms.
        for key in ("content", "text", "output"):
            if key in response:
                return response[key], usage
        message = response.get("message")
        if isinstance(message, Mapping) and "content" in message:
            return message["content"], usage
        if isinstance(message, str):
            return message, usage

        # A fake backend is allowed to return an already decoded JSON value.
        # Do not mistake its ``usage`` field for model content: remove only
        # that transport field when it is present.
        direct = dict(response)
        direct.pop("usage", None)
        return direct, usage

    content = getattr(response, "content", None)
    if content is not None:
        return content, getattr(response, "usage", None)
    return response, getattr(response, "usage", None)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)


def _decode_json(content: object) -> object:
    """Decode model content, accepting a fenced JSON response."""

    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BackendJSONError("model content is not UTF-8") from exc

    if isinstance(content, str):
        text = content.strip()
        match = _FENCE_RE.match(text)
        if match:
            text = match.group(1).strip()
        if not text:
            raise BackendJSONError("model content is empty")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise BackendJSONError(f"invalid model JSON: {exc.msg}") from exc

    # OpenAI-compatible servers sometimes expose structured text blocks.
    if isinstance(content, list) and content and all(
        isinstance(item, Mapping) and item.get("type") == "text" and "text" in item
        for item in content
    ):
        return _decode_json("".join(str(item["text"]) for item in content))

    if isinstance(content, (Mapping, list)):
        return content
    raise BackendJSONError(
        f"model content must be JSON text/object, got {type(content).__name__}"
    )


def _validated_value(value: object, validator: Callable[[object], object] | None) -> object:
    if validator is None:
        return value
    try:
        verdict = validator(value)
    except Exception as exc:  # validator failures are part of model failover
        raise BackendValidationError(str(exc) or "validator raised an error") from exc

    # Validators commonly either return a bool or a normalized value.  A
    # two-tuple (ok, value) is supported for lightweight schema adapters.
    if isinstance(verdict, tuple) and len(verdict) == 2 and isinstance(verdict[0], bool):
        if not verdict[0]:
            raise BackendValidationError("validator rejected model JSON")
        return verdict[1]
    if isinstance(verdict, bool):
        if not verdict:
            raise BackendValidationError("validator rejected model JSON")
        return value
    if verdict is None:
        raise BackendValidationError("validator returned no value")
    return verdict


def _invoke_method(
    method: Callable[..., object],
    messages: Sequence[Mapping[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    validator: Callable[[object], object] | None = None,
    pass_validator: bool = False,
) -> object:
    """Call a backend method without requiring it to accept optional kwargs."""

    try:
        signature = inspect.signature(method)
        parameters = signature.parameters
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    except (TypeError, ValueError):
        parameters = {}
        accepts_kwargs = True

    kwargs: dict[str, object] = {}
    if accepts_kwargs or "temperature" in parameters:
        kwargs["temperature"] = temperature
    if accepts_kwargs or "max_tokens" in parameters:
        kwargs["max_tokens"] = max_tokens
    if pass_validator and (accepts_kwargs or "validator" in parameters):
        kwargs["validator"] = validator
    return method(messages, **kwargs)


class OpenAICompatibleBackend:
    """A stdlib-only client for ``/chat/completions`` servers."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 8.0,
        timeout: float | None = None,
        name: str = "openai-compatible",
        extra_request_fields: Mapping[str, object] | None = None,
    ) -> None:
        base_url = str(base_url).strip()
        model = str(model).strip()
        if not base_url:
            raise ValueError("base_url must not be empty")
        if not model:
            raise ValueError("model must not be empty")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        if timeout is not None:
            timeout_seconds = timeout
        self.timeout_seconds = max(float(timeout_seconds), 0.001)
        self.name = name
        self.extra_request_fields = dict(extra_request_fields) if extra_request_fields else {}

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> BackendResponse:
        payload = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            **self.extra_request_fields,
        }
        request = urllib_request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            response = urllib_request.urlopen(request, timeout=self.timeout_seconds)
            try:
                status = response.getcode()
                body = response.read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except urllib_error.HTTPError as exc:
            try:
                body = exc.read(512).decode("utf-8", errors="replace")
            except Exception:
                body = ""
            raise BackendRequestError(
                f"HTTP {exc.code}{': ' + body[:300] if body else ''}"
            ) from exc
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise BackendRequestError(str(exc) or "request failed") from exc

        if status is not None and not 200 <= int(status) < 300:
            text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
            raise BackendRequestError(f"HTTP {status}: {text[:300]}")
        try:
            decoded = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendRequestError("response envelope is not valid JSON") from exc
        content, usage = _response_content(decoded)
        return BackendResponse(content=content, usage=usage, raw=decoded)

    # ``request`` is a useful alias for small integrations and fake servers.
    request = complete

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


class DeepSeekAPIBackend(OpenAICompatibleBackend):
    """DeepSeek's OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout_seconds: float = 8.0,
        timeout: float | None = None,
    ) -> None:
        super().__init__(
            base_url,
            model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            timeout=timeout,
            name="deepseek-api",
            # Thinking is on by default (reasoning_effort=high) and burns
            # 6-13x the visible-output token count on every call (measured
            # across artifacts/full_live_test/*); it also silently ignores
            # temperature, breaking our temperature=0 determinism assumption.
            extra_request_fields={"thinking": {"type": "disabled"}},
        )


class LocalOpenAIBackend(OpenAICompatibleBackend):
    """An explicitly configured local OpenAI-compatible model server."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 8.0,
        timeout: float | None = None,
    ) -> None:
        super().__init__(
            base_url,
            model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            timeout=timeout,
            name="local-model",
        )


# Concise aliases used by some callers/documentation.
DeepSeekBackend = DeepSeekAPIBackend
LocalModelBackend = LocalOpenAIBackend


class TieredModelClient:
    """Try configured model backends in deterministic order.

    A successful response is returned only after JSON decoding and caller
    validation succeed.  Failures from earlier tiers remain available as
    diagnostics, while usage is taken exclusively from the successful tier.
    """

    def __init__(
        self,
        backends: Sequence[object] | None = None,
        *,
        timeout_seconds: float = 8.0,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> None:
        self.backends = tuple(backends or ())
        self.timeout_seconds = max(float(timeout_seconds), 0.001)
        self.max_tokens = max(int(max_tokens), 1)
        self.temperature = float(temperature)

    @classmethod
    def from_config(cls, config: AgentConfig | None = None) -> "TieredModelClient":
        config = config or AgentConfig.from_env()
        backends: list[object] = []
        if getattr(config, "deepseek_api_key", None):
            backends.append(
                DeepSeekAPIBackend(
                    config.deepseek_api_key,
                    base_url=getattr(config, "deepseek_base_url", "https://api.deepseek.com"),
                    model=getattr(config, "deepseek_model", "deepseek-v4-flash"),
                    timeout_seconds=getattr(config, "model_timeout_seconds", 8.0),
                )
            )
        if getattr(config, "local_base_url", None) and getattr(config, "local_model", None):
            backends.append(
                LocalOpenAIBackend(
                    config.local_base_url,
                    config.local_model,
                    api_key=getattr(config, "local_api_key", None),
                    timeout_seconds=getattr(config, "model_timeout_seconds", 8.0),
                )
            )
        return cls(
            backends,
            timeout_seconds=getattr(config, "model_timeout_seconds", 8.0),
            max_tokens=getattr(config, "model_max_tokens", 512),
            temperature=getattr(config, "temperature", 0.0),
        )

    @property
    def backend_names(self) -> tuple[str, ...]:
        return tuple(self._backend_name(backend) for backend in self.backends)

    @staticmethod
    def _backend_name(backend: object) -> str:
        name = getattr(backend, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
        return type(backend).__name__.replace("Backend", "").lower() or "backend"

    def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        validator: Callable[[object], object] | None = None,
    ) -> ModelCompletionResult:
        failures: list[BackendFailure] = []
        attempted: list[str] = []

        if not self.backends:
            failures.append(
                BackendFailure(
                    backend="none",
                    stage="configuration",
                    error="no model backend configured",
                )
            )
            return ModelCompletionResult(
                value=None,
                backend=None,
                usage=None,
                failures=tuple(failures),
                attempted_backends=(),
            )

        for backend in self.backends:
            name = self._backend_name(backend)
            attempted.append(name)
            try:
                method = getattr(backend, "complete", None)
                pass_validator = False
                if not callable(method):
                    method = getattr(backend, "request", None)
                if not callable(method):
                    # A very small fake may expose complete_json directly.
                    method = getattr(backend, "complete_json", None)
                    pass_validator = callable(method)
                if not callable(method):
                    raise BackendRequestError("backend has no completion method")
                raw = _invoke_method(
                    method,
                    messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    validator=validator,
                    pass_validator=pass_validator,
                )
            except Exception as exc:
                failures.append(BackendFailure(name, "request", _safe_error(exc, backend)))
                continue

            try:
                content, raw_usage = _response_content(raw)
            except Exception as exc:
                failures.append(BackendFailure(name, "response", _safe_error(exc, backend)))
                continue

            try:
                decoded = _decode_json(content)
            except Exception as exc:
                failures.append(BackendFailure(name, "json", _safe_error(exc, backend)))
                continue

            try:
                value = _validated_value(decoded, validator)
            except Exception as exc:
                failures.append(BackendFailure(name, "validator", _safe_error(exc, backend)))
                continue

            # Deliberately compute usage only on this successful path.  A
            # malformed API response cannot contribute tokens to the result.
            usage = _coerce_usage(raw_usage)
            return ModelCompletionResult(
                value=value,
                backend=name,
                usage=usage,
                failures=tuple(failures),
                attempted_backends=tuple(attempted),
            )

        return ModelCompletionResult(
            value=None,
            backend=None,
            usage=None,
            failures=tuple(failures),
            attempted_backends=tuple(attempted),
        )
