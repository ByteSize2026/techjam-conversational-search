"""Session state models, session storage, and deterministic state reduction.

The public evaluator gives an Agent no stable user identity and no hidden
intent card.  Consequently all mutable information in this module is scoped
to one ``session_id``.  Natural-language parsing lives in :mod:`.intent` and
is re-exported here to preserve the established public import path.
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


from .intent import parse_intent_update




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
