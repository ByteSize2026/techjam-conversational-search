# TechJam Conversational E-Commerce Search Challenge

An AI shopping agent that asks useful follow-up questions and recommends the customer's
hidden target product within at most 10 turns, built on a **Router / Value-Node static state
machine** (not a model-driven tool loop — see [Why This Architecture](#why-this-architecture)).

## Project Overview

Traditional keyword search can't tell "just browsing" from "I need this exact thing," and
can't recover cleanly when a customer changes their mind mid-conversation. This project
addresses the challenge's four pillars with one architecture:

| Pillar | What we built |
| --- | --- |
| I. Intent Routing & Hybrid Pipeline | A Buying/Browsing/Mixed router picks retrieval weights per turn; category, lexical, and constraint routes are merged and deduplicated before ranking (`starter/shopping_agent/recommendation.py`). |
| II. Multi-Turn Scenario Evolution | A single-writer state reducer accumulates slots turn over turn and handles intent override (partial *and* full) without losing unrelated constraints (`state.py`); an over-general candidate pool triggers a structured clarification question instead of dumping results (`policy.py`). |
| III. Self-Evolution | `DistillProfile` maintains a short-term session profile from each turn's diff; `SessionState.session_profile` persists it across turns. **Adaptive Orchestration (dynamic re-orchestration of strategy across turns) is not implemented** — see [Limitations](#limitations-and-what-wed-do-with-more-time). |
| IV. Evaluation Matrix | Unmodified official `evaluator/local_evaluator.py`; see [Results](#results) for our numbers on the 200 public sessions. |

The full node-by-node architecture (Router/Value-Node contract, state machine diagram, why the
first "model picks its own tools" design was scrapped) is in
[`AGENT_ARCHITECTURE.md`](AGENT_ARCHITECTURE.md). The diagnostic trail for every fix below —
what we tested, what we found, what we tried that didn't work — is in
[`ARCHITECTURE_IMPROVEMENT_PLAN.md`](ARCHITECTURE_IMPROVEMENT_PLAN.md).

### Why This Architecture

An earlier version let the model pick its own next action from a tool list each turn. That
puts control flow in the model's hands, and its failure modes (stop early, loop between tools,
forget a step) can't be enumerated at design time. The current architecture separates "decide
what happens next" (a **Router** — plain code, no model call) from "produce one fact" (a
**Value Node** — deterministic algorithm or one structured LLM call, never a control-flow
decision). The result is a static, fully-drawable state machine instead of a model-driven loop.
Full writeup: [`AGENT_ARCHITECTURE.md`](AGENT_ARCHITECTURE.md).

### A Concrete Failure Mode We Fixed

Three separate bugs this project found share the same shape, and none of them threw an error
or showed up as an obvious score drop — each one *looked* like a normal turn:

1. **Intent override**: "scratch that, let's go with leather" was being misread as *abandon
   the whole search*, wiping out unrelated constraints (category, brand) the customer never
   asked to drop.
2. **Score scale**: raw retrieval scores (unbounded, favoring verbose listings) were added
   directly to bounded constraint-match bonuses, so a wordy but irrelevant product could
   outrank a product that matched every stated constraint.
3. **category_anchor extraction**: the constraint-extraction LLM call would occasionally
   return valid JSON with every field populated *except* the category, for a compound catalog
   phrase like "Outdoor & Work Rain" — and because that phrase typically appears once (the
   opening line), the whole session lost category-scoped retrieval with no recovery path.

All three were found by replaying real sessions against `events.jsonl` traces and comparing
LLM call inputs/outputs turn by turn, not by looking at the final Hit Rate number — a passing
aggregate score can still hide a session that got the right answer for a coincidental reason.
Full diagnostic writeups for each: [`ARCHITECTURE_IMPROVEMENT_PLAN.md`](ARCHITECTURE_IMPROVEMENT_PLAN.md).

## Setup

Python 3.10+.

```bash
pip install -r requirements.txt
```

The only hard runtime dependency is `pydantic` (structured LLM input/output validation). The
optional Qwen3 cross-encoder reranker (`starter/shopping_agent/qwen_reranker.py`) additionally
needs `sentence_transformers` + a local checkpoint, but is never imported unless explicitly
configured — the default path has no extra dependencies and makes no network calls.

Download the catalog (see the participant kit release):

```bash
gzip -dk catalog.jsonl.gz
mv catalog.jsonl data/catalog.jsonl
```

## Reproduce Our Results

```bash
python3 -m evaluator.local_evaluator
```

This runs the **unmodified official evaluator** against `starter/agent.py` (which wires up the
full Router/Value-Node graph) over the 200 public sessions and writes `results.json`.

### Model Backend (optional, network-dependent)

Without any configuration, the agent runs entirely offline: every LLM Value Node has a
deterministic fallback (keyword/regex parsing, template wording), so `respond()` always
returns a valid, catalog-grounded response with zero network calls. This is the path used for
official final scoring if the organizer disables network access.

To reproduce the live-model numbers reported below, copy `.env.example` to `.env`, fill in one
model backend, then:

```bash
set -a; source .env; set +a
python3 -m evaluator.local_evaluator
```

```text
DeepSeek API -> local OpenAI-compatible endpoint -> deterministic fallback
```

Supported environment variables are documented in [`.env.example`](.env.example). We used
`deepseek-v4-flash` via the DeepSeek API during development; see
[Model Choice, Cost, and Latency](#model-choice-cost-and-latency) for disclosure.

## Results

_Filled in from the final full-200-session run on the public set, using the live DeepSeek
backend (`scripts/run_full_live_test.py`, one run, no cherry-picking):_

| Metric | Value |
| --- | --- |
| Hit Rate@10 | **0.9050** (181/200) |
| MRR | 0.5089 |
| MTTC | 4.525 |
| TechnicalScore | 0.7347 |

By scenario: Browsing 0.950 (76/80), Buying 0.950 (76/80), Boundary 0.900 (9/10),
**Intent Override 0.667 (20/30)**. Intent Override is the clear weak point — it's the scenario
most likely to trigger the not-yet-fixed ranking sensitivity described in
[Limitations](#limitations-and-what-wed-do-with-more-time) (a same-attribute constraint value
getting replaced mid-session), so this gap is a known, diagnosed shortfall, not an unexplained
one.

Baseline (weak BM25 starter, for comparison): Hit Rate@10 `0.125`, MRR `0.068034`, MTTC `9.81`
(`docs/baseline_results.json`).

Per-scenario breakdown and the full diagnostic history behind every fix are in
[`ARCHITECTURE_IMPROVEMENT_PLAN.md`](ARCHITECTURE_IMPROVEMENT_PLAN.md).

## Model Choice, Cost, and Latency

- **Model**: `deepseek-v4-flash` via the DeepSeek API (`SHOPPING_AGENT_DEEPSEEK_MODEL`).
- **Why**: cheap enough for the ~5-8 structured LLM calls per turn this architecture makes
  (ExtractConstraints, ClassifyIntent, DistillProfile, AskAttribute wording, SemanticRank,
  Explain), with schema-validated JSON output.
- **Network dependency**: optional. With no model backend configured, every LLM Value Node
  falls back to a deterministic path (regex/keyword parsing or a fixed wording template) and
  the agent never makes a network call.
- **Token usage / cost**: reported per-session via the `usage` field in `respond()`'s return
  value when a live backend succeeds; aggregated token totals are printed by
  `scripts/run_full_live_test.py` at the end of a run.
- **Latency**: single-session, single-turn LLM calls observed in the ~0.5-1.5s range each;
  a full 10-turn session with several LLM Value Nodes per turn typically completes in a few
  seconds to ~30s depending on how many turns and clarification questions are needed.

## Limitations and What We'd Do With More Time

- **Adaptive Orchestration (pillar III) is not implemented.** The graph's topology and
  strategy parameters (retrieval weights, category recall budget, clarification thresholds)
  are fixed at design time; there is no mechanism that adjusts the agent's own guidance logic
  based on how the last few turns of *this* session went. We deliberately did not build this
  speculatively — every other fix in this project came from first reproducing a concrete
  failure against real sessions, and we ran out of time to do the same diagnostic pass here
  before the submission deadline. With more time, the next step is exactly that: run a
  diagnostic pass to find out whether the current fixed-strategy architecture actually
  under-performs in some identifiable class of session, before designing a feedback mechanism
  to fix it.
- **No vector/dense retrieval.** We built the diagnostic tooling to test this directly rather
  than assume it from the problem statement: 40 real sessions, 6 misses, and in every miss the
  customer's stated words were a literal substring match against the target product's own
  listing text — so a dense retrieval layer would not have recovered any of them. The two real
  gaps that diagnosis surfaced (a category-parsing single point of failure, and a retrieval
  score scale bug) were fixed instead; see `ARCHITECTURE_IMPROVEMENT_PLAN.md` item 2.
- **A newly-found ranking sensitivity is not yet fixed.** When a same-attribute constraint
  value is replaced mid-session (e.g. a cleaned-up value replacing an earlier, more verbose
  one), we observed at least one session where the target's rank got measurably worse rather
  than better, even though the new value was semantically at least as correct. This traces to
  how literal phrase specificity interacts with the retrieval score, not to constraint
  extraction — it needs its own diagnostic pass, which we did not have time for.
- **`AskAttribute` wording and `Explain` are LLM calls with near-zero decision authority** —
  they only choose how to phrase an already-decided question/introduction. Given more time we
  would replace both with a deterministic template (as our own `starter/shopping_agent/state.py`
  already does for total-fallback wording), which removes two LLM calls per turn with no
  expected loss in hit rate.

## Files

```text
starter/agent.py                          Agent facade (reset/respond) — the graded entry point
starter/shopping_agent/graph.py           Router/Value-Node state machine (the core architecture)
starter/shopping_agent/state.py           SessionState, the single-writer StateReducer, v1 deterministic fallback parser
starter/shopping_agent/llm_nodes.py       Every LLM Value Node's prompt + pydantic input/output schema
starter/shopping_agent/recommendation.py  Multi-route retrieval, candidate pool assembly, feature ranking
starter/shopping_agent/semantic_ranking.py LLM-backed listwise reranker (SemanticRank)
starter/shopping_agent/catalog.py         In-memory SQLite FTS5 repository, category resolution
starter/shopping_agent/policy.py          Intent routing and candidate-gate policy
starter/shopping_agent/qwen_reranker.py   Optional local cross-encoder reranker (not required)
starter/shopping_agent/model.py           Tiered model client (DeepSeek -> local -> none)
evaluator/local_evaluator.py              Unmodified official public-set simulator and scorer
scripts/run_full_live_test.py             Crash-safe full/partial live-model test runner with per-node tracing
data/public_set.jsonl                     200 labeled development sessions
docs/agent_api_contract.json              Machine-readable Agent contract
docs/baseline_results.json                Reproducible weak-starter reference score
AGENT_ARCHITECTURE.md                     Full architecture writeup (Router/Value-Node contract, diagrams)
ARCHITECTURE_IMPROVEMENT_PLAN.md          Diagnostic trail for every fix: what we tested, what we found
PROMPT_AUDIT.md                           Every LLM prompt in the system, with source/purpose/input/output
```

## Team Contributions

_TODO: fill in before final submission._

## Task (Official)

For each session, the agent receives an anonymized preference profile and a short customer
message. Raw user IDs, review text, timestamps, and purchase history are never disclosed. On
every turn the agent may:

- ask a natural clarification question in `message` and identify one requested field in `ask_attribute`;
- return a ranked list of up to 10 catalog `parent_asin` values;
- do both in the same response.

The session ends when the target product appears in the scored Top 10 or after turn 10.
Sessions cover Buying, Browsing, Intent Override, and Boundary behavior.

## Agent Interface (Official)

```python
class Agent:
    def reset(self, session_id: str, user_profile: dict) -> None:
        ...

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        return {
            "message": "Do you have a material preference?",
            "ask_attribute": "material",
            "recommendations": [
                {"parent_asin": "B000..."},
                {"parent_asin": "B001..."}
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30}
        }
```

`ask_attribute` is one of `category`, `material`, `color`, `size`, `style`, `brand`, `budget`, `feature`, `use_case`, `other`, or `null`. See `docs/agent_api_contract.json`.

## Technical Metrics (Official)

- **Hit Rate@10:** fraction of sessions that find the target within 10 turns.
- **MRR:** mean reciprocal rank of the target; a miss contributes zero.
- **MTTC:** mean first-hit turn; a miss is assigned turn 11.
- **Reported token usage:** prompt and completion tokens returned by the team's model client.

```text
TechnicalScore = 0.50 x HitRate@10 + 0.30 x MRR + 0.20 x Efficiency
Efficiency = clip((11 - MTTC) / 10, 0, 1)
```

Only exact `parent_asin` equality produces a hit. Core metrics are also reported by scenario.

## Data Source

The catalog and sessions are derived from Amazon Reviews 2023 by McAuley Lab, UCSD. See
`DATA_ATTRIBUTION.md` before using or redistributing the data. Sessions are sampled
deterministically from the official Clothing 5-core leave-last-out split and joined to the
frozen catalog.
