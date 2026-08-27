# Seed-2026 fixed-200 result

## Decision

PASS. Retain the deterministic provenance-aware scoped Intent Override behavior in `LegacyAgent`.

The first-round adoption gate in `prd.md` is satisfied. Public 200 and fixed 800 were intentionally not run.

## Validation

- Focused provenance/contract tests: passed.
- Full suite: `109/109` passed.
- `git diff --check`: passed.
- Python compilation for changed product modules: passed.
- Trellis context manifests: passed validation.
- Independent `trellis-check`: PASS with no remaining correctness findings.

## Fixed 200 metrics

Dataset: `holdout/holdout_200.jsonl` (seed 2026, 200 rows, scenario mix 80/80/30/10, target-disjoint from public 200).

Entry point: explicit `starter.agent.LegacyAgent` with offline `AgentConfig()`.

| Scope | Metric | Baseline | Scoped override | Delta |
| --- | --- | ---: | ---: | ---: |
| Overall | Hit@10 | 0.970000 | 0.985000 | +0.015000 |
| Overall | MRR | 0.707343 | 0.722524 | +0.015181 |
| Overall | MTTC | 3.435000 | 3.295000 | -0.140000 |
| Overall | TechnicalScore | 0.848503 | 0.863357 | +0.014854 |
| Intent Override (30) | Hit@10 | 0.800000 | 0.900000 | +0.100000 |
| Intent Override (30) | MRR | 0.348373 | 0.449577 | +0.101204 |
| Intent Override (30) | MTTC | 6.600000 | 5.666667 | -0.933333 |

Unchanged scenario metrics:

- Buying (80): Hit `1.000000`, MRR `0.785863`, MTTC `2.687500`.
- Browsing (80): Hit `1.000000`, MRR `0.769340`, MTTC `2.887500`.
- Boundary (10): Hit `1.000000`, MRR `0.660119`, MTTC `4.300000`.

## Remaining misses

- `holdout_0130`
- `holdout_0135`
- `holdout_0192`

For all three, the target remains in the structured candidate pool after the explicit override but is outside feature rank Top-10. The commit policy eventually submits ten items, so these are ranking failures rather than recall or submission-timing failures.

## Review fixes

Independent checking found and the implementation corrected four semantic edge cases before adoption:

1. An override X already disclosed as a soft clarification now upgrades the existing constraint and same-text query evidence to hard/current-epoch/override provenance.
2. Payload-free `Start over.` / `Forget everything.` no longer becomes feature evidence.
3. Explicit single-attribute replacement takes precedence over broad referenced-override matching.
4. Global reset explicitly clears legacy writable `query_terms` when no provenance objects exist, preventing compatibility fallback from resurrecting stale evidence.

