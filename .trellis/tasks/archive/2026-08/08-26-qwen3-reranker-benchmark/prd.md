# Benchmark Qwen3 Reranker for Shopping Agent

## Goal

Evaluate Qwen3-Reranker-0.6B on M1 16GB for Top-K shopping reranking, MRR/TechnicalScore gain, latency and memory, with all runtimes, model weights and caches stored on /Volumes/PeeB.

## Requirements

### Benchmark objective

- Use `Qwen/Qwen3-Reranker-0.6B` as a bounded cross-encoder over the deterministic Agent's Top-30 candidates.
- Measure whether the reranker improves MRR, Top-1 hit rate, and overall TechnicalScore without materially reducing HitRate@10.
- Keep retrieval, session state, Intent Override, CandidateGate, and clarification behavior unchanged so the experiment isolates reranking value.
- Compare feature-only order against reranking only on focused turns; over-general turns must continue to skip the expensive reranker.

### Hardware and storage

- Target machine is Apple M1 with 16 GB unified memory and no guaranteed discrete GPU.
- All newly downloaded runtimes, model weights, Hugging Face caches, package caches, temporary extraction files, and benchmark artifacts must live under `/Volumes/PeeB/ai-models/techjam`.
- Do not silently fall back to `~/.ollama`, `~/.cache/huggingface`, Homebrew caches, or system temporary storage for large downloads.
- The complete external-disk footprint for this experiment must remain below 8 GB.
- The interrupted prior setup installed Homebrew Ollama `0.33.0` on the system disk, but no model was pulled. The user selected the official Sentence Transformers path; remove the unneeded Ollama installation before downloading model assets.

### Model and runtime integrity

- Pin the exact Hugging Face revision and record license, files, byte sizes, and checksums/manifest before evaluation.
- Use the official `sentence-transformers` CrossEncoder interface on Apple MPS, with a CPU correctness fallback, because the model is a text-ranking checkpoint rather than a normal chat model.
- Run offline after assets are downloaded; benchmark execution must not fetch or update models.
- The model may only score/reorder catalog-valid candidates supplied by the deterministic pipeline. Unknown or missing IDs cannot be introduced.

### Evaluation discipline

- Preserve the current deterministic public anchor: TechnicalScore `0.710956`, HitRate@10 `0.855`, MRR `0.557188`, MTTC `5.185`.
- Create a deterministic scenario-stratified experiment split/manifest before tuning reranker instruction, candidate limit, batch size, or score fusion.
- Record initialization time, per-turn p50/p95/max latency, peak RSS, model/cache size, fallback count, and scenario-level competition metrics.
- Do not use target IDs or hidden intent cards at runtime. Ground truth may only be used by the offline evaluator.

### Scope boundary

- In scope: Qwen3-Reranker-0.6B runtime selection, external-disk installation, Top-30 scoring adapter, caching/batching, ablation, and integration decision.
- Out of scope: dense embedding retrieval, generic 8B chat inference, dialogue-policy learning, fine-tuning, and changes to the evaluator's scoring semantics.

## Acceptance Criteria

- [ ] No new model/runtime/cache artifact larger than 10 MB is stored on the system disk; external footprint is measured and below 8 GB.
- [ ] Model revision, license, manifest, offline load procedure, and one reproducible benchmark command are documented.
- [ ] Feature-only and reranked configurations run on the same frozen split and candidate inputs.
- [ ] Candidate whitelist, model failure fallback, offline mode, and CandidateGate skip behavior have automated tests.
- [ ] M1 benchmark reports cold start, p50/p95/max response latency, peak RSS, and asset size.
- [ ] The reranker is enabled by default only if it produces a stable MRR or TechnicalScore improvement without meaningful HitRate regression and stays within the agreed resource envelope.
- [ ] If the gate fails, the result is documented and the deterministic feature ranker remains the default; model installation can be cleanly removed from PeeB.

## Notes

- Official model: `Qwen/Qwen3-Reranker-0.6B`, Apache-2.0, 595,776,512 parameters, approximately 1.2 GB of published repository storage at the inspected revision.
- The current public score used no LLM or reranker and reported zero model tokens.
