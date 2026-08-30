"""Deterministic parsing of one customer message into an intent update."""

from __future__ import annotations

import re
from dataclasses import replace
from collections.abc import Sequence

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
    normalize_constraint_value,
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
_PREFERENCE_MARKER_RE = re.compile(
    r"\b(?:please\s+)?(?:prioritize|look\s+for|prefer|want|need|use)\s+",
    re.I,
)
_NO_PREFERENCE_RE = re.compile(
    r"(?:no|without)\s+(?:an?\s+)?(?:additional\s+)?preferences?"
    r"(?:\s+(?:for|on|about)\s+([a-z][a-z_ -]{1,24}))?"
    r"|\b(?:don't|do not|doesn't|does not)\s+"
    r"(?:have\s+(?:an?\s+)?(?:additional\s+)?preferences?|care|matter)"
    r"(?:\s+(?:about|for)\s+([a-z][a-z_ -]{1,24}))?", re.I,
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

# Natural-language additions deliberately use a small, catalog-backed
# vocabulary.  Anything outside these lists remains lexical evidence instead
# of becoming an unverified hard filter.
_KNOWN_VALUE_WORDS = tuple(dict.fromkeys((*_COLORS, *_MATERIALS)))
_KNOWN_VALUE_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(value) for value in _KNOWN_VALUE_WORDS),
    re.I,
)
_NEGATED_VALUE_RE = re.compile(
    r"\b(?:not|no|without|except|excluding|other\s+than)\s+(?P<value>%s)\b"
    r"|\banything\s+but\s+(?P<except>%s)\b"
    r"|\b(?:do\s+not|don't|does\s+not|doesn't)\s+(?:want|like|need|prefer)\s+(?P<dont>%s)\b"
    % (
        "|".join(re.escape(value) for value in _KNOWN_VALUE_WORDS),
        "|".join(re.escape(value) for value in _KNOWN_VALUE_WORDS),
        "|".join(re.escape(value) for value in _KNOWN_VALUE_WORDS),
    ),
    re.I,
)
_REMOVAL_RE = re.compile(
    r"\b(?P<attribute>category|material|color|colour|size|sizing|style|brand|budget|price|feature|detail|use\s+case|use_case|usage)\b"
    r"\s+(?:preference\s+)?(?:no\s+longer\s+matters?|doesn['’]?t\s+matter|does\s+not\s+matter|is\s+no\s+longer\s+important|isn['’]?t\s+important|is\s+irrelevant)\b"
    r"|\b(?:remove|drop|clear|forget)\s+(?:my\s+)?(?P<remove_attribute>category|material|color|colour|size|sizing|style|brand|budget|price|feature|detail|use\s+case|use_case|usage)\s*(?:preference|requirement)?\b",
    re.I,
)
_NEGATED_GENERIC_RE = re.compile(
    r"\b(?:not|without|except|excluding|other\s+than|anything\s+but)\s+(?P<value>[a-z][a-z0-9 -]{1,64})",
    re.I,
)
_HARD_MARKER_RE = re.compile(
    r"\b(?:must|need(?:s)?|require(?:s|d)?|essential(?:ly)?|mandatory|key\s+requirement)\b",
    re.I,
)
_SOFT_MARKER_RE = re.compile(
    r"\b(?:prefer(?:ably)?|ideally|would\s+like|if\s+possible|nice\s+to\s+have|hope\s+for)\b",
    re.I,
)
_USE_CASE_VALUE_RE = re.compile(
    r"\b(?:suitable|suited|designed|intended|good|great)\s+for\s+(?P<value>[a-z][a-z -]{1,48})"
    r"|\bfor\s+(?P<bare>hiking|running|gym|work|winter|outdoor|travel|walking|office|summer|rain)\b",
    re.I,
)
_SIZE_VALUE_RE = re.compile(
    r"\b(?:size|sizing)\s*(?:is|of|:)?\s*(?P<value>xxs|xs|s|m|l|xl|xxl|small|medium|large)\b"
    r"|\b(?P<bare>xxs|xs|xl|xxl)\b",
    re.I,
)
_FEATURE_VALUE_RE = re.compile(
    r"\b(?:preferably|ideally|must\s+be|need(?:s)?|require(?:s|d)?|with)\s+(?P<value>[a-z][a-z0-9 -]{1,48}?)(?=\s*(?:,|;|\.|!|\?|$|\band\b))",
    re.I,
)
_RATING_VALUE_RE = re.compile(
    r"\b(?:(?:at\s+least|minimum(?:\s+of)?|rated?|rating(?:\s+of)?|with\s+a\s+rating\s+of)\s*)?"
    r"(?P<value>[0-5](?:\.\d+)?)\s*(?:out\s+of\s+5|stars?)\b"
    r"(?:\s*(?:or\s+higher|and\s+above|minimum|at\s+least))?",
    re.I,
)
_RATING_COUNT_VALUE_RE = re.compile(
    r"\b(?P<value>\d[\d,]*)\s*(?:\+|or\s+(?:more|above))?\s*"
    r"(?:reviews?|ratings?|review\s+count|number\s+of\s+reviews?)\b",
    re.I,
)
_TITLE_TOKEN_RE = re.compile(
    r"\b(?:remember\s+seeing|saw|seen|mentions?|mentioned)\s+"
    r"(?P<value>[a-z0-9][a-z0-9_'’-]{1,48})\s+in\s+(?:the\s+)?"
    r"(?:product\s+)?(?:name|title)\b"
    r"|\b(?:product\s+name|title|listing)\s+(?:contains?|mentions?|has)\s+"
    r"(?P<contains>[a-z0-9][a-z0-9_'’-]{1,48})\b",
    re.I,
)

_CANONICAL_SLOT_LINE_RE = re.compile(
    r"^(?P<prefix>For\s+that,\s+what\s+matters\s+is:|A\s+key\s+requirement\s+is:|I\s+do\s+not\s+want)\s*"
    r"(?P<attribute>category|brand|budget|rating|rating_count|color|material|size|style|feature|use\s+case|use_case)\s*:\s*(?P<value>[^;]+?)\.?$",
    re.I,
)


def _canonical_slots(message: str) -> list[tuple[str, str, bool, str]]:
    """Extract the small field set emitted by the canonicalizer prompt.

    This is intentionally line-anchored so ordinary customer prose continues
    through the existing natural-language parser unchanged.  ``rating`` and
    ``rating_count`` are represented as feature evidence because they are not
    part of the public Agent attribute contract.
    """

    slots: list[tuple[str, str, bool, str]] = []
    for line in message.splitlines():
        match = _CANONICAL_SLOT_LINE_RE.fullmatch(line.strip())
        if match is None:
            continue
        attribute = match.group("attribute").lower().replace(" ", "_")
        value = _clean(match.group("value"), 320)
        if not value:
            continue
        prefix = match.group("prefix").lower()
        negative = prefix.startswith("i do not want")
        hard = negative or prefix.startswith("a key requirement")
        slots.append((attribute, value, hard, "avoid" if negative else "prefer"))
    return slots[:8]

def _extract_category(message: str) -> str | None:
    patterns = (
        r"\bin\s+(?:the\s+)?(.+?)\s+category\b",
        r"\bcategory\s*:\s*(.+?)(?=[.!?]|,|$)",
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


def _special_value_mutations(
    message: str,
    *,
    turn: int,
    scope: IntentScope,
    evidence_kind: EvidenceKind,
) -> tuple[list[ConstraintMutation], list[tuple[int, int]]]:
    """Parse numeric and title-name facts into unambiguous feature syntax.

    These values remain under the public ``feature`` slot for compatibility,
    but their prefixes are interpreted by the structured pool and ranker:
    ``rating:``, ``rating_count:``, and ``title contains:``.
    """

    mutations: list[ConstraintMutation] = []
    spans: list[tuple[int, int]] = []
    seen: set[tuple[str, str]] = set()

    def add(value: str, start: int, *, confidence: float = 0.94) -> None:
        normalized = _clean(value, 120)
        if not normalized:
            return
        key = ("feature", normalize_constraint_value("feature", normalized).lower())
        if key in seen:
            return
        seen.add(key)
        mutations.append(
            ConstraintMutation(
                action="upsert",
                attribute="feature",
                value=normalized,
                polarity="require" if scope != "none" else "prefer",
                hardness="hard" if scope != "none" else "soft",
                source="rule",
                confidence=confidence,
                disclosure_kind=("override" if scope != "none" else evidence_kind),
            )
        )

    for match in _RATING_VALUE_RE.finditer(message):
        raw = match.group("value")
        prefix = message[max(0, match.start() - 32) : match.start()]
        suffix = message[match.end() : match.end() + 24]
        hard = bool(
            re.search(r"\b(?:at\s+least|minimum|rated?|require|must)\b", prefix, re.I)
            or re.search(r"\b(?:or\s+higher|and\s+above)\b", suffix, re.I)
        )
        value = f"rating: {raw} stars or higher"
        add(value, match.start(), confidence=0.96 if hard else 0.90)
        if mutations:
            mutations[-1] = replace(
                mutations[-1],
                polarity="require" if hard or scope != "none" else "prefer",
                hardness="hard" if hard or scope != "none" else "soft",
            )
        spans.append(match.span())

    for match in _RATING_COUNT_VALUE_RE.finditer(message):
        raw = match.group("value").replace(",", "")
        add(f"rating_count: {raw} or more", match.start(), confidence=0.94)
        spans.append(match.span())

    for match in _TITLE_TOKEN_RE.finditer(message):
        raw = match.group("value") or match.group("contains") or ""
        add(f"title contains: {_clean(raw, 80)}", match.start(), confidence=0.92)
        spans.append(match.span())

    return mutations, spans


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
    value = _clean(value, 320)
    if re.match(r"^(?:something|an?\s+item)\s+in\s+.+\bcategory\b$", value, re.I):
        return None
    return value or None


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


def _hardness_and_polarity(
    message: str,
    start: int = 0,
    *,
    default_hardness: str = "soft",
) -> tuple[str, str]:
    """Infer only the small, explicit strength vocabulary we can defend."""

    prefix = message[max(0, int(start) - 72) : int(start)]
    if _HARD_MARKER_RE.search(prefix):
        return "hard", "require"
    if _SOFT_MARKER_RE.search(prefix):
        return "soft", "prefer"
    return default_hardness, "prefer"


def _known_value_mutations(
    message: str,
    *,
    excluded_spans: set[tuple[int, int]] | None = None,
    asked_attribute: str | None = None,
    disclosure_kind: EvidenceKind = "direct",
) -> tuple[list[ConstraintMutation], list[tuple[int, int]]]:
    """Extract explicit color/material/use-case/size values conservatively.

    The return value includes only spans of explicitly negated clauses so
    query evidence can omit excluded values without deleting positive facts.
    ``asked_attribute`` lets a bare answer such as ``blue`` inherit the slot
    that the policy asked about.
    """

    excluded = excluded_spans or set()
    mutations: list[ConstraintMutation] = []
    spans: list[tuple[int, int]] = []

    def add(
        attribute: str,
        value: str,
        start: int,
        *,
        polarity: str = "prefer",
        hardness: str | None = None,
        confidence: float = 0.88,
    ) -> None:
        cleaned = _clean(value, 120)
        if not cleaned:
            return
        attr = _attribute_name(attribute)
        if attr not in ALLOWED_ATTRIBUTES:
            return
        inferred_hardness, inferred_polarity = _hardness_and_polarity(
            message, start, default_hardness="soft"
        )
        final_hardness = hardness or inferred_hardness
        final_polarity = polarity if polarity != "prefer" else inferred_polarity
        # Avoid duplicate values when a phrase is matched both by the generic
        # vocabulary and an explicit use-case/size pattern.
        key = (
            attr,
            normalize_constraint_value(attr, cleaned).lower(),
            final_polarity,
        )
        if any(
            (
                item.attribute,
                _clean(item.value, 120).lower(),
                item.polarity,
            )
            == key
            for item in mutations
        ):
            return
        mutations.append(
            ConstraintMutation(
                action="upsert",
                attribute=attr,
                value=cleaned,
                polarity=final_polarity,  # type: ignore[arg-type]
                hardness=final_hardness,  # type: ignore[arg-type]
                source="rule",
                confidence=confidence,
                disclosure_kind=disclosure_kind,
            )
        )

    # Explicit negation is parsed first, and its value is never allowed to
    # reappear as positive query evidence.
    negated_values: set[str] = set()
    for match in _NEGATED_VALUE_RE.finditer(message):
        value = next((group for group in match.groups() if group), "")
        if not value or (match.start(), match.end()) in excluded:
            continue
        value = _clean(value, 120)
        negated_values.add(value.lower())
        attr = _attribute_from_text(value)
        add(
            attr,
            value,
            match.start("value")
            if match.group("value") is not None
            else match.start(),
            polarity="avoid",
            hardness="hard",
            confidence=0.95,
        )
        spans.append(match.span())

    # Known vocabulary values become soft preferences by default.  A bare
    # answer is bound to the slot just asked by the policy when one exists.
    for match in _KNOWN_VALUE_RE.finditer(message):
        value = _clean(match.group(0), 120)
        if not value or value.lower() in negated_values:
            continue
        if any(match.start() >= left and match.end() <= right for left, right in excluded):
            continue
        prefix = message[max(0, match.start() - 72) : match.start()]
        if re.search(
            r"(?:\bnot|\bno|\bwithout|\bexcept|\bexcluding|\bother\s+than|\bbut)\s*$",
            prefix,
            re.I,
        ):
            continue
        attribute = _attribute_from_text(value)
        normalized_asked_attribute = (
            _attribute_name(asked_attribute) if asked_attribute else None
        )
        if normalized_asked_attribute and normalized_asked_attribute != "other" and not re.search(
            r"\b(?:color|colour|material|fabric|made\s+from|size|style|brand|budget|feature|use|usage)\b",
            prefix,
            re.I,
        ) and len(_split_values(message)) <= 2:
            attribute = normalized_asked_attribute
        add(attribute, value.lower(), match.start())

    # Use-case expressions carry more context than the generic vocabulary.
    for match in _USE_CASE_VALUE_RE.finditer(message):
        value = match.group("value") or match.group("bare") or ""
        value = _clean(value, 80)
        if not value:
            continue
        # The value group can include polite trailing words.  Keep a known
        # use-case token where possible and otherwise retain only one phrase.
        candidate = next(
            (
                item
                for item in (
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
                if re.search(rf"\b{re.escape(item)}\b", value, re.I)
            ),
            value,
        )
        add("use_case", candidate, match.start(), confidence=0.91)

    for match in _SIZE_VALUE_RE.finditer(message):
        value = match.group("value") or match.group("bare") or ""
        if value:
            add("size", value.upper(), match.start(), confidence=0.92)

    # Explicit feature adjectives are useful for paraphrases not covered by
    # the catalog vocabulary.  They remain soft unless a hard marker appears.
    for match in _FEATURE_VALUE_RE.finditer(message):
        value = _clean(match.group("value"), 80)
        if not value:
            continue
        # Do not turn ``with black``/``with leather`` into a duplicate generic
        # feature; known colors/materials already have typed attributes.
        if value.lower() in {item.lower() for item in _KNOWN_VALUE_WORDS}:
            continue
        if value.lower() in {"something", "an item", "a product", "the best"}:
            continue
        if re.search(r"\bcategory\b", value, re.I) or re.match(
            r"^(?:something|an?\s+item)\s+in\s+", value, re.I
        ):
            continue
        if re.match(
            r"^(?:shoes?|shirts?|dresses?|jackets?|bags?|items?|products?)\s+"
            r"(?:that|which|with)\s+",
            value,
            re.I,
        ):
            continue
        if re.fullmatch(
            r"xxs|xs|s|m|l|xl|xxl|small|medium|large",
            value,
            re.I,
        ):
            continue
        add("feature", value, match.start("value"), confidence=0.80)

    return mutations, spans


def _removal_attributes(message: str) -> set[str]:
    found: set[str] = set()
    for match in _REMOVAL_RE.finditer(message):
        raw = match.group("attribute") or match.group("remove_attribute") or ""
        attribute = _attribute_name(raw)
        if attribute in ALLOWED_ATTRIBUTES:
            found.add(attribute)
    return found


def _positive_query_text(message: str, spans: Sequence[tuple[int, int]]) -> str:
    """Remove explicit negative clauses from lexical evidence."""

    if not spans:
        return message
    chars = list(message)
    for start, end in spans:
        for index in range(max(0, start), min(len(chars), end)):
            chars[index] = " "
    return "".join(chars)


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


def _last_preference_payload(message: str) -> str | None:
    """Return the value from the final explicit preference clause.

    In messages such as ``ignore the earlier X; prioritize Y instead``, X is
    negative state-transition context rather than an active search fact.  The
    last explicit preference clause owns the replacement payload because
    benchmark/user prose can contain a nested ``I need ... look for Y``.
    """

    matches = [
        (clause, match)
        for clause in re.split(r"(?<!\d)\.|\.(?!\d)|[!?;]", message)
        for match in _PREFERENCE_MARKER_RE.finditer(clause)
    ]
    if not matches:
        return None
    clause, match = matches[-1]
    value = _clean(clause[match.end() :], 320)
    # ``What I need is: leather`` is handled by the stronger marker parser;
    # normalizing the helper too keeps catalog grounding and override payload
    # selection aligned on ``leather`` rather than ``is: leather``.
    value = re.sub(r"^is\s*:\s*", "", value, flags=re.I)
    value = re.sub(
        r"\s+(?:instead|now|from\s+now\s+on)\s*$", "", value, flags=re.I
    )
    return _clean(value, 320) or None


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


def parse_intent_update(
    message: object,
    *,
    turn: int = 0,
    asked_attribute: str | None = None,
) -> IntentUpdate:
    """Extract a bounded, deterministic update from one customer message.

    The parser intentionally emits only values that are visible in the user
    message (or a small known vocabulary).  Ambiguous prose is retained as
    query evidence and can be offered to the optional model adapter later.
    """

    raw_text = str(message or "")[:1200]
    text = _clean(raw_text, 1200)
    canonical_slots = _canonical_slots(raw_text)
    if not text:
        return IntentUpdate()
    scope = _intent_scope(text)
    override = scope != "none"
    preference_payload = _last_preference_payload(text)
    referenced_payload = (
        preference_payload
        if scope == "referenced_preference_replace"
        else None
    )
    # For a referenced override, parse product facts only from the new
    # affirmative payload.  The old value remains visible to scope detection
    # above, but must not re-enter state as a budget, brand, or feature.
    fact_text = referenced_payload or text
    no_preference = _extract_no_preference(text)
    no_preference.update(_removal_attributes(text))
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
    # Canonical slot lines bypass the legacy marker payload parser.  Their
    # field labels are protocol syntax, not part of the values themselves.
    marker_value = (
        None
        if canonical_slots
        else (referenced_payload or _extract_value_after_marker(text))
    )
    category = (
        next(
            (
                value
                for attribute, value, _hard, polarity in canonical_slots
                if attribute == "category" and polarity != "avoid"
            ),
            None,
        )
        if scope == "none" and canonical_slots
        else (_extract_category(text) if scope == "none" else None)
    )
    evidence_kind = _query_evidence_kind(
        text,
        turn=int(turn),
        scope=scope,
        marker_value=marker_value,
        category=category,
    )
    replacement = _replacement_details(text)

    special_mutations: list[ConstraintMutation] = []
    special_spans: list[tuple[int, int]] = []
    if not canonical_slots:
        special_mutations, special_spans = _special_value_mutations(
            fact_text,
            turn=int(turn),
            scope=scope,
            evidence_kind=evidence_kind,
        )
        mutations.extend(special_mutations)

    # A value inside an explicitly named category belongs to the category
    # anchor.  Do not duplicate one of its words as a material/color filter
    # (for example ``Leather & Faux Leather`` -> material=leather), because
    # that can incorrectly remove valid members of the resolved category.
    category_spans: set[tuple[int, int]] = set()
    if category:
        category_match = re.search(re.escape(category), text, re.I)
        if category_match is not None:
            category_spans.add(category_match.span())

    for slot_attribute, slot_value, slot_hard, slot_polarity in canonical_slots:
        attribute = (
            "feature"
            if slot_attribute in {"rating", "rating_count"}
            else slot_attribute
        )
        value = (
            f"{slot_attribute}: {slot_value}"
            if slot_attribute in {"rating", "rating_count"}
            else slot_value
        )
        mutations.append(
            ConstraintMutation(
                action="upsert",
                attribute=attribute,
                value=value,
                polarity=(
                    "avoid"
                    if slot_polarity == "avoid"
                    else ("require" if slot_hard else "prefer")
                ),
                hardness="hard" if slot_hard or override else "soft",
                source="rule",
                confidence=0.92 if slot_hard or override else 0.86,
                disclosure_kind=("override" if override else evidence_kind),
            )
        )

    # Explicit marker values are the strongest user evidence.  On an override
    # they become hard requirements in the new epoch.  On a normal turn,
    # semicolon-separated simulator replies produce one mutation per value.
    negated_values = {
        _clean(next((group for group in match.groups() if group), ""), 120).lower()
        for match in _NEGATED_VALUE_RE.finditer(fact_text)
    }
    if marker_value and not special_mutations:
        for value in _split_values(marker_value)[:4]:
            if value.lower() in negated_values:
                continue
            attribute = _attribute_from_text(value)
            hardness = "hard" if override or "requirement" in text.lower() else "soft"
            # ``what I need is`` is a hard/decisive disclosure, but remains a
            # preference polarity for compatibility with the existing state
            # contract.  ``must``/``required`` are the explicit ``require``
            # polarity markers; hardness still captures the strength of
            # ordinary ``need`` language.
            polarity = (
                "require"
                if re.search(
                    r"\b(?:must|mandatory|required?|essential(?:ly)?|key\s+requirement)\b",
                    text,
                    re.I,
                )
                else "prefer"
            )
            mutations.append(
                ConstraintMutation(
                    action="upsert",
                    attribute=attribute,
                    value=value,
                    polarity=polarity,  # type: ignore[arg-type]
                    hardness=hardness,  # type: ignore[arg-type]
                    source="rule",
                    confidence=0.92 if override else 0.86,
                    disclosure_kind=evidence_kind,
                )
            )

    # Explicit replacement language may not use one of the simulator's
    # marker phrases (for example, ``change the color to blue``).
    if replacement and not marker_value:
        attribute, value = replacement
        hardness = "hard" if override else "soft"
        polarity = "require" if override else "prefer"
        mutations.append(
            ConstraintMutation(
                action="upsert",
                attribute=attribute,
                value=value,
                polarity=polarity,  # type: ignore[arg-type]
                hardness=hardness,  # type: ignore[arg-type]
                source="rule",
                confidence=0.92 if override else 0.88,
                disclosure_kind=("override" if override else "direct"),
            )
        )

    # A short answer to a typed question inherits that slot even when the
    # simulator/user wraps it in natural prose (``Yes — please look for 12``).
    # ``other`` is deliberately excluded: it is a broad question, not a
    # product field, so explicit words such as ``made from Nylon`` still own
    # their semantic attribute.
    asked_slot = _attribute_name(asked_attribute) if asked_attribute else None
    if (
        preference_payload
        and asked_slot in ALLOWED_ATTRIBUTES
        and asked_slot != "other"
        and not canonical_slots
        and not marker_value
        and not replacement
        and not special_mutations
        and not any(item.attribute == asked_slot for item in mutations)
    ):
        mutations.append(
            ConstraintMutation(
                action="upsert",
                attribute=asked_slot,
                value=preference_payload,
                polarity="prefer",
                hardness="hard" if override else "soft",
                source="rule",
                confidence=0.90,
                disclosure_kind=evidence_kind,
            )
        )

    # Explicit negation becomes an avoid mutation.  This is intentionally
    # hard: unlike a vague model inference, ``not red`` is a direct exclusion.
    if not no_preference:
        extra_mutations, negative_spans = _known_value_mutations(
            fact_text,
            excluded_spans={*special_spans, *category_spans},
            asked_attribute=asked_attribute,
            disclosure_kind=evidence_kind,
        )
        if replacement:
            replacement_attribute = _attribute_name(replacement[0])
            # The replacement clause contains both the new and old values;
            # the explicit replacement mutation is authoritative.  Keep
            # avoid mutations (``change color to blue, not red``) but do not
            # let generic vocabulary extraction re-add the old value.
            extra_mutations = [
                item
                for item in extra_mutations
                if item.polarity == "avoid"
                or _attribute_name(item.attribute) != replacement_attribute
            ]
        mutations.extend(extra_mutations)
    else:
        # Still parse explicit exclusions alongside a no-preference clause, but
        # avoid turning the no-preference prose itself into positive evidence.
        extra_mutations, negative_spans = _known_value_mutations(
            fact_text,
            excluded_spans={*special_spans, *category_spans},
            asked_attribute=asked_attribute,
            disclosure_kind=evidence_kind,
        )
        if replacement:
            replacement_attribute = _attribute_name(replacement[0])
            extra_mutations = [
                item
                for item in extra_mutations
                if item.polarity == "avoid"
                or _attribute_name(item.attribute) != replacement_attribute
            ]
        mutations.extend(
            item for item in extra_mutations if item.polarity == "avoid"
        )

    budget_match = _BUDGET_RE.search(fact_text)
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
                polarity="require" if not initial_provisional else "prefer",
                hardness="soft" if initial_provisional else "hard",
                source="rule",
                confidence=0.95,
                disclosure_kind=("initial" if initial_provisional else evidence_kind),
            )
        )

    # Deleting a preference is an explicit value-less remove.  The reducer
    # treats it as removing all active constraints for that attribute.
    for attribute in sorted(_removal_attributes(text)):
        mutations.append(
            ConstraintMutation(
                action="remove",
                attribute=attribute,
                value="",
                source="rule",
                confidence=0.95,
                disclosure_kind="direct",
            )
        )

    # Dedupe while preserving the strongest/earliest deterministic mutation.
    # A repeated value from the known vocabulary must not override marker
    # provenance (e.g. clarification -> direct).
    deduped: list[ConstraintMutation] = []
    # One lexical value can be seen by both the explicit marker parser and the
    # generic vocabulary parser.  De-duplicate by semantic slot/value/action,
    # retaining the first (more contextual) mutation so a generic inferred
    # polarity cannot overwrite an explicit marker's polarity.
    seen_mutations: set[tuple[str, str, str]] = set()
    for mutation in mutations:
        key = (
            _attribute_name(mutation.attribute),
            normalize_constraint_value(
                mutation.attribute, _clean(mutation.value, 120)
            ).lower(),
            mutation.action,
        )
        if key in seen_mutations:
            continue
        seen_mutations.add(key)
        deduped.append(mutation)
    mutations = deduped[:12]

    query_terms: tuple[str, ...]
    if scope != "none":
        # The reducer starts a new epoch for every explicit override.  Only
        # the replacement payload enters the new lexical projection.
        replacement_text = marker_value or (replacement[1] if replacement else "")
        if special_mutations:
            query_terms = tuple(
                normalize_constraint_value(item.attribute, item.value)
                for item in special_mutations[:8]
                if item.action != "remove"
                and item.polarity != "avoid"
                and normalize_constraint_value(item.attribute, item.value)
            )
        elif replacement_text:
            query_terms = tuple(
                _clean(value, 100)
                for value in _split_values(replacement_text)[:8]
                if _clean(value, 100)
                and not re.match(
                    r"^(?:not|no|without|except|excluding|other\s+than|anything\s+but)\b",
                    _clean(value, 100),
                    re.I,
                )
            )
        else:
            # ``Start over`` / ``Forget everything`` is a state transition,
            # not a lexical preference.  Do not turn its boilerplate into a
            # bogus feature query.
            query_terms = tuple()
    else:
        # Query evidence is bounded and accumulates across ordinary turns.
        if canonical_slots:
            query_terms = tuple(
                _clean(
                    (
                        f"{attribute}: {value}"
                        if attribute
                        in {
                            "rating",
                            "rating_count",
                            "color",
                            "material",
                            "size",
                            "style",
                            "feature",
                            "use_case",
                        }
                        else value
                    ),
                    100,
                )
                for attribute, value, _hard, polarity in canonical_slots[:8]
                if polarity != "avoid" and _clean(value, 100)
            )
        elif special_mutations:
            query_terms = tuple(
                normalize_constraint_value(item.attribute, item.value)
                for item in special_mutations[:8]
                if item.action != "remove"
                and item.polarity != "avoid"
                and normalize_constraint_value(item.attribute, item.value)
            )
        elif marker_value:
            # ``negative_spans`` uses coordinates from the original message.
            # Applying those offsets to the already-extracted marker payload
            # used to truncate unrelated text (for example ``mojo`` -> ``o``).
            # Filter negative payload clauses locally instead.
            query_terms = tuple(
                _clean(value, 100)
                for value in _split_values(marker_value)[:8]
                if _clean(value, 100)
                and not re.match(
                    r"^(?:not|no|without|except|excluding|other\s+than|anything\s+but)\b",
                    _clean(value, 100),
                    re.I,
                )
            )
        else:
            query_text = (
                preference_payload
                if preference_payload
                and re.search(r"\b(?:look\s+for|prioritize)\b", text, re.I)
                else text
            )
            query_text = _positive_query_text(query_text, negative_spans)
            query_terms = tuple(
                _clean(value, 100)
                for value in _split_values(query_text)[:8]
                if _clean(value, 100)
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
            source="rule",
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
