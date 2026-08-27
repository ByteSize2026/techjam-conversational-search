# ECom-style Agent Architecture Implementation Plan

## Gate

Do not run `task.py start` or edit product code until the user approves the final `prd.md`, `design.md` and this plan.

Gate status: approved by the user; task started and implementation completed on `feat/ecom-agent-architecture`.

## Phase 0 — Baseline and compatibility anchor

- [x] Run the full unittest suite and the existing public evaluator on the current branch.
- [x] Save score schema, scenario metrics, token usage and representative `last_diagnostics` outside tracked source files.
- [x] Record the exact branch commit, catalog/public-set paths and relevant environment configuration.

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output /tmp/ecom-agent-before.json
```

Rollback point: no product changes yet.

## Phase 1 — Action contracts and catalog toolbox

- [x] Add closed action/result/trajectory types and strict validators.
- [x] Implement catalog-only search, filter, detail, profile, ask and recommend handlers.
- [x] Bound all strings, candidate lists, result lists and numeric parameters.
- [x] Add fixture tests for valid actions, malformed arguments, missing fields, invalid IDs and deduplication.

Validation:

```bash
python3 -m unittest tests.test_agent_tools -v
```

Rollback point: modules are unused by `Agent`; current behavior remains intact.

## Phase 2 — Planner and bounded action loop

- [x] Add the planner protocol and fake/deterministic test planners.
- [x] Implement model JSON action selection through existing `TieredModelClient.complete_json`.
- [x] Implement step/candidate/time budgets, compact observations, terminal recommendation and deterministic fallback.
- [x] Implement ask-user pause without blocking inside `respond()`.
- [x] Add tests for action sequences, invalid/unknown actions, empty results, model failure and budget exhaustion.

Validation:

```bash
python3 -m unittest tests.test_action_orchestrator -v
```

Rollback point: orchestrator remains injectable and not yet the default Agent path.

## Phase 3 — Session pause/resume and profile gating

- [x] Extend `SessionState` with profile visibility, pending task, tool candidates and bounded trajectory.
- [x] Ensure reset replaces all tool state and raw profile remains session-local.
- [x] Resume after an official user reply; clear pending state on override/new intent epoch.
- [x] Aggregate model usage and errors without leaking secrets/profile unnecessarily.
- [x] Add two-session, reset, profile gating, resume and override tests.

Validation:

```bash
python3 -m unittest tests.test_agent_tool_loop tests.test_action_orchestrator -v
```

Rollback point: state fields are additive and the deterministic pipeline ignores them when tool planning is disabled.

## Phase 4 — Agent facade integration

- [x] Extract the current fixed response logic as the deterministic path without changing its result contract.
- [x] Wire explicit/configured planner mode to the orchestrator.
- [x] Map `ask_user` to valid `message/ask_attribute` and map terminal recommendations through the existing guard.
- [x] Fill invalid/short terminal recommendation lists from deterministic ranking without admitting model-invented IDs.
- [x] Add execution-mode/action diagnostics and preserve constructor injection compatibility.
- [x] Add end-to-end tests with scripted planner actions across multiple official turns.

Validation:

```bash
python3 -m unittest tests.test_agent_contract tests.test_agent_tool_loop -v
```

Rollback point: one configuration switch restores the old deterministic path.

## Phase 5 — Full regression and benchmark comparison

- [x] Run all tests offline with no API key.
- [x] Run the public evaluator in deterministic mode and compare against Phase 0.
- [x] If a planner endpoint/key is available, run a bounded tool-loop evaluation and report score, latency, token use, action counts and failure rate separately. No endpoint/key was supplied, so no model-backed score was fabricated; scripted planner tests provide deterministic tool-loop evidence.
- [x] Inspect buying, browsing, intent_override and boundary slices; diagnose rather than modify evaluator semantics.
- [x] Document supported actions, data limitations, configuration and fallback behavior.

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output /tmp/ecom-agent-after-deterministic.json
```

## Completion gate

- [x] All PRD acceptance criteria have test or evaluator evidence.
- [x] No reviewer finds evaluator changes, hidden-target access, external product enrichment, committed credentials or unbounded loops.
- [x] Deterministic offline mode remains usable.
- [x] Tool-loop trajectory demonstrates search/detail/profile/ask/recommend behavior with a scripted planner; model-backed results are reported when credentials are available, not fabricated when unavailable.
