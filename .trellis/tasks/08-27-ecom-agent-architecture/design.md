# ECom-style Agent Architecture Design

## 1. Summary

The public Agent contract remains unchanged. Internally, `respond()` gains a bounded action loop whose planner can choose catalog search, candidate filtering, product detail lookup, profile retrieval, user clarification or final recommendation. Existing retrieval/ranking remains the implementation backend and the offline fallback.

No review capability is included because the released Hackathon data has no review text. No new agent framework or service is required.

```text
reset(session_id, profile)
  -> SessionState + private SessionEnvironment(profile)

respond(user_message)
  -> parse/reduce intent
  -> resume pending task, if any
  -> planner chooses one JSON action
       search / filter / detail / get_profile
         -> bounded observation -> planner again
       ask_user
         -> save pending task -> return message + ask_attribute + safe Top-K
       recommend_products
         -> validate IDs -> return Top-K
  -> any planner/tool/budget failure
       -> current deterministic pipeline
```

## 2. Boundaries

### Public boundary

- Preserve `starter.agent.Agent`, constructor compatibility, `reset(...)` and `respond(...)`.
- Preserve the response keys and allowed `ask_attribute` values.
- Never modify `evaluator/local_evaluator.py`, public benchmark data or score formulas.
- Only catalog-valid `parent_asin` values may leave the Agent.

### Data boundary

Allowed product evidence is limited to released catalog fields: title, categories, features, description, price, details, rating, rating count and store. Missing data remains missing. Model output is a decision proposal, not a new product fact.

Raw reset profile is stored per session. It is omitted from model-planner context until the `get_user_profile` action succeeds. This is an internal visibility rule; the official evaluator still passes profile at reset as before.

## 3. Components

### 3.1 Action contracts (`starter/shopping_agent/actions.py`)

Define a closed action vocabulary and JSON-compatible types:

```python
ActionName = Literal[
    "search_products",
    "filter_products",
    "get_product_details",
    "get_user_profile",
    "ask_user",
    "recommend_products",
]

PlannedAction(name, arguments, rationale)
ActionObservation(action, ok, payload, error, elapsed_ms)
TrajectoryEntry(step, action, arguments_summary, observation_summary, status)
PendingTask(question, ask_attribute, planner_messages, candidate_ids)
```

Validation is deterministic. Unknown actions, wrong argument types, excessive ID lists and out-of-range limits become bounded error observations rather than exceptions.

The planner selects one action per step. This keeps ordering and error recovery auditable and matches the useful part of EComAgentBench's loop.

### 3.2 Catalog toolbox (`starter/shopping_agent/tools.py`)

`ShoppingToolbox` is constructed from `CatalogRepository` and a session-scoped execution context.

- `search_products(query, limit, category?)`: use existing safe FTS/category retrieval; return bounded IDs, titles, price/rating summaries and provenance.
- `filter_products(product_ids, attribute, operator, value)`: filter only the supplied bounded candidate set. Support high-confidence text equality/containment and numeric comparisons for parsed price/rating/rating_count. Missing values are `unknown` and are reported separately, not silently treated as matches.
- `get_product_details(parent_asin)`: return a bounded catalog projection. Reject unknown IDs. Long feature/description/detail values are truncated by fixed limits.
- `get_user_profile()`: return the current session's reset profile and mark `profile_loaded=true`.
- `ask_user(question, ask_attribute)`: validate length and allowed attribute, then return a pause directive; it does not fabricate the user's answer.
- `recommend_products(parent_asins, message)`: materialize, deduplicate and cap at `top_k`; reject IDs never returned by catalog tools unless they are already present in the deterministic candidate pool.

Tool outputs are plain dictionaries so tests and trajectory serialization do not depend on an external framework.

### 3.3 Planner (`starter/shopping_agent/planner.py`)

Expose a small protocol:

```python
class ActionPlanner(Protocol):
    def next_action(self, context: PlannerContext) -> PlannerResult: ...
```

`ModelActionPlanner` uses existing `TieredModelClient.complete_json` with a strict action validator. It receives:

- current active requirements and intent epoch;
- current user message and bounded prior observations;
- available action schemas;
- candidate IDs already observed;
- profile content only after `get_user_profile`;
- remaining internal steps and remaining official turns.

The prompt forbids invented products and asks for exactly one action. Model output is repaired only when deterministic and safe; otherwise it is rejected.

`DeterministicPlanner` is used in tests and can explicitly choose `get_user_profile`, search and recommend. When no model backend is configured, production falls back to the existing deterministic response pipeline rather than making fake model decisions.

### 3.4 Action loop (`starter/shopping_agent/orchestrator.py`)

`ActionOrchestrator.run(...)` executes at most a configured number of planning steps and enforces a total per-respond wall-clock budget. Recommended initial bounds:

- at most 4 planner steps per official turn;
- at most 100 candidate IDs in working memory;
- at most 20 search results per observation;
- at most 10 IDs for detail/filter actions;
- no more than one `ask_user` pause per official turn.

Each successful nonterminal action is appended as a compact observation and planning continues. Terminal behavior:

- `recommend_products`: return validated recommendations;
- `ask_user`: persist `PendingTask`, return the question and official `ask_attribute`, and optionally include safe current candidates;
- budget/error/no valid terminal action: invoke deterministic fallback.

The loop never waits for the user inside one `respond()` call.

### 3.5 Session state (`starter/shopping_agent/state.py`)

Extend session state with:

- `profile_loaded: bool`;
- private/raw profile separation or an equivalent visibility guard;
- `pending_task`;
- bounded `tool_trajectory` grouped by official turn;
- `tool_candidate_ids` and last tool error;
- planner usage totals for the current response.

On the next official turn after `ask_user`, normal intent parsing applies the answer to the state, the pending marker is cleared, and the planner resumes with a compact note that the clarification was answered. Override starts a new intent epoch and invalidates pending candidates/questions tied to the old epoch.

### 3.6 Agent facade integration (`starter/agent.py`)

Refactor the current fixed logic into a callable deterministic path without changing its behavior. `respond()` then chooses:

1. tool-loop path when an action planner is explicitly injected/configured;
2. existing deterministic path when no planner is available;
3. existing guarded fallback if either path raises.

Search/filter/detail actions reuse repository and ranking helpers. Semantic reranking can remain a tool backend operation; the action planner and semantic ranker must not recursively call each other without the orchestrator budget accounting for both.

## 4. Compatibility and rollout

- Default configuration remains offline and deterministic. Merely constructing `AgentConfig` performs no network call.
- Tool planning is opt-in through an injected planner or an explicit configuration flag plus an available existing model backend.
- Existing direct constructor injections used by tests remain supported.
- Add diagnostics identifying `execution_mode=tool_loop|deterministic|fallback`, action counts, pause reason, model backend, failures and token usage.
- Keep a single rollback switch that disables tool planning without removing the new modules.

## 5. Failure handling

- Unknown/invalid model action: return an error observation once; repeated invalid output triggers fallback.
- Tool error or empty search: preserve state and let the planner recover within remaining steps; otherwise fallback.
- Model timeout/network failure: do not retry unboundedly; use existing tiered failover once, then deterministic path.
- Invalid terminal IDs: discard them, materialize only valid IDs and fill remaining slots from deterministic ranking.
- Ask mapping failure: reject the action and ask the planner to use an allowed attribute; fallback policy may choose a valid attribute.
- Trajectory recording failures must not break the public response.

## 6. Testing strategy

- Pure contract tests for action parsing, argument bounds and observation serialization.
- Catalog fixture tests for search/filter/detail, including missing fields and invalid IDs.
- Fake-planner sequence tests for multi-action execution, terminal recommendation, invalid actions and budget exhaustion.
- Two-session tests for profile gating and pause/resume isolation.
- Override tests proving old pending work/candidates are invalidated.
- Existing Agent and evaluator regression suites.
- One full public evaluator run with execution mode and score delta reported; no score threshold is used to hide correctness regressions.

## 7. Deliberate exclusions

- Reviews and review-opinion reasoning.
- External product enrichment.
- LangChain/LangGraph, native vendor tool-calling SDKs, Redis, queues, microservices and vector DB.
- Robust benchmark generation/scoring; this architecture only exposes the capabilities and trajectory needed by that later task.

This work stays in one Trellis task because the contracts, loop, state and facade form one integration slice and cannot be usefully accepted in isolation. Implementation remains staged with rollback points.
