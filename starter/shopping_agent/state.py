"""Session state, deterministic intent updates, and state reduction.

The public evaluator gives an Agent no stable user identity and no hidden
intent card.  Consequently all mutable information in this module is scoped
to one ``session_id``.  Natural-language parsing is intentionally conservative:
it extracts high-confidence slots and keeps the original text as bounded query
evidence instead of inventing an exact constraint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Literal


Attribute = Literal[
    "category",
    "material",
    "color",
    "size",
    "style",
    "brand",
    "budget",
    "feature",
    "use_case",
    "other",
]

ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "category",
        "material",
        "color",
        "size",
        "style",
        "brand",
        "budget",
        "feature",
        "use_case",
        "other",
    }
)

_MATERIALS = (
    "cotton",
    "polyester",
    "nylon",
    "leather",
    "wool",
    "spandex",
    "silk",
    "rayon",
    "fabric",
    "linen",
    "denim",
    "cashmere",
)
_COLORS = (
    "black",
    "white",
    "blue",
    "red",
    "pink",
    "green",
    "brown",
    "gray",
    "grey",
    "purple",
    "yellow",
    "orange",
    "navy",
    "beige",
)
_USE_CASES = (
    "hiking",
    "running",
    "gym",
    "work",
    "winter",
    "outdoor",
    "travel",
    "walking",
    "office",
    "summer",
    "rain",
)
_STYLE_WORDS = (
    "fit",
    "comfort",
    "style",
    "sleeve",
    "neck",
    "casual",
    "formal",
    "slim",
    "loose",
    "warmth",
    "durability",
)
_SIZE_RE = re.compile(r"\b(?:size|sizing|width|wide|narrow|small|medium|large|xs|xl|xxl)\b", re.I)
_BUDGET_RE = re.compile(
    r"(?:\$\s*\d+(?:[,.]\d+)?|\b(?:under|below|less than|around|budget(?: of)?|up to)\s+\$?\s*\d+(?:[,.]\d+)?)",
    re.I,
)
_OVERRIDE_RE = re.compile(
    r"\b(?:actually|instead|rather)\b[^.?!;]*\b(?:ignore|disregard|forget|different|change)\b"
    r"|\b(?:ignore|disregard|forget)\b[^.?!;]*(?:earlier|previous|above|before|preference)",
    re.I,
)
_NO_PREFERENCE_RE = re.compile(
    r"(?:no|without)\s+(?:an?\s+)?(?:additional\s+)?preference\s*(?:for|on|about)?\s*([a-z][a-z_ -]{1,24})?"
    r"|\b(?:don't|do not|doesn't|does not)\s+(?:have\s+(?:an?\s+)?(?:additional\s+)?preference|care|matter)\s*(?:about|for)?\s*([a-z][a-z_ -]{1,24})?",
    re.I,
)
_BOUNDARY_RE = re.compile(
    r"\b(?:use\s+your\s+judg(?:e|)ment|you\s+decide|your\s+choice|anything\s+is\s+fine|"
    r"it\s+does(?:n't|\s+not)\s+matter)\b",
    re.I,
)
_GLOBAL_EXHAUSTED_RE = re.compile(
    r"\b(?:no\s+(?:additional|other|more)\s+preferences?|nothing\s+else|"
    r"no\s+more\s+(?:preferences?|requirements?)|that(?:'s|\s+is)\s+all|"
    r"i\s+have\s+no\s+other\s+preferences?)\b",
    re.I,
)
_RECOMMENDATION_REJECTION_RE = re.compile(
    r"\b(?:those|these|the)\s+(?:options?|recommendations?|choices?|items?)\s+"
    r"(?:are|seem|look)\s+(?:not\s+(?:quite\s+)?(?:right|suitable|what\s+i\s+want)|"
    r"wrong|off)\b"
    r"|\b(?:options?|recommendations?|choices?|items?)\s+(?:aren't|are\s+not|"
    r"don't\s+work|do\s+not\s+work|weren't|were\s+not)\b"
    r"|\bnot\s+(?:quite\s+)?(?:right|what\s+i\s+want)\b",
    re.I,
)
_CLARIFICATION_REQUEST_RE = re.compile(
    r"\b(?:ask|question)\s+me\s+(?:about|on)\b"
    r"|\b(?:keep|continue)\s+(?:asking|questioning)\b"
    r"|\b(?:ask|question)\s+(?:a|one|another)\s+(?:specific\s+)?"
    r"(?:question|attribute|preference|requirement)\b",
    re.I,
)


def _clean(value: object, limit: int = 180) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n-;,.")[:limit].rstrip()


_CONSTRAINT_LABELS: dict[str, tuple[str, ...]] = {
    "category": ("category",),
    "material": ("material",),
    "color": ("color", "colour"),
    "size": ("size", "sizing"),
    "style": ("style",),
    "brand": ("brand",),
    "budget": ("budget", "price"),
    "feature": ("feature", "detail"),
    "use_case": ("use case", "use_case", "usecase", "usage"),
}


def normalize_constraint_value(attribute: object, value: object) -> str:
    """Return a semantic constraint value without protocol field labels.

    Evaluator replies may encode a slot as ``color: black`` or
    ``material=cotton``.  The prefix identifies the field; it is not a
    preference token and must not affect either eligibility or ranking.
    """

    text = _clean(value, 320)
    attribute_key = re.sub(r"[\s-]+", "_", str(attribute or "").strip().lower())
    if attribute_key == "colour":
        attribute_key = "color"
    labels = _CONSTRAINT_LABELS.get(attribute_key, ())
    if labels:
        label_pattern = "|".join(sorted((re.escape(label) for label in labels), key=len, reverse=True))
        text = re.sub(
            rf"^\s*(?:{label_pattern})\s*[:=-]\s*",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    return _clean(text, 320)


def _attribute_from_text(value: str) -> str:
    lowered = value.lower()
    if _BUDGET_RE.search(lowered) or "budget" in lowered or "price" in lowered:
        return "budget"
    if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in _MATERIALS):
        return "material"
    if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in _COLORS) or "color" in lowered:
        return "color"
    if _SIZE_RE.search(lowered):
        return "size"
    if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in _USE_CASES):
        return "use_case"
    if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in _STYLE_WORDS):
        return "style"
    if "brand" in lowered or " by " in f" {lowered} ":
        return "brand"
    if "categor" in lowered or "department" in lowered or "type" in lowered:
        return "category"
    return "feature"


def _attribute_name(value: object) -> str:
    lowered = _clean(value, 40).lower().replace(" ", "_")
    aliases = {
        "colour": "color",
        "price": "budget",
        "pricing": "budget",
        "use": "use_case",
        "usecase": "use_case",
        "usage": "use_case",
        "fit_and_comfort": "style",
    }
    lowered = aliases.get(lowered, lowered)
    return lowered if lowered in ALLOWED_ATTRIBUTES else _attribute_from_text(lowered)


def _split_values(value: str) -> list[str]:
    # The simulator uses semicolons, but accepting a small set of separators
    # also works for natural paraphrases without creating one huge constraint.
    pieces = re.split(r"\s*;\s*|\s+and\s+|\s*,\s*", value, flags=re.I)
    output: list[str] = []
    for piece in pieces:
        cleaned = _clean(piece)
        if cleaned and cleaned.lower() not in {"please", "thanks", "thank you"}:
            output.append(cleaned)
    return list(dict.fromkeys(output))


@dataclass
class Constraint:
    attribute: str
    value: str
    polarity: Literal["prefer", "avoid", "require"] = "prefer"
    hardness: Literal["hard", "soft"] = "soft"
    source: Literal["user", "profile", "model", "rule"] = "user"
    confidence: float = 1.0
    turn: int = 0
    epoch: int = 0
    active: bool = True
    status: str = "active"

    def normalized_value(self) -> str:
        return normalize_constraint_value(self.attribute, self.value).lower()

    def as_dict(self) -> dict[str, object]:
        return {
            "attribute": self.attribute,
            "value": self.value,
            "polarity": self.polarity,
            "hardness": self.hardness,
            "source": self.source,
            "confidence": self.confidence,
            "turn": self.turn,
            "epoch": self.epoch,
            "active": self.active,
            "status": self.status,
        }


@dataclass(frozen=True)
class ConstraintMutation:
    action: Literal["upsert", "replace", "remove"]
    attribute: str
    value: str = ""
    polarity: Literal["prefer", "avoid", "require"] = "prefer"
    hardness: Literal["hard", "soft"] = "soft"
    source: Literal["user", "profile", "model", "rule"] = "user"
    confidence: float = 1.0


@dataclass(frozen=True)
class IntentUpdate:
    global_override: bool = False
    mutations: tuple[ConstraintMutation, ...] = ()
    category_anchor: str | None = None
    no_preference: frozenset[str] = frozenset()
    global_exhausted: bool = False
    boundary_signal: bool = False
    reopen_clarification: bool = False
    query_terms: tuple[str, ...] = ()
    confidence: float = 1.0

    @property
    def override(self) -> bool:
        return self.global_override


@dataclass(frozen=True)
class TurnMessage:
    turn: int
    user_message: str
    intent_epoch: int


@dataclass
class CandidateStats:
    estimated_count: int = 0
    route_overlap: float = 0.0
    category_entropy: float = 0.0
    attribute_entropy: dict[str, float] = field(default_factory=dict)
    active_hard_constraint_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "estimated_count": self.estimated_count,
            "route_overlap": round(float(self.route_overlap), 4),
            "category_entropy": round(float(self.category_entropy), 4),
            "attribute_entropy": {
                str(key): round(float(value), 4) for key, value in self.attribute_entropy.items()
            },
            "active_hard_constraint_count": self.active_hard_constraint_count,
        }


@dataclass(frozen=True)
class RuntimeContext:
    route_hint: str
    category_anchor: str | None
    hard_constraints: tuple[Constraint, ...]
    soft_preferences: tuple[Constraint, ...]
    avoided_values: tuple[Constraint, ...]
    profile_priors: tuple[str, ...]
    unanswered_attributes: tuple[str, ...]
    intent_epoch: int
    turn: int
    remaining_turns: int
    candidate_stats: CandidateStats | None = None


@dataclass
class SessionState:
    session_id: str
    profile: dict[str, object] = field(default_factory=dict)
    session_profile: dict[str, object] = field(default_factory=dict)
    category_anchor: str | None = None
    constraints: list[Constraint] = field(default_factory=list)
    no_preference: set[str] = field(default_factory=set)
    # Compatibility name is retained; explicit policy state is kept in lockstep.
    attribute_exhausted: set[str] = field(default_factory=set)
    global_exhausted: bool = False
    boundary_seen: bool = False
    exhaustion_reason: str | None = None
    ask_counts: dict[str, int] = field(default_factory=dict)
    previous_structured_pool_size: int | None = None
    previous_structured_pool_ids: tuple[str, ...] = ()
    previous_progress_fingerprint: str | None = None
    no_progress_streak: int = 0
    previous_ranked_ids: list[str] = field(default_factory=list)
    softened_constraint_keys: set[tuple[str, str]] = field(default_factory=set)
    asked_attributes: list[str] = field(default_factory=list)
    messages: list[TurnMessage] = field(default_factory=list)
    intent_epoch: int = 0
    recommendations_by_epoch: dict[int, list[str]] = field(default_factory=lambda: {0: []})
    last_candidate_ids: list[str] = field(default_factory=list)
    last_candidate_stats: CandidateStats | None = None
    active_route: str = "browsing"
    last_state_fingerprint: str | None = None
    query_terms: list[str] = field(default_factory=list)
    superseded_constraints: list[Constraint] = field(default_factory=list)
    last_diagnostics: dict[str, object] = field(default_factory=dict)

    @property
    def active_constraints(self) -> list[Constraint]:
        return [item for item in self.constraints if item.active and item.status == "active"]

    @property
    def active_preferences(self) -> list[Constraint]:
        return [item for item in self.active_constraints if item.polarity != "avoid"]

    @property
    def avoided_values(self) -> list[Constraint]:
        return [item for item in self.active_constraints if item.polarity == "avoid"]

    @property
    def asked_set(self) -> set[str]:
        return set(self.asked_attributes)

    @property
    def seen_recommendations(self) -> set[str]:
        return set(self.recommendations_by_epoch.get(self.intent_epoch, ()))

    def record_message(self, turn: int, message: str) -> None:
        self.messages.append(TurnMessage(int(turn), _clean(message, 800), self.intent_epoch))
        # Complete history is not needed for retrieval and can make prompts
        # grow without bound.  Keep a short audit trail for diagnostics.
        if len(self.messages) > 12:
            del self.messages[:-12]

    def record_asked(self, attribute: object) -> None:
        normalized = _attribute_name(attribute)
        if normalized in ALLOWED_ATTRIBUTES:
            self.ask_counts[normalized] = self.ask_counts.get(normalized, 0) + 1
            if normalized not in self.asked_attributes:
                self.asked_attributes.append(normalized)

    def update_progress(
        self,
        pool_ids: Sequence[object],
        ranked_ids: Sequence[object] = (),
        *,
        intent_fingerprint: str | None = None,
    ) -> None:
        """Record target-free pool/rank progress for clarification safeguards."""

        normalized_pool = tuple(str(value).strip() for value in pool_ids if str(value).strip())
        current_fingerprint = intent_fingerprint or self.fingerprint()
        has_previous_progress = (
            bool(self.previous_structured_pool_ids)
            or self.previous_progress_fingerprint is not None
        )
        pool_changed = normalized_pool != self.previous_structured_pool_ids
        intent_changed = current_fingerprint != self.previous_progress_fingerprint
        if not has_previous_progress:
            self.no_progress_streak = 0
        elif pool_changed or intent_changed:
            self.no_progress_streak = 0
        else:
            self.no_progress_streak += 1
        self.previous_structured_pool_ids = normalized_pool
        self.previous_structured_pool_size = len(normalized_pool)
        self.previous_progress_fingerprint = current_fingerprint
        self.previous_ranked_ids = [str(value).strip() for value in ranked_ids if str(value).strip()][:10]

    def record_recommendations(self, ids: Sequence[object]) -> None:
        current = self.recommendations_by_epoch.setdefault(self.intent_epoch, [])
        for value in ids:
            parent_asin = str(value).strip()
            if parent_asin and parent_asin not in current:
                current.append(parent_asin)
        # A session never needs an unbounded exclusion list.  Retain enough
        # history to cover all ten evaluator turns while keeping state small.
        if len(current) > 200:
            del current[:-200]

    def fingerprint(self) -> str:
        payload = {
            "category_anchor": self.category_anchor,
            "constraints": [
                item.as_dict()
                for item in self.active_constraints
            ],
            "no_preference": sorted(self.no_preference),
            "attribute_exhausted": sorted(self.attribute_exhausted),
            "global_exhausted": self.global_exhausted,
            "boundary_seen": self.boundary_seen,
            "softened_constraint_keys": sorted(self.softened_constraint_keys),
            "query_terms": list(self.query_terms),
            "intent_epoch": self.intent_epoch,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        return hashlib.sha1(encoded).hexdigest()

    def runtime_context(
        self,
        *,
        turn: int,
        remaining_turns: int,
        route_hint: str | None = None,
        candidate_stats: CandidateStats | None = None,
    ) -> RuntimeContext:
        active = self.active_constraints
        hard = tuple(item for item in active if item.hardness == "hard")
        avoided = tuple(item for item in active if item.polarity == "avoid")
        soft = tuple(item for item in active if item not in hard and item not in avoided)
        profile_tags = self.profile.get("preference_tags", ())
        if isinstance(profile_tags, str):
            profile_tags = (profile_tags,)
        if not isinstance(profile_tags, (list, tuple, set)):
            profile_tags = ()
        exhausted = self.no_preference | self.attribute_exhausted
        unanswered = tuple(
            attribute
            for attribute in (
                "material",
                "color",
                "size",
                "style",
                "brand",
                "budget",
                "feature",
                "use_case",
            )
            if attribute not in exhausted and not any(
                item.attribute == attribute for item in active
            )
        )
        return RuntimeContext(
            route_hint=route_hint or self.active_route,
            category_anchor=self.category_anchor,
            hard_constraints=hard,
            soft_preferences=soft,
            avoided_values=avoided,
            profile_priors=tuple(_clean(tag, 60) for tag in profile_tags if _clean(tag, 60)),
            unanswered_attributes=unanswered,
            intent_epoch=self.intent_epoch,
            turn=int(turn),
            remaining_turns=max(int(remaining_turns), 0),
            candidate_stats=candidate_stats if candidate_stats is not None else self.last_candidate_stats,
        )


class SessionStore:
    """Small per-process store; reset always replaces an existing session."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    @property
    def sessions(self) -> dict[str, SessionState]:
        return self._sessions

    def reset(self, session_id: str, user_profile: Mapping[str, object] | None) -> SessionState:
        key = str(session_id).strip()
        if not key:
            raise ValueError("session_id must not be empty")
        profile = dict(user_profile) if isinstance(user_profile, Mapping) else {}
        # Copy list-like profile fields so callers cannot mutate session state
        # after reset by retaining a reference to their input object.
        if isinstance(profile.get("preference_tags"), (list, tuple)):
            profile["preference_tags"] = list(profile["preference_tags"])
        state = SessionState(session_id=key, profile=profile, session_profile={})
        state.last_state_fingerprint = state.fingerprint()
        self._sessions[key] = state
        return state

    def get(self, session_id: object) -> SessionState | None:
        return self._sessions.get(str(session_id).strip())

    def require(self, session_id: object) -> SessionState:
        state = self.get(session_id)
        if state is None:
            raise KeyError(f"session has not been reset: {session_id}")
        return state


class StateReducer:
    """The only component allowed to mutate active session constraints."""

    def apply(
        self,
        state: SessionState,
        update: IntentUpdate,
        *,
        turn: int = 0,
    ) -> SessionState:
        if update.global_override:
            state.intent_epoch += 1
            state.superseded_constraints.extend(state.active_constraints)
            for item in state.constraints:
                item.active = False
                item.status = "superseded"
            state.constraints.clear()
            # Category is an independent anchor.  Keep it, but do not carry
            # old slot evidence, old asked attributes, or old seen penalties.
            state.query_terms.clear()
            state.no_preference.clear()
            state.attribute_exhausted.clear()
            state.global_exhausted = False
            state.boundary_seen = False
            state.exhaustion_reason = None
            state.ask_counts.clear()
            state.asked_attributes.clear()
            state.previous_structured_pool_size = None
            state.previous_structured_pool_ids = ()
            state.previous_progress_fingerprint = None
            state.no_progress_streak = 0
            state.previous_ranked_ids.clear()
            state.softened_constraint_keys.clear()
            state.recommendations_by_epoch.setdefault(state.intent_epoch, [])
            state.last_candidate_ids.clear()

        if state.category_anchor is None and update.category_anchor:
            state.category_anchor = _clean(update.category_anchor, 120)

        for attribute in update.no_preference:
            normalized = _attribute_name(attribute)
            if normalized not in ALLOWED_ATTRIBUTES:
                continue
            state.no_preference.add(normalized)
            # Boundary is a one-time refusal for the currently asked slot,
            # not evidence that the slot is permanently exhausted.  In
            # particular, ``other`` should be available for a second pass to
            # collect the simulator's high-information constraint reply.
            if not (normalized == "other" and update.boundary_signal):
                state.attribute_exhausted.add(normalized)
            for item in state.constraints:
                if item.active and item.attribute == normalized and item.hardness != "hard":
                    item.active = False
                    item.status = "no_preference"

        if update.global_exhausted:
            state.global_exhausted = True
            state.exhaustion_reason = "global_no_more_preferences"
        if update.boundary_signal:
            state.boundary_seen = True
            if state.exhaustion_reason is None:
                state.exhaustion_reason = "boundary"
        for mutation in update.mutations:
            attribute = _attribute_name(mutation.attribute)
            value = _clean(mutation.value)
            if attribute not in ALLOWED_ATTRIBUTES or not value:
                continue
            state.no_preference.discard(attribute)
            state.attribute_exhausted.discard(attribute)
            state.softened_constraint_keys.discard(
                (attribute, normalize_constraint_value(attribute, value).lower())
            )
            if mutation.action in {"replace", "remove"}:
                for item in state.constraints:
                    if item.active and item.attribute == attribute:
                        item.active = False
                        item.status = "replaced" if mutation.action == "replace" else "removed"
            if mutation.action == "remove":
                continue
            duplicate = next(
                (
                    item
                    for item in state.constraints
                    if item.active
                    and item.attribute == attribute
                    and item.normalized_value() == normalize_constraint_value(attribute, value).lower()
                ),
                None,
            )
            if duplicate is not None:
                duplicate.confidence = max(float(duplicate.confidence), float(mutation.confidence))
                duplicate.turn = int(turn)
                continue
            state.constraints.append(
                Constraint(
                    attribute=attribute,
                    value=value,
                    polarity=mutation.polarity,
                    hardness=mutation.hardness,
                    source=mutation.source,
                    confidence=max(0.0, min(1.0, float(mutation.confidence))),
                    turn=int(turn),
                    epoch=state.intent_epoch,
                )
            )

        for term in update.query_terms:
            cleaned = _clean(term, 100)
            if cleaned and cleaned.lower() not in {item.lower() for item in state.query_terms}:
                state.query_terms.append(cleaned)
        if len(state.query_terms) > 40:
            del state.query_terms[:-40]
        state.last_state_fingerprint = state.fingerprint()
        return state

    # ``reduce`` is a readable alias used by callers that treat the reducer as
    # a pure pipeline stage, while ``apply`` remains the explicit mutator.
    reduce = apply


def _extract_category(message: str) -> str | None:
    patterns = (
        r"\blooking\s+for\s+(.+?)(?=[.!?]|,\s*(?:but|and|with)\b|$)",
        r"\bshopping\s+for\s+(.+?)(?=[.!?]|,\s*(?:but|and|with)\b|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.I)
        if match:
            category = _clean(match.group(1), 120)
            if category and not category.lower().startswith(("a key", "something", "what")):
                return category
    return None


def _extract_value_after_marker(message: str) -> str | None:
    match = re.search(
        r"\b(?:what\s+i\s+need\s+is|key\s+requirement\s+is|what\s+matters\s+is|i\s+need)\s*[:\-]?\s*(.+)",
        message,
        re.I,
    )
    if not match:
        return None
    value = match.group(1)
    value = re.split(r"[.!?]\s*", value, maxsplit=1)[0]
    return _clean(value, 320) or None


def _extract_no_preference(message: str) -> set[str]:
    found: set[str] = set()
    for match in _NO_PREFERENCE_RE.finditer(message):
        raw = next((group for group in match.groups() if group), "")
        if raw:
            # Capture only the attribute noun; trailing polite words should not
            # turn into a made-up slot.
            raw = re.split(r"\b(?:please|use|just|any)\b", raw, maxsplit=1, flags=re.I)[0]
            found.add(_attribute_name(raw))
        else:
            found.add("other")
    if re.search(r"\b(any|anything|whatever)\s+(?:is fine|works|goes)\b", message, re.I):
        found.add("other")
    return {item for item in found if item in ALLOWED_ATTRIBUTES}


def parse_intent_update(message: object, *, turn: int = 0) -> IntentUpdate:
    """Extract a bounded, deterministic update from one customer message."""

    text = _clean(message, 1200)
    if not text:
        return IntentUpdate()
    override = bool(_OVERRIDE_RE.search(text))
    no_preference = _extract_no_preference(text)
    global_exhausted = bool(_GLOBAL_EXHAUSTED_RE.search(text))
    if re.search(
        r"\b(?:don't|do not)\s+have\s+an?\s+additional\s+preference\s+(?:for|on|about)\s+other\b",
        text,
        re.I,
    ):
        global_exhausted = True
    if global_exhausted and re.search(
        r"\b(?:no|without)\s+(?:an?\s+)?(?:additional\s+)?preference\s+(?:for|on|about)\s+"
        r"(?:material|color|colour|size|style|brand|budget|feature|use|use_case|category)\b",
        text,
        re.I,
    ):
        global_exhausted = False
    boundary_signal = bool(_BOUNDARY_RE.search(text))
    reopen_clarification = bool(
        _RECOMMENDATION_REJECTION_RE.search(text)
        and _CLARIFICATION_REQUEST_RE.search(text)
    )
    mutations: list[ConstraintMutation] = []
    marker_value = _extract_value_after_marker(text)

    # Explicit marker values are the strongest user evidence.  On override,
    # this is the new intent and is intentionally the only preference carried
    # into the new epoch.  On a normal turn, semicolon separated simulator
    # replies produce one mutation per value.
    if marker_value:
        for value in _split_values(marker_value)[:4]:
            attribute = _attribute_from_text(value)
            hardness = "hard" if override or "requirement" in text.lower() else "soft"
            mutations.append(
                ConstraintMutation(
                    action="upsert",
                    attribute=attribute,
                    value=value,
                    hardness=hardness,
                    source="rule",
                    confidence=0.92 if override else 0.86,
                )
            )

    budget_match = _BUDGET_RE.search(text)
    if budget_match and not any(item.attribute == "budget" for item in mutations):
        mutations.append(
            ConstraintMutation(
                action="upsert",
                attribute="budget",
                value=_clean(budget_match.group(0), 80),
                hardness="hard",
                source="rule",
                confidence=0.95,
            )
        )

    # A direct "I prefer cotton" / "in black" phrase can be useful even when
    # the evaluator or a human paraphrase omits its marker.
    if not mutations and not no_preference:
        for pattern, attribute in (
            (r"\b(?:in|with|made\s+of)\s+(%s)\b" % "|".join(_COLORS), "color"),
            (r"\b(?:in|with|made\s+of)\s+(%s)\b" % "|".join(_MATERIALS), "material"),
        ):
            match = re.search(pattern, text, re.I)
            if match:
                mutations.append(
                    ConstraintMutation(
                        action="upsert",
                        attribute=attribute,
                        value=match.group(1).lower(),
                        hardness="soft",
                        source="rule",
                        confidence=0.82,
                    )
                )

    category = _extract_category(text) if not override else None
    query_terms: tuple[str, ...]
    if override:
        # Keep only the new marker text.  The reducer clears the accumulated
        # terms first; this prevents old preferences from contaminating FTS.
        query_terms = tuple(_clean(value, 100) for value in _split_values(marker_value or text)[:8])
    else:
        # Query evidence is bounded and excludes boilerplate.  The reducer
        # accumulates this across turns, while override starts a fresh list.
        query_text = marker_value or text
        query_terms = tuple(_clean(value, 100) for value in _split_values(query_text)[:8])

    # If the message is explicitly boundary/no-preference, do not also treat
    # its polite text as a positive feature constraint.
    if no_preference or reopen_clarification:
        query_terms = tuple()
    return IntentUpdate(
        global_override=override,
        mutations=tuple(mutations),
        category_anchor=category,
        no_preference=frozenset(no_preference),
        global_exhausted=global_exhausted,
        boundary_signal=boundary_signal,
        reopen_clarification=reopen_clarification,
        query_terms=query_terms,
        confidence=0.9 if mutations or category else 0.55,
    )


__all__ = [
    "ALLOWED_ATTRIBUTES",
    "Attribute",
    "CandidateStats",
    "Constraint",
    "ConstraintMutation",
    "IntentUpdate",
    "RuntimeContext",
    "SessionState",
    "SessionStore",
    "StateReducer",
    "TurnMessage",
    "normalize_constraint_value",
    "parse_intent_update",
]
