# EComAgentBench compatibility research

## Sources inspected

- Upstream `Morizeyao/EComAgentBench_`, shallow-cloned on 2026-08-29.
- Upstream README, config, prediction agent/runner, tools, product database,
  evaluation runner, judge, metrics, and released benchmark.
- Current Agent contract, local evaluator, catalog, retrieval/ranking/state,
  documentation, and Trellis specs.
- Hugging Face API metadata for the prebuilt database.

## Measured facts

- Released samples: 662; unique targets: 660.
- Local catalog: 50,000 products; target overlap: 0.
- Prebuilt database: 26,905,755,648 bytes.
- Available project-volume space at research time: about 806 GiB.
- Intents: product_search 83, knowledge_reasoning 106, rating_quality 80,
  feature_combination 104, negative_constraint 57, use_case_scenario 104,
  coupon_budget 33, review_driven 95.

## Protocol comparison

| Dimension | Current project | EComAgentBench |
| --- | --- | --- |
| Entry | `Agent.reset/respond` | LLM tool loop |
| Horizon | 10 dialogue turns | 100 tool steps default |
| Catalog | 50k JSONL | ~3.72m SQLite products |
| Retrieval | in-process FTS/ranking | title BM25 + details/filter/review tools |
| Profile | aggregate profile at reset | synthetic `get_user_profile` |
| Clarification | structured `ask_attribute` | free-text `ask_user` |
| Final answer | Top-10 `parent_asin` | one `product_id` |
| Scoring | Hit@10/MRR/MTTC | exact OR rubric satisfaction |
| Judge | not required | Gemini for rubric metrics |

## Conclusion

Direct execution is invalid because all released targets are outside the local
catalog and the APIs differ. A meaningful test requires the official database
and a bridge. The bridge diagnoses transfer behavior but is not a native
EComAgentBench leaderboard reproduction.

## Revised direction after requirement clarification

The desired benchmark is not substitute-friendly retrieval. It must retain one
selected catalog product as the only correct answer. The useful upstream idea
is therefore its target-grounded sample construction: select a product, derive
facts from it, distribute those facts across query/profile/clarification, and
use a target-aware simulator. For the local benchmark, generation must add a
stronger uniqueness gate: all allowed disclosures must reduce the deterministic
catalog candidate set to exactly the selected `parent_asin`. The simulator can
be smarter about natural-language questions, but may reveal only grounded,
preconfigured target facts and must never expose the answer ID.
