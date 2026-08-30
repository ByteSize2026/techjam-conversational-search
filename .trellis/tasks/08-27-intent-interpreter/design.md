# Technical Design

## Architecture

```text
user_message + bounded state snapshot
  -> deterministic parser
  -> model trigger decision
  -> optional model parser
  -> strict validation + conservative merge
  -> IntentInterpretation
  -> StateReducer.apply
  -> existing retrieval/ranking/response pipeline
```

## Core Types

### IntentScope

```text
none
attribute_replace
referenced_preference_replace
global_reset
```

### QueryEvidence

```text
text, turn, epoch, source, confidence, status, attribute_hint
```

`SessionState` exposes active strings to existing retrieval/ranking code so migration can remain incremental.

### IntentInterpretation

```text
update: IntentUpdate
path: rules | model | rules_fallback
scope: IntentScope
confidence
trigger_reason
accepted/rejected field summaries
usage/failures
```

## Deterministic Parser

Split one message into bounded clauses, then apply explicit patterns in this order:

1. global reset / scoped change markers;
2. no-preference and remove;
3. negation / avoid;
4. budget and explicit attribute/value syntax;
5. positive soft/hard constraints and residual query evidence.

Parsing remains conservative: unresolved text stays soft; explicit negation must never be re-added as positive query evidence.

## Model Adapter

The model receives only the current message, allowed enums, active constraint summaries, bounded recent turns and the last asked attribute when available.

Strict response schema allows scope, mutations, category anchor, no-preference, exhaustion signals, query terms and confidence. Unknown keys, IDs, oversized lists/strings and invalid enums are rejected.

## Merge Rules

1. Preserve explicit deterministic facts.
2. Accept model fields only when supported by the message and confidence threshold.
3. Never flip explicit `avoid` into `prefer`.
4. Downgrade uncertain model-only constraints to soft evidence.
5. On any model failure, return the deterministic update unchanged.

## Integration

- Add a focused intent module under `starter/shopping_agent/`; keep session data/reducer types in `state.py` unless moving them materially reduces coupling.
- `Agent._respond_impl` replaces direct `parse_intent_update` with interpreter invocation before reducer application.
- Existing model client, config parsing, usage and failure conventions are reused.
- Existing catalog/retrieval/ranking interfaces are unchanged.

## Compatibility

- Preserve `parse_intent_update` for tests and callers.
- Keep `SessionState.query_terms` behavior through an active-evidence projection during migration.
- Split legacy override tests into scoped override and explicit global reset tests.

## Rollback

- Default model parser feature flag is off.
- Deterministic interpreter remains a complete path.
- Model failure never changes state beyond the deterministic parse.
- If scoped override regresses the frozen baseline, feature-gate the new semantics while retaining tests and diagnostics for correction.
