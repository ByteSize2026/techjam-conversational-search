# Trace Runner Baseline Evidence

## Repository State

- Branch at planning: `feat/ecom-agent-architecture`
- HEAD at planning: `254abd07754ffeced73bfbe228616b2e82af80d2`
- Existing unrelated changes observed and excluded: `.trellis/.template-hashes.json`, `.agents/`, `.codex/`, `.trellis/tasks/08-27-robust-evaluator-feasibility/`.

## Frozen Inputs

- `data/catalog.jsonl`: `da979b05a68af864cb0dcf9ee6a81c010c7e66a57978ad286c7a2e005fc69a67`
- `data/public_set.jsonl`: `857259f7a438e6188ac63e18995b6ff4489bfcfc4a716a798b9a2aa0ee8f7579`
- Public sample count: 200.

## Historical Deterministic Reference

Source: archived `08-27-ecom-agent-architecture/research/baseline.md` and the surviving local `/tmp/ecom-agent-after-deterministic.json`.

- HitRate@10: `0.900000`
- MRR: `0.545181`
- MTTC: `4.485000`
- Efficiency: `0.651500`
- Recommended TechnicalScore: `0.743854`
- Reported token usage: `0`

This result was produced with tool planning disabled and no model environment variables configured. It is a regression reference only, not a model-control experiment.

## Current Observability Gaps

- `TrajectoryEntry` records action, bounded arguments, observation, status and elapsed tool time, but orchestrator intentionally drops free-text rationale and does not attach per-step backend/usage/failure/context.
- `Agent.last_diagnostics` exposes only the most recent turn, although current tool-loop diagnostics include that turn's `tool_trajectory`.
- `SessionState.tool_trajectory` is bounded in memory and is not persisted by the evaluator.
- `_fill_tool_recommendations` may add deterministic-ranked and popular IDs after the planner terminal action without recording per-ID provenance.
- Official evaluator persists only session score summaries; it does not persist user messages, Agent responses, state transitions or tool steps.

## Runtime Availability

- Real `catalog.jsonl` and `public_set.jsonl` are locally present.
- `.env` declares DeepSeek, local-model, tool-planning and reranker configuration keys. Values were not read into this artifact.
- The active shell did not have these variables exported during planning; the benchmark command must explicitly source `.env`.
