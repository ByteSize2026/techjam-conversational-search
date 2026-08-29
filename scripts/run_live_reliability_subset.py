"""Bounded live-model reliability assessment over a public_set subset.

Builds a small, deterministic (first-N, not random -- reproducible) subset of
``data/public_set.jsonl`` under an untracked artifacts path, then runs the
*unmodified* ``evaluator.local_evaluator.evaluate`` twice against it:

1. with a live-model ``Agent`` (``AgentConfig.from_env()`` -- picks up
   ``SHOPPING_AGENT_DEEPSEEK_API_KEY`` when sourced), and
2. with a fully offline, deterministic-fallback ``Agent`` (explicit
   ``AgentConfig()`` with no model settings, regardless of the environment)

so the two result sets are apples-to-apples over the *same* sessions.  Does
not modify ``evaluator/local_evaluator.py`` -- no scoring semantics changed;
this only supplies a smaller ``--dataset``-equivalent input in-process and
adds a *separate* observability layer:

- wraps ``starter.shopping_agent.llm_nodes.call_llm_value_node`` (the single
  shared retry-once-then-fallback helper every LLM Value Node except
  ``SemanticRank`` routes through) to record, per output-schema type: how
  many backend attempts happened, whether the first attempt already
  validated, whether the retry was needed, and whether both attempts still
  fell back to ``None``, plus wall-clock elapsed per node call;
- wraps ``starter.shopping_agent.model.TieredModelClient.complete_json`` to
  record every raw backend attempt's elapsed time, success, backend name,
  and token usage, bucketed by which node's system prompt triggered it
  (matched against the literal prompt constants in ``llm_nodes.py``, plus
  ``LLMSemanticRanker``'s own distinct ranking prompt).

Usage::

    set -a; source .env; set +a
    python3 scripts/run_live_reliability_subset.py --sample-count 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl
from starter.agent import Agent
from starter.shopping_agent import llm_nodes
from starter.shopping_agent import model as model_mod
from starter.shopping_agent.config import AgentConfig

_ARTIFACTS_DIR = _REPO_ROOT / "artifacts"
_DEFAULT_CATALOG = _REPO_ROOT / "data" / "catalog.jsonl"
_DEFAULT_DATASET = _REPO_ROOT / "data" / "public_set.jsonl"

# Match a call's system prompt back to the node that issued it -- every
# ``llm_nodes.py`` task prompt is a distinct literal string constant, and
# ``LLMSemanticRanker`` (``semantic_ranking.py``) has its own distinct one.
_PROMPT_LABELS: dict[str, str] = {
    llm_nodes.EXTRACT_CONSTRAINTS_PROMPT: "ExtractConstraints",
    llm_nodes.CLASSIFY_INTENT_PROMPT: "ClassifyIntent",
    llm_nodes.ASK_ATTRIBUTE_FILL_MISSING_PROMPT: "AskAttribute",
    llm_nodes.ASK_ATTRIBUTE_RELAX_CONFLICT_PROMPT: "AskAttribute",
    llm_nodes.DISTILL_PROFILE_PROMPT: "DistillProfile",
    llm_nodes.EXPLAIN_PROMPT: "Explain",
    llm_nodes.COMPARE_PROMPT: "Compare",
}


class _NodeStats:
    def __init__(self) -> None:
        self.calls = 0
        self.first_attempt_success = 0
        self.retry_needed = 0
        self.retry_success = 0
        self.total_fallback = 0
        self.elapsed_ms: list[float] = []

    def as_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "first_attempt_success": self.first_attempt_success,
            "first_attempt_success_rate": round(self.first_attempt_success / self.calls, 4) if self.calls else None,
            "retry_needed": self.retry_needed,
            "retry_success": self.retry_success,
            "total_fallback": self.total_fallback,
            "total_fallback_rate": round(self.total_fallback / self.calls, 4) if self.calls else None,
            "avg_elapsed_ms": round(sum(self.elapsed_ms) / len(self.elapsed_ms), 1) if self.elapsed_ms else None,
            "max_elapsed_ms": round(max(self.elapsed_ms), 1) if self.elapsed_ms else None,
        }


class _BackendStats:
    def __init__(self) -> None:
        self.attempts = 0
        self.successes = 0
        self.elapsed_ms: list[float] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.backends: dict[str, int] = {}

    def as_dict(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "success_rate": round(self.successes / self.attempts, 4) if self.attempts else None,
            "avg_elapsed_ms": round(sum(self.elapsed_ms) / len(self.elapsed_ms), 1) if self.elapsed_ms else None,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "backends_used": self.backends,
        }


_NODE_STATS: dict[str, _NodeStats] = {}
_BACKEND_STATS: dict[str, _BackendStats] = {}


def _reset_stats() -> None:
    _NODE_STATS.clear()
    _BACKEND_STATS.clear()


def _install_instrumentation() -> None:
    original_call = llm_nodes.call_llm_value_node

    def _wrapped_call(client, *, task_prompt, user_payload, output_model):
        label = _PROMPT_LABELS.get(task_prompt, output_model.__name__)
        stats = _NODE_STATS.setdefault(label, _NodeStats())
        stats.calls += 1
        started = time.perf_counter()

        attempts: list[bool] = []
        original_complete = client.complete_json if client is not None else None

        def _tracking_complete_json(messages, validator):
            result = original_complete(messages, validator)
            attempts.append(bool(result.success))
            return result

        if client is not None:
            client.complete_json = _tracking_complete_json  # type: ignore[assignment]
        try:
            output = original_call(client, task_prompt=task_prompt, user_payload=user_payload, output_model=output_model)
        finally:
            if client is not None:
                client.complete_json = original_complete  # type: ignore[assignment]
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        stats.elapsed_ms.append(elapsed_ms)

        if attempts and attempts[0]:
            stats.first_attempt_success += 1
        elif len(attempts) >= 1:
            stats.retry_needed += 1
            if len(attempts) >= 2 and attempts[1]:
                stats.retry_success += 1
        if output is None and attempts:
            stats.total_fallback += 1
        elif not attempts and client is not None:
            # client configured but no backends -- counted as a fallback too.
            stats.total_fallback += 1
        return output

    llm_nodes.call_llm_value_node = _wrapped_call  # type: ignore[assignment]

    original_complete_json = model_mod.TieredModelClient.complete_json

    def _wrapped_complete_json(self, messages, validator):
        system_prompt = ""
        if messages and isinstance(messages[0], dict):
            system_prompt = str(messages[0].get("content", ""))
        label = "SemanticRank" if "constrained product relevance ranker" in system_prompt else "other"
        for known_prompt, known_label in _PROMPT_LABELS.items():
            if system_prompt.startswith(known_prompt.strip()[:80]):
                label = known_label
                break
        stats = _BACKEND_STATS.setdefault(label, _BackendStats())
        started = time.perf_counter()
        result = original_complete_json(self, messages, validator)
        stats.attempts += 1
        stats.elapsed_ms.append((time.perf_counter() - started) * 1000.0)
        if result.success:
            stats.successes += 1
            stats.backends[result.backend] = stats.backends.get(result.backend, 0) + 1
        if result.usage:
            stats.prompt_tokens += int(result.usage.get("prompt_tokens", 0) or 0)
            stats.completion_tokens += int(result.usage.get("completion_tokens", 0) or 0)
        return result

    model_mod.TieredModelClient.complete_json = _wrapped_complete_json  # type: ignore[assignment]


def _build_subset(dataset_path: Path, count: int, output_path: Path) -> Path:
    samples = load_jsonl(dataset_path)
    subset = samples[:count]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for sample in subset:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(_DEFAULT_CATALOG))
    parser.add_argument("--dataset", default=str(_DEFAULT_DATASET))
    parser.add_argument("--sample-count", type=int, default=20)
    parser.add_argument("--output", default=str(_ARTIFACTS_DIR / "live_reliability_subset_result.json"))
    args = parser.parse_args()

    subset_path = _build_subset(Path(args.dataset), args.sample_count, _ARTIFACTS_DIR / "live_eval_subset.jsonl")
    samples = load_jsonl(subset_path)
    catalog_ids, categories, products = catalog_index(args.catalog)

    _install_instrumentation()

    print(f"Running deterministic-fallback pass over {len(samples)} sessions...", file=sys.stderr)
    _reset_stats()
    deterministic_agent = Agent(args.catalog, config=AgentConfig())
    started = time.perf_counter()
    deterministic_result = evaluate(deterministic_agent, samples, catalog_ids, categories, products)
    deterministic_elapsed_s = time.perf_counter() - started
    deterministic_node_stats = {name: stats.as_dict() for name, stats in _NODE_STATS.items()}
    deterministic_backend_stats = {name: stats.as_dict() for name, stats in _BACKEND_STATS.items()}

    print(f"Running live-model pass over {len(samples)} sessions...", file=sys.stderr)
    _reset_stats()
    live_config = AgentConfig.from_env()
    live_agent = Agent(args.catalog, config=live_config)
    started = time.perf_counter()
    live_result = evaluate(live_agent, samples, catalog_ids, categories, products)
    live_elapsed_s = time.perf_counter() - started
    live_node_stats = {name: stats.as_dict() for name, stats in _NODE_STATS.items()}
    live_backend_stats = {name: stats.as_dict() for name, stats in _BACKEND_STATS.items()}

    def _metrics(result: dict) -> dict:
        return {
            "hit_rate_at_10": result.get("hit_rate_at_10"),
            "mrr": result.get("mrr"),
            "mttc": result.get("mttc"),
            "efficiency": result.get("efficiency"),
            "recommended_technical_score": result.get("recommended_technical_score"),
            "reported_token_usage": result.get("reported_token_usage"),
        }

    summary = {
        "sample_count": len(samples),
        "live_model_configured": bool(live_config.deepseek_api_key or (live_config.local_base_url and live_config.local_model)),
        "deterministic": {
            "elapsed_s": round(deterministic_elapsed_s, 1),
            "metrics": _metrics(deterministic_result),
            "node_stats": deterministic_node_stats,
            "backend_stats": deterministic_backend_stats,
        },
        "live": {
            "elapsed_s": round(live_elapsed_s, 1),
            "metrics": _metrics(live_result),
            "node_stats": live_node_stats,
            "backend_stats": live_backend_stats,
        },
    }
    Path(args.output).write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
