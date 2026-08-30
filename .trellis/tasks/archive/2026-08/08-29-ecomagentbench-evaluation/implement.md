# Implementation plan

## 0. Create the independent project

- [ ] Create `techjam-natural-language-benchmark` as a sibling Git repository,
      with its own README, package metadata, ignore rules, source package, and
      tests.
- [ ] Treat the existing Agent repository as a read-only runtime dependency;
      take `--agent-repo` and `--catalog` paths instead of copying code/data.
- [ ] Keep every implementation and generated artifact out of
      `techjam-conversational-search-main`.

## 1. Define sample and fact contracts

- [ ] Define versioned dataclasses/JSONL schema for normalized facts,
      partitions, simulator slots, scenario, seed, and target-only evaluator
      fields.
- [ ] Define deterministic matching operators for text, enum, numeric range,
      negative value, price, rating, and rating count.
- [ ] Add a whitelisted Agent-facing sample projection.

## 2. Build target-signature generation

- [ ] Index normalized facts over the 50,000-product catalog.
- [ ] Build category-bounded candidate pools.
- [ ] Select high-information facts, minimize signatures, and reject targets
      that cannot be uniquely identified naturally.
- [ ] Partition signature facts into query/profile/clarification/override slots.
- [ ] Render deterministic natural-language questions and replies.

## 3. Build the independent validator

- [ ] Recompute all disclosures from catalog source data.
- [ ] Reject missing targets, ungrounded facts, answer leakage, trivial title
      leakage, invalid scenario construction, and non-unique full signatures.
- [ ] Record initial and per-disclosure candidate counts for auditability.

## 4. Build the intelligent simulator and runner

- [ ] Route `ask_attribute` plus natural-language questions to hidden slots.
- [ ] Implement information-gain fallback for broad relevant questions.
- [ ] Implement repeated, unsupported, no-preference, exhausted, and max-turn
      responses without hallucinating.
- [ ] Run the existing `Agent.reset/respond` loop and score only the exact
      target ID/rank.

## 5. Test

- [ ] Unit-test normalization and each matching operator.
- [ ] Test unique signature construction and ambiguous-target rejection.
- [ ] Test question paraphrases, structured fallback, repeated/exhausted paths,
      and no target leakage.
- [ ] Test exact target hit, wrong-but-similar miss, MRR, and miss-as-turn-11.
- [ ] Run the complete existing unittest suite.

## 6. Smoke benchmark

- [ ] Generate a deterministic small manifest spanning supported scenarios.
- [ ] Validate every sample before running the Agent.
- [ ] Run locally and report exact metrics, simulator behavior, rejected-target
      rate, candidate-pool trajectories, runtime, and known limitations.
- [ ] Review smoke quality before generating a larger frozen benchmark.

## Validation commands

```bash
python3 -m unittest discover -s tests -v
python3 -m nl_benchmark generate \
  --catalog /path/to/techjam-conversational-search-main/data/catalog.jsonl \
  --samples 40 --seed 42 \
  --output /tmp/natural-language-smoke.jsonl
python3 -m nl_benchmark validate \
  --catalog /path/to/techjam-conversational-search-main/data/catalog.jsonl \
  --dataset /tmp/natural-language-smoke.jsonl
python3 -m nl_benchmark evaluate \
  --agent-repo /path/to/techjam-conversational-search-main \
  --catalog /path/to/techjam-conversational-search-main/data/catalog.jsonl \
  --dataset /tmp/natural-language-smoke.jsonl \
  --output /tmp/natural-language-smoke-results.json
```

## Review gates

- Planning review before task activation.
- Generator/validator review before trusting smoke results.
- Smoke dataset quality review before creating a larger frozen benchmark.
