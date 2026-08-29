# Router/Value-Node Shopping Agent — Implementation Plan

## Gate

Do not run `task.py start` or edit product code until the user approves the final `prd.md`, `design.md`, and this plan.

Gate status: approved — task started (`task.py start`, status in_progress). Test scope and evaluation focus revised 2026-08-29 per user direction: fewer unit tests, primary evidence of quality is the Phase 8 scenario showcase, not test count.

## Phase 0 — Baseline

- [ ] Run the full unittest suite and the existing public evaluator on the current branch (with old tool-loop code still present, before any removal).
- [ ] Save score schema, scenario metrics, and a representative `last_diagnostics` outside tracked source files, for later regression comparison.
- [ ] Record the exact branch/commit this baseline was taken from.

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output /tmp/agent-v2-before.json
```

Rollback point: no product changes yet.

## Phase 1 — Remove the discarded tool-loop

- [ ] Remove `starter/shopping_agent/actions.py`, `planner.py`, `orchestrator.py`, `tools.py` and their wiring in `starter/agent.py` (`action_planner`/`action_orchestrator` constructor params and the branch that dispatches to them).
- [ ] Remove tool-loop-only `SessionState` fields: `profile_loaded`, `pending_task`, `tool_candidate_ids`, `tool_trajectory`, `last_tool_error`.
- [ ] Remove/adjust tests that only existed for the removed modules; keep any that already test surviving pieces (`policy.py`, `state.py` core, `recommendation.py`).
- [ ] Confirm `Agent` still constructs and runs its existing fixed pipeline unmodified (this phase is subtractive only, not yet touching the new architecture).

Validation:

```bash
python3 -m unittest discover -s tests -v
```

Rollback point: revert this phase alone restores the pre-existing (pre-superseded) tree; nothing downstream depends on it yet.

## Phase 2 — State model additions

- [ ] Add new `SessionState` fields per design §3: `candidates`, `ranked`, `details_cache`, `pending_question`, `session_profile` (repurposed, already exists as an empty dict — define its shape), `node_trace`, `search_retry_count`.
- [ ] Confirm `StateReducer` remains the only mutator; new fields get a narrow, explicit update path through it (extend `IntentUpdate` or add a sibling update type consumed by the same `apply()`, per design §5.4/§5.6).
- [ ] Add one `StateReducer` single-writer test: grep/AST-check or explicit test asserting no other module assigns to the protected fields directly. This is the only new test in this phase — keep it to one focused test, not a suite.

Validation:

```bash
python3 -m unittest tests.test_state_reducer -v   # new file, single test
```

Rollback point: additive-only; existing pipeline ignores the new fields.

## Phase 3 — Router/Node registry and main loop scaffold

- [ ] Create `starter/shopping_agent/graph.py` (or similar) with `ROUTERS: dict[str, Callable]`, `NODES: dict[str, Callable]`, and the main loop from design §1, including the `MAX_INTERNAL_STEPS` defensive assertion.
- [ ] Stub every node from design §4 as a placeholder (deterministic nodes return fixed dummy values; LLM nodes are not yet wired to a model) so the full graph shape exists and is testable before business logic is filled in.
- [ ] Write the static graph test from design §9: walk `ROUTERS` from `Entry`, assert no node is revisited except the declared `CandidatePoolRouter`→`LoosenConstraints`→`Search` back-edge, and assert that back-edge is guarded by a `search_retry_count` check capped at 1.
- [ ] Write the retry-bound and true-dead-end cases from design §9 in the *same* test module as the static graph test (two cases, one fixture) against the stubbed graph — they should pass on stub logic already, since they test control flow, not business logic.

Validation:

```bash
python3 -m unittest tests.test_router_graph -v   # one new file, covers all of Phase 3's structural claims
```

Rollback point: the graph module is not yet wired into `Agent`; existing `respond()` path is untouched.

## Phase 4 — Deterministic nodes and routers

- [ ] `Entry`, `SlotCheckRouter` (wraps `ClarificationPolicy.choose_attribute`), `Search` (wraps `CatalogRecommendationEngine.search`), `CandidatePoolRouter` (wraps `CandidateGate.evaluate`, plus the empty/retry/relax/no-match branching from design §4.1), `LoosenConstraints`, `NoMatch`, `Rank` (wraps `CatalogRecommendationEngine.rank`), `FetchDetails`, `IntentRouter2`'s non-LLM branch dispatch.
- [ ] Replace stub bodies from Phase 3 with real implementations for these nodes only; LLM-backed nodes stay stubbed.
- [ ] One smoke test (not a full matrix): with all LLM nodes stubbed to deterministic fallback behavior, a session runs `Entry`→...→`Render` without error for one buying-style scenario. The empty-search-then-relax and true-dead-end scenarios are already covered structurally by Phase 3's `test_router_graph`; don't re-test them here. Full behavioral coverage of these paths comes from the Phase 8 scenario showcase, not a per-scenario unit test matrix.

Validation:

```bash
python3 -m unittest tests.test_router_graph tests.test_deterministic_nodes -v
```

Rollback point: one flag (or simply: LLM nodes still stubbed) keeps the whole system deterministic and testable without a model.

## Phase 5 — LLM Value Nodes

- [ ] Define pydantic input/output models per node (design §5.4–§5.6, §5 general contract): `ExtractConstraints` (reuses `IntentUpdate` shape), `ClassifyIntent` (§5.5 schema), `AskAttribute` (`mode`-aware wording), `DistillProfile` (§5.6, gated by `DistillTriggerRouter`), `SemanticRank` (reuse `LLMSemanticRanker`, already permutation-safe), `Explain`, `Compare`.
- [ ] Implement the retry-once-then-fallback rule (design §5, §5.1) uniformly — one small helper used by all `VL` nodes, not duplicated per node.
- [ ] One test of the shared helper itself (parse success, parse failure → retry → fallback) using a fake/scripted model client. Do not add a duplicate per-node contract test — every `VL` node routes through this one helper, so this single test is the coverage (design §9).
- [ ] Implement `DistillTriggerRouter` gating and its test (no-op turn → zero model calls).
- [ ] Implement `CandidatePoolRouter` no-model test (assert this path never touches the model client for any `CandidateStats` input) — this is a Phase 4 router but the assertion only matters once real model clients exist to accidentally call, so verify here.

Validation:

```bash
python3 -m unittest tests.test_llm_value_nodes -v   # new file, kept small: shared-helper test + 2 gating tests
```

Rollback point: a single flag disables all `VL` node model calls, falling back to each node's deterministic branch (design §10) — the system remains fully runnable offline throughout this phase.

## Phase 6 — Trace integration

- [ ] Add `NodeTraceEntry` appends to `state.node_trace` at each node execution in the main loop (design §8), bounded and cleared per official turn.
- [ ] Update `evaluator/trace_runner.py`'s projected-field set to include `node_trace`; no other change to its diff/JSONL machinery.
- [ ] No dedicated hand-computed replay test — `node_trace` correctness is exercised and made visible by the Phase 8 scenario showcase instead (design §9), which is more legible evidence than one hard-coded expected sequence.

Validation:

```bash
python3 -m unittest tests.test_trace_runner -v
```

Rollback point: trace is additive/observational; removing it does not change `respond()` behavior.

## Phase 7 — Agent facade integration

- [ ] Wire the graph from Phases 3–6 as `starter/agent.py`'s `respond()` implementation, replacing the old fixed pipeline.
- [ ] Preserve `Agent.reset`/`respond` signatures and public response schema exactly.
- [ ] Add `execution_mode`/node-count/failure diagnostics to `last_diagnostics` (successor to the old tool-loop diagnostics, adapted to the new node names).
- [ ] One end-to-end contract test covering an `ask_attribute` pause/resume cycle and an `intent_override` cycle in a single multi-turn session (not separate tests per cycle type) — enough to prove the facade wiring is correct; behavioral quality of these paths is judged from the Phase 8 scenario showcase, not from this test.

Validation:

```bash
python3 -m unittest tests.test_agent_contract -v
```

Rollback point: keep the old fixed-pipeline code path importable but unused for one commit, in case an integration issue needs a fast revert; remove it once Phase 8 passes.

## Phase 8 — Scenario showcase, regression, and benchmark comparison

- [ ] Run the 4–6 scenario showcase conversations from design §9 end-to-end through `evaluator/trace_runner.py`; save each as a readable turn-by-turn transcript (user message → nodes fired → what was asked/returned → why) under an untracked artifacts path. This is the primary deliverable of this phase — the actual evidence of interactive intelligence for judging, not the test/evaluator numbers below.
- [ ] Run all tests offline with no API key (fully deterministic fallback path).
- [ ] Run the public evaluator in deterministic mode; compare against the Phase 0 baseline. Report score deltas and reasons; do not modify evaluator semantics to mask a regression. Treat this as a safety-net check, not the target being optimized.
- [ ] If a model backend/key is available, run one bounded model-backed evaluation pass and report score/latency/token use/failure rate separately from the deterministic run.
- [ ] Update `starter`/task documentation to describe the new architecture (node table, trace format) for anyone reading the repo — this doubles as the written-description material referenced in the hackathon deliverables.

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output /tmp/agent-v2-after.json
```

## Completion gate

- [ ] All `prd.md` acceptance criteria have test or evaluator evidence.
- [ ] `starter/shopping_agent/actions.py`/`planner.py`/`orchestrator.py`/`tools.py` are removed, not merely unused.
- [ ] Static graph test passes: the node graph is a DAG except the one declared, counter-bounded back-edge.
- [ ] Deterministic offline mode remains fully usable with no API key.
- [ ] `node_trace` demonstrably replays a real turn's path; `trace_runner.py` regression suite passes.
- [ ] 4–6 scenario showcase transcripts exist and each one visibly demonstrates the behavior it's meant to (clarify-then-converge, over-general clarify, auto-loosen retry, true dead end, intent override, semantic reorder) — this is the primary judged deliverable, checked before the unit test count.
