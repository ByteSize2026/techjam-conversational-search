"""Session state, deterministic intent updates, and state reduction.

The public evaluator gives an Agent no stable user identity and no hidden
intent card.  Consequently all mutable information in this module is scoped
to one ``session_id``.  Natural-language parsing is intentionally conservative:
it extracts high-confidence slots and keeps the original text as bounded query
evidence instead of inventing an exact constraint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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

IntentScope = Literal[
    "none",
    "attribute_replace",
    "referenced_preference_replace",
    "global_reset",
]

EvidenceKind = Literal["initial", "direct", "clarification", "override"]

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
_GLOBAL_RESET_RE = re.compile(
    r"\b(?:forget|ignore|disregard)\s+(?:everything|all(?:\s+(?:of\s+)?(?:this|that|my\s+preferences?|requirements?|intent))?)\b"
    r"|\bstart(?:\s+all)?\s+over\b"
    r"|\bcompletely\s+different\s+(?:request|thing|search|product)\b",
    re.I,
)
_ATTRIBUTE_REPLACEMENT_RE = re.compile(
    r"\b(?:change|switch|replace|update|make)\s+(?:my\s+)?(?:the\s+)?"
    r"(?P<attribute>category|material|color|colour|size|sizing|style|brand|"
    r"budget|price|feature|detail|use\s+case|use_case|usage)\s+"
    r"(?:to|with|as)\s+(?P<new>[^.!?;,]+)",
    re.I,
)
_VALUE_REPLACEMENT_RE = re.compile(
    r"\b(?:i(?:'|’)d?\s+)?(?:prefer|want|need|use)\s+(?P<new>[^.!?;,]+?)\s+"
    r"(?:instead\s+of|rather\s+than)\s+(?P<old>[^.!?;,]+)",
    re.I,
)
_BARE_VALUE_REPLACEMENT_RE = re.compile(
    r"\b(?P<new>(?:black|white|blue|red|pink|green|brown|gray|grey|purple|yellow|orange|"
    r"navy|beige|cotton|polyester|nylon|leather|wool|spandex|silk|rayon|linen|denim|"
    r"cashmere|hiking|running|gym|work|winter|outdoor|travel|walking|office|summer|rain))\b\s+"
    r"(?:instead\s+of|rather\s+than)\s+(?P<old>[^.!?;,]+)",
    re.I,
)
_REFERENCE_OVERRIDE_RE = re.compile(
    r"\b(?:actually\s*,?\s*)?(?:ignore|disregard|forget)\s+my\s+"
    r"(?:earlier|previous|old|prior)\s+(?:preference|request|choice|requirements?)\b"
    r"|\b(?:scratch\s+that|changed\s+my\s+mind|on\s+second\s+thought)\b",
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
class QueryEvidence:
    """Bounded lexical evidence with enough provenance for scoped updates.

    ``query_terms`` predates provenance and remains as a compatibility
    projection on :class:`SessionState`.  New reducer writes go through this
    object so an override can retire only the evidence it actually refers to.
    """

    text: str
    turn: int = 0
    epoch: int = 0
    kind: EvidenceKind = "direct"
    attribute_hint: str | None = None
    confidence: float = 1.0
    status: str = "active"

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "turn": self.turn,
            "epoch": self.epoch,
            "kind": self.kind,
            "attribute_hint": self.attribute_hint,
            "confidence": self.confidence,
            "status": self.status,
        }


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
    disclosure_kind: EvidenceKind = "direct"

    @property
    def kind(self) -> EvidenceKind:
        """Short alias used by provenance-oriented callers."""

        return self.disclosure_kind

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
            "disclosure_kind": self.disclosure_kind,
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
    disclosure_kind: EvidenceKind = "direct"


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
    scope: IntentScope = "none"
    query_evidence: tuple[QueryEvidence, ...] = ()

    @property
    def override(self) -> bool:
        return self.global_override or self.scope != "none"


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
    # ``query_terms`` remains a writable compatibility surface for existing
    # callers and tests.  Reducer-managed evidence is the source of truth when
    # ``query_evidence`` is non-empty.
    query_terms: list[str] = field(default_factory=list)
    query_evidence: list[QueryEvidence] = field(default_factory=list)
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

    @property
    def active_query_evidence(self) -> list[QueryEvidence]:
        return [
            item
            for item in self.query_evidence
            if item.status == "active"
        ]

    @property
    def active_query_terms(self) -> list[str]:
        """Return only active lexical evidence for downstream consumers.

        A few integrations still assign ``query_terms`` directly.  Falling
        back to that legacy list when no provenance objects exist keeps those
        integrations source-compatible without allowing stale terms to leak
        after the reducer has started tracking evidence.
        """

        if not self.query_evidence:
            return list(dict.fromkeys(
                _clean(term, 100)
                for term in self.query_terms
                if _clean(term, 100)
            ))
        return list(dict.fromkeys(
            _clean(item.text, 100)
            for item in self.active_query_evidence
            if _clean(item.text, 100)
        ))

    def _sync_query_projection(self) -> None:
        self.query_terms = self.active_query_terms[:40]

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
            # Superseded evidence is intentionally absent from fingerprints;
            # changing only retired text must not count as intent progress.
            "query_evidence": [
                item.as_dict() for item in self.active_query_evidence
            ],
            "query_terms": list(self.active_query_terms),
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

    @staticmethod
    def _constraint_key(constraint: Constraint) -> tuple[str, str]:
        return (
            str(constraint.attribute).lower(),
            normalize_constraint_value(
                constraint.attribute, constraint.value
            ).lower(),
        )

    @staticmethod
    def _mutation_key(mutation: ConstraintMutation) -> tuple[str, str] | None:
        attribute = _attribute_name(mutation.attribute)
        value = _clean(mutation.value)
        if attribute not in ALLOWED_ATTRIBUTES or not value:
            return None
        return (
            attribute,
            normalize_constraint_value(attribute, value).lower(),
        )

    @staticmethod
    def _valid_scope(update: IntentUpdate) -> IntentScope:
        scope = update.scope
        if not isinstance(scope, str) or scope not in {
            "none",
            "attribute_replace",
            "referenced_preference_replace",
            "global_reset",
        }:
            scope = "none"
        # Preserve the old manually-constructed ``IntentUpdate(global_override
        # =True)`` contract: without an explicit scope it means a full reset.
        if scope == "none" and update.global_override:
            return "global_reset"
        return scope

    @staticmethod
    def _reset_epoch_policy(state: SessionState) -> None:
        """Forget derived progress while retaining the category anchor."""

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
        state.last_candidate_ids.clear()
        state.recommendations_by_epoch.setdefault(state.intent_epoch, [])

    @staticmethod
    def _supersede_constraint(
        state: SessionState,
        item: Constraint,
        *,
        status: str,
        keys: list[tuple[str, str]],
    ) -> None:
        if not item.active or item.status != "active":
            return
        keys.append(StateReducer._constraint_key(item))
        # Keep an immutable-at-the-time-of-transition audit copy.  The active
        # list itself intentionally retains inactive entries for provenance.
        state.superseded_constraints.append(replace(item))
        item.active = False
        item.status = status

    @staticmethod
    def _supersede_query_evidence(
        item: QueryEvidence,
        *,
        status: str,
    ) -> None:
        if item.status == "active":
            item.status = status

    def apply(
        self,
        state: SessionState,
        update: IntentUpdate,
        *,
        turn: int = 0,
    ) -> SessionState:
        old_epoch = state.intent_epoch
        scope = self._valid_scope(update)
        superseded_keys: list[tuple[str, str]] = []
        retained_keys: list[tuple[str, str]] = []
        added_keys: list[tuple[str, str]] = []
        carried_evidence = 0
        active_before = list(state.active_constraints)
        evidence_before = list(state.active_query_evidence)
        mutations = [
            mutation
            for mutation in update.mutations
            if self._mutation_key(mutation) is not None
        ]
        mutation_keys = [self._mutation_key(mutation) for mutation in mutations]
        mutation_keys = [key for key in mutation_keys if key is not None]
        mutation_attributes = {attribute for attribute, _value in mutation_keys}
        mutation_values: dict[str, set[str]] = {}
        for attribute, value in mutation_keys:
            mutation_values.setdefault(attribute, set()).add(value)

        if scope in {
            "attribute_replace",
            "referenced_preference_replace",
            "global_reset",
        }:
            state.intent_epoch += 1

        if scope == "global_reset":
            # Global reset is deliberately opt-in.  Preserve the category
            # anchor, but retire all slot/query evidence and derived policy
            # state.  ``constraints`` is cleared for compatibility with the
            # original full-reset behavior; the audit copies remain available
            # in ``superseded_constraints``.
            for item in list(state.constraints):
                if item.active and item.status == "active":
                    self._supersede_constraint(
                        state, item, status="superseded", keys=superseded_keys
                    )
            state.constraints.clear()
            for item in state.query_evidence:
                self._supersede_query_evidence(item, status="superseded")
            # Older integrations may have populated only the legacy string
            # projection.  Clear it explicitly; otherwise active_query_terms
            # falls back to those stale values when no evidence objects exist.
            state.query_terms.clear()
            self._reset_epoch_policy(state)

        elif scope in {"attribute_replace", "referenced_preference_replace"}:
            # A scoped transition is an epoch boundary, but not a wholesale
            # state wipe.  Replace only conflicting slot values and retire the
            # provisional evidence explicitly referred to by the template.
            for item in active_before:
                key = self._constraint_key(item)
                attribute, value = key
                conflicts = (
                    attribute in mutation_attributes
                    and value not in mutation_values.get(attribute, set())
                )
                provisional = (
                    scope == "referenced_preference_replace"
                    and item.disclosure_kind == "initial"
                    and item.hardness != "hard"
                )
                if conflicts or provisional:
                    self._supersede_constraint(
                        state,
                        item,
                        status="superseded",
                        keys=superseded_keys,
                    )
                else:
                    # Carry-forward is explicit in provenance; no old epoch
                    # participates in rank/progress comparisons.
                    item.epoch = state.intent_epoch
                    retained_keys.append(key)

            for item in evidence_before:
                attribute = item.attribute_hint
                normalized_text = normalize_constraint_value(
                    attribute or "other", item.text
                ).lower()
                same_attribute_conflict = (
                    attribute in mutation_attributes
                    and normalized_text
                    not in mutation_values.get(str(attribute), set())
                )
                conflicts = (
                    scope in {
                        "attribute_replace",
                        "referenced_preference_replace",
                    }
                    and same_attribute_conflict
                )
                provisional = (
                    scope == "referenced_preference_replace"
                    and item.kind == "initial"
                )
                if conflicts or provisional:
                    self._supersede_query_evidence(item, status="superseded")
                else:
                    item.epoch = state.intent_epoch
                    carried_evidence += 1

            self._reset_epoch_policy(state)

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
        for mutation in mutations:
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
                        self._supersede_constraint(
                            state,
                            item,
                            status=(
                                "replaced"
                                if mutation.action == "replace"
                                else "removed"
                            ),
                            keys=superseded_keys,
                        )
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
                # A repeated value in an explicit override is still new
                # evidence.  Promote an older clarification-soft constraint
                # to the override's full semantics instead of leaving X soft
                # merely because it was disclosed once before.
                if mutation.disclosure_kind == "override" or scope != "none":
                    duplicate.polarity = mutation.polarity
                    duplicate.hardness = mutation.hardness
                    duplicate.source = mutation.source
                elif mutation.hardness == "hard":
                    duplicate.hardness = "hard"
                duplicate.confidence = max(float(duplicate.confidence), float(mutation.confidence))
                duplicate.turn = int(turn)
                duplicate.epoch = state.intent_epoch
                duplicate.disclosure_kind = mutation.disclosure_kind
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
                    disclosure_kind=mutation.disclosure_kind,
                )
            )

            added_keys.append(
                (attribute, normalize_constraint_value(attribute, value).lower())
            )

        # New code supplies explicit QueryEvidence objects.  Older callers
        # that only provide query_terms are represented as direct evidence so
        # the active projection remains deterministic and bounded.
        incoming_evidence = list(update.query_evidence)
        if not incoming_evidence:
            incoming_evidence = [
                QueryEvidence(
                    text=_clean(term, 100),
                    turn=int(turn),
                    epoch=state.intent_epoch,
                    kind=(
                        "override"
                        if scope != "none"
                        else "direct"
                    ),
                    attribute_hint=(
                        _attribute_from_text(_clean(term, 100))
                        if _clean(term, 100)
                        else None
                    ),
                    confidence=float(update.confidence),
                )
                for term in update.query_terms
                if _clean(term, 100)
            ]
        for incoming in incoming_evidence:
            cleaned = _clean(incoming.text, 100)
            if not cleaned:
                continue
            existing = next(
                (
                    item
                    for item in state.active_query_evidence
                    if item.text.lower() == cleaned.lower()
                ),
                None,
            )
            if existing is not None:
                if incoming.kind == "override":
                    # A matching retained term is fresh override evidence,
                    # not a reason to keep its old clarification provenance.
                    existing.turn = int(incoming.turn or turn)
                    existing.epoch = state.intent_epoch
                    existing.kind = "override"
                    existing.confidence = max(
                        float(existing.confidence), float(incoming.confidence)
                    )
                    if incoming.attribute_hint:
                        existing.attribute_hint = _attribute_name(
                            incoming.attribute_hint
                        )
                continue
            kind = incoming.kind if incoming.kind in {
                "initial",
                "direct",
                "clarification",
                "override",
            } else "direct"
            state.query_evidence.append(
                QueryEvidence(
                    text=cleaned,
                    turn=int(incoming.turn or turn),
                    epoch=state.intent_epoch,
                    kind=kind,
                    attribute_hint=(
                        _attribute_name(incoming.attribute_hint)
                        if incoming.attribute_hint
                        else None
                    ),
                    confidence=max(0.0, min(1.0, float(incoming.confidence))),
                    status="active",
                )
            )
        if len(state.query_evidence) > 80:
            del state.query_evidence[:-80]
        state._sync_query_projection()
        state.last_state_fingerprint = state.fingerprint()
        state.last_diagnostics = {
            "intent_scope": scope,
            "scope": scope,
            "epoch_changed": state.intent_epoch != old_epoch,
            "intent_epoch": state.intent_epoch,
            "added_constraint_keys": [list(key) for key in dict.fromkeys(added_keys)],
            "retained_constraint_keys": [
                list(key) for key in dict.fromkeys(retained_keys)
            ],
            "superseded_constraint_keys": [
                list(key) for key in dict.fromkeys(superseded_keys)
            ],
            "query_evidence_count": len(state.query_evidence),
            "active_query_evidence_count": len(state.active_query_evidence),
            "superseded_query_evidence_count": sum(
                item.status == "superseded" for item in state.query_evidence
            ),
            "query_evidence_carry_forward_count": carried_evidence,
        }
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


def _replacement_details(message: str) -> tuple[str, str] | None:
    """Extract an explicit ``attribute -> value`` replacement, if present."""

    match = _ATTRIBUTE_REPLACEMENT_RE.search(message)
    if match:
        attribute = _attribute_name(match.group("attribute"))
        value = _clean(match.group("new"), 320)
        # ``change the color to blue instead of red`` still names one new
        # value; the old value is only explanatory text.
        value = re.split(r"\b(?:instead\s+of|rather\s+than)\b", value, maxsplit=1, flags=re.I)[0]
        value = _clean(value, 320)
        return (attribute, value) if value else None
    match = _VALUE_REPLACEMENT_RE.search(message)
    if match:
        value = _clean(match.group("new"), 320)
        if value:
            return _attribute_from_text(value), value
    match = _BARE_VALUE_REPLACEMENT_RE.search(message)
    if match:
        value = _clean(match.group("new"), 320)
        if value:
            return _attribute_from_text(value), value
    return None


def _intent_scope(message: str) -> IntentScope:
    """Classify only explicit override language; ordinary openings are none."""

    if _GLOBAL_RESET_RE.search(message):
        return "global_reset"
    if (
        _ATTRIBUTE_REPLACEMENT_RE.search(message)
        or _VALUE_REPLACEMENT_RE.search(message)
        or _BARE_VALUE_REPLACEMENT_RE.search(message)
    ):
        return "attribute_replace"
    if _REFERENCE_OVERRIDE_RE.search(message) or _OVERRIDE_RE.search(message):
        return "referenced_preference_replace"
    return "none"


def _query_evidence_kind(
    text: str,
    *,
    turn: int,
    scope: IntentScope,
    marker_value: str | None,
    category: str | None,
) -> EvidenceKind:
    if scope != "none":
        return "override"
    lowered = text.lower()
    if turn <= 1 and category:
        return "initial"
    if marker_value and re.search(r"\b(?:for\s+that|what\s+matters\s+is)\b", lowered):
        return "clarification"
    if marker_value and "key requirement" in lowered:
        return "initial" if turn <= 1 else "direct"
    return "direct"


def parse_intent_update(message: object, *, turn: int = 0) -> IntentUpdate:
    """Extract a bounded, deterministic update from one customer message."""

    text = _clean(message, 1200)
    if not text:
        return IntentUpdate()
    scope = _intent_scope(text)
    override = scope != "none"
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
    category = _extract_category(text) if scope == "none" else None
    evidence_kind = _query_evidence_kind(
        text,
        turn=int(turn),
        scope=scope,
        marker_value=marker_value,
        category=category,
    )
    replacement = _replacement_details(text)

    # Explicit marker values are the strongest user evidence.  On an override
    # they become hard requirements in the new epoch.  On a normal turn,
    # semicolon-separated simulator replies produce one mutation per value.
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
                    disclosure_kind=evidence_kind,
                )
            )

    # Explicit replacement language may not use one of the simulator's
    # marker phrases (for example, ``change the color to blue``).
    if replacement and not marker_value:
        attribute, value = replacement
        mutations.append(
            ConstraintMutation(
                action="upsert",
                attribute=attribute,
                value=value,
                hardness="hard" if override else "soft",
                source="rule",
                confidence=0.92 if override else 0.88,
                disclosure_kind=("override" if override else "direct"),
            )
        )

    budget_match = _BUDGET_RE.search(text)
    if budget_match and not any(item.attribute == "budget" for item in mutations):
        initial_provisional = (
            int(turn) <= 1
            and category is not None
            and "requirement" not in text.lower()
        )
        mutations.append(
            ConstraintMutation(
                action="upsert",
                attribute="budget",
                value=_clean(budget_match.group(0), 80),
                hardness="soft" if initial_provisional else "hard",
                source="rule",
                confidence=0.95,
                disclosure_kind=("initial" if initial_provisional else evidence_kind),
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
                        disclosure_kind=evidence_kind,
                    )
                )

    query_terms: tuple[str, ...]
    if scope != "none":
        # The reducer starts a new epoch for every explicit override.  Only
        # the replacement payload enters the new lexical projection.
        replacement_text = marker_value or (replacement[1] if replacement else "")
        if replacement_text:
            query_terms = tuple(
                _clean(value, 100)
                for value in _split_values(replacement_text)[:8]
            )
        else:
            # ``Start over`` / ``Forget everything`` is a state transition,
            # not a lexical preference.  Do not turn its boilerplate into a
            # bogus feature query.
            query_terms = tuple()
    else:
        # Query evidence is bounded and accumulates across ordinary turns.
        query_text = marker_value or text
        query_terms = tuple(
            _clean(value, 100)
            for value in _split_values(query_text)[:8]
        )

    # If the message is explicitly boundary/no-preference, do not also treat
    # its polite text as a positive feature constraint.
    if no_preference or reopen_clarification:
        query_terms = tuple()
    query_evidence = tuple(
        QueryEvidence(
            text=value,
            turn=int(turn),
            epoch=0,
            kind=evidence_kind,
            attribute_hint=_attribute_from_text(value),
            confidence=0.92 if scope != "none" else 0.75,
        )
        for value in query_terms
        if value
    )
    return IntentUpdate(
        global_override=override,
        scope=scope,
        mutations=tuple(mutations),
        category_anchor=category,
        no_preference=frozenset(no_preference),
        global_exhausted=global_exhausted,
        boundary_signal=boundary_signal,
        reopen_clarification=reopen_clarification,
        query_terms=query_terms,
        query_evidence=query_evidence,
        confidence=0.9 if mutations or category else 0.55,
    )


__all__ = [
    "ALLOWED_ATTRIBUTES",
    "Attribute",
    "CandidateStats",
    "Constraint",
    "ConstraintMutation",
    "EvidenceKind",
    "IntentUpdate",
    "IntentScope",
    "QueryEvidence",
    "RuntimeContext",
    "SessionState",
    "SessionStore",
    "StateReducer",
    "TurnMessage",
    "normalize_constraint_value",
    "parse_intent_update",
]
