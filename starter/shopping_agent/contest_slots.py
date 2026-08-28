"""Per-session slots: additive constraints, override decay, observed tokens."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contest_text import COLORS, MATERIALS, classify_constraint, normalise, parse_price, terms

# Typed slots can conflict (black vs blue). Phrase/feature stays additive so
# the official simulator's same-target hard+soft AND is not dropped.
_TYPED_ATTRIBUTES = frozenset(
    {"material", "color", "size", "style", "brand", "budget", "use_case"}
)


def _kind(text: str) -> tuple[str, str | None]:
    lowered = normalise(text)
    if lowered.startswith("color:"):
        candidate = lowered.split(":", 1)[1].strip()
        if candidate in COLORS:
            return "color", "gray" if candidate == "grey" else candidate
    if lowered in MATERIALS:
        return "material", lowered
    if lowered in COLORS:
        return "color", "gray" if lowered == "grey" else lowered
    if "budget" in lowered:
        price = parse_price(lowered)
        if price is not None:
            return "budget", str(price)
    return "phrase", None


@dataclass
class Slot:
    text: str
    turn: int
    kind: str
    value: str | None
    attribute: str
    active: bool = True
    provisional: bool = False
    weight: float = 1.0

    @property
    def tokens(self) -> list[str]:
        return terms(self.text)


@dataclass
class ContestState:
    session_id: str
    profile: dict = field(default_factory=dict)
    category: str | None = None
    scenario: str = "browsing"
    turn: int = 0
    constraints: list[Slot] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    exhausted: set[str] = field(default_factory=set)
    pending: str | None = None
    override_applied: bool = False
    observed: set[str] = field(default_factory=set)
    intent_scope: str = "none"
    intent_epoch: int = 0
    last_superseded: list[str] = field(default_factory=list)
    intent_log: list[dict[str, object]] = field(default_factory=list)

    @property
    def active(self) -> list[Slot]:
        return [item for item in self.constraints if item.active]

    def add_constraints(
        self,
        values: list[str],
        *,
        turn: int,
        provisional: bool = False,
        event: str | None = None,
    ) -> None:
        seen = {normalise(item.text) for item in self.constraints}
        added: list[str] = []
        for raw in values:
            text = raw.strip()
            key = normalise(text)
            if not text or key in seen:
                continue
            seen.add(key)
            kind, value = _kind(text)
            self.constraints.append(
                Slot(
                    text=text,
                    turn=turn,
                    kind=kind,
                    value=value,
                    attribute=classify_constraint(text),
                    provisional=provisional,
                )
            )
            added.append(text)
        if added or event:
            self.intent_log.append(
                {
                    "turn": turn,
                    "kind": event or ("provisional" if provisional else "constraint"),
                    "texts": added,
                    "superseded": list(self.last_superseded),
                }
            )

    def decay_provisional(self, decay: float) -> None:
        for item in self.constraints:
            if item.provisional and item.active:
                item.weight = max(float(decay), 0.0)

    def supersede_typed_attributes(self, attributes: set[str]) -> list[str]:
        typed = {item for item in attributes if item in _TYPED_ATTRIBUTES}
        dropped: list[str] = []
        if not typed:
            return dropped
        for item in self.constraints:
            if item.active and item.attribute in typed:
                item.active = False
                dropped.append(item.text)
        self.last_superseded = dropped
        return dropped

    def apply_override(
        self,
        values: list[str],
        *,
        turn: int,
        scope: str,
        decay: float,
    ) -> None:
        """Apply a scoped override without classmate-style full wipes.

        Official evaluator template is ``referenced_preference_replace``:
        decay opening slots and AND the new value. ``attribute_replace``
        drops older typed slots of the same attribute. ``global_reset``
        clears constraints but keeps the category lock.
        """

        resolved = scope if scope in {
            "attribute_replace",
            "referenced_preference_replace",
            "global_reset",
        } else "referenced_preference_replace"
        self.override_applied = True
        self.intent_scope = resolved
        self.intent_epoch += 1
        self.last_superseded = []
        if resolved == "global_reset":
            self.constraints.clear()
            self.observed.clear()
            self.add_constraints(values, turn=turn, event="global_reset")
            return
        self.decay_provisional(decay)
        if resolved == "attribute_replace":
            attributes = {classify_constraint(item) for item in values if str(item).strip()}
            self.supersede_typed_attributes(attributes)
        self.add_constraints(values, turn=turn, event=resolved)

    def mark_exhausted(self, attribute: str | None) -> None:
        if attribute:
            self.exhausted.add(attribute)

    def intent_snippets(self, *, limit: int = 3) -> list[str]:
        """Short remembered constraints for questions and diagnostics."""

        snippets: list[str] = []
        seen: set[str] = set()

        def add(raw: object) -> None:
            text = " ".join(str(raw or "").split()).strip().rstrip(".;")
            if len(text) > 56:
                text = text[:53] + "..."
            key = text.lower()
            if not text or key in seen:
                return
            seen.add(key)
            snippets.append(text)

        if self.category:
            add(self.category)
        for item in self.active:
            if len(snippets) >= limit + 1:
                break
            add(item.text)
        return snippets

    def missing_typed_attributes(self) -> list[str]:
        have = {item.attribute for item in self.active}
        order = (
            "material",
            "color",
            "size",
            "style",
            "budget",
            "use_case",
            "brand",
            "feature",
        )
        return [name for name in order if name not in have]

    def next_ask_focus(self) -> str:
        missing = self.missing_typed_attributes()
        return missing[0] if missing else "feature"

    def observe(self, message: str, chrome: set[str]) -> None:
        for token in terms(message):
            if token not in chrome:
                self.observed.add(token)

    def profile_tags(self) -> list[str]:
        tags = self.profile.get("preference_tags") if isinstance(self.profile, dict) else None
        if isinstance(tags, str):
            return [tags.lower()]
        if not isinstance(tags, (list, tuple, set)):
            return []
        return [str(tag).lower() for tag in tags if str(tag).strip()]

    def query_tokens(self) -> tuple[list[str], dict[str, float]]:
        tokens: list[str] = []
        weights: dict[str, float] = {}
        if self.category:
            for token in terms(self.category):
                tokens.append(token)
                weights[token] = max(weights.get(token, 0.0), 1.0)
        for item in self.active:
            for token in item.tokens:
                tokens.append(token)
                weights[token] = max(weights.get(token, 0.0), 2.2 * item.weight)
        for token in self.observed:
            tokens.append(token)
            weights[token] = max(weights.get(token, 0.0), 0.45)
        return tokens, weights
