"""Exact simulator templates plus a content-driven fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .contest_text import ALLOWED_ATTRIBUTES, CHROME, normalise, terms

PREFIX = "i'm looking for "
EXPLORING = ", but i'm still exploring."
REQUIREMENT = ". a key requirement is: "
DISCLOSURE_RE = re.compile(r"for that,\s*what matters is:\s*(.+?)\.?$", re.I)
NO_ADDITIONAL_RE = re.compile(r"i don't have an additional preference for\s+(.+?)\.?$", re.I)
NO_PREF_RE = re.compile(
    r"i don't have a preference for\s+(.+?);\s*please use your judgment", re.I
)
OVERRIDE_RE = re.compile(r"ignore my earlier preference\.\s*what i need is:\s*(.+?)\.?$", re.I)
# Official override stays referenced_preference_replace. These extra scopes
# are for non-simulator phrasing; they must not fire on the evaluator template.
_GLOBAL_RESET_RE = re.compile(
    r"\b(?:forget|ignore|disregard)\s+(?:everything|all(?:\s+(?:of\s+)?"
    r"(?:this|that|my\s+preferences?|requirements?))?)\b"
    r"|\bstart(?:\s+all)?\s+over\b"
    r"|\bcompletely\s+different\s+(?:request|thing|search|product)\b",
    re.I,
)
_ATTRIBUTE_REPLACEMENT_RE = re.compile(
    r"\b(?:change|switch|replace|update|make)\s+(?:my\s+)?(?:the\s+)?"
    r"(?P<attribute>category|material|color|colour|size|sizing|style|brand|"
    r"budget|price|feature|use\s+case|use_case)\s+"
    r"(?:to|with|as)\s+(?P<new>[^.!?;,]+)",
    re.I,
)
_VALUE_REPLACEMENT_RE = re.compile(
    r"\b(?:prefer|want|need|use)\s+(?P<new>[^.!?;,]+?)\s+"
    r"(?:instead\s+of|rather\s+than)\s+(?P<old>[^.!?;,]+)",
    re.I,
)
ASK_MORE_RE = re.compile(r"ask me about one specific attribute", re.I)
RETRACT_CUES = (
    "ignore my earlier",
    "ignore my previous",
    "scratch that",
    "changed my mind",
    "on second thought",
    "actually, ignore",
)
BROWSE_CUES = ("still exploring", "just browsing", "not sure yet")
REQUIRE_CUES = ("requirement", "key requirement", "must have", "i need", "really need")
NEGATION_CUES = (
    "don't have",
    "dont have",
    "no preference",
    "use your judgment",
    "your judgment",
)


@dataclass
class Opening:
    category: str | None
    scenario: str
    constraints: list[str] = field(default_factory=list)


@dataclass
class Reply:
    kind: str
    attribute: str | None = None
    constraints: list[str] = field(default_factory=list)
    scope: str = "none"


def _clean(value: str) -> str:
    return value.strip(" .,;:!?-\"'")


def _split(payload: str) -> list[str]:
    return [_clean(part) for part in payload.split(";") if _clean(part)]


def _attribute(value: str) -> str | None:
    candidate = normalise(value).replace(" ", "_").strip(" .;:")
    aliases = {"colour": "color", "price": "budget", "use": "use_case"}
    candidate = aliases.get(candidate, candidate)
    return candidate if candidate in ALLOWED_ATTRIBUTES else None


def resolve_category(fragment: str, bucket_lookup: dict[str, str]) -> tuple[str | None, str]:
    cleaned = normalise(fragment).rstrip(" .,")
    if ". " in cleaned:
        head, tail = cleaned.split(". ", 1)
        name, rest = resolve_category(head, bucket_lookup)
        if name:
            leftover = " ".join(part for part in (rest, tail) if part).strip()
            return name, leftover
    direct = bucket_lookup.get(cleaned)
    if direct:
        return direct, ""
    words = [token.strip(".,;:!?") for token in cleaned.split()]
    words = [token for token in words if token]
    for size in range(min(10, len(words)), 0, -1):
        name = bucket_lookup.get(" ".join(words[:size]))
        if name:
            return name, " ".join(words[size:])
    return None, cleaned


def scan_category(text: str, bucket_lookup: dict[str, str]) -> tuple[str | None, tuple[int, int]]:
    words = normalise(text).split()
    best: str | None = None
    span = (0, 0)
    best_len = 0
    for start in range(len(words)):
        for end in range(min(len(words), start + 10), start, -1):
            if end - start <= best_len:
                break
            name = bucket_lookup.get(
                " ".join(token.strip(".,;:!?") for token in words[start:end]).strip()
            )
            if name:
                best = name
                span = (start, end)
                best_len = end - start
                break
    return best, span


def _content(text: str) -> list[str]:
    parts = text.split(";") if ";" in text else [text]
    output: list[str] = []
    for part in parts:
        cleaned = _clean(part)
        if not cleaned:
            continue
        useful = [token for token in terms(cleaned) if token not in CHROME]
        if useful:
            output.append(cleaned)
    return output


def parse_opening(message: str, bucket_lookup: dict[str, str]) -> Opening:
    lowered = normalise(message)
    if lowered.startswith(PREFIX):
        body = lowered[len(PREFIX) :]
        if body.endswith(EXPLORING):
            fragment = body[: -len(EXPLORING)]
            category, _ = resolve_category(fragment, bucket_lookup)
            return Opening(category=category, scenario="browsing")
        marker = body.find(REQUIREMENT)
        if marker != -1:
            category, _ = resolve_category(body[:marker], bucket_lookup)
            constraint = _clean(body[marker + len(REQUIREMENT) :])
            return Opening(
                category=category,
                scenario="buying",
                constraints=[constraint] if constraint else [],
            )
        category, remainder = resolve_category(body, bucket_lookup)
        residual = _clean(remainder)
        return Opening(
            category=category,
            scenario="intent_override",
            constraints=[residual] if residual else [],
        )

    category, span = scan_category(message, bucket_lookup)
    words = normalise(message).split()
    residual = " ".join(words[: span[0]] + words[span[1] :]) if category else lowered
    if any(cue in lowered for cue in BROWSE_CUES):
        return Opening(category=category, scenario="browsing")
    constraints = _content(residual)
    scenario = "buying" if any(cue in lowered for cue in REQUIRE_CUES) else "intent_override"
    return Opening(category=category, scenario=scenario, constraints=constraints)


def _need_payload(text: str) -> list[str]:
    match = re.search(r"what i need is:\s*(.+)$", text, re.I)
    if match:
        return _split(match.group(1))
    return _content(_clean(text))


def parse_reply(message: str) -> Reply:
    text = normalise(message)
    if not text:
        return Reply(kind="unknown")
    if _GLOBAL_RESET_RE.search(text):
        return Reply(kind="override", constraints=_need_payload(text), scope="global_reset")
    match = OVERRIDE_RE.search(text)
    if match:
        return Reply(
            kind="override",
            constraints=_split(match.group(1)),
            scope="referenced_preference_replace",
        )
    attr_match = _ATTRIBUTE_REPLACEMENT_RE.search(text)
    if attr_match:
        value = _clean(attr_match.group("new"))
        value = re.split(r"\b(?:instead\s+of|rather\s+than)\b", value, maxsplit=1, flags=re.I)[0]
        value = _clean(value)
        return Reply(
            kind="override",
            constraints=[value] if value else [],
            attribute=_attribute(attr_match.group("attribute")),
            scope="attribute_replace",
        )
    value_match = _VALUE_REPLACEMENT_RE.search(text)
    if value_match:
        value = _clean(value_match.group("new"))
        return Reply(
            kind="override",
            constraints=[value] if value else [],
            scope="attribute_replace",
        )
    match = NO_PREF_RE.search(text)
    if match:
        return Reply(kind="boundary", attribute=_attribute(match.group(1)))
    match = NO_ADDITIONAL_RE.search(text)
    if match:
        return Reply(kind="no_additional", attribute=_attribute(match.group(1)))
    match = DISCLOSURE_RE.search(text)
    if match:
        return Reply(kind="disclosure", constraints=_split(match.group(1)))
    if ASK_MORE_RE.search(text):
        return Reply(kind="ask_more")

    if any(cue in text for cue in RETRACT_CUES):
        payload = text
        for cue in RETRACT_CUES:
            index = payload.find(cue)
            if index != -1:
                payload = payload[index + len(cue) :]
                break
        payload = re.sub(r"^(?:.*?\bis\b|.*?\bneed\b)[:\s]*", "", payload, count=1)
        return Reply(
            kind="override",
            constraints=_content(_clean(payload)),
            scope="referenced_preference_replace",
        )
    if any(cue in text for cue in NEGATION_CUES):
        attribute = _attribute_in(text)
        boundary = any(word in text for word in ("judgment", "judgement", "up to you"))
        return Reply(kind="boundary" if boundary else "no_additional", attribute=attribute)
    payload = text.split(":", 1)[1] if ":" in text else text
    segments = _content(_clean(payload))
    if segments:
        return Reply(kind="disclosure", constraints=segments)
    return Reply(kind="unknown")


def _attribute_in(text: str) -> str | None:
    for attribute in ALLOWED_ATTRIBUTES:
        label = attribute.replace("_", " ")
        if re.search(rf"\b{re.escape(label)}\b", text) or re.search(rf"\b{re.escape(attribute)}\b", text):
            return attribute
    return None
