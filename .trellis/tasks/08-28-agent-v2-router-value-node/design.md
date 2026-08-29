# Router/Value-Node Shopping Agent Architecture — Design

## 1. Summary

`respond()` runs a static finite-state machine. Every step is either a **Router** (pure code, decides the next node name + its arguments from current state; never calls a model; never produces a new fact) or a **Value Node** (produces exactly one fact and writes it back through the single state-commit path; may be deterministic code or one LLM call; never decides what runs next).

```text
state = load_or_reset(session)
node = "Entry"
steps = 0
while node != "Done":
    steps += 1
    assert steps <= MAX_INTERNAL_STEPS   # defensive fuse; see §7
    node, args = ROUTERS[node](state)
    state = NODES[node](state, args)     # single commit path: StateReducer
return render(state)
```

`ROUTERS` and `NODES` are two static dicts, keyed by node name. The graph is a DAG except for one deliberate, bounded back-edge (`CandidatePoolRouter` → `LoosenConstraints` → `Search`, §4.1) whose repeat count is a state counter checked by code, capped at 1. Cycles are not banned on principle — a cycle is safe as long as some code-checked, bounded counter guarantees it stops, and the model never decides whether it repeats. This graph just happens to need only one such cycle. `MAX_INTERNAL_STEPS` is a defensive fuse for a future bug that adds an unbounded cycle by mistake, not a substitute for the per-cycle counter — the harness owns the loop and its termination unconditionally; no LLM output ever influences whether the loop continues.

## 2. Boundaries (unchanged from v1)

- `starter.agent.Agent` class name, constructor compatibility, `reset()`/`respond()` signatures, and response schema are unchanged. `evaluator/local_evaluator.py` and public benchmark data are never modified.
- Only catalog-valid `parent_asin` values leave the Agent. Product facts come only from released catalog fields (title, categories, features, description, price, details, rating, rating_count, store).
- No network call on construction. Tool/model planning remains explicitly configured; default path is fully offline and deterministic.

## 3. State model

Extends the existing `SessionState` (`starter/shopping_agent/state.py`), reusing `StateReducer`/`SessionStore`/`intent_epoch` override semantics as-is.

Removed (tool-loop-only, no longer meaningful): `profile_loaded`, `pending_task`, `tool_candidate_ids`, `tool_trajectory`, `last_tool_error`.

Added:

| Field | Purpose |
|---|---|
| `candidates: list[CandidateRef]` | Search output: id + compact summary + score, cleared each new Search |
| `ranked: list[RankedRef]` | Rank/SemanticRank output: id + score + rank source |
| `details_cache: dict[str, ProductDetail]` | Populated only when `FetchDetails` runs; bounded size |
| `pending_question: PendingQuestion \| None` | The clarification question awaiting the user's next message; cleared once the following turn's `ExtractConstraints` runs |
| `session_profile: dict` | Already exists, currently unused. Becomes the distilled soft-profile bucket (§5.6). Kept intentionally small. |
| `node_trace: list[NodeTraceEntry]` | Bounded per-turn trace; see §8 |
| `search_retry_count: int` | Counts `LoosenConstraints`→`Search` retries this turn; reset to 0 at the start of every official turn; capped at 1 (§4.1) |
| `turn_budget` | Derived from the existing turn counter passed into `respond()`; not stored independently, exposed to Routers via `RuntimeContext.remaining_turns` (already exists) |

`RuntimeContext` (already exists, `state.runtime_context(...)`) remains the read-only snapshot Routers receive. No Router or Value Node reads `SessionState` directly — everything goes through `RuntimeContext` or an explicitly narrower per-node input model, matching the "minimal input" rule in §5.

## 4. Node table

Types: **R** = Router (pure code), **VD** = deterministic Value Node, **VL** = LLM Value Node.

| Node | Type | Backing implementation | Input | Output |
|---|---|---|---|---|
| `Entry` | R | new | `RuntimeContext` (has this epoch shown any recommendations yet?) | next = `ClassifyIntent` if recommendations already shown this epoch, else `ExtractConstraints` |
| `ClassifyIntent` | VL | new, pattern from old `semantic_ranking.py`-style bounded call | user message + last shown candidate ids (compact) | `{"intent": "compare_details\|refine_search\|new_search\|confirm_choice", "target_ids": [...]}` (closed enum) |
| `IntentRouter2` (rename pending, see §5.7) | R | new | `ClassifyIntent` output | `compare_details`→`FetchDetails`; `refine_search`/`new_search`→`ExtractConstraints` (override flag set for `new_search`); `confirm_choice`→`Render` |
| `ExtractConstraints` | VL | new; replaces old free-text parse | user message + last ~3 turns + current constraints | `IntentUpdate`-shaped diff (existing type in `state.py`): new/changed constraints, `global_override` flag, `no_preference` additions |
| *(commit)* | — | `StateReducer.apply` | `IntentUpdate` | writes `SessionState.constraints`, bumps `intent_epoch` on override |
| `DistillTriggerRouter` | R | new, pure code | did this turn's `IntentUpdate` add/remove anything, or was a rejection signal present? | `DistillProfile` if yes, else skip to `SlotCheckRouter` |
| `DistillProfile` | VL | new, lightweight (§5.6) | previous `session_profile` + this turn's diff only | small `ProfileUpdate` (2–3 soft fields, not a re-derivation of constraints) |
| `SlotCheckRouter` | R | reuse `ClarificationPolicy.choose_attribute` decision, wrapped | current constraints + candidate evidence + `remaining_turns` | `AskAttribute` only if `choose_attribute` returns a real attribute (evidence/entropy-justified, not-yet-asked, question budget not exhausted); else `Search` |
| `AskAttribute` | VL | new (wording only) | attribute name + `mode: "fill_missing" \| "relax_conflict"` (Router already picked both) + known constraints | one clarification sentence, framed as "tell me X" or "want to adjust X" depending on `mode` |
| `Search` | VD | reuse `CatalogRecommendationEngine.search` / `RecommendationEngine.search` (`recommendation.py`) | category + hard constraints + route weights | `CandidateSearchResult` (candidates + `CandidateStats`) |
| `CandidatePoolRouter` | R | reuse `CandidateGate.evaluate` (`policy.py`), unchanged — **no model involved** | `CandidateStats` + `search_retry_count` + `remaining_turns` | empty + `search_retry_count == 0` → `LoosenConstraints`; empty + `search_retry_count >= 1` + an active constraint remains (other than the one just auto-loosened) + turns remain → `AskAttribute` (`mode="relax_conflict"`, attribute = next-priority active constraint); empty + (nothing left to relax OR turns exhausted) → `NoMatch`; over-general → `AskAttribute` (`mode="fill_missing"`); focused → `Rank` (§4.1) |
| `LoosenConstraints` | VD | new, small, pure code | current soft/hard constraints | drops or widens exactly one constraint (fixed priority order, e.g. price band first); increments `search_retry_count`; routes back to `Search` |
| `NoMatch` | VD | new, small | — | deterministic "no results, try a different category/rephrase" message — narrow fallback for when there is nothing left to offer relaxing, not the default empty-result response |
| `Rank` | VD | reuse `CatalogRecommendationEngine.rank` feature-ranking | candidates + soft constraints + `session_profile` | top-N with score breakdown |
| `SemanticRank` | VL | reuse `LLMSemanticRanker` (`semantic_ranking.py`) — already permutation-safe, cannot invent IDs | top-N from `Rank` | reordered `ordered_parent_asins` |
| `Explain` | VL | new (generation only) | ranked results + user message | one blurb; never introduces new product facts |
| `FetchDetails` | VD | reuse old deterministic detail-lookup logic (bounded projection, reject unknown IDs) | target ids from `ClassifyIntent` | structured product detail |
| `Compare` | VL | new (generation only) | `FetchDetails` output + user message | comparison text |
| `Render` | terminal | new, thin | final `SessionState` | public response: `message`, `ask_attribute`, recommendations ≤ `top_k` |

Total node count: 18 (8 Router, 10 Value Node — 6 deterministic, 5 LLM-backed — `LoosenConstraints` adds one Router-adjacent code node but no model call). Small enough to hand-draw on one page for the demo.

### 4.1 The one intentional cycle: empty-search retry

If `Search` comes back empty, asking the user to loosen their own constraint costs a whole official turn for something code can already try. Instead, `CandidatePoolRouter` sends control to `LoosenConstraints`, which drops or widens exactly one constraint (fixed priority: soft preferences before hard constraints; price band widened before other attributes dropped — exact order is an `implement.md` detail, not decided here) and re-runs `Search`. `search_retry_count` guarantees this happens at most once per official turn: `CandidatePoolRouter` checks the counter before choosing `LoosenConstraints` again.

If the retried `Search` is *still* empty, the turn does not simply dead-end. `CandidatePoolRouter` picks the next-priority active constraint (whichever wasn't the one already auto-loosened — that one is gone from `state.constraints` already, so no separate "already tried" tracking field is needed, the priority scan over current active constraints naturally skips it) and routes to `AskAttribute` with `mode="relax_conflict"`, turning a dead end into a targeted "want to relax X?" question. Only when there is no constraint left to offer relaxing (e.g. a pure-browsing turn with zero constraints that still matched nothing — a genuine catalog/category gap) or `remaining_turns` is too low to spend on another question does it fall through to the static `NoMatch` message.

**Known limitation, called out rather than hidden:** `evaluator/local_evaluator.py`'s simulated user (`customer_reply`) only knows how to answer an `ask_attribute` by disclosing an undisclosed preference for that attribute, or saying it has none — it has no concept of "relax an existing constraint." A "no preference" reply is still usable (treated the same as the existing `no_preference` mechanism, which lets `ExtractConstraints`/`StateReducer` drop that constraint next turn), but the simulated user cannot validate whether the *relax* framing itself helped versus a generic re-ask. This node is justified for real-user value and demo storytelling, not by an expected local-benchmark score movement — same caveat as `DistillProfile` (§5.6).

## 5. LLM Value Node contract

All `VL` nodes satisfy:

1. **Minimal input**: a pydantic model per node listing only the fields that node needs — never the full `SessionState`, never full conversation history beyond what's specified in the table above.
2. **Structured output only**: pydantic model with `model_json_schema()` fed to the model's structured-output request. Parse/validate failure → retry once with the same input → still fails → the *calling Router* takes its deterministic fallback branch (e.g. `SlotCheckRouter` treats a failed `DistillProfile` as "nothing to distill" and proceeds; a failed `ExtractConstraints` falls back to keyword-only constraint parsing already used in v1).
3. **Single responsibility**: one node answers exactly one question. `ClassifyIntent` never also extracts constraints; `Explain` never also decides whether to ask a question.
4. **No exposed tool list**: the model never sees a list of callable actions. Its only degrees of freedom are the fields in its output schema.

### 5.1 Retry/fallback is orchestrator-level, not backend-level

`TieredModelClient` already retries across backends (API/local) for transport failures — that layer is unchanged. The retry-once-then-fallback rule above is a *separate, higher* layer: it governs schema-validation failure regardless of which backend answered. These two retry layers must not compound into unbounded attempts; total worst case per `VL` node call is: (existing backend tiers) × (1 schema retry).

### 5.2 Adaptive parameters vs. adaptive control flow

This is the direct answer to the problem statement's "runtime workflow re-orchestration" pillar, and the resolution to the tension flagged during design discussion:

- **Allowed** ("parameter adaptation"): the *values* Routers compare against shift with state — `IntentRouter`'s buying/browsing weight blend, `adaptive_category_budget`'s recall size, and (new) `ClarificationPolicy`'s implicit threshold tightening as `remaining_turns` shrinks, forcing convergence to a best-guess recommendation rather than another question. This directly targets the MTTC metric.
- **Not allowed** ("structure adaptation"): a model deciding which node runs next, or rewriting `ROUTERS`/`NODES` at runtime. The node graph is fixed at design time.

The pitch: "workflow orchestration" (fixed graph, data-driven parameters) vs. "autonomous agent" (model-driven action sequence) is a known industry split; this system deliberately picks the former for a low-latency, budget-constrained, reproducibly-scored setting, and the "intelligence" shows up in the quality of each single-purpose judgment and in how the thresholds move — not in who gets to pick the next step.

### 5.3 Candidate-pool judgment stays deterministic

`CandidatePoolRouter` uses `CandidateGate.evaluate` unchanged. No LLM call is added here: the existing count/entropy heuristic is cheap, already tuned, and every removed model call on this path is a removed latency/failure source for zero measurable gain. If test evidence later shows the heuristic under-performs, revisit — not assumed now.

### 5.4 `ExtractConstraints` schema

Reuses the existing `IntentUpdate` shape from `state.py` (already consumed by `StateReducer.apply`) as the pydantic output model — no new state-commit contract needed, only a new producer of that same shape.

### 5.5 `ClassifyIntent` schema

```python
class ClassifyIntentOutput(BaseModel):
    intent: Literal["compare_details", "refine_search", "new_search", "confirm_choice"]
    target_ids: list[str] = []   # bounded, validated against last-shown candidate ids
```

### 5.6 `DistillProfile` — lightweight, per user's explicit scope decision

Deliberately shallow for this task: the evaluator scores isolated single-user sessions, so there is no cross-session signal to actually measure. This node exists to prove the data flow works, not to be tuned.

- **Input**: previous `session_profile` (small dict) + this turn's `IntentUpdate` diff + any rejection signal already captured by `ClassifyIntent`. Never the full constraint set (that's redundant — constraints already live in `state.constraints`).
- **Output**: 2–3 soft fields only (e.g. `price_sensitivity`, `style_signal`) — schema intentionally small, not designed for extension yet.
- **Trigger**: gated by `DistillTriggerRouter`, code-only, fires only when this turn actually changed something. No LLM call on a no-op turn.
- **Consumer**: `Rank`'s soft-preference weighting and `Explain`'s tone; written through `StateReducer` only, no second write path.
- Deepening this (richer schema, cross-session persistence) is explicitly out of scope (see `prd.md`); revisit after the rest of the architecture is stable.

### 5.7 Naming note

`policy.IntentRouter` (existing class) does **not** decide the next node — it returns weighting parameters (`RouteDecision`) consumed by `Search`. Under this taxonomy it is a **Value Node**, not a Router, despite its name. Recommend renaming to avoid confusion during implementation (e.g. `RouteWeights`/`RouteWeightPolicy`), tracked as an `implement.md` item rather than decided here. The actual next-node Router that consumes `ClassifyIntent`'s output is placeholder-named `IntentRouter2` in §4 pending that rename.

## 6. Router branch tables (edge cases)

Carried forward from v1, re-attached to the correct v2 node — not reinvented:

- **`SlotCheckRouter`**: this is the primary early-convergence path, not the turn-10 fallback. `ClarificationPolicy.choose_attribute` already returns `None` (→ `Search`) as soon as either (a) the one-question budget (`max_questions`, default 1) is used up, or (b) no remaining attribute has enough evidence/entropy to be worth asking — both can happen as early as turn 2–3, independent of `remaining_turns`. `remaining_turns <= 0` is a *separate*, additional forced-`Search` condition layered on top (never ask a question that can't be answered in time even if evidence would otherwise justify it) — it is the backstop, not the mechanism that produces early stopping.
- **`CandidatePoolRouter`**: zero candidates on the first pass this turn → `LoosenConstraints` → retry `Search` once (§4.1), not straight to `NoMatch` (never asks a question about an empty result set on the *first* pass — old v1 rule — but now tries a free code-driven fix first). Still zero after that retry → ask to relax the next-priority active constraint if one exists and turns remain (§4.1); only a true dead end (nothing left to relax, or no turns left) reaches the static `NoMatch` message. Non-zero + over-general (count/entropy threshold from `CandidateGate`) → `AskAttribute` (`mode="fill_missing"`), but only if `remaining_turns` allows another question; otherwise fall through to `Rank` on the truncated pool (same "forced convergence near budget exhaustion" rule as §5.2).
- **`IntentRouter2`**: `target_ids` from `ClassifyIntent` that don't exist in `last_candidate_ids`/`details_cache` are dropped before `FetchDetails` runs (never trust model-supplied IDs blindly — same discipline v1 applied to tool-loop terminal IDs).
- **`DistillTriggerRouter`**: override turns (`global_override=True`) always trigger distillation once (the rejection-of-everything signal is itself informative), even if no *new* constraint was added.
- **`Entry`**: a turn that resumes after `pending_question` was asked bypasses `ClassifyIntent` and goes straight to `ExtractConstraints` — the reply is answering *that* question, not commenting on shown results, so intent classification would be redundant and adds a needless model call.

## 7. Termination guarantee

Primary guarantee: `ROUTERS` describes a DAG in `Entry`, with exactly one declared exception — the `CandidatePoolRouter`↔`LoosenConstraints`↔`Search` back-edge (§4.1), whose repetition is bounded by `search_retry_count` (a state counter, checked by `CandidatePoolRouter`, capped at 1). This is a static, enumerable property of the table plus one counted exception, so it is checked by a static test, not trusted informally — see §9. The `MAX_INTERNAL_STEPS` assertion in the main loop (§1) is a defensive fuse that raises immediately rather than looping forever if this invariant is ever broken by a future edit (e.g. a new back-edge added without a counter); it is not load-bearing for correctness and is not tuned as a budget the way `remaining_turns` (official turns) is.

To be explicit about the general rule this graph follows: a cycle is not forbidden by the Router/Value-Node principle itself. What's forbidden is a model deciding whether a cycle repeats. A cycle bounded by a state counter that a Router checks is exactly as safe as no cycle at all. This graph currently needs only the one back-edge above; adding another later is fine as long as it follows the same shape (counter in state, checked by the Router, capped, reset each official turn).

## 8. Trace format

Every node execution appends one entry to `state.node_trace` (bounded, cleared at the start of each official turn — not across turns, to keep it small):

```python
class NodeTraceEntry(TypedDict):
    step: int
    node: str
    kind: Literal["router", "value_node_deterministic", "value_node_llm"]
    input_summary: str    # bounded, redacted the same way trace_runner.sanitize() already redacts
    output_summary: str
    elapsed_ms: float
```

`evaluator/trace_runner.py`'s `TracingAgentProxy`/`project_state()` already generically project whatever fields exist on the session; the only change needed there is adding `node_trace` to the projected field set. No rewrite of its diff/JSONL machinery.

This is not read by `evaluator/local_evaluator.py` (confirmed: it only reads the public `respond()` return value) — trace exists purely for debugging, demo storytelling ("here is the exact path this turn took"), and as the trajectory source for the separate `08-27-robust-evaluator-feasibility` line of work.

## 9. Testing and evaluation strategy

Tests exist to prove the architecture's structural claims are real (the graph terminates, the state has one writer, the fallback layer actually falls back) — not to chase coverage. Whether the system *behaves intelligently* (asks a sensible question, converges early, recovers from a dead end gracefully) is not something a unit test assertion is well suited to judge, and per-node/per-branch test proliferation was costing implementation time without adding evidence of that. That evidence comes from the scenario showcase below instead.

**Structural test set (kept — cheap, each one proves a specific claim in this doc):**

- **Static graph test**: walk `ROUTERS` from `Entry` enumerating all reachable transitions; assert no node is revisited except the one declared `LoosenConstraints`→`Search` back-edge, and assert that back-edge is guarded by a `search_retry_count` check capped at 1. This is the termination proof for §7, not a comment.
- **Retry-bound + true-dead-end test** (one module, two cases sharing a fixture): (a) force `Search` empty twice with an active constraint remaining → `LoosenConstraints` runs exactly once, turn ends at `AskAttribute(mode="relax_conflict")`, never a third `Search` call; (b) force `Search` empty twice with zero constraints active → turn ends at `NoMatch`.
- **`StateReducer` single-writer test**: assert no code path other than `StateReducer.apply` mutates `SessionState.constraints`/`session_profile`.
- **Shared VL retry/fallback helper test**: one test of the retry-once-then-fallback helper itself (parse success, parse failure → retry → fallback) — proves the mechanism once rather than once per node, since every `VL` node routes through the same helper (§5.1).
- **`DistillTriggerRouter` gating test**: a no-op turn must produce zero model calls (cheap, catches an easy regression).
- **`CandidatePoolRouter` no-model test**: assert this path never touches the model client, for any `CandidateStats` input (cheap, guards §5.3's latency claim).

**Dropped from an earlier draft of this plan, deliberately**: a dedicated test for every §6 Router branch, a dedicated pydantic contract test per `VL` node, and a separate hand-computed `node_trace` replay test. These would mostly re-test the shared mechanisms above under different labels; the scenario showcase exercises the real branches anyway and is more legible evidence than an assertion count.

**Scenario showcase (primary evidence of quality — this is the actual demo/judging material):**

4–6 hand-picked conversations run end-to-end through `evaluator/trace_runner.py`, saved as readable turn-by-turn transcripts (user message → which nodes fired → what was asked/returned → why), covering:

- vague query → proactive clarification → convergence by turn 3–4 (the `SlotCheckRouter` early-stop story from §6)
- over-general query → `AskAttribute(mode="fill_missing")`
- empty search → auto-loosen → retry → `AskAttribute(mode="relax_conflict")` (the one cycle, shown actually working)
- true dead end → `NoMatch`
- intent override mid-session (`global_override` path)
- multi-turn constraint accumulation → `SemanticRank` visibly changing result order

These transcripts are what gets shown for Impact/Innovation/Presentation judging — "here is the system reasoning, turn by turn" — not the test count or even the evaluator score alone.

**Regression check**: full existing `unittest` suite (now smaller per above) + one public-evaluator run compared against the Phase 0 baseline. This is a safety net confirming nothing broke, not the thing being optimized — consistent with the whole premise of this task (see `prd.md`).

## 10. Compatibility and rollout

- Default configuration stays offline/deterministic; constructing `AgentConfig` performs no network call.
- A single flag disables all `VL` nodes' model calls, falling back to each node's deterministic branch (schema-failure fallback path, reused as the "no model configured" path too — one fallback mechanism, not two).
- `starter/agent.py` construction no longer wires `action_planner`/`action_orchestrator`; those parameters and the modules backing them (`actions.py`/`planner.py`/`orchestrator.py`/`tools.py`) are removed, not merely unused.

## 11. Deliberate exclusions

- Deep profile personalization, cross-session memory (§5.6 scope limit).
- Robust Evaluator / rubric generation (separate task).
- Review-text reasoning (no review data in this catalog).
- Any mechanism by which a model output changes `ROUTERS`/`NODES` at runtime.
