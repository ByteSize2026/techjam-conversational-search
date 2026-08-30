"""Rule-first, optionally model-assisted natural-language intent parsing.

The deterministic parser remains the complete offline implementation.  This
module is a deliberately small adapter around it: a model may suggest a
bounded update for ambiguous prose, but it never receives catalog identifiers
and it never mutates :class:`~.state.SessionState` directly.  Every model
response is validated before the conservative merge is handed to
``StateReducer``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import json
import math
import re
import time
from typing import Literal

from .config import AgentConfig
from .intent import (
    _GLOBAL_RESET_RE,
    _HARD_MARKER_RE,
    _REFERENCE_OVERRIDE_RE,
    _last_preference_payload,
    _attribute_name,
    _intent_scope,
    _clean,
    parse_intent_update,
)
from .model import (
    BackendFailure,
    ModelCompletionResult,
    TieredModelClient,
    TokenUsage,
    _coerce_usage,
)
from .state import (
    ALLOWED_ATTRIBUTES,
    ConstraintMutation,
    EvidenceKind,
    IntentScope,
    IntentUpdate,
    QueryEvidence,
    SessionState,
    normalize_constraint_value,
    rating_count_lower_bound,
    rating_lower_bound,
)
from .catalog import normalize_attribute_value


InterpretationPath = Literal["rules", "model", "rules_fallback"]

_SCOPES = frozenset(
    {"none", "attribute_replace", "referenced_preference_replace", "global_reset"}
)
_ACTIONS = frozenset({"upsert", "replace", "remove"})
_POLARITIES = frozenset({"prefer", "avoid", "require"})
_HARDNESSES = frozenset({"soft", "hard"})
_TOP_LEVEL_FIELDS = frozenset(
    {
        "scope",
        "mutations",
        "category_anchor",
        "no_preference",
        "global_exhausted",
        "boundary_signal",
        "reopen_clarification",
        "query_terms",
        "confidence",
    }
)
_CANONICAL_FIELDS = frozenset({"canonical_text"})
_MUTATION_FIELDS = frozenset(
    {"action", "attribute", "value", "polarity", "hardness", "confidence"}
)
_MAX_MUTATIONS = 12
_MAX_QUERY_TERMS = 12
_MAX_STRING = 120
_MIN_MODEL_CONFIDENCE = 0.40
_CATALOG_ENUMERATED_ATTRIBUTES = frozenset(
    {"brand", "category", "color", "material", "size", "style", "feature"}
)
_CANONICAL_ATTRIBUTE_PATTERN = (
    r"category|brand|budget|rating|rating_count|color|material|size|style|feature|use\s+case|use_case"
)
_CANONICAL_FACT_LINE_RE = re.compile(
    rf"^(?:For\s+that,\s+what\s+matters\s+is:|A\s+key\s+requirement\s+is:|I\s+do\s+not\s+want)\s*"
    rf"(?P<attribute>{_CANONICAL_ATTRIBUTE_PATTERN})\s*:\s*(?P<value>[^;]+?)\.?$",
    re.I,
)
_CANONICAL_REMOVAL_LINE_RE = re.compile(
    rf"^The\s+(?P<attribute>{_CANONICAL_ATTRIBUTE_PATTERN})\s+preference\s+no\s+longer\s+matters\.?$",
    re.I,
)
_CANONICAL_OVERRIDE_LINE_RE = re.compile(
    rf"^Actually,\s*change\s+the\s+(?P<attribute>{_CANONICAL_ATTRIBUTE_PATTERN})\s+to\s+(?P<value>[^;]+?)\.?$",
    re.I,
)
_CANONICAL_GLOBAL_RESET_RE = re.compile(
    r"^Actually,\s*forget\s+everything\.?$", re.I
)
# Amazon parent ASINs in this project are ten-character ``B...`` values with
# at least one digit.  Requiring a digit avoids treating ordinary words such
# as ``breathable`` as IDs while still rejecting catalog-like model output.
_MODEL_ID_RE = re.compile(r"\bB(?=[0-9A-Z]{9}\b)(?=[0-9A-Z]*\d)[0-9A-Z]{9}\b", re.I)
_REFERENCE_RE = re.compile(
    r"\b(?:this|that|it|one|those|these|same|another|earlier|previous|prior|above|below)\b",
    re.I,
)
_CHANGE_RE = re.compile(
    r"\b(?:change|switch|replace|update|alter|modify|keep|instead|rather)\b",
    re.I,
)
_NO_PREFERENCE_RE = re.compile(
    r"\b(?:no|without)\s+(?:an?\s+)?(?:additional\s+)?preference\b"
    r"|\b(?:don't|do not|doesn't|does not)\s+(?:care|matter|have)\b",
    re.I,
)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "actually",
        "anything",
        "earlier",
        "for",
        "i",
        "if",
        "in",
        "is",
        "it",
        "instead",
        "my",
        "of",
        "on",
        "or",
        "please",
        "previous",
        "prefer",
        "preferably",
        "prior",
        "some",
        "something",
        "that",
        "the",
        "to",
        "want",
        "with",
        "yes",
    }
)


def _finite_unit(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return parsed


def _strict_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _bounded_text(value: object, *, field: str, limit: int = _MAX_STRING) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = _clean(value, limit)
    if not text:
        raise ValueError(f"{field} must not be empty")
    if _MODEL_ID_RE.search(text):
        raise ValueError(f"{field} must not contain a catalog identifier")
    return text


def _validate_optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field=field)


def validate_intent_json(value: object) -> dict[str, object]:
    """Validate and normalize the model's strict intent JSON response.

    The validator is intentionally fail-closed.  Unknown fields are rejected
    instead of silently trusted, and all values are bounded before they can
    reach the reducer or a downstream query.  ``source``, ``epoch`` and
    ``status`` are not model-controlled fields.
    """

    if not isinstance(value, Mapping):
        raise ValueError("intent response must be a JSON object")
    unknown = set(value) - _TOP_LEVEL_FIELDS
    if unknown:
        raise ValueError(f"intent response has unknown fields: {sorted(map(str, unknown))}")

    raw_scope = value.get("scope", "none")
    if not isinstance(raw_scope, str) or raw_scope not in _SCOPES:
        raise ValueError("scope is not an allowed intent scope")

    raw_mutations = value.get("mutations", [])
    if not isinstance(raw_mutations, list) or len(raw_mutations) > _MAX_MUTATIONS:
        raise ValueError("mutations must be a bounded list")
    mutations: list[dict[str, object]] = []
    for index, raw_mutation in enumerate(raw_mutations):
        if not isinstance(raw_mutation, Mapping):
            raise ValueError(f"mutations[{index}] must be an object")
        unknown_mutation = set(raw_mutation) - _MUTATION_FIELDS
        if unknown_mutation:
            raise ValueError(
                f"mutations[{index}] has unknown fields: "
                f"{sorted(map(str, unknown_mutation))}"
            )
        action = raw_mutation.get("action", "upsert")
        attribute = raw_mutation.get("attribute")
        if not isinstance(action, str) or action not in _ACTIONS:
            raise ValueError(f"mutations[{index}].action is not allowed")
        if not isinstance(attribute, str) or attribute not in ALLOWED_ATTRIBUTES:
            raise ValueError(f"mutations[{index}].attribute is not allowed")
        raw_value = raw_mutation.get("value", "")
        if action == "remove" and raw_value in (None, ""):
            normalized_value = ""
        else:
            normalized_value = _bounded_text(
                raw_value, field=f"mutations[{index}].value"
            )
        polarity = raw_mutation.get("polarity", "prefer")
        hardness = raw_mutation.get("hardness", "soft")
        if not isinstance(polarity, str) or polarity not in _POLARITIES:
            raise ValueError(f"mutations[{index}].polarity is not allowed")
        if not isinstance(hardness, str) or hardness not in _HARDNESSES:
            raise ValueError(f"mutations[{index}].hardness is not allowed")
        confidence = _finite_unit(
            raw_mutation.get("confidence", value.get("confidence", 0.0)),
            field=f"mutations[{index}].confidence",
        )
        if action != "remove" and not normalized_value:
            raise ValueError(f"mutations[{index}].value must not be empty")
        mutations.append(
            {
                "action": action,
                "attribute": attribute,
                "value": normalized_value,
                "polarity": polarity,
                "hardness": hardness,
                "confidence": confidence,
            }
        )

    raw_no_preference = value.get("no_preference", [])
    if not isinstance(raw_no_preference, list) or len(raw_no_preference) > len(ALLOWED_ATTRIBUTES):
        raise ValueError("no_preference must be a bounded list")
    no_preference: list[str] = []
    for index, attribute in enumerate(raw_no_preference):
        if not isinstance(attribute, str) or attribute not in ALLOWED_ATTRIBUTES:
            raise ValueError(f"no_preference[{index}] is not allowed")
        if attribute not in no_preference:
            no_preference.append(attribute)

    raw_query_terms = value.get("query_terms", [])
    if not isinstance(raw_query_terms, list) or len(raw_query_terms) > _MAX_QUERY_TERMS:
        raise ValueError("query_terms must be a bounded list")
    query_terms: list[str] = []
    for index, term in enumerate(raw_query_terms):
        text = _bounded_text(term, field=f"query_terms[{index}]", limit=100)
        if text.lower() not in {item.lower() for item in query_terms}:
            query_terms.append(text)

    category_anchor = _validate_optional_text(
        value.get("category_anchor"), field="category_anchor"
    )
    confidence = _finite_unit(value.get("confidence", 0.0), field="confidence")
    return {
        "scope": raw_scope,
        "mutations": mutations,
        "category_anchor": category_anchor,
        "no_preference": no_preference,
        "global_exhausted": _strict_bool(
            value.get("global_exhausted", False), field="global_exhausted"
        ),
        "boundary_signal": _strict_bool(
            value.get("boundary_signal", False), field="boundary_signal"
        ),
        "reopen_clarification": _strict_bool(
            value.get("reopen_clarification", False), field="reopen_clarification"
        ),
        "query_terms": query_terms,
        "confidence": confidence,
    }


def validate_canonical_json(value: object) -> str:
    """Validate a model-produced utterance against the closed grammar."""
    if not isinstance(value, Mapping) or set(value) != _CANONICAL_FIELDS:
        raise ValueError("canonical response must contain only canonical_text")
    text = value.get("canonical_text")
    if not isinstance(text, str) or not text.strip() or len(text) > 1200:
        raise ValueError("canonical_text must be a bounded non-empty string")
    if _MODEL_ID_RE.search(text):
        raise ValueError("canonical text contains a catalog-like ID")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(";" in line for line in lines):
        raise ValueError("canonical text must contain one fact per line")
    if not lines or any(
        not (
            _CANONICAL_FACT_LINE_RE.fullmatch(line)
            or _CANONICAL_REMOVAL_LINE_RE.fullmatch(line)
            or _CANONICAL_OVERRIDE_LINE_RE.fullmatch(line)
            or _CANONICAL_GLOBAL_RESET_RE.fullmatch(line)
        )
        for line in lines
    ):
        raise ValueError(
            "canonical text must use an approved template and state-machine field per line"
        )
    return text.strip()


# Readable aliases for integrations/tests that use either naming convention.
validate_model_intent = validate_intent_json
validate_intent_response = validate_intent_json


def _tokens(value: object) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[^\W_]+", str(value or ""), flags=re.UNICODE)
        if token.lower() not in _STOPWORDS and len(token) > 1
    }


def _supported_by_message(message: str, value: str) -> bool:
    normalized_message = re.sub(r"\s+", " ", message.lower()).strip()
    normalized_value = re.sub(r"\s+", " ", value.lower()).strip()
    if normalized_value and normalized_value in normalized_message:
        return True
    terms = _tokens(value)
    message_terms = _tokens(message)
    if not terms:
        return False
    return len(terms & message_terms) / len(terms) >= 0.5


def _has_explicit_negation(message: str, value: str) -> bool:
    value_terms = _tokens(value)
    if not value_terms:
        return False
    for match in re.finditer(
        r"\b(?:not|without|except|excluding|other\s+than|anything\s+but)\b"
        r"\s+(?P<payload>[^,.;!?]+)",
        message,
        re.I,
    ):
        if value_terms <= _tokens(match.group("payload")):
            return True
    return False


def _looks_like_ambiguous_message(message: str, update: IntentUpdate) -> list[str]:
    reasons: list[str] = []
    if update.confidence < 0.72:
        reasons.append("low_rule_confidence")
    # A resolved marker such as ``For that, what matters is: leather`` is
    # already deterministic.  Other references remain candidates for model
    # clarification even when the rule parser found a weak residual feature.
    resolved_reference = bool(
        re.search(
            r"\b(?:for\s+(?:that|this)|what\s+matters\s+is|key\s+requirement\s+is)\b",
            message,
            re.I,
        )
    )
    if _REFERENCE_RE.search(message) and not resolved_reference and update.scope == "none":
        reasons.append("unresolved_reference")
    if _CHANGE_RE.search(message) and update.scope == "none":
        reasons.append("unclear_change_scope")
    if len(
        {
            (item.attribute, normalize_constraint_value(item.attribute, item.value).lower())
            for item in update.mutations
            if item.action != "remove"
        }
    ) < len([item for item in update.mutations if item.action != "remove"]):
        reasons.append("conflicting_clauses")
    if not update.mutations and not update.category_anchor and update.query_terms:
        reasons.append("unparsed_preference")
    elif update.category_anchor and not update.mutations and any(
        token in _tokens(message)
        for token in {"prefer", "ideally", "need", "want", "looking", "suitable", "durable", "comfortable"}
    ):
        # A category-only opening is deterministic; category plus an
        # unstructured preference is the useful model trigger.
        if len(_tokens(message) - _tokens(update.category_anchor)) > 2:
            reasons.append("unparsed_preference")
    return list(dict.fromkeys(reasons))


def _snapshot(
    state: SessionState | Mapping[str, object] | None, *, max_recent_turns: int
) -> dict[str, object]:
    if state is None:
        return {"constraints": [], "query_terms": [], "recent_turns": [], "asked_attributes": []}
    if isinstance(state, Mapping):
        # Permit callers to provide a pre-sanitized snapshot without exposing
        # a mutable SessionState object to the adapter.  Copy only the known,
        # bounded fields; the prompt builder below applies the same limits.
        raw_constraints = state.get("constraints", [])
        raw_terms = state.get("query_terms", [])
        raw_turns = state.get("recent_turns", [])
        raw_asked = state.get("asked_attributes", [])
        constraints = raw_constraints if isinstance(raw_constraints, list) else []
        terms = raw_terms if isinstance(raw_terms, list) else []
        turns = raw_turns if isinstance(raw_turns, list) else []
        asked = raw_asked if isinstance(raw_asked, list) else []
        safe_constraints: list[dict[str, object]] = []
        for item in constraints[:12]:
            if not isinstance(item, Mapping):
                continue
            safe_constraints.append(
                {
                    "attribute": _clean(item.get("attribute"), 40),
                    "value": _clean(item.get("value"), _MAX_STRING),
                    "polarity": _clean(item.get("polarity"), 16),
                    "hardness": _clean(item.get("hardness"), 16),
                }
            )
        safe_turns: list[dict[str, object]] = []
        for item in turns[-max(max_recent_turns, 0) :]:
            if not isinstance(item, Mapping):
                continue
            safe_turns.append(
                {
                    "turn": item.get("turn", 0),
                    "message": _clean(item.get("message"), 280),
                }
            )
        return {
            "constraints": safe_constraints,
            "query_terms": [_clean(item, 100) for item in terms[-12:] if _clean(item, 100)],
            "recent_turns": safe_turns,
            "asked_attributes": [
                _attribute_name(item) for item in asked[-10:] if _attribute_name(item) in ALLOWED_ATTRIBUTES
            ],
        }
    constraints = [
        {
            "attribute": item.attribute,
            "value": normalize_constraint_value(item.attribute, item.value)[:_MAX_STRING],
            "polarity": item.polarity,
            "hardness": item.hardness,
        }
        for item in state.active_constraints[:12]
    ]
    recent_turns = [
        {"turn": int(item.turn), "message": _clean(item.user_message, 280)}
        for item in state.messages[-max(max_recent_turns, 0) :]
    ]
    tags = state.profile.get("preference_tags", ())
    if isinstance(tags, str):
        tags = (tags,)
    if not isinstance(tags, (list, tuple, set)):
        tags = ()
    return {
        "constraints": constraints,
        "query_terms": [_clean(item, 100) for item in state.active_query_terms[-12:]],
        "recent_turns": recent_turns,
        "asked_attributes": list(state.asked_attributes[-10:]),
        "profile_priors": [_clean(item, 60) for item in tags if _clean(item, 60)][:8],
    }


def _message_tokens(value: object) -> list[str]:
    return [
        token
        for token in re.findall(r"[^\W_]+", str(value or "").casefold(), flags=re.UNICODE)
        if len(token) >= 2 and token not in _STOPWORDS
    ]


def _generic_preference_phrases(message: str) -> list[str]:
    """Return bounded values following an explicit preference marker.

    Catalog grounding must inspect the customer's value, not the boilerplate
    around it.  Otherwise a sentence such as ``Please look for Snap`` can
    incorrectly admit the catalog feature ``look`` and discard ``Snap``.
    """

    payloads: list[str] = []
    pattern = re.compile(
        r"\b(?:look\s+for|prioritize|prefer|want|need)\s+"
        r"(?:the\s+)?(?:brand|label|maker|manufacturer)?\s*"
        r"(?P<value>[^.!?;,]+)",
        re.I,
    )
    def add_value(value: object) -> None:
        value = _clean(value, 120)
        if not value:
            return
        # A bare number has no field semantics without the asked slot.  Do
        # not let catalog coincidences turn ``5.0`` into color=5 or size=5;
        # the deterministic parser/model can still keep it as lexical or
        # typed numeric evidence when the context is explicit.
        if not re.search(r"[^\W\d_]", value, flags=re.UNICODE):
            return
        # Preserve the complete normalized payload as well as filtered
        # n-grams.  Articles and punctuation can be part of an exact catalog
        # label (``The Highest Heel`` or a comma-separated occasion value).
        full_phrase = normalize_attribute_value(value)
        if full_phrase and full_phrase not in payloads:
            payloads.append(full_phrase)
        tokens = _message_tokens(value)
        for width in range(1, min(5, len(tokens)) + 1):
            for index in range(max(len(tokens) - width + 1, 0)):
                phrase = " ".join(tokens[index : index + width])
                if phrase and phrase not in payloads:
                    payloads.append(phrase)

    for match in pattern.finditer(message):
        add_value(match.group("value"))
    # The marker regex above stops at commas to avoid swallowing neighboring
    # clauses.  The sentence-aware helper safely restores the final complete
    # payload for exact comma-bearing catalog values.
    add_value(_last_preference_payload(message))
    return sorted(
        payloads,
        key=lambda item: (-len(item.split()), -len(item), item),
    )


def _direct_brand_phrases(message: str) -> list[str]:
    """Return values from explicit brand-only constructions."""

    payloads: list[str] = []
    patterns = (
        r"\b(?:brand|label|maker|manufacturer)\s*(?:is|:|=)?\s*(?P<value>[^.!?;,]+)",
        r"\bmade\s+by\s+(?P<value>[^.!?;,]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, message, re.I):
            full_phrase = normalize_attribute_value(match.group("value"))
            if full_phrase and full_phrase not in payloads:
                payloads.append(full_phrase)
            tokens = _message_tokens(match.group("value"))
            for width in range(1, min(5, len(tokens)) + 1):
                for index in range(max(len(tokens) - width + 1, 0)):
                    phrase = " ".join(tokens[index : index + width])
                    if phrase and phrase not in payloads:
                        payloads.append(phrase)
    return sorted(payloads, key=lambda item: (-len(item.split()), -len(item), item))


def _contains_catalog_phrase(message: str, value: object) -> bool:
    phrase = normalize_attribute_value(value)
    if not phrase:
        return False
    haystack = normalize_attribute_value(message)
    return bool(
        re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", haystack, re.I)
    )


def enrich_catalog_intent(
    update: IntentUpdate,
    message: object,
    repository: object | None,
    *,
    source: Literal["rule", "profile"] = "rule",
    confidence: float = 0.94,
) -> IntentUpdate:
    """Turn catalog-backed phrases into typed deterministic evidence.

    The natural-language parser deliberately has no catalog dependency.  This
    adapter runs immediately after it, using only the frozen local catalog to
    recognize exact brands, detail values, and categories.  It never invents a
    value: an unmatched phrase remains ordinary lexical evidence.
    """

    resolver = getattr(repository, "resolve_attribute_value", None)
    if not callable(resolver):
        return update
    text = _clean(message, 1200)
    referenced_payload = (
        _last_preference_payload(text)
        if update.scope == "referenced_preference_replace"
        else None
    )
    # Catalog grounding sees only the active replacement value.  This keeps a
    # catalog-backed decoy mentioned in ``ignore earlier X`` from being
    # promoted back into the new state.
    grounding_text = referenced_payload or text
    mutations: list[ConstraintMutation] = []
    for item in update.mutations:
        attribute = _attribute_name(item.attribute)
        value = normalize_constraint_value(attribute, item.value)
        if item.action == "remove" or attribute in {"budget", "use_case", "other"}:
            mutations.append(item)
            continue
        if attribute == "feature" and (
            rating_lower_bound(value) is not None
            or rating_count_lower_bound(value) is not None
        ):
            mutations.append(item)
            continue
        title_match = (
            re.match(r"^title\s+contains\s*:\s*(?P<token>.+)$", value, re.I)
            if attribute == "feature"
            else None
        )
        if title_match is not None:
            if resolver("title_token", title_match.group("token")) is not None:
                mutations.append(item)
            continue
        if attribute in _CATALOG_ENUMERATED_ATTRIBUTES and resolver(attribute, value) is not None:
            mutations.append(item)
    evidence = list(update.query_evidence)
    changed = False
    if source == "profile":
        # Profile tags are trusted historical context but remain soft.  Re-tag
        # special facts parsed before catalog grounding so they do not look
        # like fresh user utterances in diagnostics or ranking weights.
        mutations = [
            replace(
                item,
                source="profile",
                confidence=min(float(item.confidence), 0.82),
                hardness="soft",
                explicit=True,
                disclosure_kind="initial",
            )
            for item in mutations
        ]
    existing = {
        (
            _attribute_name(item.attribute),
            normalize_constraint_value(item.attribute, item.value).lower(),
        )
        for item in mutations
        if item.action != "remove" and item.value
    }
    evidence_seen = {item.text.casefold() for item in evidence}
    disclosure: EvidenceKind = "initial" if source == "profile" else (
        "override" if update.scope != "none" else "direct"
    )

    def add(attribute: str, value: str, *, hard: bool = False) -> None:
        nonlocal changed
        attr = _attribute_name(attribute)
        normalized = normalize_constraint_value(attr, value)
        if not normalized:
            return
        key = (attr, normalized.lower())
        if key in existing:
            return
        final_hard = hard or update.scope != "none"
        mutations.append(
            ConstraintMutation(
                action="upsert",
                attribute=attr,
                value=value,
                polarity="require" if final_hard else "prefer",
                hardness="hard" if final_hard else "soft",
                source=source,
                confidence=(0.82 if source == "profile" else confidence),
                disclosure_kind=disclosure,
                explicit=True,
            )
        )
        existing.add(key)
        changed = True
        term = f"{attr}: {value}"
        if term.casefold() not in evidence_seen:
            evidence.append(
                QueryEvidence(
                    text=term,
                    turn=0,
                    kind=disclosure,
                    attribute_hint=attr,
                    confidence=(0.82 if source == "profile" else confidence),
                    source=source,
                )
            )
            evidence_seen.add(term.casefold())

    # Exact natural-language rating and title facts were already parsed by
    # ``parse_intent_update``.  Resolve the remaining labels from bounded
    # message n-grams rather than scanning every catalog value on each turn.
    tokens = _message_tokens(grounding_text)
    phrases = {
        " ".join(tokens[index : index + width])
        for width in range(1, min(5, len(tokens)) + 1)
        for index in range(max(len(tokens) - width + 1, 0))
    }
    title_context = bool(
        re.search(r"\b(?:title|listing|product\s+name|name)\b", grounding_text, re.I)
    )
    ordered_phrases = sorted(
        phrases, key=lambda item: (-len(item.split()), -len(item), item)
    )
    preference_phrases = _generic_preference_phrases(text)
    if referenced_payload:
        payload_tokens = _message_tokens(referenced_payload)
        preference_phrases = sorted(
            {
                *preference_phrases,
                *(
                    " ".join(payload_tokens[index : index + width])
                    for width in range(1, min(5, len(payload_tokens)) + 1)
                    for index in range(max(len(payload_tokens) - width + 1, 0))
                ),
            },
            key=lambda item: (-len(item.split()), -len(item), item),
        )
    brand_phrases = sorted(
        {*preference_phrases, *_direct_brand_phrases(grounding_text)},
        key=lambda item: (-len(item.split()), -len(item), item),
    )
    brand_candidate = next(
        (
            candidate
            for candidate in brand_phrases
            if resolver("brand", candidate) is not None
        ),
        None,
    )
    brand_context = bool(
        brand_candidate
        and (
            bool(preference_phrases)
            or bool(
                re.search(
                    r"\b(?:brand|label|maker|manufacturer|made\s+by)\b",
                    grounding_text,
                    re.I,
                )
            )
        )
    )
    if brand_candidate and brand_context:
        add("brand", brand_candidate)

    # A clear title phrase owns its token; a clear brand phrase owns the
    # otherwise ambiguous words around it.  Only when neither applies do we
    # admit the longest catalog value for each remaining typed slot.
    if (
        not mutations
        and not update.category_anchor
        and not title_context
        and not brand_context
    ):
        marker_order = (
            ("material", r"\b(?:material|fabric|made\s+from)\b"),
            ("color", r"\b(?:color|colour|shade)\b"),
            ("size", r"\b(?:size|sizing|fit)\b"),
            ("style", r"\b(?:style|pattern|design)\b"),
            ("feature", r"\b(?:feature|detail|includes?|has|with)\b"),
        )
        explicit_attribute = next(
            (
                attribute
                for attribute, pattern in marker_order
                if re.search(pattern, grounding_text, re.I)
            ),
            None,
        )
        if explicit_attribute:
            candidate = next(
                (
                    phrase
                    for phrase in ordered_phrases
                    if resolver(explicit_attribute, phrase) is not None
                ),
                None,
            )
            if candidate:
                add(explicit_attribute, candidate)
        elif preference_phrases:
            # A slot-neutral phrase may occur in more than one catalog field.
            # Preserve the longest exact value the customer actually said;
            # only then use the smallest posting list to choose its field.
            # This keeps comma-bearing labels intact instead of replacing
            # them with an accidentally rarer subphrase.
            attribute_priority = {
                "style": 0,
                "material": 1,
                "color": 2,
                "size": 3,
                "feature": 4,
            }
            candidates: list[tuple[int, int, int, str, str]] = []
            for attribute, priority in attribute_priority.items():
                for phrase in preference_phrases:
                    resolved = resolver(attribute, phrase)
                    if resolved is None:
                        continue
                    _normalized, matching_ids = resolved
                    candidates.append(
                        (
                            -len(phrase.split()),
                            len(matching_ids),
                            priority,
                            attribute,
                            phrase,
                        )
                    )
            if candidates:
                _length, _count, _priority, attribute, candidate = min(candidates)
                add(attribute, candidate)

    # ``title_token`` is intentionally opt-in through a name/title phrase.
    title_matches = re.finditer(
        r"\b(?:remember\s+seeing|saw|seen|mentions?|mentioned)\s+"
        r"(?P<token>[a-z0-9][a-z0-9_'’-]{1,48})\s+in\s+(?:the\s+)?"
        r"(?:product\s+)?(?:name|title)\b"
        r"|\b(?:product\s+name|title|listing)\s+(?:contains?|mentions?|has)\s+"
        r"(?P<contains>[a-z0-9][a-z0-9_'’-]{1,48})\b",
        grounding_text,
        re.I,
    )
    for match in title_matches:
        token = _clean(match.group("token") or match.group("contains") or "", 80)
        if token and resolver("title_token", token) is not None:
            add("feature", f"title contains: {token}")

    # A category phrase that the parser could not isolate can still be
    # resolved when the user explicitly uses category language.
    if update.category_anchor is None and re.search(
        r"\b(?:category|department|type|looking\s+for|shopping\s+for|in\s+the)\b",
        text,
        re.I,
    ):
        category_match = re.search(
            r"\b(?:in\s+the\s+|category\s*:\s*|looking\s+for\s+|shopping\s+for\s+)"
            r"(?P<category>[a-z0-9][a-z0-9 &'’\-/]{1,100}?)(?=\s+category\b|[.!?,]|$)",
            text,
            re.I,
        )
        if category_match:
            candidate = _clean(category_match.group("category"), 120)
            resolution = getattr(repository, "resolve_category", lambda _value: None)(candidate)
            if getattr(resolution, "resolved", False):
                changed = True
                return replace(
                    update,
                    mutations=tuple(mutations[:_MAX_MUTATIONS]),
                    category_anchor=candidate,
                    query_evidence=tuple(evidence[:_MAX_QUERY_TERMS]),
                    query_terms=tuple(dict.fromkeys((*update.query_terms, candidate)))[:_MAX_QUERY_TERMS],
                    confidence=max(update.confidence, confidence) if changed else update.confidence,
                )

    return replace(
        update,
        mutations=tuple(mutations[:_MAX_MUTATIONS]),
        query_evidence=tuple(evidence[:_MAX_QUERY_TERMS]),
        query_terms=tuple(dict.fromkeys((*update.query_terms, *[item.text for item in evidence])))[:_MAX_QUERY_TERMS],
        confidence=max(update.confidence, confidence) if changed else update.confidence,
    )


def profile_intent_update(
    profile: Mapping[str, object] | None,
    repository: object | None,
) -> IntentUpdate:
    """Parse explicit profile tags into low-weight, catalog-backed evidence."""

    tags = profile.get("preference_tags", ()) if isinstance(profile, Mapping) else ()
    if isinstance(tags, str):
        tags = (tags,)
    if not isinstance(tags, (list, tuple, set)):
        return IntentUpdate()
    combined = IntentUpdate()
    for raw_tag in list(tags)[:8]:
        tag = _clean(raw_tag, 240)
        if not tag:
            continue
        parsed = parse_intent_update(tag, turn=0)
        enriched = enrich_catalog_intent(
            parsed,
            tag,
            repository,
            source="profile",
            confidence=0.82,
        )
        combined = replace(
            combined,
            mutations=tuple((*combined.mutations, *enriched.mutations))[:_MAX_MUTATIONS],
            query_terms=tuple(dict.fromkeys((*combined.query_terms, *enriched.query_terms)))[:_MAX_QUERY_TERMS],
            query_evidence=tuple((*combined.query_evidence, *enriched.query_evidence))[:_MAX_QUERY_TERMS],
            confidence=max(combined.confidence, enriched.confidence),
        )
    return combined


def _failure_dict(failure: object) -> dict[str, str]:
    if isinstance(failure, BackendFailure):
        return failure.as_dict()
    as_dict = getattr(failure, "as_dict", None)
    if callable(as_dict):
        try:
            result = as_dict()
            if isinstance(result, Mapping):
                return {str(key): str(value)[:500] for key, value in result.items()}
        except Exception:
            pass
    return {"backend": "model", "stage": "unknown", "error": str(failure)[:500]}


def _completion_parts(completion: object) -> tuple[object | None, str | None, TokenUsage | None, tuple[object, ...]]:
    if isinstance(completion, ModelCompletionResult):
        return completion.value, completion.backend, completion.usage, completion.failures
    if isinstance(completion, Mapping):
        value = completion.get("value", completion.get("data"))
        backend = completion.get("backend")
        usage = completion.get("usage")
        failures = completion.get("failures", ())
        if value is None and any(key in completion for key in _TOP_LEVEL_FIELDS):
            # Tiny injected clients sometimes return the decoded model object
            # directly instead of wrapping it in ModelCompletionResult.
            value = dict(completion)
            backend = backend or "model-client"
    else:
        value = getattr(completion, "value", getattr(completion, "data", None))
        backend = getattr(completion, "backend", None)
        usage = getattr(completion, "usage", None)
        failures = getattr(completion, "failures", ())
    if not isinstance(backend, str) or not backend.strip():
        backend = None
    if not isinstance(failures, (list, tuple)):
        failures = (failures,) if failures else ()
    return value, backend, _coerce_usage(usage), tuple(failures)


@dataclass(frozen=True)
class IntentInterpretation:
    """Validated intent result plus model/fallback diagnostics."""

    update: IntentUpdate
    path: InterpretationPath = "rules"
    scope: IntentScope = "none"
    confidence: float = 1.0
    trigger_reason: str = "high_confidence_rules"
    accepted: tuple[dict[str, object], ...] = ()
    rejected: tuple[dict[str, object], ...] = ()
    backend: str | None = None
    usage: TokenUsage | None = None
    failures: tuple[object, ...] = ()
    latency_ms: float = 0.0

    @property
    def model_backend(self) -> str | None:
        return self.backend

    @property
    def model_failures(self) -> tuple[object, ...]:
        return self.failures

    @property
    def diagnostics(self) -> dict[str, object]:
        usage: object | None = self.usage.as_dict() if self.usage else None
        failure_values = [_failure_dict(item) for item in self.failures]
        return {
            "intent_path": self.path,
            "intent_interpretation_path": self.path,
            "intent_scope": self.scope,
            "scope": self.scope,
            "intent_confidence": round(float(self.confidence), 4),
            "intent_trigger_reason": self.trigger_reason,
            "intent_accepted": [dict(item) for item in self.accepted],
            "intent_rejected": [dict(item) for item in self.rejected],
            "intent_model_backend": self.backend,
            "intent_model_failures": failure_values,
            "intent_usage": usage,
            "intent_latency_ms": round(float(self.latency_ms), 3),
        }

    def as_dict(self) -> dict[str, object]:
        return {"update": self.update, **self.diagnostics}


# Backwards/forward-friendly result name for callers that prefer ``Result``.
IntentInterpretationResult = IntentInterpretation


class IntentInterpreter:
    """Interpret one message without ever mutating session state."""

    def __init__(
        self,
        client: TieredModelClient | object | None = None,
        config: AgentConfig | None = None,
        *,
        model_client: TieredModelClient | object | None = None,
        enabled: bool | None = None,
        trigger_threshold: float | None = None,
        accept_threshold: float | None = None,
        max_recent_turns: int | None = None,
        catalog_repository: object | None = None,
    ) -> None:
        if client is not None and model_client is not None:
            raise ValueError("pass either client or model_client, not both")
        self.config = config or AgentConfig.from_env()
        self.client = model_client if model_client is not None else client
        self.catalog_repository = catalog_repository
        self._client_explicit = self.client is not None
        configured_enabled = bool(
            getattr(self.config, "intent_model_enabled", False)
        )
        # Explicit injection is an opt-in test/integration request.  A client
        # discovered from configuration still follows the feature flag.
        self.enabled = self._client_explicit if enabled is None else bool(enabled)
        if enabled is None and not self._client_explicit:
            self.enabled = configured_enabled
        self.trigger_threshold = self._unit_or_default(
            trigger_threshold
            if trigger_threshold is not None
            else getattr(self.config, "intent_model_trigger_threshold", 0.72),
            0.72,
        )
        self.accept_threshold = self._unit_or_default(
            accept_threshold
            if accept_threshold is not None
            else getattr(self.config, "intent_model_accept_threshold", 0.65),
            0.65,
        )
        try:
            self.max_recent_turns = max(
                int(
                    max_recent_turns
                    if max_recent_turns is not None
                    else getattr(self.config, "intent_model_recent_turns", 4)
                ),
                0,
            )
        except (TypeError, ValueError):
            self.max_recent_turns = 4
        self.last_interpretation: IntentInterpretation | None = None

    @staticmethod
    def _unit_or_default(value: object, default: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) and 0.0 <= value <= 1.0 else default

    def _model_client(self) -> object:
        if self.client is None:
            self.client = TieredModelClient.from_config(self.config)
        return self.client

    def _catalog_value_supported(self, attribute: str, value: str) -> bool:
        """Check structured model values against the loaded catalog index.

        Numeric constraints are ranges rather than enumerated catalog values;
        ``use_case`` and ``other`` remain lexical slots.  All other values
        that can become structured state must be observed in the catalog.
        An interpreter constructed without a repository keeps the historical
        standalone behavior used by small unit fixtures.
        """

        normalized_attribute = _attribute_name(attribute)
        normalized_value = normalize_constraint_value(
            normalized_attribute, value
        )
        if (
            normalized_attribute == "feature"
            and (
                rating_lower_bound(normalized_value) is not None
                or rating_count_lower_bound(normalized_value) is not None
            )
        ):
            return True
        repository = self.catalog_repository
        if repository is None or normalized_attribute not in _CATALOG_ENUMERATED_ATTRIBUTES:
            return True
        resolver = getattr(repository, "resolve_attribute_value", None)
        if not callable(resolver):
            return True
        if normalized_attribute == "feature":
            title_match = re.match(
                r"^title\s+contains\s*:\s*(?P<token>.+)$",
                normalized_value,
                re.I,
            )
            if title_match is not None:
                return resolver("title_token", title_match.group("token")) is not None
        return resolver(normalized_attribute, normalized_value) is not None

    def _validate_canonical_completion(self, value: object) -> str:
        """Validate canonical syntax and every structured value atomically."""

        canonical_text = value if isinstance(value, str) else validate_canonical_json(value)
        # A string here has normally already passed TieredModelClient's
        # validator. Re-wrap it so duck-typed clients receive the same strict
        # grammar check instead of bypassing it.
        canonical_text = validate_canonical_json({"canonical_text": canonical_text})
        update = parse_intent_update(canonical_text)
        for mutation in update.mutations:
            if mutation.action == "remove" or not mutation.value:
                continue
            if not self._catalog_value_supported(mutation.attribute, mutation.value):
                raise ValueError(
                    f"canonical {mutation.attribute} value is not present in the catalog"
                )
        if update.category_anchor and not self._catalog_value_supported(
            "category", update.category_anchor
        ):
            raise ValueError("canonical category value is not present in the catalog")
        return canonical_text

    @staticmethod
    def _canonical_fact_supported_by_message(
        message: str, attribute: object, value: object
    ) -> bool:
        """Require canonical facts to be grounded in the current utterance.

        The model may see ``asked_attribute`` for field assignment, but it
        must not copy a value from profile/history into a later answer.  The
        few numeric feature syntaxes need semantic-number alignment because
        their canonical labels do not appear literally in natural language.
        """

        normalized_attribute = _attribute_name(attribute)
        normalized_value = normalize_constraint_value(attribute, value)
        if not normalized_value:
            return False
        if normalized_attribute == "use_case" and normalize_attribute_value(
            normalized_value
        ) in {"men", "mens", "women", "womens", "boys", "girls", "unisex"}:
            return False
        # Cross-language translation cannot use literal token alignment.  The
        # canonicalizer prompt no longer receives state/history, and catalog
        # admission still applies, so CJK messages may rely on the model's
        # translation while English benchmark traffic remains span-checked.
        cross_language = bool(re.search(r"[\u3400-\u9fff]", message))
        if normalized_attribute == "feature":
            title_match = re.match(
                r"^title\s+contains\s*:\s*(?P<token>.+)$",
                normalized_value,
                re.I,
            )
            if title_match is not None:
                return cross_language or _contains_catalog_phrase(
                    message, title_match.group("token")
                )
            rating = rating_lower_bound(normalized_value)
            if rating is not None:
                message_numbers = {
                    float(item)
                    for item in re.findall(r"\d+(?:\.\d+)?", message)
                }
                return cross_language or float(rating) in message_numbers
            rating_count = rating_count_lower_bound(normalized_value)
            if rating_count is not None:
                message_counts = {
                    int(item.replace(",", ""))
                    for item in re.findall(r"\d[\d,]*", message)
                }
                return cross_language or int(rating_count) in message_counts
        if normalized_attribute == "budget":
            value_numbers = {
                float(item.replace(",", ""))
                for item in re.findall(r"\d+(?:[,.]\d+)*", normalized_value)
            }
            message_numbers = {
                float(item.replace(",", ""))
                for item in re.findall(r"\d+(?:[,.]\d+)*", message)
            }
            return cross_language or (
                bool(value_numbers) and value_numbers <= message_numbers
            )
        return cross_language or _contains_catalog_phrase(message, normalized_value)

    def _canonical_update_supported_by_message(
        self, message: str, update: IntentUpdate
    ) -> bool:
        evidence_message = re.sub(
            r"\bI\s+(?:initially|originally)\s+thought\s+I\s+wanted\b[^.?!;]*",
            " ",
            message,
            flags=re.I,
        )
        for mutation in update.mutations:
            if mutation.action == "remove":
                if not (
                    re.search(
                        r"\b(?:remove|drop|clear|forget|ignore|no\s+longer)\b",
                        evidence_message,
                        re.I,
                    )
                    and re.search(
                        rf"\b{re.escape(str(mutation.attribute))}\b",
                        evidence_message,
                        re.I,
                    )
                ):
                    return False
                continue
            if not self._canonical_fact_supported_by_message(
                evidence_message, mutation.attribute, mutation.value
            ):
                return False
        if update.category_anchor and not self._canonical_fact_supported_by_message(
            evidence_message, "category", update.category_anchor
        ):
            return False
        return True

    @staticmethod
    def _merge_canonical_update(
        deterministic: IntentUpdate, canonical: IntentUpdate
    ) -> IntentUpdate:
        """Let a validated translation fill gaps without deleting local facts."""

        def semantic_value(attribute: object, value: object) -> str:
            normalized = normalize_constraint_value(attribute, value)
            if _attribute_name(attribute) == "feature":
                title_match = re.match(
                    r"^title\s+contains\s*:\s*(?P<token>.+)$",
                    normalized,
                    re.I,
                )
                if title_match is not None:
                    normalized = title_match.group("token")
            return normalize_attribute_value(normalized).casefold()

        mutations = list(deterministic.mutations)
        existing_values = {
            semantic_value(item.attribute, item.value)
            for item in mutations
            if item.action != "remove" and item.value
        }
        if deterministic.category_anchor:
            existing_values.add(
                normalize_attribute_value(deterministic.category_anchor).casefold()
            )
        local_values = set(existing_values)
        existing_keys = {
            (
                item.action,
                _attribute_name(item.attribute),
                normalize_constraint_value(item.attribute, item.value).casefold(),
            )
            for item in mutations
        }
        for item in canonical.mutations:
            normalized = semantic_value(item.attribute, item.value)
            key = (item.action, _attribute_name(item.attribute), normalized)
            # If local catalog grounding already typed this exact value, do
            # not let the model reinterpret it as another field (Snap as a
            # title token instead of the locally resolved closure style).
            if key in existing_keys or (
                item.action != "remove" and normalized in existing_values
            ):
                continue
            mutations.append(
                replace(
                    item,
                    source="model",
                    explicit=True,
                    confidence=max(float(item.confidence), 0.90),
                )
            )
            existing_keys.add(key)
            if item.action != "remove" and normalized:
                existing_values.add(normalized)

        evidence = list(deterministic.query_evidence)
        evidence_seen = {
            semantic_value(item.attribute_hint or "feature", item.text)
            for item in evidence
        }
        for item in canonical.query_evidence:
            semantic_key = semantic_value(
                item.attribute_hint or "feature", item.text
            )
            if semantic_key in evidence_seen:
                continue
            evidence.append(
                replace(item, source="model", confidence=max(item.confidence, 0.90))
            )
            evidence_seen.add(semantic_key)

        # Protocol transitions stay deterministic.  A model that phrases an
        # ordinary preference as ``Actually, change ...`` may add the fact,
        # but cannot start a new epoch or clear prior state.
        scope: IntentScope = deterministic.scope
        query_terms = tuple(
            dict.fromkeys(
                (
                    *deterministic.query_terms,
                    *(item.text for item in evidence),
                )
            )
        )[:_MAX_QUERY_TERMS]
        canonical_category = canonical.category_anchor
        if canonical_category and normalize_attribute_value(
            canonical_category
        ).casefold() in local_values:
            canonical_category = None
        return replace(
            deterministic,
            global_override=deterministic.global_override or scope != "none",
            scope=scope,
            mutations=tuple(mutations[:_MAX_MUTATIONS]),
            category_anchor=deterministic.category_anchor or canonical_category,
            no_preference=frozenset(
                {*deterministic.no_preference, *canonical.no_preference}
            ),
            global_exhausted=deterministic.global_exhausted,
            boundary_signal=deterministic.boundary_signal,
            reopen_clarification=deterministic.reopen_clarification,
            query_terms=query_terms,
            query_evidence=tuple(evidence[:_MAX_QUERY_TERMS]),
            confidence=max(deterministic.confidence, canonical.confidence),
        )

    def _should_call_model(self, message: str, update: IntentUpdate) -> tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        # Protocol boundary messages are already unambiguous and should never
        # spend a model call merely because they contain ordinary prose.
        if update.boundary_signal or update.no_preference or update.global_exhausted or update.reopen_clarification:
            return False, "explicit_boundary_or_exhaustion"
        mode = str(getattr(self.config, "intent_model_mode", "model_first")).lower()
        if mode in {"model_first", "canonicalizer"}:
            return True, mode
        reasons = _looks_like_ambiguous_message(message, update)
        if not reasons and update.confidence >= self.trigger_threshold:
            return False, "high_confidence_rules"
        return True, ";".join(reasons or ["low_rule_confidence"])

    def _messages(
        self,
        message: str,
        state: SessionState | Mapping[str, object] | None,
        turn: int,
        asked_attribute: str | None,
    ) -> list[dict[str, str]]:
        schema = {
            "scope": sorted(_SCOPES),
            "mutations": {
                "action": sorted(_ACTIONS),
                "attribute": sorted(ALLOWED_ATTRIBUTES),
                "polarity": sorted(_POLARITIES),
                "hardness": sorted(_HARDNESSES),
            },
            "max_mutations": _MAX_MUTATIONS,
            "max_query_terms": _MAX_QUERY_TERMS,
        }
        system = (
            "You are a constrained intent parser. Reply with one JSON object and "
            "nothing else. Use only the schema and enum values supplied. Never "
            "return a parent_asin, product ID, ranking, catalog field, or state "
            "command. A mutation is only an intent suggestion; the caller "
            "validates and merges it. Keep unresolved references as query_terms. "
            f"Schema contract: {json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
        )
        payload = {
            "message": _clean(message, 1200),
            "turn": max(int(turn), 0),
            "asked_attribute": _attribute_name(asked_attribute) if asked_attribute else None,
            "state": _snapshot(state, max_recent_turns=self.max_recent_turns),
        }
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:8000],
            },
        ]

    def _canonical_messages(
        self,
        message: str,
        state: SessionState | Mapping[str, object] | None,
        turn: int,
        asked_attribute: str | None,
    ) -> list[dict[str, str]]:
        system = (
            "Rewrite the customer's message into the existing shopping agent's "
            "canonical structured language. Reply with one JSON object only: "
            '{"canonical_text":"..."}. Use only facts explicitly present in the '
            "customer message and preserve negation, removal, and override intent. "
            "Every output value must be supported by words or numbers in the CURRENT customer message. "
            "Never copy a value from profile, state, an earlier turn, or your own prior answer. "
            "The canonical_text MUST contain one approved template per line, with no semicolons "
            "and no prose outside templates. Allowed fields are category, brand, budget, rating, "
            "rating_count, color, material, size, style, feature, and use_case. For review counts "
            "use 'rating_count: 250 or more'; for a product-name keyword use the feature syntax "
            "'title contains: mojo'. Use exactly lines such as "
            "'For that, what matters is: category: Dresses.', 'A key requirement is: rating: "
            "5.0 stars or higher.', 'For that, what matters is: rating_count: 250 or more.', "
            "'For that, what matters is: feature: title contains: mojo.', 'I do not want color: black.', "
            "'The color preference no longer matters.', or 'Actually, change the brand to Skechers.'. Use "
            "'Actually, forget everything.' only for an explicit global reset. Never invent a "
            "field label and never output product IDs."
        )
        payload = {
            "message": _clean(message, 1200),
            "turn": max(int(turn), 0),
            "asked_attribute": _attribute_name(asked_attribute) if asked_attribute else None,
        }
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:8000]},
        ]

    @staticmethod
    def _summary(mutation: ConstraintMutation | Mapping[str, object]) -> dict[str, object]:
        if isinstance(mutation, Mapping):
            return {
                "action": mutation.get("action"),
                "attribute": mutation.get("attribute"),
                "value": mutation.get("value", ""),
                "polarity": mutation.get("polarity"),
                "hardness": mutation.get("hardness"),
                "confidence": mutation.get("confidence"),
            }
        return {
            "action": mutation.action,
            "attribute": mutation.attribute,
            "value": mutation.value,
            "polarity": mutation.polarity,
            "hardness": mutation.hardness,
            "confidence": mutation.confidence,
        }

    def _merge(
        self,
        message: str,
        deterministic: IntentUpdate,
        model: Mapping[str, object],
        *,
        turn: int,
        state: SessionState | Mapping[str, object] | None = None,
    ) -> tuple[IntentUpdate, tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
        model_confidence = float(model.get("confidence", 0.0))
        accepted: list[dict[str, object]] = []
        rejected: list[dict[str, object]] = []
        model_scope = str(model.get("scope", "none"))
        scope: IntentScope = deterministic.scope
        if scope == "none" and model_scope != "none":
            if model_scope == "global_reset":
                # A model cannot invent a destructive reset.  The deterministic
                # parser must have seen the explicit reset language.
                rejected.append({"field": "scope", "value": model_scope, "reason": "destructive_scope_not_explicit"})
                model_scope = "none"
            elif model_scope == "attribute_replace" and not _CHANGE_RE.search(message):
                rejected.append({"field": "scope", "value": model_scope, "reason": "replacement_not_supported"})
                model_scope = "none"
            elif model_scope == "referenced_preference_replace" and not (
                _REFERENCE_RE.search(message) or _REFERENCE_OVERRIDE_RE.search(message)
            ):
                rejected.append({"field": "scope", "value": model_scope, "reason": "reference_not_supported"})
                model_scope = "none"
            else:
                scope = model_scope  # type: ignore[assignment]
                accepted.append({"field": "scope", "value": scope})
        elif scope != "none" and model_scope not in {"none", scope}:
            rejected.append({"field": "scope", "value": model_scope, "reason": "deterministic_scope_wins"})

        mutations: list[ConstraintMutation] = list(deterministic.mutations)
        deterministic_keys = {
            (
                _attribute_name(item.attribute),
                normalize_constraint_value(item.attribute, item.value).lower(),
            )
            for item in deterministic.mutations
            if item.action != "remove" and item.value
        }
        deterministic_removals = {
            _attribute_name(item.attribute)
            for item in deterministic.mutations
            if item.action == "remove"
        }
        raw_mutations = model.get("mutations", [])
        catalog_lexical_terms: list[str] = []
        if not isinstance(raw_mutations, list):
            raw_mutations = []
        for raw in raw_mutations:
            if not isinstance(raw, Mapping):
                continue
            attribute = str(raw.get("attribute", ""))
            value = str(raw.get("value", ""))
            action = str(raw.get("action", "upsert"))
            polarity = str(raw.get("polarity", "prefer"))
            hardness = str(raw.get("hardness", "soft"))
            item_confidence = min(model_confidence, float(raw.get("confidence", model_confidence)))
            summary = self._summary(raw)
            message_lower = message.casefold()
            value_normalized = normalize_constraint_value(attribute, value)
            # Models often use replace for an ordinary preference.  Treat it
            # as a non-destructive upsert unless the user explicitly asked to
            # change/switch/replace an existing value.
            if action == "replace" and scope == "none" and not _CHANGE_RE.search(message):
                action = "upsert"
            if attribute in {"other", "feature"} and value_normalized:
                brand_markers = ("brand", "label", "made by", "from ", " by ")
                if any(marker in message_lower for marker in brand_markers):
                    attribute = "brand"
            if attribute == "other" and value_normalized and any(
                marker in message_lower for marker in ("prioritize", "look for", "prefer")
            ):
                # Catalog labels are brands in the shopping domain; preserve
                # the original value while giving downstream ranking a slot.
                attribute = "brand"
            key = (
                _attribute_name(attribute),
                normalize_constraint_value(attribute, value).lower(),
            )
            if key in deterministic_keys:
                accepted.append({**summary, "reason": "deterministic_fact_preserved"})
                continue
            if item_confidence < _MIN_MODEL_CONFIDENCE:
                rejected.append({**summary, "reason": "below_confidence_floor"})
                continue
            if action == "remove":
                # A model may not invent destructive slot changes.  The rule
                # parser must have independently recognized a removal for the
                # same attribute; this also keeps a vague model ``remove``
                # response from clearing an unrelated active preference.
                removal_explicit = bool(
                    _REFERENCE_OVERRIDE_RE.search(message)
                    or re.search(r"\b(?:remove|drop|ignore|forget|no longer|instead of)\b", message, re.I)
                )
                state_has_attribute = False
                if isinstance(state, SessionState):
                    state_has_attribute = any(
                        str(item.attribute) == _attribute_name(attribute)
                        for item in state.active_constraints
                    )
                elif isinstance(state, Mapping):
                    raw_constraints = state.get("constraints", [])
                    state_has_attribute = any(
                        isinstance(item, Mapping)
                        and str(item.get("attribute", "")) == _attribute_name(attribute)
                        for item in raw_constraints if isinstance(raw_constraints, list)
                    )
                if _attribute_name(attribute) not in deterministic_removals and not (
                    removal_explicit and state_has_attribute
                ):
                    rejected.append({**summary, "reason": "remove_not_supported_by_message"})
                    continue
            elif action == "replace" and scope == "none":
                # Replacement is destructive in the reducer.  It is allowed
                # only inside a deterministic scoped override; a model-only
                # preference must be an upsert instead.
                rejected.append({**summary, "reason": "replacement_not_supported"})
                continue
            if (
                action != "remove"
                and not self._catalog_value_supported(attribute, value)
            ):
                rejected.append(
                    {
                        **summary,
                        "attribute": _attribute_name(attribute),
                        "reason": "catalog_value_not_supported",
                    }
                )
                if _supported_by_message(message, value):
                    catalog_lexical_terms.append(value)
                continue
            if polarity == "avoid" and not _has_explicit_negation(message, value):
                # Avoid mutations feed the candidate eligibility path; a
                # model must not create one without explicit user evidence.
                rejected.append({**summary, "reason": "avoid_not_explicit"})
                continue
            # Model-only values are always soft, even when the surrounding
            # message contains a generic ``need`` marker.  The marker may
            # establish that the customer cares about something, but it does
            # not prove that an inferred synonym is a hard requirement.
            effective_hardness: Literal["hard", "soft"] = "soft"
            effective_polarity: Literal["prefer", "avoid", "require"] = "prefer"
            if polarity == "avoid" and _has_explicit_negation(message, value):
                effective_polarity = "avoid"
            elif polarity == "prefer":
                effective_polarity = "prefer"
            if action == "remove":
                effective_polarity = "prefer"
                effective_hardness = "soft"
            explicit = bool(value_normalized and value_normalized.casefold() in message_lower)
            disclosure: EvidenceKind = "override" if scope != "none" else "direct"
            mutation = ConstraintMutation(
                action=action,  # type: ignore[arg-type]
                attribute=attribute,
                value=value,
                polarity=effective_polarity,
                hardness=effective_hardness,
                source="model",
                confidence=max(0.0, min(1.0, item_confidence)),
                disclosure_kind=disclosure,
                explicit=explicit,
            )
            mutations.append(mutation)
            accepted.append({**summary, "attribute": attribute, "action": action, "explicit": explicit, "effective_hardness": effective_hardness, "effective_polarity": effective_polarity})

        category = deterministic.category_anchor
        model_category = model.get("category_anchor")
        if category is None and isinstance(model_category, str) and model_category and model_confidence >= _MIN_MODEL_CONFIDENCE:
            # Category inference is still bounded to the model's validated
            # text field.  It may supply a semantic anchor (for example,
            # ``something for the trail`` -> ``outdoor``); it is never an ID
            # or a direct catalog selection.
            if self._catalog_value_supported("category", model_category):
                category = model_category
                accepted.append({"field": "category_anchor", "value": category})
            else:
                rejected.append(
                    {
                        "field": "category_anchor",
                        "value": model_category,
                        "reason": "catalog_value_not_supported",
                    }
                )
                if _supported_by_message(message, model_category):
                    catalog_lexical_terms.append(model_category)

        no_preference = set(deterministic.no_preference)
        model_no_preference = model.get("no_preference", [])
        if isinstance(model_no_preference, list):
            # Slot exhaustion/removal remains a destructive operation.  Only
            # carry a model field through when the deterministic parser has
            # identified the same attribute in the user's message.
            for item in model_no_preference:
                attribute = str(item)
                if attribute in deterministic.no_preference:
                    no_preference.add(attribute)
                elif attribute in ALLOWED_ATTRIBUTES:
                    rejected.append(
                        {
                            "field": "no_preference",
                            "value": attribute,
                            "reason": "no_preference_not_explicit",
                        }
                    )
        query_terms: list[str] = list(deterministic.query_terms)
        model_terms = model.get("query_terms", [])
        model_query_terms: list[str] = []
        if isinstance(model_terms, list):
            for raw_term in model_terms:
                term = str(raw_term)
                if not term or term.lower() in {item.lower() for item in query_terms}:
                    continue
                if model_confidence < _MIN_MODEL_CONFIDENCE:
                    rejected.append({"field": "query_terms", "value": term, "reason": "below_confidence_floor"})
                    continue
                query_terms.append(term)
                model_query_terms.append(term)
                accepted.append({"field": "query_terms", "value": term})
        for term in catalog_lexical_terms:
            if term.lower() in {item.lower() for item in query_terms}:
                continue
            query_terms.append(term)
            model_query_terms.append(term)
            accepted.append(
                {
                    "field": "query_terms",
                    "value": term,
                    "reason": "catalog_rejection_kept_as_lexical_evidence",
                }
            )
        query_terms = query_terms[:12]
        evidence = list(deterministic.query_evidence)
        existing_evidence = {item.text.lower() for item in evidence}
        for term in model_query_terms:
            if term.lower() in existing_evidence:
                continue
            evidence.append(
                QueryEvidence(
                    text=term,
                    turn=int(turn),
                    kind="override" if scope != "none" else "direct",
                    attribute_hint=_attribute_name(term),
                    confidence=max(0.0, min(1.0, model_confidence)),
                    source="model",
                )
            )
            existing_evidence.add(term.lower())
        return (
            IntentUpdate(
                global_override=deterministic.global_override or scope != "none",
                mutations=tuple(mutations[:_MAX_MUTATIONS]),
                category_anchor=category,
                no_preference=frozenset(no_preference),
                global_exhausted=deterministic.global_exhausted,
                boundary_signal=deterministic.boundary_signal,
                reopen_clarification=deterministic.reopen_clarification,
                query_terms=tuple(query_terms),
                confidence=max(deterministic.confidence, model_confidence),
                scope=scope,
                query_evidence=tuple(evidence[:_MAX_QUERY_TERMS]),
            ),
            tuple(accepted),
            tuple(rejected),
        )

    def interpret(
        self,
        message: object,
        state: SessionState | Mapping[str, object] | None = None,
        turn: int = 0,
        asked_attribute: str | None = None,
        *,
        snapshot: SessionState | Mapping[str, object] | None = None,
    ) -> IntentInterpretation:
        """Return one validated interpretation; never mutate ``state``."""

        started = time.perf_counter()
        if snapshot is not None and state is None:
            state = snapshot
        text = _clean(message, 1200)
        if asked_attribute is None and state is not None:
            if isinstance(state, Mapping):
                raw_asked = state.get("asked_attributes", [])
                if isinstance(raw_asked, list) and raw_asked:
                    asked_attribute = str(raw_asked[-1])
            elif state.asked_attributes:
                asked_attribute = state.asked_attributes[-1]
        raw_deterministic = parse_intent_update(
            text, turn=max(int(turn), 0), asked_attribute=asked_attribute
        )
        # The parser itself stays catalog-independent for offline reuse.  When
        # a repository is available, promote exact local values to typed
        # evidence before model invocation; this frozen enriched update is also
        # the atomic fallback if the model fails.
        deterministic = enrich_catalog_intent(
            raw_deterministic,
            text,
            self.catalog_repository,
            source="rule",
            confidence=0.94,
        )
        should_call, reason = self._should_call_model(text, deterministic)
        canonicalizer_mode = str(
            getattr(self.config, "intent_model_mode", "rules_first")
        ).lower() == "canonicalizer"
        fallback_update = deterministic
        if not should_call:
            result = IntentInterpretation(
                update=deterministic,
                path="rules",
                scope=deterministic.scope,
                confidence=deterministic.confidence,
                trigger_reason=reason,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
            self.last_interpretation = result
            return result

        try:
            client = self._model_client()
            complete_json = getattr(client, "complete_json", None)
            if not callable(complete_json):
                raise RuntimeError("intent model client has no complete_json method")
            completion = complete_json(
                self._canonical_messages(text, state, int(turn), asked_attribute)
                if canonicalizer_mode
                else self._messages(text, state, int(turn), asked_attribute),
                self._validate_canonical_completion
                if canonicalizer_mode
                else validate_intent_json,
            )
            value, backend, usage, failures = _completion_parts(completion)
            if backend is None or value is None:
                result = IntentInterpretation(
                    update=fallback_update,
                    path="rules_fallback",
                    scope=fallback_update.scope,
                    confidence=fallback_update.confidence,
                    trigger_reason=reason,
                    backend=None,
                    usage=None,
                    failures=failures,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )
                self.last_interpretation = result
                return result
            # TieredModelClient normally validates before returning.  Repeat
            # validation for duck-typed clients so custom integrations cannot
            # bypass the strict contract.
            if canonicalizer_mode:
                canonical_text = self._validate_canonical_completion(value)
                normalized_update = parse_intent_update(canonical_text, turn=int(turn), asked_attribute=asked_attribute)
                if not self._canonical_update_supported_by_message(
                    text, normalized_update
                ):
                    raise ValueError(
                        "canonical facts are not supported by the current customer message"
                    )
                # A canonicalizer may mention both the current product type
                # and a rejected/earlier type.  When the deterministic parser
                # already recognized the explicit ``looking for X`` anchor,
                # keep that anchor authoritative and use the model only to
                # fill additional slots.
                if (
                    deterministic.category_anchor
                    and normalized_update.category_anchor
                    and normalized_update.category_anchor.lower()
                    != deterministic.category_anchor.lower()
                ):
                    rejected_category = normalized_update.category_anchor.lower()
                    normalized_update = replace(
                        normalized_update,
                        category_anchor=deterministic.category_anchor,
                        mutations=tuple(
                            item
                            for item in normalized_update.mutations
                            if str(item.attribute).lower() != "category"
                        ),
                        query_terms=tuple(
                            item
                            for item in normalized_update.query_terms
                            if item.lower() != rejected_category
                        ),
                        query_evidence=tuple(
                            item
                            for item in normalized_update.query_evidence
                            if item.text.lower() != rejected_category
                        ),
                    )
                normalized_update = self._merge_canonical_update(
                    deterministic, normalized_update
                )
                result = IntentInterpretation(
                    update=normalized_update,
                    path="model",
                    scope=normalized_update.scope,
                    confidence=1.0,
                    trigger_reason=reason,
                    accepted=({"canonical_text": canonical_text},),
                    backend=backend,
                    usage=usage,
                    failures=failures,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )
                self.last_interpretation = result
                return result
            normalized = validate_intent_json(value)
            merged, accepted, rejected = self._merge(
                text, deterministic, normalized, turn=int(turn), state=state
            )
            result = IntentInterpretation(
                update=merged,
                path="model",
                scope=merged.scope,
                confidence=merged.confidence,
                trigger_reason=reason,
                accepted=accepted,
                rejected=rejected,
                backend=backend,
                usage=usage,
                failures=failures,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
            self.last_interpretation = result
            return result
        except Exception as exc:
            failure = BackendFailure(
                backend="intent-model",
                stage="adapter",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            result = IntentInterpretation(
                update=fallback_update,
                path="rules_fallback",
                scope=fallback_update.scope,
                confidence=fallback_update.confidence,
                trigger_reason=reason,
                failures=(failure,),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
            self.last_interpretation = result
            return result

    parse = interpret


__all__ = [
    "IntentInterpretation",
    "IntentInterpretationResult",
    "IntentInterpreter",
    "InterpretationPath",
    "validate_canonical_json",
    "validate_intent_json",
    "validate_intent_response",
    "validate_model_intent",
]
