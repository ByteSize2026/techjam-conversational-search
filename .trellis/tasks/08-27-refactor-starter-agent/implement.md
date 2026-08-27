# Implementation Plan

## 1. Establish the baseline

- Run the full unittest suite before edits and record any pre-existing failure.
- Compile/import the current Agent and note the starting line count.

## 2. Extract retrieval

- Add `starter/shopping_agent/retrieval.py` with route retrieval, category quota,
  merging, diversification, source counting, and diagnostics.
- Wire an instance into `Agent` and keep `_retrieve`, `_diversify`, and
  `_source_counts` delegates.
- Run retrieval/adaptive-category and contract tests.

## 3. Extract deterministic ranking

- Add `starter/shopping_agent/ranking.py` with candidate stats, feature ranking,
  lexical evidence, entropy/attribute extraction, and rank evidence.
- Keep `Agent` delegates, especially dynamic `_feature_rank` dispatch required by
  frozen-trace benchmarking.
- Run deterministic-policy, adaptive-category, and benchmark tests.

## 4. Extract response and semantic helpers

- Add `starter/shopping_agent/response.py` for contract guards, fallback/ID
  validation, semantic application, usage/failure extraction, and response copy.
- Keep equivalent `Agent` methods as delegates where current consumers or turn
  orchestration expect them.
- Run contract, model fallback, and Qwen reranker tests.

## 5. Integration and review

- Run `python3 -m unittest discover -s tests -v`.
- Compile changed Python modules and verify `starter.agent.Agent` imports.
- Compare diagnostics keys and inspect the diff for accidental policy/weight or
  evaluator/data changes.
- Confirm `starter/agent.py` is materially smaller and each new module has one
  clear responsibility.

## Review gates

- Stop and restore the prior boundary if any direct internal consumer cannot be
  supported by a small delegate.
- Treat output ordering, scores, recommendation counts, diagnostics, and message
  text changes as regressions unless a current test proves the old behaviour was
  invalid.
