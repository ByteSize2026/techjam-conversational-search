"""Per-session slots: additive constraints, override decay, observed tokens."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contest_text import COLORS, MATERIALS, classify_constraint, normalise, parse_price, terms


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

    @property
    def active(self) -> list[Slot]:
        return [item for item in self.constraints if item.active]

    def add_constraints(self, values: list[str], *, turn: int, provisional: bool = False) -> None:
        seen = {normalise(item.text) for item in self.constraints}
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

    def decay_provisional(self, decay: float) -> None:
        for item in self.constraints:
            if item.provisional and item.active:
                item.weight = max(float(decay), 0.0)

    def mark_exhausted(self, attribute: str | None) -> None:
        if attribute:
            self.exhausted.add(attribute)

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
