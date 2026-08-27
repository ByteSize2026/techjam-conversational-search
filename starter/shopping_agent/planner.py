"""Replaceable planners for selecting one bounded shopping action at a time."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .actions import ActionObservation, PendingTask, PlannedAction, parse_planned_action
from .model import TieredModelClient
from .state import SessionState

ACTION_SCHEMAS: tuple[dict[str, object], ...] = (
    {"name": "search_products", "arguments": {"query": "string", "limit": "1..20", "category": "optional string"}},
    {"name": "filter_products", "arguments": {"product_ids": "array", "attribute": "string", "operator": "contains|equals|not_contains|lt|lte|gt|gte|between", "value": "value", "max_value": "optional number"}},
    {"name": "get_product_details", "arguments": {"parent_asin": "observed catalog ID"}},
    {"name": "get_user_profile", "arguments": {}},
    {"name": "ask_user", "arguments": {"question": "string", "ask_attribute": "official attribute enum"}},
    {"name": "recommend_products", "arguments": {"parent_asins": "observed catalog IDs", "message": "string"}},
)


@dataclass(frozen=True)
class PlannerContext:
    session_id: str
    user_message: str
    turn: int
    remaining_turns: int
    remaining_steps: int
    intent_epoch: int
    requirements: tuple[dict[str, object], ...]
    category_anchor: str | None
    no_preference: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    observations: tuple[ActionObservation, ...] = ()
    profile_loaded: bool = False
    profile: dict[str, object] | None = None
    resumed_after_ask: bool = False
    answered_clarification: dict[str, object] | None = None

    @classmethod
    def from_state(
        cls,
        state: SessionState,
        *,
        user_message: str,
        turn: int,
        remaining_steps: int,
        observations: Sequence[ActionObservation] = (),
        resumed_after_ask: bool = False,
        resumed_task: PendingTask | None = None,
    ) -> PlannerContext:
        profile = dict(state.session_profile) if state.profile_loaded else None
        return cls(
            session_id=state.session_id,
            user_message=user_message[:1200],
            turn=int(turn),
            remaining_turns=max(10 - int(turn), 0),
            remaining_steps=max(int(remaining_steps), 0),
            intent_epoch=state.intent_epoch,
            requirements=tuple(item.as_dict() for item in state.active_constraints[:20]),
            category_anchor=state.category_anchor,
            no_preference=tuple(sorted(state.no_preference)),
            candidate_ids=tuple(state.tool_candidate_ids[:100]),
            observations=tuple(observations[-4:]),
            profile_loaded=state.profile_loaded,
            profile=profile,
            resumed_after_ask=bool(resumed_after_ask),
            answered_clarification=(
                {
                    "question": resumed_task.question[:500],
                    "ask_attribute": resumed_task.ask_attribute,
                    "asked_turn": resumed_task.created_turn,
                }
                if resumed_after_ask and resumed_task is not None
                else None
            ),
        )

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "user_message": self.user_message,
            "turn": self.turn,
            "remaining_turns": self.remaining_turns,
            "remaining_steps": self.remaining_steps,
            "intent_epoch": self.intent_epoch,
            "requirements": list(self.requirements),
            "category_anchor": self.category_anchor,
            "no_preference": list(self.no_preference),
            "candidate_ids": list(self.candidate_ids),
            "observations": [item.as_dict() for item in self.observations],
            "profile_loaded": self.profile_loaded,
            "resumed_after_ask": self.resumed_after_ask,
            "answered_clarification": dict(self.answered_clarification or {}),
        }
        if self.profile_loaded:
            payload["profile"] = dict(self.profile or {})
        return payload


@dataclass(frozen=True)
class PlannerResult:
    action: PlannedAction | None
    backend: str | None = None
    usage: dict[str, int] | None = None
    failures: tuple[object, ...] = ()
    error: str | None = None


class ActionPlanner(Protocol):
    def next_action(self, context: PlannerContext) -> object:
        ...


class ModelActionPlanner:
    """Use the existing JSON completion client to choose one tool action."""

    def __init__(self, client: TieredModelClient) -> None:
        self.client = client

    @staticmethod
    def _validate(value: object) -> PlannedAction:
        return parse_planned_action(value)

    def next_action(self, context: PlannerContext) -> PlannerResult:
        system = (
            "You are a shopping tool planner. Choose exactly one JSON action. "
            "Use only catalog products returned by tools; never invent product IDs or facts. "
            "Read the profile with get_user_profile before using it. Ask the user only when "
            "a missing preference materially changes the choice. End with recommend_products. "
            "Return {\"name\":...,\"arguments\":...,\"rationale\":...}."
        )
        payload = {
            "available_actions": ACTION_SCHEMAS,
            "state": context.as_dict(),
        }
        completion = self.client.complete_json(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
            validator=self._validate,
        )
        usage = completion.usage.as_dict() if completion.usage else None
        action = completion.value if isinstance(completion.value, PlannedAction) else None
        return PlannerResult(
            action=action,
            backend=completion.backend,
            usage=usage,
            failures=tuple(completion.failures),
            error=None if action is not None else "model did not return a valid action",
        )


class ScriptedActionPlanner:
    """Deterministic planner used by tests and offline trajectory demos."""

    def __init__(self, actions: Sequence[object]) -> None:
        self.actions = list(actions)
        self.contexts: list[PlannerContext] = []

    def next_action(self, context: PlannerContext) -> PlannedAction:
        self.contexts.append(context)
        if not self.actions:
            raise RuntimeError("scripted planner is exhausted")
        return parse_planned_action(self.actions.pop(0))


class DeterministicActionPlanner:
    """A small data-driven action loop for smoke tests, not the default Agent path."""

    def next_action(self, context: PlannerContext) -> PlannedAction:
        action_names = [item.action for item in context.observations]
        if "search_products" not in action_names:
            query = " ".join(
                item
                for item in (
                    context.category_anchor or "",
                    *(str(req.get("value") or "") for req in context.requirements),
                    context.user_message,
                )
                if item
            )
            return PlannedAction("search_products", {"query": query[:800], "limit": 10})
        if context.candidate_ids:
            return PlannedAction(
                "recommend_products",
                {
                    "parent_asins": list(context.candidate_ids[:10]),
                    "message": "Here are the closest matches I found.",
                },
            )
        return PlannedAction(
            "ask_user",
            {"question": "What feature matters most to you?", "ask_attribute": "feature"},
        )


def normalize_planner_result(value: object) -> PlannerResult:
    if isinstance(value, PlannerResult):
        return value
    try:
        return PlannerResult(action=parse_planned_action(value))
    except (TypeError, ValueError) as exc:
        return PlannerResult(action=None, error=f"{type(exc).__name__}: {exc}"[:500])


__all__ = [
    "ACTION_SCHEMAS",
    "ActionPlanner",
    "DeterministicActionPlanner",
    "ModelActionPlanner",
    "PlannerContext",
    "PlannerResult",
    "ScriptedActionPlanner",
    "normalize_planner_result",
]
