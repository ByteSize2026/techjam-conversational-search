# Qwen3 Reranker Benchmark Design

## Decision to Test

Insert a specialized cross-encoder after deterministic feature ranking:

```text
state + retrieval
  -> Feature Ranker Top-30
  -> Qwen3-Reranker-0.6B pair scores
  -> whitelist repair / score fusion
  -> Top-10
```

This experiment targets MRR and Top-1 placement. It does not increase candidate recall and therefore does not replace the later dense-retrieval experiment.

## Runtime Choice

The official checkpoint is a Qwen3 causal model adapted for text ranking. Its score is derived from the relative logits of `yes` and `no` after a fixed query/document instruction. The selected runtime order is:

1. `sentence-transformers` CrossEncoder on Apple MPS, using the official checkpoint and pinned revision.
2. Direct Transformers on MPS if CrossEncoder lacks a required batching/control surface.
3. CPU fallback for correctness/latency comparison only.

Ollama is excluded. A chat completion that merely generates `yes` is insufficient because the experiment needs a comparable relevance score for every candidate. The official CrossEncoder interface exposes the required ranking score directly.

## External Storage Layout

```text
/Volumes/PeeB/ai-models/techjam/
  runtimes/
    qwen3-reranker-venv/
  models/
    huggingface/
  caches/
    pip/
    tmp/
  benchmarks/
    manifests/
    results/
    traces/
```

Every setup/benchmark command must export at least:

```text
HF_HOME=/Volumes/PeeB/ai-models/techjam/models/huggingface
PIP_CACHE_DIR=/Volumes/PeeB/ai-models/techjam/caches/pip
TMPDIR=/Volumes/PeeB/ai-models/techjam/caches/tmp
```

The Python virtual environment also lives on PeeB. Benchmark code must enable offline loading after the initial download.

## Ranking Contract

The reranker receives:

- bounded distilled intent and active constraints;
- at most 30 catalog-valid candidates;
- compressed product text: title, leaf categories, store, price, and the most relevant feature/detail evidence;
- an English instruction tailored to shopping requirement satisfaction.

It returns one finite score per input candidate. Python remains responsible for candidate identity, tie-breaking, missing/NaN repair, timeout fallback, and Top-10 construction. A failed batch preserves Feature Ranker order.

Initial integration should compare pure reranker ordering with a small, predeclared fusion grid between normalized feature and reranker scores. Do not tune per sample.

## Experiment Design

1. Freeze a scenario-stratified manifest before model/prompt tuning.
2. Materialize deterministic Top-30 candidate traces once so all reranker configurations see identical inputs.
3. Establish runtime baselines for MPS and CPU with fixed batch sizes.
4. Compare feature-only, reranker-only, and limited score fusion.
5. Run the locked evaluation once after configuration selection.

Primary decision metric is TechnicalScore, with MRR and Top-1 hit rate as direct reranking diagnostics. HitRate@10 is a guardrail. MTTC is reported but is not expected to improve substantially because the question policy is unchanged.

## Resource and Rollback Gates

- External assets and runtime remain below 8 GB.
- No implicit system-disk model/cache writes.
- No unhandled model failure crosses the Agent boundary.
- Reranker is not default unless metric improvement is stable and latency is suitable for the evaluator.
- Rollback is a single feature flag returning to deterministic Feature Ranker order.

## Known Limitation

Qwen3-Reranker-0.6B is trained for general text relevance, not Amazon purchase prediction. A negative result is valid evidence; it should not be hidden by repeated prompt/weight tuning on all 200 public targets.
