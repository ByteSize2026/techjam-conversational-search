"""Independent, crash-tolerant tracing around the frozen local evaluator.

The proxy in this module observes the public ``reset/respond`` boundary and
optional bounded Agent diagnostics.  It never changes an Agent response and
does not expose benchmark targets to the Agent or runtime event stream.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Self

from evaluator.local_evaluator import (
    catalog_index,
    evaluate,
    load_jsonl,
    normalize_recommendations,
)
from starter.agent import Agent

SCHEMA_VERSION = 1
_MAX_TEXT = 2_000
_MAX_ITEMS = 100
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "client_secret",
}
_METRIC_KEYS = (
    "sample_count",
    "hit_rate_at_10",
    "mrr",
    "mttc",
    "efficiency",
    "recommended_technical_score",
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_value(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _secret_values() -> tuple[str, ...]:
    values: list[str] = []
    for key, value in os.environ.items():
        lowered = key.lower()
        if (
            value
            and len(value) >= 6
            and any(marker in lowered for marker in ("api_key", "token", "secret", "password"))
        ):
            values.append(value)
    return tuple(sorted(set(values), key=len, reverse=True))


def _redact_text(value: object, *, secrets: Sequence[str] = ()) -> str:
    text = str(value)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)bearer\s+[a-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|authorization|password|client[_-]?secret)(\s*[:=]\s*)[^\s,;}]+",
        r"\1\2[REDACTED]",
        text,
    )
    text = re.sub(r"(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return text[:_MAX_TEXT]


def sanitize(
    value: object,
    *,
    secrets: Sequence[str] = (),
    depth: int = 0,
) -> object:
    """Convert arbitrary diagnostics to bounded JSON values and redact secrets."""

    if depth >= 10:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _redact_text(value, secrets=secrets)
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for raw_key, item in list(value.items())[:_MAX_ITEMS]:
            key = _redact_text(raw_key, secrets=secrets)[:120]
            normalized = key.lower().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized.endswith("_api_key"):
                output[key] = "[REDACTED]"
            else:
                output[key] = sanitize(item, secrets=secrets, depth=depth + 1)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize(item, secrets=secrets, depth=depth + 1) for item in value[:_MAX_ITEMS]]
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        try:
            return sanitize(as_dict(), secrets=secrets, depth=depth + 1)
        except (AttributeError, TypeError, ValueError):
            pass
    return _redact_text(value, secrets=secrets)


def project_state(agent: object, session_id: str) -> dict[str, object]:
    """Project the current Agent session into a bounded non-profile snapshot."""

    try:
        return _project_state(agent, session_id)
    except Exception:  # noqa: BLE001 - optional introspection must stay transparent
        return {}


def _project_state(agent: object, session_id: str) -> dict[str, object]:
    store = getattr(agent, "store", None)
    getter = getattr(store, "get", None)
    if not callable(getter):
        return {}
    try:
        state = getter(session_id)
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}
    if state is None:
        return {}
    constraints: list[dict[str, object]] = []
    for item in list(getattr(state, "active_constraints", ()))[:20]:
        constraints.append(
            {
                "attribute": str(getattr(item, "attribute", ""))[:40],
                "value": str(getattr(item, "value", ""))[:120],
                "polarity": str(getattr(item, "polarity", ""))[:20],
                "hardness": str(getattr(item, "hardness", ""))[:20],
                "source": str(getattr(item, "source", ""))[:20],
            }
        )
    pending = getattr(state, "pending_task", None)
    tool_ids = [str(item)[:80] for item in list(getattr(state, "tool_candidate_ids", ()))[:20]]
    last_ids = [str(item)[:80] for item in list(getattr(state, "last_candidate_ids", ()))[:20]]
    seen = list(getattr(state, "seen_recommendations", ()))
    fingerprint = getattr(state, "fingerprint", None)
    try:
        state_fingerprint = fingerprint() if callable(fingerprint) else None
    except (AttributeError, TypeError, ValueError):
        state_fingerprint = None
    # design.md Section 8: the Router/Value-Node graph's bounded per-turn
    # node trace (starter.shopping_agent.state.SessionState.node_trace).
    # Projected as-is (already a bounded list of small dicts, sanitize()
    # below still bounds/redacts it like every other field here) -- no other
    # change to this module's diff/JSONL machinery is needed.
    node_trace = list(getattr(state, "node_trace", ()))
    return {
        "intent_epoch": int(getattr(state, "intent_epoch", 0)),
        "category_anchor": getattr(state, "category_anchor", None),
        "active_constraints": constraints,
        "no_preference": sorted(str(item)[:40] for item in getattr(state, "no_preference", ()))[:20],
        "profile_loaded": bool(getattr(state, "profile_loaded", False)),
        "pending_attribute": getattr(pending, "ask_attribute", None),
        "asked_attributes": [str(item)[:40] for item in list(getattr(state, "asked_attributes", ()))[:20]],
        "tool_candidate_count": len(getattr(state, "tool_candidate_ids", ())),
        "tool_candidate_ids": tool_ids,
        "last_candidate_count": len(getattr(state, "last_candidate_ids", ())),
        "last_candidate_ids": last_ids,
        "seen_recommendation_count": len(seen),
        "state_fingerprint": state_fingerprint,
        "node_trace": node_trace,
    }


def state_diff(before: Mapping[str, object], after: Mapping[str, object]) -> dict[str, object]:
    """Return a stable, shallow structural diff between state projections."""

    added = {key: after[key] for key in sorted(after.keys() - before.keys())}
    removed = {key: before[key] for key in sorted(before.keys() - after.keys())}
    changed = {
        key: {"before": before[key], "after": after[key]}
        for key in sorted(before.keys() & after.keys())
        if before[key] != after[key]
    }
    return {"added": added, "removed": removed, "changed": changed}


class JsonlRecorder:
    """Append versioned event envelopes and flush every line."""

    def __init__(self, path: str | Path, run_id: str, *, secrets: Sequence[str] = ()) -> None:
        self.path = Path(path)
        self.run_id = str(run_id)
        self.secrets = tuple(secrets)
        self.sequence = 0
        self._handle = self.path.open("x", encoding="utf-8")

    def append(
        self,
        event_type: str,
        *,
        sample_index: int | None = None,
        sample_id: str | None = None,
        scenario_type: str | None = None,
        session_id: str | None = None,
        turn: int | None = None,
        payload: object = None,
    ) -> dict[str, object]:
        self.sequence += 1
        event = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": str(event_type),
            "sample_index": sample_index,
            "sample_id": sample_id,
            "scenario_type": scenario_type,
            "session_id": session_id,
            "turn": turn,
            "timestamp": _utc_now(),
            "payload": sanitize(payload if payload is not None else {}, secrets=self.secrets),
        }
        self._handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._handle.flush()
        return event

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class TracingAgentProxy:
    """Transparent facade that records public I/O and optional diagnostics."""

    def __init__(
        self,
        agent: object,
        recorder: JsonlRecorder,
        samples: Sequence[Mapping[str, object]],
        catalog_ids: set[str],
    ) -> None:
        self.agent = agent
        self.recorder = recorder
        self.catalog_ids = catalog_ids
        self.samples = [
            {
                "sample_index": index,
                "sample_id": str(sample.get("sample_id", index)),
                "scenario_type": str(sample.get("scenario_type", "")),
            }
            for index, sample in enumerate(samples)
        ]
        self._next_sample = 0
        self._sessions: dict[str, dict[str, object]] = {}
        self.fatal_recorder_error: BaseException | None = None

    def _append(
        self, event_type: str, meta: Mapping[str, object], **kwargs: object
    ) -> bool:
        if self.fatal_recorder_error is not None:
            return False
        try:
            self.recorder.append(
                event_type,
                sample_index=_int_value(meta["sample_index"]),
                sample_id=str(meta["sample_id"]),
                scenario_type=str(meta["scenario_type"]),
                session_id=str(kwargs.pop("session_id", "")) or None,
                turn=kwargs.pop("turn", None),  # type: ignore[arg-type]
                payload=kwargs.pop("payload", {}),
            )
        except Exception as exc:  # noqa: BLE001 - tracing must not replace Agent output
            self.fatal_recorder_error = exc
            return False
        return True

    def reset(self, session_id: str, user_profile: dict) -> None:
        if self.fatal_recorder_error is not None:
            raise RuntimeError("trace recorder failed before the next evaluator session") from (
                self.fatal_recorder_error
            )
        if self._next_sample >= len(self.samples):
            raise RuntimeError("evaluator reset count exceeds supplied samples")
        meta = self.samples[self._next_sample]
        self._next_sample += 1
        self._sessions[str(session_id)] = meta
        started = time.perf_counter()
        try:
            result = self.agent.reset(session_id, user_profile)  # type: ignore[attr-defined]
        except BaseException as exc:
            self._append(
                "reset_failed",
                meta,
                session_id=session_id,
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        self._append(
            "reset_completed",
            meta,
            session_id=session_id,
            payload={
                "profile_present": bool(user_profile),
                "profile_fields": sorted(str(key)[:80] for key in user_profile)[:20],
                "profile_hash": _stable_hash(user_profile),
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "state": project_state(self.agent, session_id),
            },
        )
        return result

    def respond(
        self, session_id: str, user_message: str, turn: int, top_k: int
    ) -> object:
        meta = self._sessions.get(str(session_id))
        if meta is None:
            raise RuntimeError("respond called before traced reset")
        before = project_state(self.agent, session_id)
        self._append(
            "turn_started",
            meta,
            session_id=session_id,
            turn=int(turn),
            payload={"user_message": user_message, "top_k": top_k, "state_before": before},
        )
        started = time.perf_counter()
        try:
            response = self.agent.respond(session_id, user_message, turn, top_k)  # type: ignore[attr-defined]
        except BaseException as exc:
            after = project_state(self.agent, session_id)
            self._append(
                "turn_failed",
                meta,
                session_id=session_id,
                turn=int(turn),
                payload={
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "state_after": after,
                    "state_diff": state_diff(before, after),
                },
            )
            raise
        after = project_state(self.agent, session_id)
        diagnostics = getattr(self.agent, "last_diagnostics", {})
        raw_recommendations = response.get("recommendations") if isinstance(response, Mapping) else None
        normalized = normalize_recommendations(raw_recommendations, self.catalog_ids)
        self._append(
            "turn_completed",
            meta,
            session_id=session_id,
            turn=int(turn),
            payload={
                "user_message": user_message,
                "raw_response": response,
                "normalized_recommendations": normalized,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "state_before": before,
                "state_after": after,
                "state_diff": state_diff(before, after),
                "diagnostics": diagnostics,
            },
        )
        return response


def _read_events(path: str | Path) -> list[dict[str, object]]:
    return [item for item in load_jsonl(path) if isinstance(item, dict)]


def _official_metrics(result: Mapping[str, object]) -> dict[str, object]:
    metrics = {key: result.get(key) for key in _METRIC_KEYS if key in result}
    metrics["reported_token_usage"] = result.get("reported_token_usage", {})
    metrics["scenario_metrics"] = result.get("scenario_metrics", {})
    return metrics


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _usage_counts(value: object) -> tuple[int, int]:
    usage = _mapping(value)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        prompt if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0 else 0,
        completion
        if isinstance(completion, int)
        and not isinstance(completion, bool)
        and completion >= 0
        else 0,
    )


def _failure_text(diagnostics: Mapping[str, object]) -> str:
    fragments: list[str] = []
    for failure in _items(diagnostics.get("tool_failures")):
        item = _mapping(failure)
        fragments.extend(str(item.get(key, "")) for key in ("stage", "error", "backend"))
    for entry in _items(diagnostics.get("tool_trajectory")):
        trajectory = _mapping(entry)
        fragments.append(str(trajectory.get("status", "")))
        observation = _mapping(trajectory.get("observation"))
        fragments.append(str(observation.get("error", "")))
    return " ".join(fragments).lower()


def _fallback_reason(diagnostics: Mapping[str, object]) -> str | None:
    if diagnostics.get("event") == "pipeline_error":
        return "pipeline.guard_error"
    raw = diagnostics.get("tool_fallback_reason")
    failure_text = _failure_text(diagnostics)
    if raw == "invalid_action_limit":
        if "timeout" in failure_text or "timed out" in failure_text:
            return "planner.timeout"
        if diagnostics.get("tool_failures"):
            return "planner.backend_failure"
        return "planner.invalid_action"
    if raw == "tool_error_limit":
        if any(
            marker in failure_text
            for marker in ("validation", "requires observed", "unknown product", "invalid")
        ):
            return "tool.validation_error"
        return "tool.execution_error"
    if raw == "policy_error_limit":
        return "planner.policy_rejected"
    if raw == "structured_action_unavailable":
        return "planner.structured_action_unavailable"
    reasons = {
        "planner_timeout": "planner.timeout",
        "time_budget_exhausted": "orchestrator.time_budget",
        "step_budget_exhausted": "orchestrator.step_budget",
        "planner_budget_exhausted": "planner.budget",
    }
    if raw is not None:
        return reasons.get(str(raw), f"agent.deterministic_fallback.{str(raw)[:80]}")
    if diagnostics.get("execution_mode") == "fallback":
        return "agent.deterministic_fallback"
    return None


def _metric_delta(current: object, baseline: object) -> dict[str, float] | None:
    if (
        not isinstance(current, (int, float))
        or isinstance(current, bool)
        or not isinstance(baseline, (int, float))
        or isinstance(baseline, bool)
    ):
        return None
    absolute = float(current) - float(baseline)
    relative = absolute / abs(float(baseline)) if float(baseline) != 0.0 else 0.0
    return {"absolute": round(absolute, 6), "relative": round(relative, 6)}


def analyze_trace(
    events: Sequence[Mapping[str, object]],
    evaluation: Mapping[str, object],
    samples: Sequence[Mapping[str, object]],
    *,
    baseline: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Join targets only after evaluation and derive attribution diagnostics."""

    completed = [item for item in events if item.get("event_type") == "turn_completed"]
    by_sample_turn: dict[tuple[str, int], Mapping[str, object]] = {}
    action_counts: Counter[str] = Counter()
    action_status: Counter[str] = Counter()
    planner_backends: Counter[str] = Counter()
    planner_failure_stages: Counter[str] = Counter()
    planner_error_codes: Counter[str] = Counter()
    execution_modes: Counter[str] = Counter()
    recommendation_sources: Counter[str] = Counter()
    fallback_reasons: Counter[str] = Counter()
    fallback_scenarios: Counter[str] = Counter()
    fallback_sessions: set[str] = set()
    fallback_turns = 0
    invalid_action_count = 0
    tool_error_count = 0
    pause_count = 0
    resume_count = 0
    intent_override_count = 0
    response_prompt_tokens = 0
    response_completion_tokens = 0
    planner_prompt_tokens = 0
    planner_completion_tokens = 0
    session_ids: set[str] = set()
    for event in completed:
        sample_id = str(event.get("sample_id", ""))
        scenario_type = str(event.get("scenario_type", ""))
        turn = _int_value(event.get("turn"))
        by_sample_turn[(sample_id, turn)] = event
        session_id = str(event.get("session_id", ""))
        session_ids.add(session_id)
        payload = _mapping(event.get("payload"))
        diagnostics = _mapping(payload.get("diagnostics"))
        execution_mode = str(diagnostics.get("execution_mode") or "unknown")
        execution_modes[execution_mode] += 1
        if diagnostics.get("epoch_changed") is True:
            intent_override_count += 1
        reason = _fallback_reason(diagnostics)
        if reason:
            fallback_turns += 1
            fallback_reasons[reason] += 1
            fallback_sessions.add(session_id)
            fallback_scenarios[scenario_type] += 1
        for entry in _items(diagnostics.get("tool_trajectory")):
            trajectory = _mapping(entry)
            action = str(trajectory.get("action", ""))
            status = str(trajectory.get("status", ""))
            if action == "resume_after_ask":
                resume_count += 1
            if action not in {"", "orchestrator", "resume_after_ask"}:
                action_counts[action] += 1
                action_status[status] += 1
            if action == "ask_user" and status == "ok":
                pause_count += 1
            if status == "planner_error":
                invalid_action_count += 1
            if status == "policy_error":
                invalid_action_count += 1
            if status == "tool_error":
                tool_error_count += 1
            planner = _mapping(trajectory.get("planner"))
            backend = planner.get("backend")
            if backend:
                planner_backends[str(backend)] += 1
            step_prompt, step_completion = _usage_counts(planner.get("usage"))
            if action != "orchestrator":
                planner_prompt_tokens += step_prompt
                planner_completion_tokens += step_completion
            nested_failures = _items(planner.get("failures"))
            flat_stage = planner.get("failure_stage")
            flat_code = planner.get("error_code")
            for failure in nested_failures:
                detail = _mapping(failure)
                stage = str(detail.get("stage") or flat_stage or "unknown")
                planner_failure_stages[stage] += 1
                code = str(detail.get("error_code") or flat_code or "unknown")
                planner_error_codes[code] += 1
            if flat_stage and not nested_failures:
                planner_failure_stages[str(flat_stage)] += 1
            if flat_code and not nested_failures:
                planner_error_codes[str(flat_code)] += 1
        for item in _items(diagnostics.get("recommendation_provenance")):
            recommendation_source = str(_mapping(item).get("source") or "unknown")
            recommendation_sources[recommendation_source] += 1
        response = _mapping(payload.get("raw_response"))
        response_prompt, response_completion = _usage_counts(response.get("usage"))
        response_prompt_tokens += response_prompt
        response_completion_tokens += response_completion

    target_by_sample = {
        str(sample.get("sample_id")): str(
            _mapping(sample.get("ground_truth")).get("parent_asin", "")
        )
        for sample in samples
    }
    attribution: Counter[str] = Counter()
    scenario_attribution: dict[str, Counter[str]] = {}
    annotations: list[dict[str, object]] = []
    for raw_official in _items(evaluation.get("sessions")):
        if not isinstance(raw_official, Mapping):
            continue
        official = raw_official
        sample_id = str(official.get("sample_id", ""))
        hit_turn = official.get("first_hit_turn")
        target_source: str | None = None
        if isinstance(hit_turn, int):
            hit_event = by_sample_turn.get((sample_id, hit_turn))
            payload = (
                _mapping(hit_event.get("payload")) if hit_event is not None else {}
            )
            diagnostics = _mapping(payload.get("diagnostics"))
            for item in _items(diagnostics.get("recommendation_provenance")):
                provenance = _mapping(item)
                if str(provenance.get("parent_asin")) == target_by_sample.get(sample_id):
                    target_source = str(provenance.get("source") or "unknown")
                    break
        bucket = "miss"
        if official.get("hit"):
            if target_source == "planner_selected":
                bucket = "pure_planner_hit"
            elif target_source in {"deterministic_fill", "popular_fill"}:
                bucket = "deterministic_fill_assisted_hit"
            elif target_source == "fallback_pipeline":
                bucket = "fallback_assisted_hit"
            elif target_source == "deterministic_pipeline":
                bucket = "deterministic_pipeline_hit"
            else:
                bucket = "unattributed_hit"
        attribution[bucket] += 1
        scenario = str(official.get("scenario_type") or "unknown")
        scenario_attribution.setdefault(scenario, Counter())[bucket] += 1
        annotations.append(
            {
                "sample_id": sample_id,
                "scenario_type": official.get("scenario_type"),
                "hit": bool(official.get("hit")),
                "first_hit_turn": hit_turn,
                "best_rank": official.get("best_rank"),
                "target_source": target_source,
                "attribution": bucket,
            }
        )

    turn_count = len(completed)
    session_count = len({item for item in session_ids if item})
    official_usage = _mapping(evaluation.get("reported_token_usage"))
    official_prompt, official_completion = _usage_counts(official_usage)
    baseline_delta: dict[str, object] = {}
    if baseline:
        for key in _METRIC_KEYS:
            delta = _metric_delta(evaluation.get(key), baseline.get(key))
            if delta is not None:
                baseline_delta[key] = delta
    return {
        "schema_version": SCHEMA_VERSION,
        "official_metrics": _official_metrics(evaluation),
        "execution": {
            "session_count": session_count,
            "turn_count": turn_count,
            "average_actions_per_turn": round(sum(action_counts.values()) / turn_count, 6) if turn_count else 0.0,
            "action_distribution": dict(sorted(action_counts.items())),
            "action_status_distribution": dict(sorted(action_status.items())),
            "invalid_action_count": invalid_action_count,
            "tool_error_count": tool_error_count,
            "planner_backend_distribution": dict(sorted(planner_backends.items())),
            "planner_failure_stage_distribution": dict(
                sorted(planner_failure_stages.items())
            ),
            "planner_error_code_distribution": dict(sorted(planner_error_codes.items())),
            "execution_mode_distribution": dict(sorted(execution_modes.items())),
            "pause_count": pause_count,
            "resume_count": resume_count,
            "intent_override_count": intent_override_count,
            "fallback_turn_count": fallback_turns,
            "fallback_turn_rate": round(fallback_turns / turn_count, 6) if turn_count else 0.0,
            "fallback_session_count": len({item for item in fallback_sessions if item}),
            "fallback_session_rate": (
                round(len({item for item in fallback_sessions if item}) / session_count, 6)
                if session_count
                else 0.0
            ),
            "fallback_reasons": dict(sorted(fallback_reasons.items())),
            "fallback_turns_by_scenario": dict(sorted(fallback_scenarios.items())),
            "recommendation_source_distribution": dict(
                sorted(recommendation_sources.items())
            ),
            "response_token_usage": {
                "prompt_tokens": response_prompt_tokens,
                "completion_tokens": response_completion_tokens,
                "total_tokens": response_prompt_tokens + response_completion_tokens,
                "tokens_per_session": (
                    round(
                        (response_prompt_tokens + response_completion_tokens)
                        / session_count,
                        6,
                    )
                    if session_count
                    else 0.0
                ),
            },
            "planner_step_token_usage": {
                "prompt_tokens": planner_prompt_tokens,
                "completion_tokens": planner_completion_tokens,
                "total_tokens": planner_prompt_tokens + planner_completion_tokens,
            },
            "token_usage_reconciliation": {
                "official_reported": {
                    "prompt_tokens": official_prompt,
                    "completion_tokens": official_completion,
                    "total_tokens": official_prompt + official_completion,
                },
                "response_trace_matches_official": (
                    response_prompt_tokens == official_prompt
                    and response_completion_tokens == official_completion
                ),
            },
        },
        "recommendation_attribution": dict(sorted(attribution.items())),
        "recommendation_attribution_by_scenario": {
            scenario: dict(sorted(counts.items()))
            for scenario, counts in sorted(scenario_attribution.items())
        },
        "evaluation_annotations": annotations,
        "baseline_delta": baseline_delta,
    }


def _git_metadata() -> dict[str, object]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args],
                check=True,
                capture_output=True,
                text=True,
                cwd=_REPOSITORY_ROOT,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return "unknown"

    status = run("status", "--porcelain")
    return {"commit": run("rev-parse", "HEAD"), "branch": run("branch", "--show-current"), "dirty": bool(status)}


def _agent_metadata(agent: Agent) -> dict[str, object]:
    config = agent.config
    planner_client = getattr(getattr(agent, "action_planner", None), "client", None)
    planner_backends = getattr(planner_client, "backend_names", ())
    # The old tool-loop wiring (action_planner/action_orchestrator) was removed
    # from starter.agent.Agent; these are read defensively via getattr so this
    # module keeps working against both the current fixed pipeline and any
    # test double (e.g. StaticAgent) that still sets the attributes explicitly.
    tool_planning_enabled = bool(getattr(agent, "action_orchestrator", None) is not None)
    return {
        "configured_execution_mode": (
            "tool_loop" if tool_planning_enabled else "deterministic"
        ),
        "tool_planning_requested": bool(
            getattr(config, "tool_planning_enabled", False)
        ),
        "tool_planning_enabled": tool_planning_enabled,
        "planner_backends": [str(item) for item in planner_backends] if isinstance(planner_backends, (list, tuple)) else [],
        "deepseek_configured": bool(getattr(config, "deepseek_api_key", None)),
        "deepseek_model": str(getattr(config, "deepseek_model", ""))[:120],
        "local_model_configured": bool(getattr(config, "local_base_url", None) and getattr(config, "local_model", None)),
        "local_model": str(getattr(config, "local_model", "") or "")[:120],
        "model_timeout_seconds": getattr(config, "model_timeout_seconds", None),
        "model_max_tokens": getattr(config, "model_max_tokens", None),
        "temperature": getattr(config, "temperature", None),
        "tool_max_steps": getattr(config, "tool_max_steps", None),
        "tool_timeout_seconds": getattr(config, "tool_timeout_seconds", None),
        "retrieval_limit": getattr(config, "retrieval_limit", None),
        "candidate_limit": getattr(config, "candidate_limit", None),
    }


def _write_json_atomic(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _prepare_output_dir(output_dir: str | Path) -> Path:
    output = Path(output_dir).resolve()
    data = (_REPOSITORY_ROOT / "data").resolve()
    if output == data or data in output.parents:
        raise ValueError("trace output directory must not be data/ or one of its descendants")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise FileExistsError(f"trace output directory must be empty: {output}")
    return output


def run_trace_evaluation(
    *,
    catalog_path: str | Path,
    dataset_path: str | Path,
    output_dir: str | Path,
    baseline_result_path: str | Path | None = None,
    agent: Agent | None = None,
) -> dict[str, object]:
    output = _prepare_output_dir(output_dir)
    run_id = uuid.uuid4().hex
    started_at = _utc_now()
    samples = load_jsonl(dataset_path)
    catalog_ids, categories, products = catalog_index(catalog_path)
    real_agent = agent or Agent(catalog_path)
    config_path = _REPOSITORY_ROOT / "docs/evaluation_config.json"
    evaluator_path = Path(__file__).with_name("local_evaluator.py")
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "started_at": started_at,
        "python": platform.python_version(),
        "git": _git_metadata(),
        "inputs": {
            "catalog_path": str(Path(catalog_path)),
            "catalog_sha256": _sha256_file(catalog_path),
            "dataset_path": str(Path(dataset_path)),
            "dataset_sha256": _sha256_file(dataset_path),
            "sample_count": len(samples),
            "evaluation_config_sha256": _sha256_file(config_path) if config_path.exists() else None,
            "evaluator_sha256": (
                _sha256_file(evaluator_path) if evaluator_path.exists() else None
            ),
        },
        "agent": _agent_metadata(real_agent),
    }
    manifest_path = output / "manifest.json"
    _write_json_atomic(manifest_path, manifest)
    recorder: JsonlRecorder | None = None
    try:
        recorder = JsonlRecorder(output / "events.jsonl", run_id, secrets=_secret_values())
        proxy = TracingAgentProxy(real_agent, recorder, samples, catalog_ids)
        result = evaluate(proxy, samples, catalog_ids, categories, products)  # type: ignore[arg-type]
        if proxy.fatal_recorder_error is not None:
            raise RuntimeError("trace recorder failed during evaluator-contained Agent call") from proxy.fatal_recorder_error
        recorder.close()
        _write_json_atomic(output / "evaluation.json", result)
        baseline = None
        if baseline_result_path is not None:
            baseline = json.loads(Path(baseline_result_path).read_text(encoding="utf-8"))
            if not isinstance(baseline, dict):
                raise ValueError("baseline result must contain a JSON object")
        analysis = analyze_trace(_read_events(output / "events.jsonl"), result, samples, baseline=baseline)
        _write_json_atomic(output / "analysis.json", analysis)
        execution = _mapping(analysis.get("execution"))
        observed_modes = _mapping(execution.get("execution_mode_distribution"))
        manifest.update(
            {
                "status": "completed",
                "completed_at": _utc_now(),
                "event_count": recorder.sequence,
                "observed_execution_modes": dict(observed_modes),
                "tool_loop_observed": _int_value(observed_modes.get("tool_loop")) > 0,
            }
        )
        _write_json_atomic(manifest_path, manifest)
        return {"manifest": manifest, "evaluation": result, "analysis": analysis}
    except BaseException as exc:
        if recorder is not None:
            recorder.close()
        manifest.update(
            {
                "status": "failed",
                "completed_at": _utc_now(),
                "error": sanitize(f"{type(exc).__name__}: {exc}", secrets=_secret_values()),
                "event_count": recorder.sequence if recorder is not None else 0,
            }
        )
        _write_json_atomic(manifest_path, manifest)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace the frozen TechJam local evaluator")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--dataset", default="data/public_set.jsonl")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--baseline-result")
    args = parser.parse_args()
    result = run_trace_evaluation(
        catalog_path=args.catalog,
        dataset_path=args.dataset,
        output_dir=args.output_dir,
        baseline_result_path=args.baseline_result,
    )
    evaluation = result["evaluation"]
    assert isinstance(evaluation, Mapping)
    print(json.dumps(_official_metrics(evaluation), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
