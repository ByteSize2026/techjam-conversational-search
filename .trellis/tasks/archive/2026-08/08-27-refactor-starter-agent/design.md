# Technical Design

## Current problem

`starter/agent.py` combines six concerns in one class: dependency construction,
public session protocol, turn orchestration, retrieval and route merging,
deterministic feature ranking/statistics, semantic adapter handling, and response
contract enforcement. The file is difficult to scan and its private helpers are
also used by tests and benchmark instrumentation.

## Proposed boundaries

### `starter/agent.py`

Keep `Agent` as the stable submission facade. It owns dependency construction,
session lifecycle, the high-level turn pipeline, diagnostics assembly, and small
delegating compatibility methods. The turn orchestrator will continue to resolve
`self._feature_rank` dynamically so `_FrozenTraceAgent` can replace it at runtime.

### `starter/shopping_agent/retrieval.py`

Introduce a retrieval component that owns route-specific query construction,
category quota preservation, route merging, browsing diversification, source
counts, and retrieval diagnostics. It receives `CatalogRepository` and
`AgentConfig`; it does not own session state.

### `starter/shopping_agent/ranking.py`

Introduce a deterministic ranking component plus pure helpers for candidate
statistics, entropy/attribute evidence, lexical BM25-like scores, feature fusion,
and `RankEvidence`. It receives repository/config and consumes explicit state and
runtime context.

### `starter/shopping_agent/response.py`

Move protocol-boundary helpers here: top-k normalization, semantic ordering,
usage/failure extraction, valid catalog ID filtering, fallback construction,
payload guarding, and stable user-facing message construction. The response
guard receives repository membership/popularity through an explicit repository
dependency.

## Compatibility strategy

`Agent` keeps the existing helper names as thin delegates. This protects direct
test calls and benchmark instrumentation without making the new components know
about `Agent`. In particular, `_respond_impl` will call `self._feature_rank`, not
the ranking component directly, so runtime replacement remains effective.

Existing attributes (`repository`, `catalog`, `connection`, `store`, `sessions`,
`_sessions`, policies, `structured_pool`, `semantic_ranker`, diagnostics) remain
on `Agent`. Constants can live in the responsible module but their current values
must not change.

## Data flow

1. `Agent.respond` resolves/reset-defensively the session and guards exceptions.
2. `_respond_impl` reduces the user message into state and selects a route.
3. The retrieval component returns candidates and its diagnostics.
4. Structured pool logic may replace that pool as it does today.
5. The ranking component computes statistics and deterministic ordering.
6. The existing semantic ranker optionally reorders the bounded prefix.
7. Existing clarification and commit policies produce the response decision.
8. Response helpers validate IDs/schema; `Agent` publishes diagnostics.

## Trade-offs

- Thin compatibility delegates leave several underscore methods on `Agent`, but
  avoid breaking repository tooling and make this a behaviour-preserving change.
- The high-level turn pipeline remains in one method initially. Extracting a
  separate turn service would require a larger mutable-context contract and make
  diagnostics/state compatibility riskier without materially improving the
  first refactor.
- Components use composition instead of inheritance, keeping dependencies
  explicit and independently testable.

## Rollback

The refactor is file-local and additive: reverting the extracted modules and
restoring the original `starter/agent.py` returns to the previous architecture.
No persistent data or schema migration is involved.
