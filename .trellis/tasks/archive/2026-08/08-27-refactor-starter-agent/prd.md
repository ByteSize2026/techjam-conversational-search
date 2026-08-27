# Refactor starter agent into focused modules

## Goal

Reduce the maintenance burden of the 1,025-line `starter/agent.py` by moving
cohesive implementation responsibilities into focused modules while keeping
the evaluator-facing `Agent` contract and current recommendation behaviour
compatible.

## Requirements

- `starter.agent` must continue to export `Agent` with the existing constructor,
  `reset(session_id, user_profile)`, and
  `respond(session_id, user_message, turn, top_k)` signatures.
- Existing constructor injection points, public attributes, diagnostics, session
  isolation, offline fallback, and response schema must remain available.
- Existing test and benchmark integrations that call or replace `_retrieve`,
  `_feature_rank`, `_rank_evidence`, `_valid_ids`, and `_model_backends` must keep
  working during this refactor.
- Retrieval, deterministic ranking/statistics, semantic-result handling, and
  response validation should have explicit module boundaries under
  `starter/shopping_agent/`; `starter/agent.py` should primarily construct and
  orchestrate those components.
- The change must remain offline-first and introduce no new runtime dependency,
  network requirement, or catalog/data format change.
- This task is structural: do not intentionally retune retrieval weights,
  ranking scores, clarification policy, commit policy, or recommendation copy.
- Preserve unrelated user files and untracked artifacts.

## Acceptance Criteria

- [x] `from starter.agent import Agent` and all current constructor injection
      paths continue to work.
- [x] `Agent.reset` and `Agent.respond` retain their evaluator-facing signatures
      and valid response contract.
- [x] Direct internal compatibility calls used by current tests/scripts still
      work, including benchmark replacement of `agent._feature_rank`.
- [x] Retrieval ordering, feature scores, semantic application, diagnostics,
      fallback behaviour, and session state have no intentional behavioural
      change.
- [x] `starter/agent.py` is reduced from 1,025 lines to a focused facade and turn
      orchestrator, with extracted modules named and documented by responsibility.
- [x] `python3 -m unittest discover -s tests -v` passes.
- [x] Python syntax/import compilation succeeds for changed modules.
- [x] A final diff review finds no evaluator, frozen data, or unrelated changes.

## Notes

- Primary protocol sources: `.trellis/spec/agent/contract-and-state.md`,
  `docs/agent_api_contract.json`, and `evaluator/local_evaluator.py`.
- Existing private-method consumers make compatibility shims preferable to a
  breaking cleanup in this task.
