# Document evaluator comparison results

## Goal

Create one durable, evidence-backed comparison report for the four evaluator
runs completed on 2026-08-31, with a static chart that makes the quality,
latency, token, and cost trade-offs easy to inspect.

## Requirements

- Write an Explanation/Reference Markdown document for team members and
  competition-report readers.
- Record these four runs without treating their scores as one common
  leaderboard:
  - mechanical public-set evaluator, 200 samples, official offline Agent;
  - mechanical public-set evaluator, 200 samples, DeepSeek model-first Agent;
  - intelligent natural-language simulator v2, frozen 100 samples, official
    offline Agent;
  - intelligent natural-language simulator v2, frozen 100 samples, DeepSeek
    model-first Agent.
- Include test configuration, sample counts, score metrics, wall time, token
  usage, estimated model cost, scenario breakdowns, limitations, result paths,
  and reproduction commands.
- Clearly distinguish the mechanical 200-sample dataset from the independent
  intelligent 100-sample benchmark.
- Explain that reported model usage covers successful backend responses and
  may omit token consumption from failed validation attempts.
- Generate two independent static analysis figures: one for the official
  evaluator (200 samples) and one for the intelligent natural-language
  evaluator v2 (100 frozen samples). Never place both datasets on the same
  canvas or shared scale.
- Use metric-appropriate encodings: dumbbell charts for two-configuration
  quality comparisons, faceted scenario charts, stacked token bars, latency
  dot plots, and cache-assumption cost intervals. Do not share axes across
  unlike units.
- Replace chartable result tables in the report with the two figures and
  concise prose. Keep exact values in direct labels or nearby text.
- Call the primary benchmark the "official evaluator"; do not label it
  "mechanical" or "mechanical public set".
- Do not expose API keys or copy per-sample target IDs/traces into the report.
- Preserve all existing result files and unrelated worktree changes.

## Acceptance Criteria

- [x] A Markdown report exists under `docs/development/` and links to the chart.
- [x] The report contains a verified four-run summary table with correct units.
- [x] Cross-evaluator comparisons carry an explicit comparability warning.
- [x] Token and cost calculations state pricing and cache assumptions.
- [ ] Two independent PNG/SVG figure pairs are generated under
      `docs/diagrams/` and render legibly.
- [ ] Every number in each figure is traceable to the report or result
      artifact.
- [x] Markdown links, image path, and reproduction commands are checked.

## Notes

- This is a lightweight, documentation-only task; PRD-only planning is
  sufficient.
- The DeepSeek price reference used during the runs was the 2026 official
  pricing page: V4 Flash cache-miss input/output rates were $0.22/$0.66 per
  million tokens off-peak and $0.44/$1.32 per million tokens at peak.
