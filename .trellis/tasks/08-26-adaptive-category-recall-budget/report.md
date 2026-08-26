# Adaptive Category Recall Benchmark Report

## Decision

Accept the adaptive category recall pipeline as the frozen feature-only candidate generator for the subsequent Qwen reranker benchmark.

The implementation uses no LLM, embedding model, API, or network backend. Both fixed and adaptive runs reported zero model tokens.

## Configuration

- Resolved category size `<=500`: recall the complete category.
- Resolved category size `>500`:
  - no hard constraints: 50%;
  - one hard constraint: 35%;
  - two or more hard constraints: 20%;
  - clamp to 100–400 candidates.
- Score the complete resolved category with hard/avoid constraints, current query evidence, and soft preferences before applying the large-category budget.
- Use rating count/rating only as a weak final tie-break among equally relevant products.
- Preserve a bounded non-category lexical/constraint tail of 100 candidates.
- Absolute cheap candidate-pool cap: 600.
- Semantic/model input remains capped at Top-30; this benchmark injected a deterministic passthrough and loaded no model.

## Data Integrity

- Frozen public set: 200 sessions.
- Exact coarse category alias coverage: 200/200 public targets belong to the resolved catalog set.
- Production Agent/catalog code contains no `ground_truth`, `sample_id`, public target ASIN, or evaluator import.
- Evaluator, catalog, and public labels were not modified.

## Competition Metrics

| Metric | Fixed | Adaptive | Delta |
| --- | ---: | ---: | ---: |
| TechnicalScore | 0.710956 | 0.747367 | +0.036411 |
| HitRate@10 | 0.855 | 0.905 | +0.050 |
| MRR | 0.557188 | 0.543222 | -0.013966 |
| MTTC | 5.185 | 4.405 | -0.780 |

The adaptive pipeline finds 10 additional sessions and converts earlier on average. MRR falls slightly because the broader candidate pipeline adds lower-ranked hits and perturbs some existing Top-10 orders. All scenario-level TechnicalScores improve; reranking is the next stage responsible for recovering MRR.

## Recall Metrics

| Metric | Fixed | Adaptive | Delta |
| --- | ---: | ---: | ---: |
| Retrieval Recall@30 | 0.770 | 0.990 | +0.220 |
| Retrieval Recall@100 | 0.905 | 1.000 | +0.095 |
| Retrieval Recall@200 | 0.930 | 1.000 | +0.070 |
| Feature Recall@30 | 0.880 | 0.965 | +0.085 |

Feature Recall@30 gains 17 sessions with no losses. Retrieval Recall@100 and Recall@200 reach 1.0 with no losses; Recall@30 gains 45 sessions and loses one.

## `public_0028` Diagnostic

Fixed retrieval placed the target at rank 108 from turn 3 onward, so the fixed Top-100 feature cutoff never saw it.

With adaptive recall, the target enters retrieval near the top and reaches Feature Top-30 late in the session:

| Turn | Retrieval rank | Feature rank |
| ---: | ---: | ---: |
| 1 | 22 | 157 |
| 2 | 3 | 104 |
| 3 | 2 | 88 |
| 8 | 10 | 39 |
| 9 | 13 | 29 |
| 10 | 6 | 19 |

It still does not reach Top-10 without reranking. This makes it a valid reranker test case instead of an impossible recall miss.

## Runtime and Artifacts

- Fixed p50/p95 per turn: 137/258 ms.
- Adaptive p50/p95 per turn: 219/397 ms.
- Runtime does not enter TechnicalScore; it is retained as a feasibility guard only.
- Full target-free trace and post-hoc metrics:
  `/Volumes/PeeB/ai-models/techjam/benchmarks/results/adaptive-category-recall-full.json`
- Artifact size: approximately 27 MB.

## Validation

```text
44 unittest tests passed
git diff --check passed
reported_token_usage = 0
category target coverage = 200/200
```

## Handoff to Qwen Benchmark

The Qwen benchmark must use the adaptive feature-only pipeline and the same frozen public manifest. Qwen may rerank at most Feature Top-30 and must not change retrieval, category budgets, state updates, or clarification behavior during the ablation.
