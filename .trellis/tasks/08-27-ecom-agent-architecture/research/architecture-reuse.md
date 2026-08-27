# Agent architecture reuse findings

## Current repository

- `starter/agent.py:Agent._respond_impl` is a fixed per-turn pipeline: parse, reduce state, route, retrieve, feature-rank, optionally rerank, choose clarification, return Top-K.
- `starter/shopping_agent/catalog.py:CatalogRepository` already owns the frozen catalog, valid-ID set, SQLite FTS5 search, category resolution, popularity fallback and bounded materialization.
- `starter/shopping_agent/state.py` already provides session isolation, constraints, intent epochs, override handling, no-preference handling and runtime context.
- `starter/shopping_agent/model.py:TieredModelClient.complete_json` already provides bounded JSON completions, DeepSeek/local OpenAI-compatible failover, token accounting and offline behavior. A JSON action protocol can therefore be added without a new SDK or native function-calling dependency.
- `starter/agent.py` catches internal failures and guards the public response. The tool loop must remain inside this facade.

## EComAgentBench reference

- `../EComAgentBench_/src/prediction/agent.py` uses a bounded plain-Python loop: model chooses one tool, Python validates and executes it, observation returns to the model, and `recommend_product` terminates.
- Reusable behavior: explicit tool schemas, one action per planning step, bounded execution, trajectory capture, profile as an explicit tool, ask-user as a pause point, terminal recommendation validation.
- Direct code copying is not appropriate because its DB schema and benchmark-owned user simulator differ from this repository.
- Review tools are not reusable for this task because the Hackathon catalog contains ratings/counts but no review text.

## Chosen adaptation

- Keep the official `reset/respond` facade.
- Use a JSON action schema through the existing `complete_json` client rather than adding LangChain/LangGraph or a vendor SDK.
- Execute catalog-only actions inside one `respond`; pause on `ask_user` and resume when the next official turn arrives.
- Preserve the current fixed pipeline as the deterministic fallback and as a source of retrieval/ranking implementations.
- Restrict all product facts to the released catalog; the model may plan but cannot invent IDs or enrich facts from the network.
