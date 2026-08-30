"""LLM Value Node contract: pydantic schemas + the shared retry/fallback helper.

``08-28-agent-v2-router-value-node`` Phase 5 (design.md Section 5).  Every
LLM Value Node in ``graph.py`` (``ClassifyIntent``, ``ExtractConstraints``,
``DistillProfile``, ``AskAttribute`` wording, ``Explain``, ``Compare`` --
``SemanticRank`` reuses ``LLMSemanticRanker`` directly, see its own docstring
in ``graph.py``) is a single-purpose, structured-input/structured-output
model call that goes through exactly one shared mechanism:
:func:`call_llm_value_node`.

Design.md Section 5's contract, concretely:

1. **Minimal input**: each node's own small pydantic *input* shape (built by
   the calling node in ``graph.py``, not here -- input construction needs
   ``GraphState``/``SessionState`` access this module deliberately does not
   have) -- never the full ``SessionState``.
2. **Structured output only**: a pydantic *output* model below.
   ``model_json_schema()`` is embedded directly in the system prompt so the
   schema is the model's only stated degree of freedom (Section 5, item 4).
   Parse/validate failure retries once with the *same* input; if that also
   fails, :func:`call_llm_value_node` returns ``None`` and the calling node
   takes its own deterministic fallback branch (Section 5, item 2).  This is
   deliberately the *only* place that retry-once-then-fallback rule is
   implemented -- every node routes through it rather than reimplementing it
   (Section 5.1, Section 9's "shared VL retry/fallback helper test").
3. **Single responsibility**: one model per node, one question each.
4. **No exposed tool list**: the model is never shown a list of callable
   actions; its schema is its entire action space.

Retry accounting (Section 5.1): :func:`call_llm_value_node` issues at most
two ``TieredModelClient.complete_json`` calls (the schema-retry layer this
module owns).  Each of those calls already tries every configured backend
once on its own (``TieredModelClient``'s transport-tier retry, unchanged).
The two layers do not compound into unbounded attempts: worst case per node
call is ``(configured backend count) * 2``.

"No model configured" (``client`` is ``None``, or the caller's
``GraphServices.llm_nodes_enabled`` flag is off -- see ``graph.py``) reuses
this exact same return-``None``-then-fallback path rather than a second
mechanism (design.md Section 10).
"""

from __future__ import annotations

import json
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .model import TieredModelClient
from .state import Constraint, ConstraintMutation, IntentUpdate, SessionState

# ---------------------------------------------------------------------------
# Output schemas -- one pydantic model per LLM Value Node (design.md Section
# 4/Section 5.4-5.6).  ``extra="ignore"`` keeps a chatty backend's stray
# fields from becoming a validation failure; the fields actually declared
# below remain the model's entire contract.
# ---------------------------------------------------------------------------


class ConstraintMutationModel(BaseModel):
    """One ``ExtractConstraints`` mutation entry.

    Deliberately a narrower action space than ``state.ConstraintMutation``
    (no ``"replace"``) -- ``upsert``/``remove`` covers every case the v1
    keyword parser (``parse_intent_update``) already produces, and a smaller
    enum is a smaller failure surface for the model to get wrong.
    """

    model_config = ConfigDict(extra="ignore")

    action: Literal["upsert", "remove"] = "upsert"
    attribute: str
    value: str = ""
    polarity: Literal["prefer", "avoid", "require"] = "prefer"
    hardness: Literal["hard", "soft"] = "soft"
    confidence: float = 0.85


class ExtractConstraintsOutput(BaseModel):
    """design.md Section 5.4: reuses ``IntentUpdate``'s shape as the output
    contract -- same fields, pydantic-typed so they can be requested as
    structured output and validated before ever reaching ``StateReducer``.
    """

    model_config = ConfigDict(extra="ignore")

    global_override: bool = False
    mutations: list[ConstraintMutationModel] = Field(default_factory=list)
    category_anchor: str | None = None
    no_preference: list[str] = Field(default_factory=list)
    query_terms: list[str] = Field(default_factory=list)
    confidence: float = 0.8


class ClassifyIntentOutput(BaseModel):
    """design.md Section 5.5 -- exact schema, closed enum."""

    model_config = ConfigDict(extra="ignore")

    intent: Literal["compare_details", "refine_search", "new_search", "confirm_choice"]
    target_ids: list[str] = Field(default_factory=list)


class AskAttributeOutput(BaseModel):
    """Wording only -- the attribute and ``mode`` are already Router-chosen
    (design.md Section 4); this model's only degree of freedom is the
    sentence itself.
    """

    model_config = ConfigDict(extra="ignore")

    question_text: str = Field(min_length=1, max_length=300)


class DistillProfileOutput(BaseModel):
    """design.md Section 5.6 -- deliberately shallow, 2 soft fields, not
    designed for extension yet (explicit scope decision, ``prd.md``
    Out of Scope).
    """

    model_config = ConfigDict(extra="ignore")

    price_sensitivity: str | None = Field(default=None, max_length=60)
    style_signal: str | None = Field(default=None, max_length=60)


class ExplainOutput(BaseModel):
    """Generation only -- wording over facts ``Rank``/``SemanticRank``
    already computed; never a new product claim.
    """

    model_config = ConfigDict(extra="ignore")

    # 400 rejected valid live output; schema-in-prompt doesn't reliably bound length (see f1_verify_20260829_233954).
    message: str = Field(min_length=1, max_length=600)


class CompareOutput(BaseModel):
    """Generation only -- wording over ``FetchDetails``'s structured facts."""

    model_config = ConfigDict(extra="ignore")

    message: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------------------
# Shared retry-once-then-fallback helper (design.md Section 5.1, Section 9).
# ---------------------------------------------------------------------------

_OutputT = TypeVar("_OutputT", bound=BaseModel)

_USER_CONTENT_LIMIT = 4000


def _schema_instructions(output_model: type[BaseModel]) -> str:
    schema = output_model.model_json_schema()
    return (
        "Respond with a single JSON object only (no markdown fences, no "
        "commentary) that validates against this JSON Schema: "
        + json.dumps(schema, ensure_ascii=False, sort_keys=True)
    )


def call_llm_value_node(
    client: TieredModelClient | None,
    *,
    task_prompt: str,
    user_payload: object,
    output_model: type[_OutputT],
) -> _OutputT | None:
    """Design.md Section 5's single shared VL-node call path.

    Returns a validated ``output_model`` instance on success, or ``None`` if
    no model is usable or two attempts both fail to produce a valid,
    schema-conforming response.  ``None`` is the uniform signal every VL
    node/Router in ``graph.py`` reads as "take the deterministic fallback
    branch" -- there is no second failure signal to special-case.
    """

    if client is None or not getattr(client, "backends", None):
        return None

    system_prompt = f"{task_prompt.strip()}\n\n{_schema_instructions(output_model)}"
    content = (
        user_payload
        if isinstance(user_payload, str)
        else json.dumps(user_payload, ensure_ascii=False, sort_keys=True, default=str)
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content[:_USER_CONTENT_LIMIT]},
    ]

    def _validator(value: object) -> _OutputT:
        return output_model.model_validate(value)

    # Design.md Section 5.1: retry once with the same input, then fall back.
    # Each ``complete_json`` call below already exhausts every configured
    # backend on its own (TieredModelClient's transport-tier retry).
    for _attempt in range(2):
        result = client.complete_json(messages, _validator)
        if result.success and isinstance(result.value, output_model):
            return result.value
    return None


# ---------------------------------------------------------------------------
# ExtractConstraints: output -> IntentUpdate.  Tightly coupled to both
# schemas, so the conversion lives next to them rather than in graph.py.
# ---------------------------------------------------------------------------


def _normalize_attribute_guess(value: object) -> str:
    """Best-effort attribute-name normalization for the *lookup* below.

    Not the authoritative normalizer -- ``StateReducer.apply`` re-derives
    and validates every mutation's attribute against ``ALLOWED_ATTRIBUTES``
    independently (``state._attribute_name``).  This is only used to find a
    same-turn active constraint's existing value for an empty-``value``
    "remove" mutation (mirroring ``graph._loosen_constraints_node``'s own
    "carry the value along or the reducer silently drops it" note); a missed
    lookup degrades to that mutation being skipped, not a crash.
    """

    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def extract_constraints_output_to_intent_update(
    output: ExtractConstraintsOutput,
    *,
    session: SessionState,
) -> IntentUpdate:
    active_by_attribute: dict[str, Constraint] = {
        item.attribute: item for item in session.active_constraints
    }
    mutations: list[ConstraintMutation] = []
    for item in output.mutations[:8]:
        attribute = _normalize_attribute_guess(item.attribute)
        if not attribute:
            continue
        value = item.value.strip()
        if not value and item.action == "remove":
            existing = active_by_attribute.get(attribute)
            if existing is not None:
                value = existing.value
        if not value:
            continue
        mutations.append(
            ConstraintMutation(
                action=item.action,
                attribute=attribute,
                value=value,
                polarity=item.polarity,
                hardness=item.hardness,
                source="model",
                confidence=max(0.0, min(1.0, float(item.confidence))),
            )
        )
    category_anchor = (output.category_anchor or "").strip() or None
    no_preference = frozenset(
        normalized
        for normalized in (_normalize_attribute_guess(value) for value in output.no_preference)
        if normalized
    )
    query_terms = tuple(
        cleaned[:100]
        for cleaned in (str(value or "").strip() for value in output.query_terms[:8])
        if cleaned
    )
    return IntentUpdate(
        global_override=bool(output.global_override),
        mutations=tuple(mutations),
        category_anchor=category_anchor,
        no_preference=no_preference,
        query_terms=query_terms,
        confidence=max(0.0, min(1.0, float(output.confidence))),
    )


# ---------------------------------------------------------------------------
# Task prompts -- one line per node's single question (design.md Section
# 5.1's "minimal input" rule applies to the *system* prompt too: each string
# below states only the one question that node answers).
# ---------------------------------------------------------------------------

EXTRACT_CONSTRAINTS_PROMPT = (
    "You extract shopping constraints from one customer message in a "
    "multi-turn conversation, into the given output fields. Only report "
    "what the message actually supports; never invent a value that isn't "
    "there. Fill each field using its own rule below -- do not skip a "
    "field just because the wording is unusual; an unusual-looking value "
    "copied verbatim is not the same as an invented one.\n\n"
    "- category_anchor: the product category or type the customer names, "
    "copied verbatim. It may be several words (e.g. 'winter running "
    "shoes', not shortened to 'shoes'); do not move it into query_terms "
    "instead. Null if no category or product type is named at all.\n\n"
    "- mutations: one entry per attribute value the customer states. Each "
    "entry's `attribute` must be one of: category, material, color, size, "
    "style, brand, budget, feature, use_case, other. When "
    "`answering_attribute` is given, the message is a direct reply to a "
    "question about that attribute:\n"
    "  * If the reply declines (e.g. 'no preference', 'any is fine'), do "
    "not report a mutation for it -- see no_preference below instead.\n"
    "  * Otherwise report a mutation for `answering_attribute`. Use the "
    "customer's own words for the value rather than paraphrasing them into "
    "different wording, even if the value itself looks unusual (e.g. text "
    "that reads like it was copied from a product listing) -- an unusual "
    "value is still a real value, not something to discard. This means "
    "the shortest phrase that actually names the value, not the whole "
    "sentence: drop lead-in words like 'what matters is', and if the "
    "reply lists more than one related detail (e.g. separated by a "
    "semicolon), take the core value as the mutation and put any leftover "
    "detail in query_terms instead of concatenating everything into one "
    "value. Reporting nothing is wrong whenever the customer stated "
    "something.\n"
    "  * Exception: if the reply plainly names a different, unrelated "
    "kind of requirement instead of answering what was asked (e.g. an "
    "override marker like 'actually, what I need is: X' introducing a "
    "requirement the question never asked about, such as a care "
    "instruction when a size was asked), classify it by the reply's own "
    "content instead of `answering_attribute`.\n\n"
    "- global_override: true only if the customer is abandoning the "
    "product category itself for something unrelated (e.g. switching "
    "from jackets to running shoes). Swapping out one or more attribute "
    "values within the same search stays false, even with phrases like "
    "'scratch that' or 'never mind' attached (e.g. 'scratch that, let's "
    "go with leather' while still shopping for the same jacket) -- "
    "report that as an `upsert`/`remove` mutation on just that attribute "
    "instead.\n\n"
    "- no_preference: attributes the customer explicitly declined to "
    "state a preference for.\n\n"
    "- query_terms: any other free-text descriptive words (features, use "
    "cases) the customer used that are not already captured by a "
    "mutation above -- short keywords, not full sentences."
)

CLASSIFY_INTENT_PROMPT = (
    "You classify a customer's follow-up message after recommendations "
    "were already shown. Pick exactly one intent: compare_details (the "
    "customer wants more detail on specific already-shown items -- list "
    "their parent_asin values in target_ids, using only IDs present in "
    "last_candidate_ids), refine_search (the customer is narrowing or "
    "adjusting the same search), new_search (the customer is starting an "
    "unrelated search), or confirm_choice (the customer is done and has "
    "picked/accepted an item)."
)

ASK_ATTRIBUTE_FILL_MISSING_PROMPT = (
    "Write one short, friendly clarification question asking the customer "
    "for their preference on the given attribute. Do not mention any other "
    "attribute or invent product facts."
)

ASK_ATTRIBUTE_RELAX_CONFLICT_PROMPT = (
    "The customer's current requirement on the given attribute matched no "
    "products. Write one short, friendly question asking whether they would "
    "like to relax or change that specific requirement so more options can "
    "be shown. Do not mention any other attribute or invent product facts."
)

DISTILL_PROFILE_PROMPT = (
    "You maintain a tiny long-term shopping profile from this turn's "
    "constraint changes and any rejection signal. Only fill a field when "
    "this turn's diff actually supports it; leave a field null if unsure. "
    "Do not restate the constraints themselves -- only soft signals about "
    "price sensitivity or style preference."
)

EXPLAIN_PROMPT = (
    "Write one short, friendly sentence introducing the shown "
    "recommendations to the customer, in light of their message. Reference "
    "only the product titles supplied; do not state a price, rating, or "
    "any other fact not given to you."
)

COMPARE_PROMPT = (
    "Write a short comparison of the given products for the customer, "
    "based only on the supplied fields. Do not state a fact that is not "
    "present in the input."
)


__all__ = [
    "ASK_ATTRIBUTE_FILL_MISSING_PROMPT",
    "ASK_ATTRIBUTE_RELAX_CONFLICT_PROMPT",
    "AskAttributeOutput",
    "CLASSIFY_INTENT_PROMPT",
    "COMPARE_PROMPT",
    "ClassifyIntentOutput",
    "CompareOutput",
    "ConstraintMutationModel",
    "DISTILL_PROFILE_PROMPT",
    "DistillProfileOutput",
    "EXPLAIN_PROMPT",
    "EXTRACT_CONSTRAINTS_PROMPT",
    "ExplainOutput",
    "ExtractConstraintsOutput",
    "call_llm_value_node",
    "extract_constraints_output_to_intent_update",
]
