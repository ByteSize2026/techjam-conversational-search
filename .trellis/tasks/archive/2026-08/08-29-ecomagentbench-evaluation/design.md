# Design: intelligent target-exact simulator

## Core idea

The benchmark is a controlled identification game. Generation selects one
catalog product, computes which truthful facts distinguish it, and creates a
natural-language conversation policy around those facts. Evaluation succeeds
only when the target `parent_asin` appears in the Agent's ranked output.

## 1. Catalog fact normalization

Normalize each product into comparable facts:

- category path and product type;
- store/brand-like values;
- price bucket and numeric price;
- rating and rating-count thresholds;
- structured `details` key/value pairs;
- bounded feature/description phrases;
- safe title concepts, excluding ASIN-like strings and overly identifying
  verbatim titles when they would make the question trivial.

Each fact carries source field, normalized value, surface forms, polarity,
comparison operator, and confidence. Matching semantics are deterministic and
shared by generation and validation.

## 2. Discriminative signature search

For a target product, start with a broad category candidate pool. Estimate each
candidate fact's information gain: how much applying it shrinks the pool while
retaining the target. Greedily add useful facts, then minimize the set by
removing redundant facts.

Generation constraints:

- initial disclosed facts leave a configurable candidate range, e.g. 10-200;
- adding profile and clarification facts monotonically shrinks the pool;
- the full signature yields exactly `{target_parent_asin}`;
- signatures depending on malformed, answer-like, or unnatural facts are
  rejected;
- targets without a valid signature are resampled.

The validator recomputes candidate pools independently from the serialized
signature and fails closed if uniqueness no longer holds.

## 3. Information partition and natural language

Partition the signature into:

- **query facts:** visible in the first user message;
- **profile facts:** available in `user_profile.preference_tags` or summary;
- **clarification facts:** revealed only after a relevant question;
- **override facts:** optional old/new values for intent-change scenarios.

Render facts with deterministic paraphrase banks, such as numeric comparisons,
indirect product-type descriptions, negation, use-case language, and colloquial
preferences. Serialized samples retain both structured facts for validation and
rendered text for the Agent; the Agent receives only the rendered protocol
inputs.

## 4. Intelligent simulator

The simulator maintains:

- target-safe hidden fact slots;
- facts already disclosed;
- remaining candidate IDs or counts;
- question/attribute history;
- turn and exhaustion state.

Question routing combines two signals:

1. `ask_attribute` from the Agent when present;
2. deterministic semantic matching over the natural-language `message`, using
   field aliases, normalized values, question intents, and paraphrase terms.

Routing behavior:

- A strong match reveals the corresponding undisclosed slot.
- Several matched slots may be ranked by information gain; reveal a bounded
  number, normally one.
- A broad but relevant question reveals the highest-information compatible
  hidden fact instead of returning a useless canned response.
- An irrelevant/unsupported question receives a truthful boundary response.
- Repeated questions return a concise reminder without fabricating new facts.
- Once all configured facts are exhausted, the simulator says so and stops
  volunteering information.

The simulator never reads recommendation rankings to choose a response and
never reveals `parent_asin`, ground-truth labels, or undeclared target fields.

## 5. Scenarios

- `direct_search`: enough natural-language facts are initially visible.
- `multi_constraint`: several heterogeneous facts must be combined.
- `profile_hidden`: one or more discriminative facts are only in the profile.
- `clarification_required`: initial/profile facts are non-unique; a relevant
  question unlocks the fact that makes the target unique.
- `negative_constraint`: a truthful avoided value helps isolate the target.
- `budget_rating`: numeric constraints and thresholds matter.
- `intent_override`: an early decoy preference is explicitly replaced, while
  the final full disclosure still uniquely identifies the target.

Only emit scenarios whose required facts are supported by the selected target
and catalog counterexamples.

## 6. Evaluation

Use the existing Agent API in a separate benchmark runner:

1. reset with generated anonymous profile;
2. send initial natural-language query;
3. inspect exact target rank after each response;
4. if not hit and a question exists, ask the simulator for the next reply;
5. continue for at most 10 turns.

Metrics:

- exact Hit@10;
- exact Top-1;
- MRR of the target;
- MTTC with misses counted as turn 11;
- per-scenario breakdown;
- question routing/repeat/exhaustion diagnostics;
- candidate-pool size after each disclosure, for benchmark auditing only.

The Agent never receives candidate-pool diagnostics or ground truth.

## 7. Artifacts

All artifacts live in a sibling standalone Git project named
`techjam-natural-language-benchmark`:

- a Python package for generator, validator, simulator, runner, scoring, and
  Agent loading;
- a versioned JSONL schema for frozen samples;
- deterministic unit fixtures in temporary directories;
- a CLI taking `--agent-repo` and `--catalog` paths;
- smoke manifests/results/reports in the benchmark project's ignored output
  directory unless the user later chooses to publish a frozen dataset.

The Agent repository is mounted conceptually read-only. The loader temporarily
adds the provided repository root to the Python import path, imports
`starter.agent.Agent`, verifies its constructor/reset/respond surface, and
instantiates it with the supplied catalog. No benchmark module is imported by
production Agent code, and no file is written back to the Agent repository.

## Risks and controls

- **False uniqueness:** use one shared matcher plus an independent validator
  pass and property-focused tests.
- **Trivial title leakage:** reject verbatim/near-verbatim target titles and
  ASIN-shaped text from visible facts.
- **Simulator too helpful:** disclosure is limited to configured slots and
  logged; information-gain fallback applies only to relevant broad questions.
- **Simulator too brittle:** combine structured `ask_attribute` with semantic
  question routing and alias/paraphrase banks.
- **Template overfitting:** hold out paraphrase families and freeze manifests;
  optional LLM paraphrases come later, with strict fact validation.
- **Generator leakage into Agent:** prediction receives a whitelisted view;
  target/signature/candidate sets remain evaluator-only.

## Rollback

All work is isolated in the standalone benchmark repository. Removing that
repository leaves the production Agent repository byte-for-byte unchanged by
implementation work.
