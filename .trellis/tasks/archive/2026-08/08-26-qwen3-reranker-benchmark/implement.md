# Qwen3 Reranker Benchmark Implementation Plan

## Gate

The adaptive category recall task is now a prerequisite. Do not run further Qwen metric evaluation until `.trellis/tasks/08-26-adaptive-category-recall-budget` freezes the accepted deterministic feature-only Top-30 traces. Existing adapter/runtime work may be preserved, but candidate generation must not be tuned concurrently with reranking.

## Phase 0 — Storage and Runtime Preflight

- [x] Verify that no Ollama model was pulled, then uninstall the unused Homebrew Ollama formula.
- [ ] Create the PeeB storage layout and export Hugging Face, pip, and temp cache variables.
- [ ] Verify free space, filesystem writability, and cleanup commands.
- [x] Select the official Sentence Transformers CrossEncoder path on Apple MPS with CPU fallback; Ollama is out of scope.

Gate: no large download starts until every target path resolves under `/Volumes/PeeB/ai-models/techjam`.

## Phase 1 — Frozen Baseline and Candidate Traces

- [ ] Re-run the 16 unit tests and deterministic 200-session anchor.
- [ ] Create a deterministic scenario-stratified experiment manifest.
- [ ] Add an experiment-only trace that records distilled query, candidate IDs/text, feature order, turn, route, and gate decision without exposing targets to Agent runtime.
- [ ] Materialize identical Top-30 inputs for all reranker configurations.

Gate: trace replay reproduces the feature-only order and public anchor within exact deterministic expectations.

## Phase 2 — External Model Installation

- [ ] Create the Python environment under PeeB and install pinned dependencies with caches redirected to PeeB.
- [ ] Download `Qwen/Qwen3-Reranker-0.6B` at a pinned revision into `HF_HOME` on PeeB.
- [ ] Record license, revision, file manifest, checksums, total bytes, and package versions.
- [ ] Prove offline model load with network disabled/offline environment flags.
- [ ] Audit system cache directories for accidental large writes; remove setup residue.

Gate: offline score for a known relevant/irrelevant pair is deterministic and all large assets are on PeeB.

## Phase 3 — Reranker Adapter

- [ ] Implement bounded batch scoring for at most 30 candidates.
- [ ] Add shopping-specific instruction and compressed product document format.
- [ ] Validate score count, finite values, whitelist preservation, stable ties, and timeout/failure fallback.
- [ ] Skip reranking on CandidateGate over-general path.
- [ ] Add config flags for runtime, device, batch size, candidate limit, timeout, and score-fusion weight.

Gate: unit tests cover valid ranking, malformed scores, missing model, offline load failure, whitelist repair, and gate skip.

## Phase 4 — M1 Performance and Metric Ablation

- [ ] Benchmark MPS and CPU cold start, warm p50/p95/max, peak RSS, and batch sizes.
- [ ] Compare feature-only, reranker-only, and a small predefined fusion grid.
- [ ] Report overall and per-scenario HitRate, MRR, MTTC, Top-1 rate, and TechnicalScore.
- [ ] Select configuration on development/validation data, then run locked evaluation once.
- [ ] Quantify metric gain per added second and per GB.

Gate: enable by default only with stable score gain, acceptable latency, no meaningful HitRate regression, and resource compliance.

## Phase 5 — Integration or Rejection

- [ ] If accepted, document one offline setup/run command and deterministic fallback behavior.
- [ ] If rejected, preserve benchmark evidence, keep feature ranker default, and remove model/runtime assets if the user does not want them retained.
- [ ] Confirm the unused Homebrew Ollama formula is removed and no Ollama model data remains on the system disk.
- [ ] Run full unittest and public evaluator on the final default configuration.

## Validation Commands

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output /tmp/qwen3-reranker-result.json
```

Environment setup commands will be added only after runtime compatibility and storage paths are verified; they must never rely on default system-disk caches.

## Rollback

- Disable the reranker feature flag.
- Restore Feature Ranker order without changing retrieval or state.
- Remove only the task-owned directory under `/Volumes/PeeB/ai-models/techjam`; do not delete shared user data.
