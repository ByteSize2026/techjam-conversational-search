# Qwen3 Reranker Benchmark Report

## Decision

Close this experiment with **validation evidence of benefit**, but do not enable the reranker by default yet. The Colab T4 run shows a material same-split gain; the locked split, the original M1 resource envelope, and the final prompt/template configuration remain follow-up work.

## Frozen deterministic anchor

The accepted adaptive feature-only pipeline was reproduced locally on all 200 public sessions:

| Metric | Feature-only |
| --- | ---: |
| TechnicalScore | 0.743854 |
| HitRate@10 | 0.900 |
| MRR | 0.545181 |
| MTTC | 4.485 |

This is a full-public-set anchor and must not be compared directly with the seed-17 validation subset below.

## Colab T4 validation result

Both configurations ran on the same 40 seed-17 validation sessions with the same live deterministic Agent. Qwen only reranked Feature Top-30 candidates.

| Metric | Feature-only | Qwen Top-30 | Delta |
| --- | ---: | ---: | ---: |
| TechnicalScore | 0.681467 | 0.734271 | +0.052804 |
| HitRate@10 | 0.825 | 0.875 | +0.050 |
| MRR | 0.483224 | 0.567569 | +0.084345 |
| MTTC | 4.800 | 4.675 | -0.125 |

The observed gain is real for this exact validation configuration: all four competition metrics improved, including two additional Top-10 hits among 40 sessions.

## Runtime and reproducibility

- GPU: Tesla T4, CUDA 12.8.
- Model: `Qwen/Qwen3-Reranker-0.6B`.
- Revision: `e61197ed45024b0ed8a2d74b80b4d909f1255473`.
- Candidate limit: 30; batch size: 8; fusion weight: 1.0.
- Source delivery: guarded source ZIP upload, so the private repository requires no GitHub token in Colab.
- Model snapshot is downloaded once, then benchmark execution forces offline loading.

## Limitations and follow-up

- The locked 40-session split was intentionally not run or tuned against, so this task does not make a final generalization claim.
- The Colab validation command did not supply `--frozen-trace`. Because baseline and Qwen used the same live Agent and split, the within-run comparison remains valid, but it is not the strict frozen-candidate replay promised by the original experiment plan.
- The task began as an M1 16 GB feasibility benchmark. Local M1 inference was too slow and mostly timed out; the successful metric evidence is from Colab T4, not an accepted M1 deployment profile.
- The current adapter manually embeds `<Instruct>` and `<Query>` labels in its query text while the checkpoint also provides its own prompt/chat template defaults. The measured result is valid for that exact prompt, but the shopping instruction wiring should be audited against the official model-card construction before a final locked run.
- Qwen remains opt-in. Enabling it by default requires a clean prompt/template configuration, a frozen validation rerun if that configuration changes, one locked evaluation, and an explicit deployment/resource decision.

## Validation

- Full unittest suite: 51/51 passed.
- Notebook JSON and code-cell syntax checks passed.
- Public deterministic evaluator reproduced the 200-session adaptive anchor exactly.
- Source ZIP recipe audit found no catalog, credentials, repository metadata, or model weights.
- `git diff --check` and Trellis context validation passed.

