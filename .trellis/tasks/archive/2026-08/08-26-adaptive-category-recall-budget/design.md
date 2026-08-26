# Adaptive Category Recall Budget Design

## Decision

Introduce a cheap category index and a quota-preserving multi-route recall stage before feature ranking:

```text
session state
  -> resolve most-specific catalog category
  -> adaptive category budget
  -> category route + lexical/constraint/profile routes
  -> quota-preserving deterministic union
  -> cheap feature ranking over the expanded pool
  -> optional semantic reranking of Top-30 only
  -> Top-10 response
```

The purpose is boundary recall. It does not change evaluator semantics and it does not use a model. Its frozen candidate traces become the input contract for the later Qwen reranker benchmark.

## Category Index and Resolution

`CatalogRepository` will build normalized lookup structures while loading the immutable catalog:

- normalized category label -> ordered unique product IDs;
- normalized complete category path/label aliases -> canonical label;
- canonical label -> category size.

Resolution prefers the longest and most specific matching catalog label present in the bounded category anchor. It must use normalized token/phrase matching, not substring matches such as `men` inside `women`. When several incomparable labels match, prefer the smaller catalog set only when one label is an exact trailing phrase; otherwise report ambiguity and use the global bounded fallback.

## Budget Function

The budget is a pure function of category size and hard-constraint count:

```python
if category_size <= 500:
    budget = category_size
else:
    ratio = 0.50 if hard_count == 0 else 0.35 if hard_count == 1 else 0.20
    budget = min(category_size, max(100, min(ceil(category_size * ratio), 400)))
```

Unknown categories use the existing bounded retrieval budget and do not trigger a full-catalog scan.

## Route Quotas and Union

Category retrieval receives its computed budget independently. Other lexical routes retain bounded per-route limits. The merger operates in two stages:

1. add up to the category quota in category-route rank order;
2. interleave the remaining routes with deterministic reciprocal-rank fusion, deduplicating IDs.

The final cheap candidate pool is bounded by a separately configured maximum. If the category budget itself is larger, the maximum must be at least the allocated category quota so the quota contract is not violated.

For categories above 500, the proportional budget controls output count only. The repository must not choose that subset by popularity. It first scores every member of the resolved category with current hard constraints, avoided values, query terms, and soft preferences, then applies the budget. Popularity and average rating are weak final ranking priors or deterministic tie-breaks among equally relevant candidates, never recall filters. Categories at or below 500 are returned completely.

## CandidateGate Boundary

CandidateGate answers one question only: whether the expensive semantic ranker should run. Its over-general cutoff may reduce recommendations and trigger clarification, but must not truncate the cheap category-route union before feature ranking. This separates:

- recall breadth: category budget and route quotas;
- model cost: semantic Top-30;
- response width: evaluator Top-10.

## Diagnostics and Evaluation

Production diagnostics contain no target ID. They record category resolution and route sizes. A separate offline diagnostic may join public ground truth after Agent execution to compute:

- retrieval Recall@30/100/200;
- feature Recall@30;
- route contribution and first target rank;
- evaluator HitRate@10, MRR, MTTC, TechnicalScore;
- p50/p95/max latency and peak candidate count.

`public_0028` is a named debugging case only in reports/tests that exercise the public fixture, never in runtime ranking code. Every task-owned evaluation must construct `AgentConfig()` with no Qwen path, API key, or local model endpoint; model token usage must remain zero.

## Rollout and Rollback

The adaptive budget is introduced behind configuration defaults that allow the previous fixed budget to be restored. Rollout requires unit tests, a target-free trace audit, a public-set ablation, and scenario-level regression review. Rollback disables adaptive category budgets without changing session state or response contracts.

## Known Risks

- Catalog category strings are inconsistent and may contain hierarchy fragments; resolution must be tested against combined anchors such as `Card Cases & Money Organizers Wallets`.
- Full recall for many small categories is cheap individually, but mixed or ambiguous category anchors must remain bounded.
- Increasing the pool can improve recall while harming MRR; the feature ranker and popularity weights must be evaluated separately rather than tuned simultaneously.
- Existing uncommitted Qwen integration overlaps `starter/agent.py`; adaptive retrieval changes must preserve the opt-in adapter while all task-owned tests keep it disabled. After this task freezes feature-only Top-30 traces, the Qwen task resumes against those identical candidates.
