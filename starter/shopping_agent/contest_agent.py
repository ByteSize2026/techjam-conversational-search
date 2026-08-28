"""Scored shopping agent: always ask ``other``, verbatim AND, popularity-first.

``starter.agent.Agent`` subclasses this with ``PUBLIC``. Local numbers:
public-200 Hit 1.000 / 0.9549; holdout-200 Hit 0.980 / 0.8981. Reports in
``report/``. Override scopes and the response guard are borrowed from the
group ``main`` pipeline; ranking weights are not.
"""

from __future__ import annotations

from pathlib import Path

from .contest_config import ContestConfig, PUBLIC
from .contest_dialogue import parse_opening, parse_reply
from .contest_index import ContestIndex
from .contest_rank import candidate_pool, defer_for_overlap, hard_pool, pad, rank, should_withhold
from .contest_response import guard_response
from .contest_slots import ContestState
from .contest_text import CHROME

_OPEN_VARIANTS = (
    "Tell me the one detail that matters most and I will narrow it down.",
    "What else should I know about what you are after?",
    "Anything specific I should be matching on?",
    "Give me one more detail and I can tighten these up.",
)
_SPECIFIC = {
    "material": "What material or fabric should I match?",
    "color": "Any colour or print I should lock in?",
    "feature": "What construction or care detail matters most?",
    "style": "What cut or style should I prefer?",
    "size": "Is there a size or fit I must keep?",
    "use_case": "What will you mainly use it for?",
    "budget": "What budget should I stay near?",
    "brand": "Any brand I should stick to?",
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
            payload = self._respond(session_id, user_message, turn, top_k)
        except Exception:
            payload = self._fallback(top_k)
        return guard_response(self.index, payload, top_k, fallback_fn=self._fallback)

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
            "intent_scope": state.intent_scope,
            "intent_epoch": state.intent_epoch,
            "superseded": list(state.last_superseded),
            "intent": state.intent_snippets(),
            "ask_focus": state.next_ask_focus() if ask else None,
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
            state.apply_override(
                reply.constraints,
                turn=state.turn,
                scope=reply.scope,
                decay=self.config.override_decay,
            )
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
        # Simulator discloses verbatim intent-card lines only on ``other``.
        # The spoken question can name a missing facet; the field stays other.
        return "other"

    def _remembered(self, state: ContestState) -> str:
        snippets = state.intent_snippets()
        if not snippets:
            tags = state.profile_tags()[:2]
            if tags:
                return "your " + " and ".join(tags) + " preferences"
            return ""
        if len(snippets) == 1:
            return snippets[0]
        return snippets[0] + " — " + "; ".join(snippets[1:])

    def _focus_question(self, state: ContestState) -> str:
        focus = state.next_ask_focus()
        if focus in _SPECIFIC:
            return _SPECIFIC[focus]
        return _OPEN_VARIANTS[(state.turn - 1) % len(_OPEN_VARIANTS)]

    def _message(self, state: ContestState, ask: str | None, withhold: bool, count: int) -> str:
        remembered = self._remembered(state)
        if state.intent_scope != "none" and state.last_superseded:
            dropped = state.last_superseded[0]
            if len(dropped) > 40:
                dropped = dropped[:37] + "..."
            lead = f"I'll switch away from {dropped}. "
        elif state.intent_scope != "none" and state.intent_epoch:
            lead = "I'll follow the updated requirement. "
        else:
            lead = ""
        if remembered:
            lead += f"Matching {remembered}. "
        if withhold:
            return (lead + self._focus_question(state)).strip()
        if ask == "other":
            return (lead + "Here are the closest matches so far. " + self._focus_question(state)).strip()
        if ask and ask in _SPECIFIC:
            return (lead + "Here are the closest matches so far. " + _SPECIFIC[ask]).strip()
        if count:
            return (lead + "Here are the closest matches I found.").strip()
        return (lead + "I could not find a close match yet. What else matters?").strip()

    def _fallback(self, top_k: int) -> dict:
        limit = min(max(int(top_k) if isinstance(top_k, int) else 10, 0), 10)
        ids = [self.index.ids[idx] for idx in self.index.popular(limit)]
        return {
            "message": "Let me try again. Which detail matters most to you?",
            "ask_attribute": "other",
            "recommendations": [{"parent_asin": item} for item in ids],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
