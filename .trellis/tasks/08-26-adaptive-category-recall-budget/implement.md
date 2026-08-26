# Adaptive Category Recall Budget Implementation Plan

## Gate

The user approved implementation after correcting the dependency direction: this task must finish first. Pause further Qwen metric runs, preserve the existing opt-in adapter, and force every task-owned evaluation through the deterministic feature-only configuration.

## Phase 1 — Frozen Diagnostics

- [x] Preserve the current public evaluator result and create a target-free per-turn candidate trace.
- [x] Add an offline analyzer for retrieval Recall@30/100/200 and feature Recall@30.
- [x] Record fixed-budget route ranks for representative small, medium, and large categories, including `public_0028` as a public diagnostic.
- [x] Add an explicit no-model guard that fails the experiment if a model backend is observed or reported token usage is non-zero.

Gate: diagnostics reproduce the current fixed-budget behavior without changing Agent output and prove zero model invocations/tokens.

## Phase 2 — Category Index and Budget Function

- [x] Add deterministic normalized category indexes/counts to `CatalogRepository`.
- [x] Implement most-specific category resolution with exact coarse aliases and explicit unknown/ambiguous fallback.
- [x] Implement and unit-test the bounded budget function for `<=500` and all large-category specificity levels.

Gate: pure unit tests cover category sizes 0, 1, 500, 501, and a size that hits the 400 ceiling.

## Phase 3 — Quota-Preserving Retrieval

- [x] Add an exact category route using the adaptive budget.
- [x] Preserve its allocated candidates and a bounded non-category tail through deterministic route union and deduplication.
- [x] Decouple CandidateGate model skipping from the cheap feature-stage candidate cutoff.
- [x] Keep semantic reranking limited to Top-30 and final output limited to Top-10.
- [x] Add target-free diagnostics for category resolution, budgets, route counts, and union size.

Gate: fixtures prove the final cheap pool retains the category quota and contains only valid unique catalog IDs.

## Phase 4 — Ablation

- [x] Run fixed versus adaptive budgets on the frozen public set.
- [x] Exercise 20%/35%/50% proportional behavior and the 100/400 bounds without per-sample tuning.
- [x] Report overall and scenario-level recall, HitRate, MRR, MTTC, TechnicalScore, latency, and maximum pool size.
- [x] Inspect Top-10 effects: HitRate and TechnicalScore improve while MRR drops slightly; defer Top-30 ordering recovery to the isolated reranker task.
- [x] Freeze the accepted feature-only traces for the subsequent Qwen benchmark.

Gate: retain the adaptive default only with measurable recall/score improvement and acceptable latency/regression.

## Phase 5 — Review and Finish

- [ ] Run an independent correctness review for category resolution, quota preservation, evaluator isolation, and overfitting.
- [ ] Run full unittest, evaluator, and `git diff --check`.
- [ ] Update relevant Trellis specs only if a reusable category-retrieval convention was learned.
- [ ] Commit only task-owned files, then finish and archive the Trellis task.

## Validation Commands

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator \
  --catalog data/catalog.jsonl \
  --dataset data/public_set.jsonl \
  --output /tmp/adaptive-category-recall.json
git diff --check
```

The diagnostic script command will be added during implementation and must write experiment artifacts outside `data/`.

## Rollback

- Disable adaptive category budgeting and restore the previous fixed retrieval budget.
- Keep category indexes unused or remove them if they impose measurable initialization cost.
- Do not modify or restore evaluator files because this task never changes them.
