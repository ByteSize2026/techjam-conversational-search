"""Deterministic parsing of one customer message into an intent update."""

from __future__ import annotations

import re

from .state import (
    ALLOWED_ATTRIBUTES,
    ConstraintMutation,
    EvidenceKind,
    IntentScope,
    IntentUpdate,
    QueryEvidence,
    _BUDGET_RE,
    _COLORS,
    _MATERIALS,
    _attribute_from_text,
    _attribute_name,
    _clean,
    _split_values,
)

_OVERRIDE_RE = re.compile(
    r"\b(?:actually|instead|rather)\b[^.?!;]*\b(?:ignore|disregard|forget|different|change)\b"
    r"|\b(?:ignore|disregard|forget)\b[^.?!;]*(?:earlier|previous|above|before|preference)", re.I,
)
_GLOBAL_RESET_RE = re.compile(
    r"\b(?:forget|ignore|disregard)\s+(?:everything|all(?:\s+(?:of\s+)?(?:this|that|my\s+preferences?|requirements?|intent))?)\b"
    r"|\bstart(?:\s+all)?\s+over\b|\bcompletely\s+different\s+(?:request|thing|search|product)\b", re.I,
)
_ATTRIBUTE_REPLACEMENT_RE = re.compile(
    r"\b(?:change|switch|replace|update|make)\s+(?:my\s+)?(?:the\s+)?"
    r"(?P<attribute>category|material|color|colour|size|sizing|style|brand|budget|price|feature|detail|use\s+case|use_case|usage)\s+"
    r"(?:to|with|as)\s+(?P<new>[^.!?;,]+)", re.I,
)
_VALUE_REPLACEMENT_RE = re.compile(
    r"\b(?:i(?:'|’)d?\s+)?(?:prefer|want|need|use)\s+(?P<new>[^.!?;,]+?)\s+(?:instead\s+of|rather\s+than)\s+(?P<old>[^.!?;,]+)", re.I,
)
_BARE_VALUE_REPLACEMENT_RE = re.compile(
    r"\b(?P<new>(?:black|white|blue|red|pink|green|brown|gray|grey|purple|yellow|orange|navy|beige|cotton|polyester|nylon|leather|wool|spandex|silk|rayon|linen|denim|cashmere|hiking|running|gym|work|winter|outdoor|travel|walking|office|summer|rain))\b\s+(?:instead\s+of|rather\s+than)\s+(?P<old>[^.!?;,]+)", re.I,
)
_REFERENCE_OVERRIDE_RE = re.compile(
    r"\b(?:actually\s*,?\s*)?(?:ignore|disregard|forget)\s+my\s+(?:earlier|previous|old|prior)\s+(?:preference|request|choice|requirements?)\b"
    r"|\b(?:scratch\s+that|changed\s+my\s+mind|on\s+second\s+thought)\b", re.I,
)
_NO_PREFERENCE_RE = re.compile(
    r"(?:no|without)\s+(?:an?\s+)?(?:additional\s+)?preference\s*(?:for|on|about)?\s*([a-z][a-z_ -]{1,24})?"
    r"|\b(?:don't|do not|doesn't|does not)\s+(?:have\s+(?:an?\s+)?(?:additional\s+)?preference|care|matter)\s*(?:about|for)?\s*([a-z][a-z_ -]{1,24})?", re.I,
)
_BOUNDARY_RE = re.compile(
    r"\b(?:use\s+your\s+judg(?:e|)ment|you\s+decide|your\s+choice|anything\s+is\s+fine|it\s+does(?:n't|\s+not)\s+matter)\b", re.I,
)
_GLOBAL_EXHAUSTED_RE = re.compile(
    r"\b(?:no\s+(?:additional|other|more)\s+preferences?|nothing\s+else|no\s+more\s+(?:preferences?|requirements?)|that(?:'s|\s+is)\s+all|i\s+have\s+no\s+other\s+preferences?)\b", re.I,
)
_RECOMMENDATION_REJECTION_RE = re.compile(
    r"\b(?:those|these|the)\s+(?:options?|recommendations?|choices?|items?)\s+(?:are|seem|look)\s+(?:not\s+(?:quite\s+)?(?:right|suitable|what\s+i\s+want)|wrong|off)\b"
    r"|\b(?:options?|recommendations?|choices?|items?)\s+(?:aren't|are\s+not|don't\s+work|do\s+not\s+work|weren't|were\s+not)\b|\bnot\s+(?:quite\s+)?(?:right|what\s+i\s+want)\b", re.I,
)
_CLARIFICATION_REQUEST_RE = re.compile(
    r"\b(?:ask|question)\s+me\s+(?:about|on)\b|\b(?:keep|continue)\s+(?:asking|questioning)\b"
    r"|\b(?:ask|question)\s+(?:a|one|another)\s+(?:specific\s+)?(?:question|attribute|preference|requirement)\b", re.I,
)

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



__all__ = ["parse_intent_update"]
