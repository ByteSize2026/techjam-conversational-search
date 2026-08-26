# Adaptive Category Recall Budget

## Goal

Replace the fixed retrieval cutoff with a category-size-aware recall budget so products in medium and large categories are not discarded before feature ranking, while preserving bounded latency and the official Agent/evaluator contract.

## Requirements

### Category-aware budget

- Precompute a deterministic category index and category counts from the frozen catalog during repository initialization.
- Resolve the most specific trustworthy catalog category represented by the session category anchor; unmatched or ambiguous anchors must use a bounded global fallback rather than inventing a category.
- For a resolved category containing at most 500 products, allow the category route to recall the complete category.
- For a category containing more than 500 products, calculate a proportional budget from conversational specificity and clamp it to a minimum of 100 and maximum of 400:
  - no active hard constraint / browsing: 50%;
  - one active hard constraint: 35%;
  - two or more active hard constraints: 20%.
- Never request more candidates than actually exist in the resolved category.

### Multi-route preservation

- Treat exact category retrieval as an independent route with its own quota; lexical, constraint, profile, and popularity candidates must not consume that quota before route union.
- Merge routes deterministically, deduplicate by `parent_asin`, and retain source provenance for diagnostics.
- CandidateGate may skip an expensive semantic reranker, but it must not silently reduce the category recall budget before cheap feature ranking.
- Keep the semantic reranker input separately bounded at no more than 30 candidates.

### Ranking and observability

- Apply category and hard-constraint relevance before popularity priors; a popular unrelated product must not outrank a relevant category match merely because of rating count.
- The proportional budget defines only how many large-category candidates survive. It must not be implemented as a popularity sample: score the complete resolved category with current hard constraints, avoided values, query terms, and soft preferences before applying the budget. Rating count/rating may only act as a weak final prior or deterministic tie-break among equally relevant products.
- Record the resolved category, category size, recall ratio, category-route budget, route candidate counts, union size, and target-free cutoff diagnostics.
- Provide an offline diagnostic that measures retrieval Recall@30/100/200 and feature-stage Recall@30 without exposing ground truth to Agent runtime.

### Compatibility and scope

- Do not modify `evaluator/local_evaluator.py`, public labels, metric formulas, or Agent response fields.
- Use only the Python standard library and the existing in-memory SQLite catalog path.
- Do not add dense embeddings, Qwen changes, learned ranking, evaluator-aware shortcuts, or product-specific exceptions in this task.
- This task is a prerequisite for the Qwen reranker benchmark. Freeze the adaptive retrieval candidate pipeline and its feature-only traces before any further Qwen metric run.
- All acceptance and ablation runs in this task must instantiate the deterministic feature-only Agent explicitly; DeepSeek, Qwen, local model endpoints, and every other LLM/model backend must remain disabled.

## Acceptance Criteria

- [ ] Categories with 500 or fewer products receive a complete category-route budget.
- [ ] Categories above 500 products use the specified 50%/35%/20% ratio and 100–400 clamp deterministically.
- [ ] Exact category-route candidates survive route union up to their allocated quota and remain catalog-valid and unique.
- [ ] CandidateGate over-generality can skip semantic ranking without truncating the cheap category/feature candidate pool.
- [ ] Unit tests cover a small category, a large category at all three specificity levels, an unknown category, deterministic ties, quota preservation, and the semantic Top-30 boundary.
- [ ] An offline report compares fixed-budget and adaptive-budget Retrieval Recall@30/100/200, Feature Recall@30, public HitRate@10, MRR, MTTC, TechnicalScore, and latency.
- [ ] `public_0028` is reported as a diagnostic case, but no sample ID or ground-truth ASIN appears in production Agent logic.
- [ ] Full unittest passes, `git diff --check` passes, and the frozen evaluator is unchanged.
- [ ] The adaptive strategy is retained only if it improves recall or official metrics without unacceptable scenario-level or latency regression; otherwise the fixed-budget path remains available for rollback.

## Notes

- Catalog inspection on 2026-08-26 found 370 leaf `Wallets`, 137 `Mid-Calf`, 192 `Jumpsuits`, 260 `Belts`, 499 `Tanks & Camis`, and 2,807 `T-Shirts`; the leaf-category median was 11. This distribution motivates a bounded proportional budget rather than a single Top-100 cutoff.
- In the current trace, `public_0028` reached approximately retrieval rank 108 after its disclosed wallet/material/color constraints, then disappeared at the fixed Top-100 feature cutoff.
