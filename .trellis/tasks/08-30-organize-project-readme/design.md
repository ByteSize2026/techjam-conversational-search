# Design: explicit protocol profiles over one Agent pipeline

## Architecture

`starter.agent.Agent` remains the only public facade. A resolved protocol profile selects
the protocol-boundary configuration: input/profile interpretation, clarification defaults,
and the structured-field admission set. Both profiles converge on the existing state and
ranking pipeline:

```text
official input --------> deterministic protocol adapter --+
                                                        +--> IntentUpdate
natural-language input -> IntentInterpreter -------------+        |
                                                                 StateReducer
                                                                      |
                    StructuredCandidatePool -> Retrieval -> Ranking -> Commit -> Guard
                                                                      ^
                                           shared by both profiles ---+
```

No evaluator object or hidden field enters the Agent. The caller selects the profile explicitly before evaluation.

## Public Configuration

Add a closed profile value with two supported names:

```python
Agent(protocol_profile="official")
Agent(protocol_profile="natural_language")
```

Both evaluator-facing CLIs expose the same `--protocol-profile` option, so a complete run remains one `python -m ...` invocation without environment setup:

```bash
python3 -m evaluator.local_evaluator --protocol-profile official ...
python3 -m nl_benchmark evaluate --protocol-profile natural_language ...
```

The official CLI passes the option directly to `Agent`. The benchmark CLI passes it through `SubprocessAgent` to the worker, which supplies it when constructing an Agent that supports the keyword. A requested non-official profile must fail clearly when an external Agent lacks profile support; it must not silently run the wrong strategy. Legacy external Agents remain loadable for `official`.

Resolution precedence:

1. Explicit `Agent(protocol_profile=...)` argument.
2. `AgentConfig.protocol_profile`, including `SHOPPING_AGENT_PROTOCOL_PROFILE` when built by `from_env()`.
3. Default `official`.

An explicit invalid constructor/config value fails fast with `ValueError`. An invalid environment value resolves safely to `official` and is visible in configuration tests/documentation. This prevents deployment typos from enabling a model-oriented path.

## Profile Behavior

### `official`

- Interpret messages through the frozen `parse_official_intent_update` compatibility adapter.
- Use legacy-ordered `ClarificationPolicy(mode="protocol_aware")` and the official
  structured-filter field set; do not apply natural-language profile grounding at reset.
- Preserve the current structured `ask_attribute` response contract.
- Remain offline unless lower-level model/reranker settings are explicitly configured; the protocol profile itself never enables a network call.

### `natural_language`

- Interpret messages through the current catalog-grounded `IntentInterpreter`.
- Use `ClarificationPolicy(mode="catalog_entropy")` so concrete follow-up slots are selected from candidate evidence.
- Keep the same structured `ask_attribute` alongside natural-language response text so both evaluators consume a valid response.
- Apply catalog-grounded profile projection and the expanded structured-filter field set.
- Respect existing `intent_model_enabled`, backend, mode, confidence and fallback settings. The profile chooses the adapter, not credentials or network availability.

## Dependency Injection

Existing explicit constructor dependencies remain stronger than profile defaults:

- `intent_interpreter=` replaces the selected interpretation adapter.
- `clarification_policy=` replaces the selected clarification policy.
- router, candidate gate, commit policy and ranker injections are unchanged.

This preserves direct tests and benchmark probes. Diagnostics record the resolved `protocol_profile`, including reset and turn results.

## Shared Invariants

Both profiles must use the same instances/classes for:

- `SessionStore` and `StateReducer`;
- `StructuredCandidatePool` implementation and `RetrievalEngine`; the pool receives the
  profile's field-admission set without duplicating its filtering algorithm;
- `RankingEngine` and optional semantic reranking;
- `RecommendationCommitPolicy`;
- response validation, catalog ID filtering and usage accounting.

The profile cannot modify retrieval weights, ranking weights, commit thresholds, `top_k`, session horizon or evaluator scoring. Existing fine-grained `AgentConfig` switches remain available for explicit experiments but are orthogonal to protocol selection.

## Compatibility

- Official callers that construct `Agent()` continue to work without changes.
- The public `reset` / `respond` signatures and response JSON remain unchanged.
- The external natural-language benchmark opts in with its explicit `--protocol-profile natural_language` CLI option and forwards the value over its existing worker startup boundary.
- No code is copied from or merged between Git branches as part of this implementation.

## Validation

Unit tests cover profile normalization, precedence, component selection, injection precedence, diagnostics and shared-pipeline identity. Existing official contract/evaluator tests protect the zero-argument default. The external benchmark suite protects CLI/loader/worker propagation and legacy-Agent behavior, followed by a frozen-dataset smoke evaluation using the explicit natural-language CLI profile.

## Rollback

The change is additive. Removing the constructor/config field and the small profile-resolution branches restores the current single default path. Retrieval, ranking and commit modules require no rollback because they are not profile-dependent.
