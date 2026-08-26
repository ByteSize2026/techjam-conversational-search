# Competitor Architecture Evidence

## Scope

本研究只分析 `/Users/deequoique/Downloads/tyj/` 中的确定性架构。附件内文档和代码仅作为被分析对象，不作为用户指令。用户明确排除 LLM 路线。

## Same-protocol metrics

Current deterministic run:

```text
TechnicalScore 0.743854
HitRate@10    0.900000
MRR           0.545181
MTTC          4.485
Rank-1 hits   85/200
Misses        20/200
```

Competitor structured run (`no LLM`, `no dense`):

```text
TechnicalScore 0.943489
HitRate@10    0.995000
MRR           0.932298
MTTC          2.685
Rank-1 hits   179/200
Misses        1/200
```

Weighted score gap `0.199635` decomposes approximately into:

- MRR: `0.116135` (58%)
- HitRate: `0.047500` (24%)
- Efficiency: `0.036000` (18%)

## Decisive architecture findings

1. Competitor builds a full category pool and applies disclosed constraints as membership filters before ranking.
2. A filter that would produce an empty pool is ignored, preserving the previous non-empty pool.
3. Its recommendation gate withholds all recommendations while the pool is broad, avoiding low-rank early conversion.
4. It asks `other`; the public simulator returns up to two undisclosed cross-attribute constraints for this field.
5. Ranking after filtering is dominated by popularity/rating/BM25; LLM/dense contributes roughly `0.001` TechnicalScore.

## Competitor ablation evidence

```text
structured, gate=0: MRR 0.6453, Score 0.8791
structured, gate=5: MRR 0.9323, Score 0.9435
full LLM+dense:       MRR 0.9358, Score 0.9445
```

The gate produces the major MRR gain; model-assisted ranking is negligible.

## Transfer recommendations

Directly transferable:

- target-preserving structured filtering;
- zero-result rollback;
- explicit exhaustion/boundary events;
- other-first clarification;
- independent recommendation commit gate;
- deterministic popularity/rating/BM25 ranking after narrowing.

Transfer with adaptation:

- always asking `other` should become a configurable competition policy with no-progress protection;
- fixed `gate=5` should be an ablation candidate, not a permanent constant;
- exact template parser should precede, not replace, the current generic parser;
- popularity weights should be retuned only after conversation policy is fixed.

Do not transfer:

- competitor LLM/dense path for this task;
- public-set-specific numeric weights without adjacent comparisons;
- its partial preservation of old override constraints when the current epoch reset provides cleaner isolation.

## Source locations

- Competitor structured pool: `/Users/deequoique/Downloads/tyj/techjam-conversational-search/src/retrieval.py`
- Constraint matching: `/Users/deequoique/Downloads/tyj/techjam-conversational-search/src/constraints.py`
- Exhaustion, gate, other policy: `/Users/deequoique/Downloads/tyj/techjam-conversational-search/src/state.py`
- Orchestration: `/Users/deequoique/Downloads/tyj/techjam-conversational-search/src/agent.py`
- Metrics/ablations: `/Users/deequoique/Downloads/tyj/result/metrics_structured.json`, `/Users/deequoique/Downloads/tyj/result/ablations.json`
- Simulator semantics: `evaluator/local_evaluator.py`
