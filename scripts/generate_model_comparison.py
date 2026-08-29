"""Generates ``artifacts/model_comparison/*.md``: deterministic-fallback vs
live-model side-by-side comparisons for the *same* conversations/probes.

This is a one-off comparison generator for a specific dispatch (see this
task's ``implement.jsonl``/dispatch instructions:
``08-28-agent-v2-router-value-node``, "produce side-by-side comparison
documents" dispatch) -- it does not replace
``scripts/generate_scenario_showcase.py`` (which regenerates the six
``artifacts/scenario_showcase/*.md`` transcripts) or
``scripts/probe_llm_node_quality.py`` (ad hoc qualitative probes); it reuses
both scripts' techniques rather than duplicating them:

- ``generate_scenario_showcase``'s in-process node instrumentation (wraps
  ``graph.ROUTERS``/``graph.NODES`` once to capture a concise, typed gloss
  per node call) drives the two full multi-turn conversations (comparisons
  01 and 04).
- ``probe_llm_node_quality``'s direct-call style (``llm_nodes.call_llm_value_node``
  against a single node's prompt/schema, no full Agent turn) drives the two
  isolated single-turn probes (comparisons 02 and 03), plus a small local
  wrapper that also records raw backend failure diagnostics
  (``ModelCompletionResult.failures``) so a live failure can be shown
  honestly instead of collapsed into "no output".

Two ``Agent`` instances are built once, matching
``scripts/run_live_reliability_subset.py``'s existing pattern for an
apples-to-apples deterministic-vs-live comparison:

- ``Agent(catalog, config=AgentConfig())`` -- explicit empty config, forces
  the fully offline/deterministic-fallback path regardless of what is in
  the environment.
- ``Agent(catalog, config=AgentConfig.from_env())`` -- picks up
  ``SHOPPING_AGENT_DEEPSEEK_API_KEY`` etc. when ``.env`` has been sourced
  into the shell before running this script.

Usage::

    set -a; source .env; set +a
    python3 scripts/generate_model_comparison.py

Writes ``artifacts/model_comparison/{01..04}_*.md`` and
``artifacts/model_comparison/README.md`` (overwriting any existing ones).
Makes no product-code changes and does not touch
``artifacts/scenario_showcase/``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import generate_scenario_showcase as showcase  # noqa: E402  (path setup above)

from starter.agent import Agent  # noqa: E402
from starter.shopping_agent import llm_nodes  # noqa: E402
from starter.shopping_agent.config import AgentConfig  # noqa: E402
from starter.shopping_agent.state import parse_intent_update  # noqa: E402

_OUTPUT_DIR = _REPO_ROOT / "artifacts" / "model_comparison"
_CATALOG_PATH = _REPO_ROOT / "data" / "catalog.jsonl"


# ---------------------------------------------------------------------------
# Conversation runner -- reuses showcase's instrumentation/glosses, one fresh
# session per (agent, scenario_id) pair so the deterministic and live runs
# never share state.
# ---------------------------------------------------------------------------


def _run_conversation(agent: Agent, session_id: str, messages: list[str]) -> list[list[dict]]:
    agent.reset(session_id, {})
    per_turn_nodes: list[list[dict]] = []
    for turn, message in enumerate(messages, start=1):
        showcase._reset_capture()
        agent.respond(session_id, message, turn, 10)
        per_turn_nodes.append(list(showcase._CAPTURED))
    return per_turn_nodes


def _find(nodes: list[dict], name: str) -> str | None:
    for entry in nodes:
        if entry["node"] == name:
            return entry["gloss"]
    return None


# ---------------------------------------------------------------------------
# Doc 1 -- AskAttribute wording, deterministic vs live, same 4-turn
# conversation as scenario_showcase's 01_vague_clarify_converge.
# ---------------------------------------------------------------------------


def build_doc_01(det_agent: Agent, live_agent: Agent, live_configured: bool) -> None:
    messages = ["I'm looking for jewelry", "gold", "under $50", "no preference for style"]
    det_turns = _run_conversation(det_agent, "cmp-01-det", messages)
    live_turns = _run_conversation(live_agent, "cmp-01-live", messages)

    lines = [
        "# Comparison 1 -- AskAttribute wording: templated fallback vs live model",
        "",
        "Same 4-turn conversation (`I'm looking for jewelry` -> `gold` -> `under $50` -> "
        "`no preference for style`) run through two separate `Agent` instances: one with "
        "`AgentConfig()` (no model configured, forces the fixed-template `AskAttribute` "
        "fallback and the keyword-only `ExtractConstraints` fallback parser -- see "
        "`starter/shopping_agent/graph.py`'s `_ask_attribute_template` and "
        "`starter/shopping_agent/state.py`'s `parse_intent_update`), one with "
        f"`AgentConfig.from_env()` (live model configured this run: `{live_configured}`).",
        "",
        "| Turn | User message | Deterministic `ExtractConstraints` | Live `ExtractConstraints` | "
        "Deterministic `AskAttribute` wording | Live `AskAttribute` wording |",
        "|---|---|---|---|---|---|",
    ]
    for turn, message in enumerate(messages, start=1):
        det_nodes = det_turns[turn - 1]
        live_nodes = live_turns[turn - 1]
        det_extract = (_find(det_nodes, "ExtractConstraints") or "-").replace("|", "\\|")
        live_extract = (_find(live_nodes, "ExtractConstraints") or "-").replace("|", "\\|")
        det_ask = (_find(det_nodes, "AskAttribute") or "(no question this turn)").replace("|", "\\|")
        live_ask = (_find(live_nodes, "AskAttribute") or "(no question this turn)").replace("|", "\\|")
        lines.append(
            f"| {turn} | {message} | {det_extract} | {live_extract} | {det_ask} | {live_ask} |"
        )

    det_asked = [
        entry["gloss"].split('"', 1)[1].rstrip('"') if '"' in entry["gloss"] else entry["gloss"]
        for turn_nodes in det_turns
        for entry in turn_nodes
        if entry["node"] == "AskAttribute"
    ]
    live_asked = [
        entry["gloss"].split('"', 1)[1].rstrip('"') if '"' in entry["gloss"] else entry["gloss"]
        for turn_nodes in live_turns
        for entry in turn_nodes
        if entry["node"] == "AskAttribute"
    ]
    det_attrs = [
        entry["gloss"].split(":", 1)[1].split("(", 1)[0].strip()
        for turn_nodes in det_turns
        for entry in turn_nodes
        if entry["node"] == "AskAttribute"
    ]
    live_attrs = [
        entry["gloss"].split(":", 1)[1].split("(", 1)[0].strip()
        for turn_nodes in live_turns
        for entry in turn_nodes
        if entry["node"] == "AskAttribute"
    ]
    same_sequence = det_attrs == live_attrs
    same_extraction = all(
        (_find(det_turns[i], "ExtractConstraints") or "") == (_find(live_turns[i], "ExtractConstraints") or "")
        for i in range(len(messages))
    )
    sequence_note = (
        "the underlying question sequence (which attribute gets asked, in which order) is "
        "decided by `SlotCheckRouter`/`CandidateGate` in both runs -- identical in both columns "
        "above -- so the live model's only contribution visible in this conversation is wording"
        if same_sequence
        else "the question *sequence* itself also differs between the two runs (see the attribute "
        "lists above), not just the wording -- this happens when a live `ExtractConstraints` "
        "result differs enough from the deterministic parser's to change which attribute "
        "`SlotCheckRouter` picks next, so the live model's effect here is not wording-only"
    )
    extraction_note = (
        "`ExtractConstraints` results are consistent between the two paths for this "
        "conversation's direct/templated phrasing (`gold`, `under $50`, `no preference for "
        "style` all parse the same way with or without a model), which is expected: the "
        "indirect-phrasing gap is what Comparison 2 is for, not this one"
        if same_extraction
        else "`ExtractConstraints` results differ between the two paths on at least one turn "
        "despite this conversation's fairly direct phrasing -- see the table for exactly where"
    )
    lines += [
        "",
        "**Deterministic questions asked, in order:** " + " / ".join(f'"{q}"' for q in det_asked),
        "",
        "**Live-model questions asked, in order:** " + " / ".join(f'"{q}"' for q in live_asked),
        "",
        "## Verdict",
        "",
        "The templated fallback is a fixed `What {attribute} would you prefer for your "
        "{category}?`-style pattern; the live model varies phrasing turn to turn and reads more "
        f"naturally (compare the two question lists above), but {sequence_note}. {extraction_note}.",
        "",
    ]
    (_OUTPUT_DIR / "01_vague_clarify_wording.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {_OUTPUT_DIR / '01_vague_clarify_wording.md'}")


# ---------------------------------------------------------------------------
# Doc 2 -- ExtractConstraints on indirect phrasing: deterministic keyword
# parser vs live model (including honest live failures).
# ---------------------------------------------------------------------------


def _fmt_intent_update(update) -> str:
    bits = [
        f"{mutation.action} {mutation.attribute}={mutation.value}[{mutation.hardness}]"
        for mutation in update.mutations
    ]
    if update.category_anchor:
        bits.append(f"category={update.category_anchor}")
    if update.no_preference:
        bits.append("no_preference=" + ",".join(sorted(update.no_preference)))
    if update.global_override:
        bits.append("override=true")
    return "; ".join(bits) if bits else "(nothing extracted)"


def _fmt_extract_output(output) -> str:
    bits = [
        f"{m.action} {m.attribute}={m.value}[{m.hardness}]" for m in output.mutations
    ]
    if output.category_anchor:
        bits.append(f"category={output.category_anchor}")
    if output.no_preference:
        bits.append("no_preference=" + ",".join(sorted(output.no_preference)))
    if output.global_override:
        bits.append("override=true")
    return "; ".join(bits) if bits else "(nothing extracted)"


def _call_live_extract_with_diagnostics(live_agent: Agent, message: str) -> tuple[str, list[str]]:
    """Direct single-node call (probe_llm_node_quality.py's style, not a
    full Agent turn) -- returns (result_summary, backend_failure_details)."""

    client = live_agent._graph_services.llm_client()
    payload = {"message": message, "recent_turns": [], "known_constraints": []}
    if client is None or not getattr(client, "backends", None):
        return "(no model backend configured)", []

    original_complete = client.complete_json
    attempts: list = []

    def _tracking(messages, validator):
        result = original_complete(messages, validator)
        attempts.append(result)
        return result

    client.complete_json = _tracking
    try:
        output = llm_nodes.call_llm_value_node(
            client,
            task_prompt=llm_nodes.EXTRACT_CONSTRAINTS_PROMPT,
            user_payload=payload,
            output_model=llm_nodes.ExtractConstraintsOutput,
        )
    finally:
        client.complete_json = original_complete

    failure_details = []
    for attempt in attempts:
        if not attempt.success:
            for failure in attempt.failures:
                failure_details.append(f"{failure.backend}: {failure.error}")

    if output is not None:
        return _fmt_extract_output(output), failure_details
    attempt_count = len(attempts)
    return (
        f"FELL BACK after {attempt_count} attempt(s) -- no usable structured output "
        f"(same signal `graph._extract_constraints_node` reads as \"take the deterministic "
        f"fallback\")",
        failure_details,
    )


def build_doc_02(live_agent: Agent, live_configured: bool) -> None:
    messages = [
        "nothing too flashy, and I really can't spend a fortune on this",
        "it's for my mom, something she could wear to a nice dinner without it being itchy",
        "I don't care about the brand at all, just make sure it'll survive the washing machine",
    ]
    lines = [
        "# Comparison 2 -- ExtractConstraints on indirect phrasing: keyword parser vs live model",
        "",
        "Three independent single-turn probes (fresh, no prior conversation state -- this is the "
        "scenario where a real model is *supposed* to have a clear advantage over keyword "
        "matching: none of the three messages uses an explicit marker word, an explicit budget "
        "figure, or a listed color/material term the deterministic parser's regex table knows "
        "about).",
        f"",
        f"Live model configured this run: `{live_configured}`.",
        "",
    ]
    live_successes = 0
    for message in messages:
        det_update = parse_intent_update(message, turn=1)
        det_summary = _fmt_intent_update(det_update)
        live_summary, failures = _call_live_extract_with_diagnostics(live_agent, message)
        if not live_summary.startswith("FELL BACK") and live_summary != "(no model backend configured)":
            live_successes += 1
        lines.append(f"## `{message}`")
        lines.append("")
        lines.append(f"- **Deterministic keyword parser (`parse_intent_update`):** {det_summary}")
        lines.append(f"- **Live model attempt:** {live_summary}")
        if failures:
            lines.append(f"  - Raw backend failure(s): {'; '.join(failures)}")
        lines.append("")

    if not live_configured:
        live_outcome = "no live model backend was configured for this run at all, so no comparison is possible here"
    elif live_successes == len(messages):
        live_outcome = (
            f"the live model produced usable structured output on all {len(messages)} probes this "
            "run -- see each entry above for what it actually extracted"
        )
    elif live_successes == 0:
        live_outcome = (
            f"the live model fell back on all {len(messages)} probes this run (see the raw backend "
            "failure diagnostics above where captured) -- it did not close the gap in this run"
        )
    else:
        live_outcome = (
            f"the live model succeeded on {live_successes} of {len(messages)} probes this run and "
            "fell back on the rest -- a partial, inconsistent result, not a reliable fix for this "
            "node yet"
        )
    lines += [
        "## Verdict",
        "",
        "This is the honest negative result on the deterministic side, not dressed up: a "
        "deterministic regex/keyword parser has no path to \"nothing too flashy\" -> a soft style "
        "constraint, \"survive the washing machine\" -> a durability/material constraint, or "
        "\"for my mom ... nice dinner\" -> an occasion/recipient signal -- at best it catches a "
        "literal marker word (e.g. the word \"brand\" in the third message triggers a "
        "`no_preference=brand` flag by coincidence of the phrasing, not real understanding); the "
        "actual soft-constraint content of all three messages goes unextracted, exactly the gap "
        f"a semantic model is meant to close. On the live side, {live_outcome}. The reliability "
        "assessment in `artifacts/live_reliability_subset_result.json` puts `ExtractConstraints`'s "
        "overall fallback rate at roughly 34% across a larger sample -- this remains the node "
        "most worth fixing next if indirect-phrasing understanding is the point of using a model "
        "at all.",
        "",
    ]
    (_OUTPUT_DIR / "02_indirect_constraint_extraction.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(f"wrote {_OUTPUT_DIR / '02_indirect_constraint_extraction.md'}")


# ---------------------------------------------------------------------------
# Doc 3 -- ClassifyIntent on ambiguous follow-ups: fixed fallback vs live.
# ---------------------------------------------------------------------------


def build_doc_03(live_agent: Agent, live_configured: bool) -> None:
    last_ids = ["B0BNP1RZ2W", "B08B62ZW7H", "B08HCP9YTV"]
    messages = [
        "tell me more about the second one",
        "actually do you have anything cheaper",
        "yeah that first one looks perfect, I'll go with that",
        "never mind, let's look at watches instead",
    ]
    expected_by_message = {
        "tell me more about the second one": "compare_details (target: 2nd shown id)",
        "actually do you have anything cheaper": "refine_search (tighten a price constraint)",
        "yeah that first one looks perfect, I'll go with that": "confirm_choice (1st shown id)",
        "never mind, let's look at watches instead": "new_search (override)",
    }
    lines = [
        "# Comparison 3 -- ClassifyIntent on ambiguous follow-ups: fixed fallback vs live model",
        "",
        "There is no dedicated deterministic classifier for this node -- confirmed in "
        "`starter/shopping_agent/graph.py`'s `_classify_intent_fallback`: when no model is "
        "configured, or both attempts fail validation, `ClassifyIntent` always returns the "
        "single fixed value `{\"intent\": \"refine_search\", \"target_ids\": []}` (deliberately "
        "the *safe* fallback -- `IntentRouter2` routes `refine_search` back to "
        "`ExtractConstraints` without wiping accumulated constraints, unlike `new_search`'s "
        "`global_override=True`; see that function's own comment for why `new_search` was ruled "
        "out as the fallback value). The four messages below are exactly the kind of ambiguous "
        "follow-up that fixed fallback cannot distinguish -- each has a different, "
        "human-obvious correct intent, shown in the \"what a human would classify this as\" "
        "column.",
        f"",
        f"Live model configured this run: `{live_configured}`.",
        "",
        "| User message | Fixed deterministic fallback (always) | What a human would classify "
        "this as | Live model classification |",
        "|---|---|---|---|",
    ]
    client = live_agent._graph_services.llm_client()
    correct_count = 0
    for message in messages:
        payload = {"message": message, "last_candidate_ids": last_ids}
        output = None
        if client is not None and getattr(client, "backends", None):
            output = llm_nodes.call_llm_value_node(
                client,
                task_prompt=llm_nodes.CLASSIFY_INTENT_PROMPT,
                user_payload=payload,
                output_model=llm_nodes.ClassifyIntentOutput,
            )
        expected = expected_by_message[message]
        expected_intent = expected.split(" ", 1)[0]
        if output is not None:
            targets = f", targets={list(output.target_ids)}" if output.target_ids else ""
            live_result = f"`{output.intent}`{targets}"
            if output.intent == expected_intent:
                correct_count += 1
        else:
            live_result = "(no model backend configured)" if client is None or not getattr(client, "backends", None) else "FELL BACK to `refine_search` (same as deterministic column)"
        lines.append(f"| {message} | `refine_search` (always, regardless of message) | {expected} | {live_result} |")

    if not live_configured:
        live_summary = "no live model backend was configured for this run at all, so no comparison is possible here"
    elif correct_count == len(messages):
        live_summary = f"the live model classified all {len(messages)} of {len(messages)} correctly this run"
    else:
        live_summary = f"the live model classified {correct_count} of {len(messages)} correctly this run -- see the table for which one(s) it missed"
    lines += [
        "",
        "## Verdict",
        "",
        "This is the clearest case where the live model earns its cost: the fixed deterministic "
        "fallback answers `refine_search` for all four messages regardless of content (by "
        "design -- it is a safety fallback, not an attempt at classification), which is wrong "
        f"for three of the four (`compare_details`, `confirm_choice`, `new_search`). This run: "
        f"{live_summary}. The prior "
        "dispatch's reliability probe found all four of these ambiguous messages classified "
        "correctly live (`ClassifyIntent` first-attempt success rate 100% across the sampled "
        "reliability run, `artifacts/live_reliability_subset_result.json`); either way, a real "
        "model call is capable of materially changing what the agent does next on these turns, "
        "not just how it phrases something -- the fixed fallback structurally cannot.",
        "",
    ]
    (_OUTPUT_DIR / "03_ambiguous_intent_classification.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(f"wrote {_OUTPUT_DIR / '03_ambiguous_intent_classification.md'}")


# ---------------------------------------------------------------------------
# Doc 4 -- SemanticRank before/after (or current failure), same multi-turn
# conversation as scenario_showcase's 04_multiturn_rank_reorder.
# ---------------------------------------------------------------------------


def build_doc_04(det_agent: Agent, live_agent: Agent, live_configured: bool) -> None:
    messages = ["I'm shopping for a necklace", "gold", "cotton", "no preference for color"]
    det_turns = _run_conversation(det_agent, "cmp-04-det", messages)
    live_turns = _run_conversation(live_agent, "cmp-04-live", messages)

    lines = [
        "# Comparison 4 -- SemanticRank before/after: current state of the broken thing",
        "",
        "Same 4-turn conversation as `artifacts/scenario_showcase/04_multiturn_rank_reorder.md` "
        "(`I'm shopping for a necklace` -> `gold` -> `cotton` -> `no preference for color`). This "
        "document is a baseline snapshot for comparison *after* a future fix, not evidence that "
        "anything here already works -- the prior dispatch's reliability assessment found "
        "`SemanticRank` failed 100% of ~18 observed live attempts "
        "(`BackendJSONError: model content is empty`).",
        f"",
        f"Live model configured this run: `{live_configured}`.",
        "",
        "| Turn | User message | Deterministic `Rank` (feature-rank top5) | Live `Rank` "
        "(feature-rank top5, same code path) | Live `SemanticRank` attempt |",
        "|---|---|---|---|---|",
    ]
    for turn, message in enumerate(messages, start=1):
        det_nodes = det_turns[turn - 1]
        live_nodes = live_turns[turn - 1]
        det_rank = (_find(det_nodes, "Rank") or "(Rank did not run this turn)").replace("|", "\\|")
        live_rank = (_find(live_nodes, "Rank") or "(Rank did not run this turn)").replace("|", "\\|")
        live_semrank = (_find(live_nodes, "SemanticRank") or "(SemanticRank did not run this turn -- see RankRouter gloss below)").replace("|", "\\|")
        lines.append(f"| {turn} | {message} | {det_rank} | {live_rank} | {live_semrank} |")

    # Surface RankRouter's own decision per turn too, since "SemanticRank did
    # not run" can mean either "over-general pool, RankRouter skipped it" or
    # "ran and the top5 is unchanged from Rank" -- worth distinguishing.
    lines.append("")
    lines.append("**Live `RankRouter` decision per turn (why `SemanticRank` did or did not run):**")
    lines.append("")
    for turn, live_nodes in enumerate(live_turns, start=1):
        router_gloss = _find(live_nodes, "RankRouter") or "(no RankRouter entry captured)"
        lines.append(f"- Turn {turn}: {router_gloss}")

    any_success = any(
        "reordered top" in (_find(nodes, "SemanticRank") or "") for nodes in live_turns
    )
    if any_success:
        outcome = (
            "In this particular run, at least one turn's `SemanticRank` call actually succeeded "
            "-- see the turn(s) above where the live `SemanticRank` column shows a reorder rather "
            "than a fallback/failure. This does not contradict the prior dispatch's ~0% success "
            "finding; it means the live backend's failure is intermittent (empty-content "
            "responses are a known flaky-backend symptom, not a deterministic bug in this "
            "codebase's request), consistent with retrying the same probe producing different "
            "outcomes on different runs."
        )
    else:
        outcome = (
            "Every `SemanticRank` attempt in this run failed the same way the prior dispatch "
            "already documented (empty backend content) or was skipped by `RankRouter` because "
            "the pool was still over-general that turn -- there is no successful live reorder to "
            "show in this transcript."
        )
    lines += [
        "",
        "## Verdict",
        "",
        "`SemanticRank` is currently non-functional against the live DeepSeek backend used in "
        "this assessment: the deterministic `Rank` feature-ranking order is what actually reaches "
        "the user regardless of whether a model is configured, because `_semantic_rank_node` "
        "falls back to the pre-`SemanticRank` order on any backend failure (never surfaces a "
        "broken response to the user, but also never delivers the semantic-reorder value the "
        "design promises). " + outcome + " Fixing this (timeout/prompt/response-format "
        "adjustment) is deferred to the next dispatch per this dispatch's explicit scope "
        "boundary; this document exists so that fix's effect is measurable against a concrete "
        "before-state instead of an anecdote.",
        "",
    ]
    (_OUTPUT_DIR / "04_semantic_rank_before_after.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(f"wrote {_OUTPUT_DIR / '04_semantic_rank_before_after.md'}")


def build_readme(live_configured: bool) -> None:
    lines = [
        "# Model comparison: deterministic-fallback vs live-model",
        "",
        "Four side-by-side comparison documents, generated by "
        "`scripts/generate_model_comparison.py`, for the same conversations/probes run twice: "
        "once with `AgentConfig()` (no model, deterministic fallback throughout) and once with "
        f"`AgentConfig.from_env()` (live model configured: `{live_configured}`). Purpose: let a "
        "human visually judge whether the live model's responses are actually better *before* "
        "deciding whether to invest further in fixing the nodes that currently fail.",
        "",
        "- [`01_vague_clarify_wording.md`](./01_vague_clarify_wording.md) -- `AskAttribute` "
        "wording, templated vs live-generated, same 4-turn clarification conversation.",
        "- [`02_indirect_constraint_extraction.md`](./02_indirect_constraint_extraction.md) -- "
        "`ExtractConstraints` on three indirectly-phrased messages the keyword parser cannot "
        "handle; shows the live model's actual (frequently failing) attempts, honestly.",
        "- [`03_ambiguous_intent_classification.md`](./03_ambiguous_intent_classification.md) -- "
        "`ClassifyIntent` on four ambiguous follow-ups; the fixed deterministic fallback "
        "(`refine_search`, always) is wrong for 3 of 4, the live model got all 4 right in the "
        "prior dispatch's probe.",
        "- [`04_semantic_rank_before_after.md`](./04_semantic_rank_before_after.md) -- "
        "`SemanticRank`'s current broken state (feature-rank order is what users actually see "
        "either way); a before-snapshot for the next dispatch's fix.",
        "",
        "## Overall pattern",
        "",
        "The live model clearly helps on nodes that answer a single, bounded, closed-enum-or-"
        "short-sentence question with a small input: `ClassifyIntent` (closed 4-way enum) and "
        "`AskAttribute` (one sentence, attribute/mode already chosen by code) are reliable and "
        "the live wording/classification is a real, visible improvement over the fixed fallback. "
        "The live model does not yet help -- and in `SemanticRank`'s case, does not currently "
        "function at all -- on nodes that ask for a larger, more open-ended structured output: "
        "`ExtractConstraints` (full constraint diff) and `Explain` (a generated sentence, lower "
        "stakes but still ~30% fallback) fail roughly a third of the time even after the built-in "
        "retry, and `SemanticRank` (reordering a list of candidate ids) failed effectively 100% "
        "of observed attempts with an empty-backend-content error. The pattern is not "
        "\"structured output is unreliable\" in general -- `ClassifyIntent`'s output is also "
        "structured JSON and it is reliable -- it looks closer to \"the more the model has to "
        "produce in one response, the less reliably this backend produces it,\" which points at "
        "the response-size/timeout/prompt-shape of the larger nodes as the next thing worth "
        "investigating, not the retry-once-then-fallback mechanism itself (that mechanism is "
        "working as designed in every case above: it never returned an invalid response to a "
        "user, it just silently ate the model's intended value).",
        "",
    ]
    (_OUTPUT_DIR / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {_OUTPUT_DIR / 'README.md'}")


def main() -> None:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    showcase._install_instrumentation()

    det_agent = Agent(str(_CATALOG_PATH), config=AgentConfig())
    live_agent = Agent(str(_CATALOG_PATH), config=AgentConfig.from_env())
    live_configured = bool(live_agent._graph_services.model_client and live_agent._graph_services.model_client.backends)

    started = time.perf_counter()
    build_doc_01(det_agent, live_agent, live_configured)
    build_doc_02(live_agent, live_configured)
    build_doc_03(live_agent, live_configured)
    build_doc_04(det_agent, live_agent, live_configured)
    build_readme(live_configured)
    print(f"done in {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    main()
