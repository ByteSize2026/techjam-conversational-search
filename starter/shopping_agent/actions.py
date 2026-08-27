"""Closed, bounded action contracts for the shopping-agent tool loop."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

ActionName = Literal[
    "search_products",
    "filter_products",
    "get_product_details",
    "get_user_profile",
    "ask_user",
    "recommend_products",
]

ACTION_NAMES: frozenset[str] = frozenset(
    {
        "search_products",
        "filter_products",
        "get_product_details",
        "get_user_profile",
        "ask_user",
        "recommend_products",
    }
)


class ActionValidationError(ValueError):
    """A planner proposal does not satisfy the public action vocabulary."""


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "").strip()[: max(int(limit), 0)]


def _json_arguments(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ActionValidationError("action arguments must be an object")
    output: dict[str, object] = {}
    for raw_key, raw_value in list(value.items())[:20]:
        key = _bounded_text(raw_key, 60)
        if key:
            output[key] = raw_value
    return output


@dataclass(frozen=True)
class PlannedAction:
    name: ActionName
    arguments: dict[str, object] = field(default_factory=dict)
    rationale: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "arguments": dict(self.arguments),
            "rationale": self.rationale,
        }


def parse_planned_action(value: object) -> PlannedAction:
    """Validate one decoded planner action without repairing unknown actions."""

    if isinstance(value, PlannedAction):
        # Dataclass type annotations are not runtime validators.  Re-validate
        # injected/scripted actions as strictly as model-decoded mappings so a
        # malformed ``PlannedAction`` cannot bypass the public action boundary.
        name = _bounded_text(value.name, 80)
        arguments = _json_arguments(value.arguments)
        rationale = _bounded_text(value.rationale, 400)
        if name not in ACTION_NAMES:
            raise ActionValidationError(f"unknown action: {name or '<empty>'}")
        return PlannedAction(name=name, arguments=arguments, rationale=rationale)  # type: ignore[arg-type]
    if not isinstance(value, Mapping):
        raise ActionValidationError("planned action must be an object")
    name = _bounded_text(value.get("name") or value.get("action"), 80)
    if name not in ACTION_NAMES:
        raise ActionValidationError(f"unknown action: {name or '<empty>'}")
    arguments = _json_arguments(value.get("arguments", value.get("args")))
    rationale = _bounded_text(value.get("rationale") or value.get("reason"), 400)
    return PlannedAction(name=name, arguments=arguments, rationale=rationale)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ActionObservation:
    action: str
    ok: bool
    payload: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "ok": bool(self.ok),
            "payload": dict(self.payload),
            "error": self.error,
            "elapsed_ms": round(max(float(self.elapsed_ms), 0.0), 3),
        }


@dataclass(frozen=True)
class TrajectoryEntry:
    turn: int
    step: int
    action: str
    arguments_summary: dict[str, object]
    observation_summary: dict[str, object]
    status: str
    rationale: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "turn": int(self.turn),
            "step": int(self.step),
            "action": self.action,
            "arguments": dict(self.arguments_summary),
            "observation": dict(self.observation_summary),
            "status": self.status,
            "rationale": self.rationale,
        }


@dataclass
class PendingTask:
    question: str
    ask_attribute: str
    intent_epoch: int
    candidate_ids: list[str] = field(default_factory=list)
    created_turn: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "question": self.question,
            "ask_attribute": self.ask_attribute,
            "intent_epoch": self.intent_epoch,
            "candidate_ids": list(self.candidate_ids),
            "created_turn": self.created_turn,
        }


def bounded_ids(value: object, *, limit: int = 100) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ActionValidationError("product IDs must be an array")
    output: list[str] = []
    seen: set[str] = set()
    for raw in value[: max(int(limit), 0)]:
        item = _bounded_text(raw, 80)
        if item and item not in seen:
            output.append(item)
            seen.add(item)
    return output


def summarize_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    """Create a bounded, non-secret trajectory projection."""

    output: dict[str, object] = {}
    for key, value in list(arguments.items())[:12]:
        clean_key = _bounded_text(key, 60)
        if not clean_key:
            continue
        if isinstance(value, str):
            output[clean_key] = value[:240]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            output[clean_key] = [_bounded_text(item, 80) for item in list(value)[:12]]
        elif isinstance(value, (int, float, bool)) or value is None:
            output[clean_key] = value
        else:
            output[clean_key] = _bounded_text(value, 240)
    return output


__all__ = [
    "ACTION_NAMES",
    "ActionName",
    "ActionObservation",
    "ActionValidationError",
    "PendingTask",
    "PlannedAction",
    "TrajectoryEntry",
    "bounded_ids",
    "parse_planned_action",
    "summarize_arguments",
]
