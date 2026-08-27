"""Offline fixed-versus-adaptive category-recall diagnostics.

The diagnostic deliberately supplies a deterministic semantic passthrough to
the Agent.  It never constructs a DeepSeek client, local endpoint, Qwen
reranker, embedding model, or network connection.  The trace is target-free:
the Agent records only its own candidate IDs and feature order.  After the
evaluator finishes, this script joins those traces to the public target IDs
solely to calculate recall metrics.

Example::

    python3 scripts/diagnose_adaptive_recall.py \
      --catalog data/catalog.jsonl \
      --public-set data/public_set.jsonl \
      --sample-limit 5 \
      --output /tmp/adaptive-recall.json

The full public set is supported by omitting ``--sample-limit``.  The output
is an experiment artifact, not a replacement for the frozen evaluator.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import statistics
import sys
import time


MAX_RECALL_K = (30, 100, 200)
MAX_TURNS = 10


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_repo_on_path() -> None:
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


# ``python scripts/diagnose_adaptive_recall.py`` places ``scripts`` (rather
# than the repository root) first on ``sys.path``.  Add the root before
# importing the submission Agent so this diagnostic is importable both as a
# script and as ``scripts.diagnose_adaptive_recall`` in unit tests.
_ensure_repo_on_path()
from starter.agent import LegacyAgent as Agent


def _read_jsonl(path: str | Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row at {path}:{line_number} is not an object")
            rows.append(value)
    return rows


class FeatureOnlyPassthroughRanker:
    """A semantic-ranker-shaped no-op used by both diagnostic configurations."""

    candidate_limit = 30

    def rank(self, _intent_summary: object, candidates: Sequence[object]) -> object:
        from starter.shopping_agent.semantic_ranking import SemanticRankingResult

        identifiers = tuple(
            str(getattr(candidate, "parent_asin", "")).strip()
            for candidate in candidates
            if str(getattr(candidate, "parent_asin", "")).strip()
        )
        return SemanticRankingResult(
            ordered_parent_asins=identifiers,
            scores={},
            backend=None,
            usage=None,
            failures=(),
        )


class TargetFreeTraceAgent(Agent):
    """Instrument Agent internals without supplying target information."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.trace_sessions: list[dict[str, object]] = []
        self.diagnostics: list[dict[str, object]] = []
        self._current_trace: dict[str, object] | None = None
        self._pending_retrieval_ids: list[str] = []

    @property
    def agent(self) -> "TargetFreeTraceAgent":
        """Compatibility view retained for callers of the old wrapper."""

        return self

    def reset(self, session_id: str, user_profile: dict) -> None:
        super().reset(session_id, user_profile)
        self._current_trace = {
            "session_id": str(session_id),
            "turns": [],
        }
        self.trace_sessions.append(self._current_trace)
        self._pending_retrieval_ids = []

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> object:
        started = time.perf_counter()
        before = len(self._turns())
        try:
            return super().respond(session_id, user_message, turn, top_k)
        finally:
            turns = self._turns()
            if len(turns) == before:
                turns.append(
                    {
                        "turn": int(turn),
                        "retrieval_candidate_ids": [],
                        "feature_ranked_ids": [],
                    }
                )
            turns[-1]["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 6)
            diagnostics = self.last_diagnostics
            self.diagnostics.append(dict(diagnostics))

    def _retrieve(self, state: object, latest: str, budget: int, **kwargs: object) -> list[object]:
        result = super()._retrieve(state, latest, budget, **kwargs)  # type: ignore[arg-type]
        self._pending_retrieval_ids = [
            str(getattr(item, "parent_asin", "")).strip()
            for item in result
            if str(getattr(item, "parent_asin", "")).strip()
        ]
        return result

    def _feature_rank(
        self,
        state: object,
        candidates: Sequence[object],
        context: object,
        **kwargs: object,
    ) -> list[object]:
        result = super()._feature_rank(state, candidates, context, **kwargs)  # type: ignore[arg-type]
        turn_value = getattr(context, "turn", len(self._turns()) + 1)
        try:
            turn = int(turn_value)
        except (TypeError, ValueError):
            turn = len(self._turns()) + 1
        self._turns().append(
            {
                "turn": turn,
                "retrieval_candidate_ids": list(self._pending_retrieval_ids),
                "feature_ranked_ids": [
                    str(getattr(item, "parent_asin", "")).strip()
                    for item in result
                    if str(getattr(item, "parent_asin", "")).strip()
                ],
            }
        )
        self._pending_retrieval_ids = []
        return result

    def _turns(self) -> list[dict[str, object]]:
        if self._current_trace is None:
            return []
        turns = self._current_trace.get("turns")
        if not isinstance(turns, list):
            turns = []
            self._current_trace["turns"] = turns
        return turns


def _assert_feature_only(agent: TargetFreeTraceAgent, evaluator_result: Mapping[str, object]) -> None:
    """Fail closed if any diagnostic accidentally used a model or tokens."""

    config = agent.config
    if bool(getattr(config, "deepseek_enabled", False)) or bool(getattr(config, "local_model_enabled", False)):
        raise RuntimeError("feature-only diagnostic has a model configuration enabled")
    if bool(getattr(config, "qwen_reranker_enabled", False)):
        raise RuntimeError("feature-only diagnostic unexpectedly enabled Qwen")
    usage = evaluator_result.get("reported_token_usage")
    if not isinstance(usage, Mapping):
        raise RuntimeError("feature-only diagnostic did not report token usage")
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(name, -1)
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            raise RuntimeError("feature-only diagnostic reported non-zero or invalid token usage")
    if not isinstance(agent.semantic_ranker, FeatureOnlyPassthroughRanker):
        raise RuntimeError("feature-only diagnostic did not use the deterministic passthrough ranker")
    for diagnostics in agent.diagnostics:
        if diagnostics.get("model_backend"):
            raise RuntimeError("feature-only diagnostic observed a model backend")
        diagnostic_usage = diagnostics.get("usage")
        if diagnostic_usage is not None:
            raise RuntimeError("feature-only diagnostic observed per-turn model usage")


def _trace_copy(trace: Mapping[str, object]) -> dict[str, object]:
    turns: list[dict[str, object]] = []
    raw_turns = trace.get("turns")
    if isinstance(raw_turns, list):
        for raw_turn in raw_turns:
            if not isinstance(raw_turn, Mapping):
                continue
            turns.append(
                {
                    "turn": int(raw_turn.get("turn", len(turns) + 1)),
                    "retrieval_candidate_ids": [
                        str(value).strip()
                        for value in raw_turn.get("retrieval_candidate_ids", [])
                        if str(value).strip()
                    ],
                    "feature_ranked_ids": [
                        str(value).strip()
                        for value in raw_turn.get("feature_ranked_ids", [])
                        if str(value).strip()
                    ],
                    "latency_ms": float(raw_turn.get("latency_ms", 0.0)),
                }
            )
    return {
        "session_id": str(trace.get("session_id", "")),
        "turns": turns,
    }


def _posthoc_recall(
    samples: Sequence[Mapping[str, object]],
    traces: Sequence[Mapping[str, object]],
) -> tuple[dict[str, float], list[dict[str, object]]]:
    """Join target IDs only after Agent execution and calculate recall."""

    if len(samples) != len(traces):
        raise RuntimeError(f"trace/sample count mismatch: {len(traces)} != {len(samples)}")
    joined: list[dict[str, object]] = []
    recall_hits = {f"retrieval_recall_at_{k}": 0 for k in MAX_RECALL_K}
    recall_hits["feature_recall_at_30"] = 0
    for sample, trace in zip(samples, traces):
        ground_truth = sample.get("ground_truth")
        if not isinstance(ground_truth, Mapping):
            raise ValueError(f"sample {sample.get('sample_id', '')} has no ground_truth")
        target = str(ground_truth.get("parent_asin", "")).strip()
        if not target:
            raise ValueError(f"sample {sample.get('sample_id', '')} has an empty target")
        turns = trace.get("turns") if isinstance(trace, Mapping) else []
        if not isinstance(turns, list):
            turns = []
        retrieval_hits: dict[int, bool] = {}
        feature_hit = False
        for raw_turn in turns:
            if not isinstance(raw_turn, Mapping):
                continue
            retrieval_ids = raw_turn.get("retrieval_candidate_ids", [])
            feature_ids = raw_turn.get("feature_ranked_ids", [])
            if isinstance(retrieval_ids, list):
                for k in MAX_RECALL_K:
                    retrieval_hits[k] = retrieval_hits.get(k, False) or target in retrieval_ids[:k]
            if isinstance(feature_ids, list):
                feature_hit = feature_hit or target in feature_ids[:30]
        for k in MAX_RECALL_K:
            if retrieval_hits.get(k, False):
                recall_hits[f"retrieval_recall_at_{k}"] += 1
        if feature_hit:
            recall_hits["feature_recall_at_30"] += 1
        joined.append(
            {
                "sample_id": str(sample.get("sample_id", "")),
                "scenario_type": str(sample.get("scenario_type", "")),
                "retrieval_hit_at": {str(k): bool(retrieval_hits.get(k, False)) for k in MAX_RECALL_K},
                "feature_hit_at_30": feature_hit,
            }
        )
    count = len(samples)
    return (
        {
            key: round(value / count, 6) if count else 0.0
            for key, value in recall_hits.items()
        },
        joined,
    )


def _technical_score(metric: Mapping[str, object]) -> float | None:
    try:
        hit_rate = float(metric.get("hit_rate_at_10"))
        mrr = float(metric.get("mrr"))
        mttc = float(metric.get("mttc"))
    except (TypeError, ValueError):
        return None
    efficiency = max(0.0, min(1.0, (11.0 - mttc) / 10.0))
    return round(0.50 * hit_rate + 0.30 * mrr + 0.20 * efficiency, 6)


def _evaluator_metrics(result: Mapping[str, object]) -> dict[str, object]:
    names = ("hit_rate_at_10", "mrr", "mttc")
    overall = {name: result.get(name) for name in names}
    overall["technical_score"] = result.get("recommended_technical_score")
    sessions = result.get("sessions")
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    if isinstance(sessions, list):
        for session in sessions:
            if isinstance(session, Mapping):
                grouped[str(session.get("scenario_type", "unknown"))].append(session)
    scenario_metrics: dict[str, dict[str, object]] = {}
    for scenario, entries in sorted(grouped.items()):
        count = len(entries)
        hits = [bool(entry.get("hit")) for entry in entries]
        reciprocals = [float(entry.get("reciprocal_rank", 0.0) or 0.0) for entry in entries]
        turns = [
            float(entry.get("first_hit_turn"))
            if bool(entry.get("hit")) and entry.get("first_hit_turn") is not None
            else 11.0
            for entry in entries
        ]
        metric: dict[str, object] = {
            "sample_count": count,
            "hit_rate_at_10": round(statistics.fmean(int(value) for value in hits), 6) if count else 0.0,
            "mrr": round(statistics.fmean(reciprocals), 6) if count else 0.0,
            "mttc": round(statistics.fmean(turns), 6) if count else None,
        }
        metric["technical_score"] = _technical_score(metric)
        scenario_metrics[scenario] = metric
    return {"overall": overall, "scenario": scenario_metrics}


def _latency_metrics(traces: Sequence[Mapping[str, object]], wall_time_ms: float) -> dict[str, object]:
    values: list[float] = []
    for trace in traces:
        turns = trace.get("turns") if isinstance(trace, Mapping) else []
        if isinstance(turns, list):
            for turn in turns:
                if isinstance(turn, Mapping):
                    try:
                        value = float(turn.get("latency_ms", 0.0))
                    except (TypeError, ValueError):
                        continue
                    if value >= 0:
                        values.append(value)
    ordered = sorted(values)

    def percentile(percent: float) -> float | None:
        if not ordered:
            return None
        if len(ordered) == 1:
            return round(ordered[0], 6)
        position = (len(ordered) - 1) * percent / 100.0
        low, high = int(position), min(int(position) + 1, len(ordered) - 1)
        value = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
        return round(value, 6)

    return {
        "turn_count": len(values),
        "p50_ms": percentile(50),
        "p95_ms": percentile(95),
        "max_ms": round(max(values), 6) if values else None,
        "wall_time_ms": round(max(wall_time_ms, 0.0), 6),
    }


def run_mode(
    catalog_path: str | Path,
    samples: Sequence[Mapping[str, object]],
    *,
    adaptive: bool,
) -> dict[str, object]:
    """Run one explicit feature-only configuration and return its trace."""

    _ensure_repo_on_path()
    from evaluator.local_evaluator import catalog_index, evaluate
    from starter.shopping_agent.config import AgentConfig

    config = AgentConfig(adaptive_category_recall_enabled=bool(adaptive))
    ranker = FeatureOnlyPassthroughRanker()
    agent = TargetFreeTraceAgent(
        catalog_path,
        config=config,
        semantic_ranker=ranker,
    )
    identifiers, categories, products = catalog_index(catalog_path)
    started = time.perf_counter()
    result = evaluate(agent, [dict(sample) for sample in samples], identifiers, categories, products)
    wall_time_ms = (time.perf_counter() - started) * 1000.0
    _assert_feature_only(agent, result)
    traces = [_trace_copy(trace) for trace in agent.trace_sessions]
    recall, joined = _posthoc_recall(samples, traces)
    return {
        "config": {
            "adaptive_category_recall_enabled": bool(adaptive),
            "semantic_ranker": "deterministic_passthrough",
            "model": None,
            "network": "disabled",
            "reported_token_usage": result.get("reported_token_usage"),
        },
        "sample_ids": [str(sample.get("sample_id", "")) for sample in samples],
        "sample_count": len(samples),
        "recall": recall,
        "evaluator": _evaluator_metrics(result),
        "latency": _latency_metrics(traces, wall_time_ms),
        "target_free_diagnostics": [dict(diagnostic) for diagnostic in agent.diagnostics],
        # The trace intentionally has no ground_truth field.  ``joined`` is a
        # post-hoc metric view and likewise records only hit booleans/ranks,
        # never the target ID itself.
        "target_free_trace": traces,
        "posthoc": joined,
    }


def run_diagnostic(
    catalog_path: str | Path,
    public_set_path: str | Path,
    *,
    sample_limit: int | None = None,
) -> dict[str, object]:
    """Compare fixed and adaptive budgets on identical public samples."""

    samples = _read_jsonl(public_set_path)
    if sample_limit is not None:
        if sample_limit < 0:
            raise ValueError("sample_limit must be non-negative")
        samples = samples[:sample_limit]
    fixed = run_mode(catalog_path, samples, adaptive=False)
    adaptive = run_mode(catalog_path, samples, adaptive=True)
    return {
        "schema_version": "adaptive-category-recall-diagnostic-v1",
        "catalog": str(catalog_path),
        "public_set": str(public_set_path),
        "sample_limit": sample_limit,
        "sample_ids": [str(sample.get("sample_id", "")) for sample in samples],
        "model_guard": {
            "mode": "feature_only",
            "network": "disabled",
            "reported_token_usage": 0,
            "target_join": "posthoc_only",
        },
        "fixed": fixed,
        "adaptive": adaptive,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline adaptive category recall diagnostic")
    parser.add_argument("--catalog", required=True, help="local catalog.jsonl")
    parser.add_argument("--public-set", required=True, help="local public_set.jsonl")
    parser.add_argument("--sample-limit", type=int, help="optional deterministic prefix for smoke tests")
    parser.add_argument("--output", required=True, help="diagnostic JSON output path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_diagnostic(
        args.catalog,
        args.public_set,
        sample_limit=args.sample_limit,
    )
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "output": str(output),
        "sample_count": len(report["sample_ids"]),
        "fixed": report["fixed"]["evaluator"],
        "adaptive": report["adaptive"]["evaluator"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
