# Target-exact natural-language benchmark

## Goal

Borrow EComAgentBench's target-grounded generation and simulated-user ideas to
build a smaller benchmark over this project's frozen 50,000-product catalog.
Every sample has exactly one scored answer: the product selected when the
sample is generated. The simulator must disclose enough truthful information
through natural language and clarification to make that target uniquely
identifiable; semantically similar substitutes do not count as correct.

## Requirements

- Generate every sample from one real catalog product and keep its
  `parent_asin` as the sole ground-truth answer.
- Extract only catalog-grounded facts from title, category, store, price,
  rating, rating count, details, features, and description. Never invent a
  target property.
- Build a discriminative target signature: an ordered set of facts whose
  conjunction uniquely identifies the selected product among all 50,000
  products under the benchmark's matching rules.
- Reject and resample a target if no sufficiently natural, non-ID signature can
  uniquely identify it. `parent_asin` and direct answer leakage are forbidden
  in user-visible text.
- Split signature facts across the initial natural-language query, anonymous
  profile, and hidden clarification slots. The complete allowed disclosure set
  must uniquely identify the target; the initial query should normally leave
  multiple plausible candidates so dialogue remains meaningful.
- Upgrade the simulator beyond fixed keyword matching. It must understand the
  Agent's natural-language question together with structured `ask_attribute`,
  map it to one or more hidden target facts, and answer naturally and
  truthfully.
- When a question is vague but valid, the simulator may reveal the unused fact
  with the highest information gain for the remaining candidate pool. It must
  not volunteer the answer ID, exact hidden target record, or facts outside the
  configured disclosure policy.
- Support clear simulator boundaries: already answered, no preference/no
  available fact, unsupported question, repeated question, maximum turns, and
  exhausted information.
- Score exact target retrieval only. Primary metrics are Hit@10, MRR, MTTC,
  and exact Top-1; misses remain misses even if another product appears
  semantically acceptable.
- Include scenario breakdowns for direct natural-language search,
  multi-constraint search, profile-hidden facts, clarification-dependent facts,
  negative constraints, budget/rating constraints, and intent override where
  catalog evidence supports them.
- Keep dataset generation deterministic from a seed. Freeze generated samples
  to auditable JSONL so normal evaluation performs no network calls and does
  not regenerate questions.
- First version uses deterministic natural-language templates and paraphrase
  banks. Optional LLM paraphrasing may be added later only with fact validation
  and frozen outputs.
- Preserve `starter.agent.Agent` and the existing evaluator contract. Add this
  as a completely independent Git project rather than changing official
  TechJam scoring or adding benchmark code to the Agent repository.
- The standalone project must accept the Agent repository and catalog paths as
  runtime inputs. It may import `starter.agent.Agent` through a documented
  adapter boundary, but must not write into, vendor, or require commits in the
  Agent repository.
- Preserve unrelated changes in the dirty worktree.

## Acceptance Criteria

- [ ] A generator can sample real targets and find target signatures that are
      unique under documented deterministic matching semantics.
- [ ] Every emitted sample passes a validator proving: target exists, all
      disclosed facts are grounded, no ID leakage occurs, initial ambiguity is
      within configured bounds, and full disclosure uniquely returns target.
- [ ] A simulator routes natural-language and structured Agent questions to
      hidden facts, answers consistently, and never exposes ungrounded facts.
- [ ] Exact-only scoring reports Hit@10, MRR, MTTC, Top-1, per-scenario results,
      clarification turns, and simulator routing diagnostics.
- [ ] Tests cover unique-signature construction, ambiguous-target rejection,
      paraphrase/question routing, repeated or exhausted questions, exact miss,
      and target isolation from the Agent.
- [ ] Existing `python3 -m unittest discover -s tests -v` tests remain green.
- [ ] A deterministic smoke dataset and evaluation run complete locally using
      the frozen 50,000-product catalog without external APIs.
- [ ] All implementation, tests, schemas, generated fixtures, and reports live
      in the independent `techjam-natural-language-benchmark` Git project; the
      existing Agent repository receives no implementation changes.

## Out of Scope

- Treating any non-target substitute as correct.
- Using EComAgentBench's official 662 samples or 26.9 GB product database.
- Review-driven scenarios, because this catalog has no equivalent review corpus.
- Training or tuning against generated test targets.
- Calling an LLM judge during ordinary evaluation.

## Notes

The selected target is intentionally privileged ground truth. The simulator is
allowed to know it; the Agent is not. Benchmark validity comes from proving the
visible/obtainable facts uniquely identify that target without leaking its ID.

The planned sibling layout is:

```text
techjam-conversational-search-main/       # read-only tested project
techjam-natural-language-benchmark/       # independent Git project
```
