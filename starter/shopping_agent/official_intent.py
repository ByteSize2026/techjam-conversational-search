"""Frozen deterministic adapter for generated official-evaluator messages.

Natural-language parsing evolves independently in :mod:`intent`.  This module
keeps the narrower official protocol's update construction stable while both
profiles share every downstream pipeline component.
"""

from __future__ import annotations

import re

from .intent import (
    _BOUNDARY_RE,
    _CLARIFICATION_REQUEST_RE,
    _GLOBAL_EXHAUSTED_RE,
    _RECOMMENDATION_REJECTION_RE,
    _extract_category,
    _extract_no_preference,
    _extract_value_after_marker,
    _intent_scope,
    _query_evidence_kind,
    _replacement_details,
)
from .state import (
    ConstraintMutation,
    IntentUpdate,
    QueryEvidence,
    _BUDGET_RE,
    _COLORS,
    _MATERIALS,
    _attribute_from_text,
    _clean,
    _split_values,
)


def parse_official_intent_update(message: object, *, turn: int = 0) -> IntentUpdate:
    """Parse one official message with the pre-natural-language semantics."""

    text = _clean(message, 1200)
    if not text:
        return IntentUpdate()
    scope = _intent_scope(text)
    override = scope != "none"
    no_preference = _extract_no_preference(text)
    global_exhausted = bool(_GLOBAL_EXHAUSTED_RE.search(text))
    if re.search(
        r"\b(?:don't|do not)\s+have\s+an?\s+additional\s+preference\s+"
        r"(?:for|on|about)\s+other\b",
        text,
        re.I,
    ):
        global_exhausted = True
    if global_exhausted and re.search(
        r"\b(?:no|without)\s+(?:an?\s+)?(?:additional\s+)?preference\s+"
        r"(?:for|on|about)\s+(?:material|color|colour|size|style|brand|budget|feature|use|use_case|category)\b",
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

    if marker_value:
        for value in _split_values(marker_value)[:4]:
            mutations.append(
                ConstraintMutation(
                    action="upsert",
                    attribute=_attribute_from_text(value),
                    value=value,
                    hardness=(
                        "hard"
                        if override or "requirement" in text.lower()
                        else "soft"
                    ),
                    source="rule",
                    confidence=0.92 if override else 0.86,
                    disclosure_kind=evidence_kind,
                )
            )

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
                disclosure_kind="override" if override else "direct",
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
                disclosure_kind="initial" if initial_provisional else evidence_kind,
            )
        )

    if not mutations and not no_preference:
        for pattern, attribute in (
            (r"\b(?:in|with|made\s+of)\s+(%s)\b" % "|".join(_COLORS), "color"),
            (
                r"\b(?:in|with|made\s+of)\s+(%s)\b" % "|".join(_MATERIALS),
                "material",
            ),
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

    if scope != "none":
        replacement_text = marker_value or (replacement[1] if replacement else "")
        query_terms = (
            tuple(_clean(value, 100) for value in _split_values(replacement_text)[:8])
            if replacement_text
            else tuple()
        )
    else:
        query_text = marker_value or text
        query_terms = tuple(
            _clean(value, 100) for value in _split_values(query_text)[:8]
        )
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


__all__ = ["parse_official_intent_update"]
