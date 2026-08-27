"""Bounded execution loop for planner-selected shopping tools."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .actions import (
    ActionObservation,
    PendingTask,
    TrajectoryEntry,
    summarize_arguments,
)
from .planner import ActionPlanner, PlannerContext, normalize_planner_result
from .state import SessionState
from .tools import ShoppingToolbox


@dataclass(frozen=True)
class OrchestratorResult:
    response: dict[str, object] | None
    fallback_needed: bool
    reason: str
    action_count: int
    backend: str | None = None
    usage: dict[str, int] | None = None
    failures: tuple[object, ...] = ()


def _observation_summary(observation: ActionObservation) -> dict[str, object]:
    payload = observation.payload
    summary: dict[str, object] = {
        "ok": observation.ok,
        "elapsed_ms": round(observation.elapsed_ms, 3),
    }
    if observation.error:
        summary["error"] = observation.error[:300]
    for key in (
        "count",
        "input_count",
        "matched_count",
        "unknown_count",
        "profile_loaded",
        "pause",
        "terminal",
        "ask_attribute",
        "parent_asin",
    ):
        if key in payload:
            summary[key] = payload[key]
    products = payload.get("products")
    if isinstance(products, list):
        summary["product_ids"] = [
            str(item.get("parent_asin"))[:80]
            for item in products[:20]
            if isinstance(item, dict) and item.get("parent_asin")
        ]
    ids = payload.get("parent_asins") or payload.get("candidate_ids")
    if isinstance(ids, list):
        summary["product_ids"] = [str(item)[:80] for item in ids[:20]]
    profile = payload.get("profile")
    if isinstance(profile, dict):
        summary["profile_fields"] = sorted(str(key)[:80] for key in profile)[:20]
    return summary


class ActionOrchestrator:
    """Run one action per planner step, terminating or pausing within bounds."""

    def __init__(
        self,
        planner: ActionPlanner,
        *,
        max_steps: int = 4,
        timeout_seconds: float = 8.0,
        max_invalid_actions: int = 2,
    ) -> None:
        self.planner = planner
        self.max_steps = min(max(int(max_steps), 1), 12)
        self.timeout_seconds = max(float(timeout_seconds), 0.01)
        self.max_invalid_actions = min(max(int(max_invalid_actions), 1), 4)

    def run(
        self,
        state: SessionState,
        toolbox: ShoppingToolbox,
        *,
        user_message: str,
        turn: int,
        resumed_after_ask: bool = False,
        resumed_task: PendingTask | None = None,
    ) -> OrchestratorResult:
        started = time.perf_counter()
        observations: list[ActionObservation] = []
        failures: list[object] = []
        invalid_count = 0
        prompt_tokens = 0
        completion_tokens = 0
        saw_usage = False
        backend: str | None = None

        if resumed_after_ask and resumed_task is not None:
            state.record_tool_entry(
                TrajectoryEntry(
                    turn=int(turn),
                    step=0,
                    action="resume_after_ask",
                    arguments_summary={},
                    observation_summary={
                        "ask_attribute": resumed_task.ask_attribute,
                        "asked_turn": resumed_task.created_turn,
                    },
                    status="resumed",
                )
            )

        for step in range(1, self.max_steps + 1):
            if time.perf_counter() - started >= self.timeout_seconds:
                return self._fallback(
                    "time_budget_exhausted",
                    step - 1,
                    backend,
                    prompt_tokens,
                    completion_tokens,
                    saw_usage,
                    failures,
                    state=state,
                    turn=turn,
                )
            context = PlannerContext.from_state(
                state,
                user_message=user_message,
                turn=turn,
                remaining_steps=self.max_steps - step + 1,
                observations=observations,
                resumed_after_ask=resumed_after_ask,
                resumed_task=resumed_task,
            )
            try:
                raw_result = self.planner.next_action(context)
                planner_result = normalize_planner_result(raw_result)
            except Exception as exc:  # noqa: BLE001 - planner is an injected boundary
                planner_result = normalize_planner_result(None)
                planner_result = type(planner_result)(
                    action=None,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
            if planner_result.backend:
                backend = planner_result.backend
            failures.extend(planner_result.failures)
            if planner_result.usage:
                prompt = planner_result.usage.get("prompt_tokens")
                completion = planner_result.usage.get("completion_tokens")
                if isinstance(prompt, int) and prompt >= 0 and isinstance(completion, int) and completion >= 0:
                    prompt_tokens += prompt
                    completion_tokens += completion
                    saw_usage = True
            if time.perf_counter() - started >= self.timeout_seconds:
                return self._fallback(
                    "time_budget_exhausted",
                    step,
                    backend,
                    prompt_tokens,
                    completion_tokens,
                    saw_usage,
                    failures,
                    state=state,
                    turn=turn,
                )
            action = planner_result.action
            if action is None:
                invalid_count += 1
                observation = ActionObservation(
                    action="planner_error",
                    ok=False,
                    error=planner_result.error or "invalid planner action",
                )
                observations.append(observation)
                self._record(state, turn, step, None, observation, "planner_error")
                if invalid_count >= self.max_invalid_actions:
                    return self._fallback(
                        "invalid_action_limit",
                        step,
                        backend,
                        prompt_tokens,
                        completion_tokens,
                        saw_usage,
                        failures,
                        state=state,
                        turn=turn,
                    )
                continue

            observation = toolbox.execute(action)
            observations.append(observation)
            status = "ok" if observation.ok else "tool_error"
            self._record(state, turn, step, action, observation, status)
            if time.perf_counter() - started >= self.timeout_seconds and not (
                observation.ok
                and action.name in {"ask_user", "recommend_products"}
            ):
                return self._fallback(
                    "time_budget_exhausted",
                    step,
                    backend,
                    prompt_tokens,
                    completion_tokens,
                    saw_usage,
                    failures,
                    state=state,
                    turn=turn,
                )
            if not observation.ok:
                invalid_count += 1
                state.last_tool_error = observation.error
                if invalid_count >= self.max_invalid_actions:
                    return self._fallback(
                        "tool_error_limit",
                        step,
                        backend,
                        prompt_tokens,
                        completion_tokens,
                        saw_usage,
                        failures,
                        state=state,
                        turn=turn,
                    )
                continue

            payload = observation.payload
            if action.name == "ask_user" and payload.get("pause") is True:
                question = str(payload.get("question") or "What preference matters most to you?")[:500]
                attribute = str(payload.get("ask_attribute") or "other")
                raw_candidate_ids = payload.get("candidate_ids")
                candidate_ids = (
                    [str(item) for item in raw_candidate_ids[:20]]
                    if isinstance(raw_candidate_ids, list)
                    else []
                )
                state.pending_task = PendingTask(
                    question=question,
                    ask_attribute=attribute,
                    intent_epoch=state.intent_epoch,
                    candidate_ids=candidate_ids,
                    created_turn=int(turn),
                )
                state.record_asked(attribute)
                state.last_candidate_ids = list(candidate_ids[: toolbox.top_k])
                state.record_recommendations(state.last_candidate_ids)
                response: dict[str, object] = {
                    "message": question,
                    "ask_attribute": attribute,
                    "recommendations": [
                        {"parent_asin": item} for item in state.last_candidate_ids
                    ],
                }
                usage = self._usage(prompt_tokens, completion_tokens, saw_usage)
                if usage:
                    response["usage"] = usage
                return OrchestratorResult(
                    response=response,
                    fallback_needed=False,
                    reason="ask_user",
                    action_count=step,
                    backend=backend,
                    usage=usage,
                    failures=tuple(failures),
                )

            if action.name == "recommend_products" and payload.get("terminal") is True:
                raw_ids = payload.get("parent_asins")
                ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
                state.pending_task = None
                state.last_candidate_ids = list(ids[: toolbox.top_k])
                state.record_recommendations(state.last_candidate_ids)
                response = {
                    "message": str(payload.get("message") or "Here are the closest matches I found.")[:500],
                    "ask_attribute": None,
                    "recommendations": [
                        {"parent_asin": item} for item in state.last_candidate_ids
                    ],
                }
                usage = self._usage(prompt_tokens, completion_tokens, saw_usage)
                if usage:
                    response["usage"] = usage
                return OrchestratorResult(
                    response=response,
                    fallback_needed=False,
                    reason="recommend_products",
                    action_count=step,
                    backend=backend,
                    usage=usage,
                    failures=tuple(failures),
                )

        return self._fallback(
            "step_budget_exhausted",
            self.max_steps,
            backend,
            prompt_tokens,
            completion_tokens,
            saw_usage,
            failures,
            state=state,
            turn=turn,
        )

    @staticmethod
    def _record(
        state: SessionState,
        turn: int,
        step: int,
        action: object | None,
        observation: ActionObservation,
        status: str,
    ) -> None:
        arguments = getattr(action, "arguments", {}) if action is not None else {}
        # Model rationale is untrusted free text and may echo profile content.
        # The bounded action/result summaries are sufficient to audit the loop.
        rationale = ""
        name = str(getattr(action, "name", observation.action))
        state.record_tool_entry(
            TrajectoryEntry(
                turn=int(turn),
                step=int(step),
                action=name,
                arguments_summary=summarize_arguments(arguments),
                observation_summary=_observation_summary(observation),
                status=status,
                rationale=rationale,
            )
        )

    @staticmethod
    def _usage(prompt: int, completion: int, saw_usage: bool) -> dict[str, int] | None:
        if not saw_usage:
            return None
        return {"prompt_tokens": prompt, "completion_tokens": completion}

    @classmethod
    def _fallback(
        cls,
        reason: str,
        action_count: int,
        backend: str | None,
        prompt: int,
        completion: int,
        saw_usage: bool,
        failures: list[object],
        *,
        state: SessionState | None = None,
        turn: int | None = None,
    ) -> OrchestratorResult:
        if state is not None and turn is not None:
            state.record_tool_entry(
                TrajectoryEntry(
                    turn=int(turn),
                    step=int(action_count) + 1,
                    action="orchestrator",
                    arguments_summary={},
                    observation_summary={"reason": reason},
                    status="fallback",
                )
            )
        return OrchestratorResult(
            response=None,
            fallback_needed=True,
            reason=reason,
            action_count=action_count,
            backend=backend,
            usage=cls._usage(prompt, completion, saw_usage),
            failures=tuple(failures),
        )


__all__ = ["ActionOrchestrator", "OrchestratorResult"]
