"""Contest shopping agent: always ask `other`, category lock, optional gate."""

from __future__ import annotations

from pathlib import Path

from .contest_config import ContestConfig, PUBLIC
from .contest_dialogue import parse_opening, parse_reply
from .contest_index import ContestIndex
from .contest_rank import candidate_pool, defer_for_overlap, hard_pool, pad, rank, should_withhold
from .contest_slots import ContestState
from .contest_text import CHROME

_OPEN_VARIANTS = (
    "Tell me the one detail that matters most and I will narrow it down.",
    "What else should I know about what you are after?",
    "Anything specific I should be matching on?",
    "Give me one more detail and I can tighten these up.",
)
_SPECIFIC = {
    "material": "What material are you hoping for?",
    "color": "Any particular colour you have in mind?",
    "feature": "Is there a specific feature that matters most to you?",
    "style": "What style or cut are you going for?",
    "size": "Is there a size or fit you need?",
    "use_case": "What will you mainly be using it for?",
    "budget": "Roughly what budget are you working with?",
}


class ContestAgent:
    """Duck-typed Agent for the official evaluator."""

    def __init__(
        self,
        catalog_path: str | Path = "data/catalog.jsonl",
        *,
        config: ContestConfig | None = None,
        index: ContestIndex | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.config = config or PUBLIC
        self.index = index or ContestIndex(self.catalog_path)
        self._sessions: dict[str, ContestState] = {}
        self.last_diagnostics: dict[str, object] = {"event": "initialized", "catalog_size": len(self.index)}

    def reset(self, session_id: str, user_profile: dict) -> None:
        profile = dict(user_profile) if isinstance(user_profile, dict) else {}
        self._sessions[session_id] = ContestState(session_id=session_id, profile=profile)
        self.last_diagnostics = {"event": "reset", "session_id": session_id}

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        try:
            return self._respond(session_id, user_message, turn, top_k)
        except Exception:
            return self._fallback(top_k)

    def _state(self, session_id: str) -> ContestState:
        state = self._sessions.get(session_id)
        if state is None:
            state = ContestState(session_id=session_id)
            self._sessions[session_id] = state
        return state

    def _respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        state = self._state(session_id)
        state.turn = max(int(turn), 1)
        limit = min(max(int(top_k) if isinstance(top_k, int) else 10, 0), 10)
        message = user_message if isinstance(user_message, str) else ""
        if state.category is None and not state.constraints and state.turn <= 1:
            opening = parse_opening(message, self.index.bucket_lookup)
            state.category = opening.category
            state.scenario = opening.scenario
            state.add_constraints(opening.constraints, turn=state.turn, provisional=True)
        else:
            self._apply_reply(state, message)
        if self.config.use_observed_fallback:
            state.observe(message, CHROME)

        pool = candidate_pool(self.index, state, self.config)
        filtered = hard_pool(self.index, state, pool) if self.config.hard_filter else list(pool)
        withhold = should_withhold(state, self.config, len(pool), len(filtered))
        working = filtered if (self.config.hard_filter and filtered) else pool
        if not withhold and defer_for_overlap(self.index, state, self.config, working):
            withhold = True
        ranked: list[int] = []
        if not withhold:
            ranked = rank(self.index, state, self.config, working, limit=max(limit, 24))
            if self.config.pad_to_top_k:
                ranked = pad(self.index, ranked, working, limit)
            ranked = ranked[:limit]

        ask = self._choose_ask(state)
        state.pending = ask
        if ask:
            state.asked.append(ask)

        self.last_diagnostics = {
            "event": "respond",
            "session_id": session_id,
            "turn": state.turn,
            "scenario": state.scenario,
            "category": state.category,
            "pool": len(pool),
            "hard_pool": len(filtered),
            "withhold": withhold,
            "ask": ask,
            "gate_size": self.config.gate_size,
        }
        return {
            "message": self._message(state, ask, withhold, len(ranked)),
            "ask_attribute": ask,
            "recommendations": [{"parent_asin": self.index.ids[idx]} for idx in ranked],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

    def _apply_reply(self, state: ContestState, message: str) -> None:
        reply = parse_reply(message)
        if reply.kind == "override":
            state.decay_provisional(self.config.override_decay)
            state.override_applied = True
            state.add_constraints(reply.constraints, turn=state.turn)
            return
        if reply.kind == "disclosure":
            state.add_constraints(reply.constraints, turn=state.turn)
            return
        if reply.kind == "no_additional":
            state.mark_exhausted(reply.attribute or state.pending)
            return
        if reply.kind == "boundary":
            # One-shot "no preference"; asking ``other`` afterwards still
            # discloses remaining intent-card values.
            return

    def _choose_ask(self, state: ContestState) -> str | None:
        if not self.config.ask_other:
            return None
        if "other" in state.exhausted:
            return None
        return "other"

    def _message(self, state: ContestState, ask: str | None, withhold: bool, count: int) -> str:
        if withhold:
            return _OPEN_VARIANTS[(state.turn - 1) % len(_OPEN_VARIANTS)]
        if ask == "other":
            return "Here are my best matches so far. " + _OPEN_VARIANTS[(state.turn - 1) % len(_OPEN_VARIANTS)]
        if ask and ask in _SPECIFIC:
            return "Here are my best matches so far. " + _SPECIFIC[ask]
        if count:
            return "Here are the closest matches I found."
        return "I could not find a close match yet. What else matters?"

    def _fallback(self, top_k: int) -> dict:
        limit = min(max(int(top_k) if isinstance(top_k, int) else 10, 0), 10)
        ids = [self.index.ids[idx] for idx in self.index.popular(limit)]
        return {
            "message": "Let me try again. Which detail matters most to you?",
            "ask_attribute": "other",
            "recommendations": [{"parent_asin": item} for item in ids],
        }
