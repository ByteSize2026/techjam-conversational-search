# Implementation Plan

## Phase 1 — Freeze current behavior

- [ ] Run full unittest and save current public evaluator output outside `results.json`.
- [ ] Confirm the current anchor matches `0.901407 / 1.000000 / 0.805024 / 3.005000`.
- [ ] Add target-free parsing fixtures for all required language behaviors.

## Phase 2 — Deterministic intent core

- [ ] Add interpretation/scope/query-evidence types.
- [ ] Extend rule parsing for negation, removal, replacement, clause splitting and hardness.
- [ ] Implement scoped override and explicit global reset semantics.
- [ ] Update reducer, fingerprint and active query projection.
- [ ] Run focused and full tests, then evaluator non-regression gate.

Rollback point: a complete rules-only version before model integration.

## Phase 3 — Optional model path

- [ ] Add strict JSON validator and model prompt contract.
- [ ] Reuse tiered backend and add independent intent-parser config.
- [ ] Implement trigger and conservative merge policies.
- [ ] Add malformed, hostile, timeout and backend-failure tests.
- [ ] Verify high-confidence rule paths make zero model calls.

## Phase 4 — Integration and evaluation

- [ ] Add bounded intent diagnostics and usage aggregation.
- [ ] Run rules-only and optional-model modes.
- [ ] Report overall/scenario metrics, latency, call rate and fallback rate.
- [ ] Confirm current architecture results only; do not reuse external dense/LLM comparison numbers.
- [ ] Update documentation with offline fallback and known limitations.

## Validation Commands

```bash
python3 -m unittest tests.test_agent_contract tests.test_deterministic_policy tests.test_model_fallback -v
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --output /tmp/intent-interpreter-final.json
```

## Review Focus

- No stale query evidence after scoped replacement.
- No accidental global reset for ordinary change language.
- No model-generated hard filter without explicit support.
- No network access or model loading in default configuration.
- No changes to catalog, evaluator, ranking or response contract.
