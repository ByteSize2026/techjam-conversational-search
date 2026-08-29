"""Crash-safe full live-model test over ``data/public_set.jsonl``.

The user's API account balance may run out at an unpredictable point mid-run.
Every session result and every LLM-node call outcome is written and
``fsync``-flushed to disk the instant it is produced -- killing this process
at ANY point (SIGKILL included, since durability here does not depend on the
signal-handling path) leaves a valid, fully parseable, non-truncated JSONL
trail of everything completed so far. Nothing is buffered in memory and
written only at the end.

Reuses ``evaluator.local_evaluator.evaluate`` UNCHANGED (called once per
sample, not once for the whole batch) so scoring semantics are byte-identical
to the frozen evaluator; this script only adds an observability layer next to
it, the same instrumentation approach as ``scripts/run_live_reliability_subset.py``
but writing incrementally instead of aggregating in memory until the end.

Usage::

    set -a; source .env; set +a
    python3 scripts/run_full_live_test.py                  # full 200 sessions
    python3 scripts/run_full_live_test.py --limit 3         # smoke test
    python3 scripts/run_full_live_test.py --out-dir DIR     # explicit output dir

Output (under ``artifacts/full_live_test/<run_id>/``, untracked):

- ``manifest.json``      -- run identity, config, no secrets.
- ``events.jsonl``       -- one record per LLM node call (+ raw backend
                             attempts) and per run lifecycle event
                             (``run_aborted``, ``run_summary``).
- ``sessions.jsonl``     -- one record per completed session's official
                             evaluator outcome (hit/rank/reciprocal_rank).

If the live backend stops responding (balance exhausted, network down, rate
limited) the script detects it via a run of consecutive raw backend failures
and stops itself early rather than silently burning through the remaining
sessions on pure deterministic fallback while still spending wall-clock time
on failed live attempts.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl
from evaluator.trace_runner import sanitize
from starter.agent import Agent
from starter.shopping_agent import llm_nodes
from starter.shopping_agent import model as model_mod
from starter.shopping_agent.config import AgentConfig

_DEFAULT_CATALOG = _REPO_ROOT / "data" / "catalog.jsonl"
_DEFAULT_DATASET = _REPO_ROOT / "data" / "public_set.jsonl"
_CONSECUTIVE_FAILURE_LIMIT = 5

# Every ``llm_nodes.py`` task prompt is a distinct literal string constant;
# match a call's system prompt back to the node that issued it. Reused from
# ``run_live_reliability_subset.py``'s already-validated mapping.
_PROMPT_LABELS: dict[str, str] = {
    llm_nodes.EXTRACT_CONSTRAINTS_PROMPT: "ExtractConstraints",
    llm_nodes.CLASSIFY_INTENT_PROMPT: "ClassifyIntent",
    llm_nodes.ASK_ATTRIBUTE_FILL_MISSING_PROMPT: "AskAttribute",
    llm_nodes.ASK_ATTRIBUTE_RELAX_CONFLICT_PROMPT: "AskAttribute",
    llm_nodes.DISTILL_PROFILE_PROMPT: "DistillProfile",
    llm_nodes.EXPLAIN_PROMPT: "Explain",
    llm_nodes.COMPARE_PROMPT: "Compare",
}

_current_ctx: dict[str, object] = {
    "sample_id": None,
    "sample_index": None,
    "scenario_type": None,
    "target": None,
}
_consecutive_backend_failures = 0
_last_backend_error: str | None = None


class _FlushWriter:
    """Append-only JSONL writer; every record is written, flushed, and
    fsync'd before the call returns -- the actual crash-safety guarantee,
    independent of any signal handler running."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


_events_writer: _FlushWriter | None = None
_sessions_writer: _FlushWriter | None = None


def _redact(value: object) -> object:
    try:
        return sanitize(value)
    except Exception:
        return "<unsanitizable>"


def _failure_diagnostics(result: object) -> tuple[str | None, list[dict[str, str]]]:
    """Best-effort failure reason for a ``ModelCompletionResult``.

    ``ModelCompletionResult`` (``starter/shopping_agent/model.py``) has no
    ``.error`` attribute -- only a ``.failures`` tuple of per-backend
    ``BackendFailure(backend, stage, error)`` records, where ``stage`` is one
    of ``request``/``response``/``json``/``validator``/``configuration``.
    Reading ``.error`` via ``getattr(result, "error", "")`` (the previous
    approach here) always silently returned the default, so every recorded
    failure showed an empty string regardless of cause. Read ``.failures``
    instead so ``events.jsonl`` carries the actual stage/error per attempt.
    """

    failures = getattr(result, "failures", None) or ()
    payloads = [f.as_dict() if hasattr(f, "as_dict") else dict(f) for f in failures]
    if not payloads:
        return None, []
    summary = "; ".join(
        f"{p.get('backend')}/{p.get('stage')}: {p.get('error')}" for p in payloads
    )
    return summary[:500], payloads


def _install_instrumentation() -> None:
    """Wrap the two chokepoints every LLM Value Node call passes through --
    same technique as ``run_live_reliability_subset.py``, but each wrapper
    writes its own flushed event immediately instead of aggregating into an
    in-memory stats object that would only ever be dumped at the very end."""

    original_call = llm_nodes.call_llm_value_node

    def _wrapped_call(client, *, task_prompt, user_payload, output_model):
        label = _PROMPT_LABELS.get(task_prompt, output_model.__name__)
        started = time.perf_counter()
        attempts: list[dict[str, object]] = []
        original_complete = client.complete_json if client is not None else None

        def _tracking_complete_json(messages, validator):
            attempt_started = time.perf_counter()
            result = original_complete(messages, validator)
            error_summary, failure_detail = _failure_diagnostics(result)
            attempts.append(
                {
                    "success": bool(result.success),
                    "elapsed_ms": round((time.perf_counter() - attempt_started) * 1000, 1),
                    "backend": getattr(result, "backend", None),
                    "usage": dict(result.usage) if getattr(result, "usage", None) else None,
                    "error": None if result.success else error_summary,
                    "failures": failure_detail,
                }
            )
            return result

        if client is not None:
            client.complete_json = _tracking_complete_json  # type: ignore[assignment]
        try:
            output = original_call(
                client, task_prompt=task_prompt, user_payload=user_payload, output_model=output_model
            )
        finally:
            if client is not None:
                client.complete_json = original_complete  # type: ignore[assignment]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        if _events_writer is not None:
            _events_writer.write(
                {
                    "event_type": "llm_node_call",
                    **_current_ctx,
                    "node": label,
                    "success": output is not None,
                    "elapsed_ms": elapsed_ms,
                    "attempts": attempts,
                    "input": _redact(user_payload),
                    "output": _redact(output.model_dump() if output is not None else None),
                }
            )
        return output

    llm_nodes.call_llm_value_node = _wrapped_call  # type: ignore[assignment]

    original_complete_json = model_mod.TieredModelClient.complete_json

    def _wrapped_complete_json(self, messages, validator):
        global _consecutive_backend_failures, _last_backend_error
        system_prompt = ""
        if messages and isinstance(messages[0], dict):
            system_prompt = str(messages[0].get("content", ""))
        label = "SemanticRank" if "constrained product relevance ranker" in system_prompt else "other"
        for known_prompt, known_label in _PROMPT_LABELS.items():
            if system_prompt.startswith(known_prompt.strip()[:80]):
                label = known_label
                break
        started = time.perf_counter()
        result = original_complete_json(self, messages, validator)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        error_summary, failure_detail = _failure_diagnostics(result)
        if result.success:
            _consecutive_backend_failures = 0
        else:
            _consecutive_backend_failures += 1
            _last_backend_error = error_summary or "unknown backend failure"
        if _events_writer is not None:
            _events_writer.write(
                {
                    "event_type": "backend_attempt",
                    **_current_ctx,
                    "node": label,
                    "success": bool(result.success),
                    "elapsed_ms": elapsed_ms,
                    "backend": getattr(result, "backend", None),
                    "usage": dict(result.usage) if getattr(result, "usage", None) else None,
                    "error": None if result.success else error_summary,
                    "failures": failure_detail,
                }
            )
        return result

    model_mod.TieredModelClient.complete_json = _wrapped_complete_json  # type: ignore[assignment]

    original_respond = Agent.respond

    def _wrapped_respond(self, session_id, user_message, turn, top_k):  # type: ignore[no-untyped-def]
        response = original_respond(self, session_id, user_message, turn, top_k)
        # Records where the target sits in the *full* ranked pool each turn,
        # not just whether it cleared the top-10 cutoff -- distinguishes a
        # target that is steadily climbing (real convergence) from one that
        # jumps around with no trend (SemanticRank re-sampling noise) across
        # turns where the customer gave no new information.
        target = _current_ctx.get("target")
        if target and _events_writer is not None:
            state = self.sessions.get(session_id)
            full_rank = None
            pool_size = 0
            if state is not None:
                ranked_ids = [item.parent_asin for item in state.ranked]
                pool_size = len(ranked_ids)
                if target in ranked_ids:
                    full_rank = ranked_ids.index(target) + 1
            top10_ids = {
                item.get("parent_asin")
                for item in (response.get("recommendations") or [])
                if isinstance(item, dict)
            }
            _events_writer.write(
                {
                    "event_type": "target_rank",
                    **_current_ctx,
                    "turn": int(turn),
                    "full_rank": full_rank,
                    "pool_size": pool_size,
                    "top10_hit": target in top10_ids,
                }
            )
        return response

    Agent.respond = _wrapped_respond  # type: ignore[assignment]


def _write_run_summary(run_id: str, completed: int, planned: int, aborted: bool, reason: str | None) -> None:
    if _events_writer is None:
        return
    _events_writer.write(
        {
            "event_type": "run_summary",
            "run_id": run_id,
            "completed_sessions": completed,
            "planned_sessions": planned,
            "aborted_early": aborted,
            "abort_reason": reason,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(_DEFAULT_CATALOG))
    parser.add_argument("--dataset", default=str(_DEFAULT_DATASET))
    parser.add_argument("--limit", type=int, default=None, help="cap number of sessions (for smoke testing)")
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:12]
    out_dir = Path(args.out_dir) if args.out_dir else _REPO_ROOT / "artifacts" / "full_live_test" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    global _events_writer, _sessions_writer
    _events_writer = _FlushWriter(out_dir / "events.jsonl")
    _sessions_writer = _FlushWriter(out_dir / "sessions.jsonl")

    samples = load_jsonl(args.dataset)
    if args.limit:
        samples = samples[: args.limit]
    catalog_ids, categories, products = catalog_index(args.catalog)

    config = AgentConfig.from_env()
    manifest = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sample_count_planned": len(samples),
        "catalog_path": str(args.catalog),
        "dataset_path": str(args.dataset),
        "deepseek_model": config.deepseek_model,
        "deepseek_base_url": config.deepseek_base_url,
        "temperature": config.temperature,
        "model_timeout_seconds": config.model_timeout_seconds,
        "live_model_configured": bool(
            config.deepseek_api_key or (config.local_base_url and config.local_model)
        ),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"[run {run_id}] output dir: {out_dir}", file=sys.stderr)
    print(f"[run {run_id}] live model configured: {manifest['live_model_configured']}", file=sys.stderr)

    _install_instrumentation()
    agent = Agent(args.catalog, config=config)

    def _handle_signal(signum: int, _frame: object) -> None:
        print(f"\n[run {run_id}] caught signal {signum} -- flushing and exiting cleanly", file=sys.stderr)
        _write_run_summary(run_id, completed_holder["n"], len(samples), True, f"signal:{signum}")
        if _events_writer is not None:
            _events_writer.close()
        if _sessions_writer is not None:
            _sessions_writer.close()
        sys.exit(130)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    completed_holder = {"n": 0}
    aborted = False
    abort_reason: str | None = None

    for index, sample in enumerate(samples):
        _current_ctx["sample_id"] = sample.get("sample_id")
        _current_ctx["sample_index"] = index
        _current_ctx["scenario_type"] = sample.get("scenario_type")
        _current_ctx["target"] = str((sample.get("ground_truth") or {}).get("parent_asin", ""))

        if _consecutive_backend_failures >= _CONSECUTIVE_FAILURE_LIMIT:
            abort_reason = (
                f"{_consecutive_backend_failures} consecutive backend failures, "
                f"last error: {_last_backend_error}"
            )
            print(f"[run {run_id}] ABORTING early: {abort_reason}", file=sys.stderr)
            aborted = True
            break

        started = time.perf_counter()
        try:
            result = evaluate(agent, [sample], catalog_ids, categories, products)
        except Exception as exc:  # keep whatever is already durably on disk, stop cleanly
            abort_reason = f"{type(exc).__name__}: {str(exc)[:500]}"
            _events_writer.write({"event_type": "run_aborted", "reason": abort_reason, **_current_ctx})
            print(f"[run {run_id}] ABORTED at sample {index} ({sample.get('sample_id')}): {exc}", file=sys.stderr)
            aborted = True
            break

        elapsed_s = round(time.perf_counter() - started, 2)
        session = result["sessions"][0]
        _sessions_writer.write(
            {
                "event_type": "session_result",
                "sample_index": index,
                **session,
                "elapsed_s": elapsed_s,
                "reported_token_usage": result.get("reported_token_usage"),
            }
        )
        completed_holder["n"] += 1
        print(
            f"[run {run_id}] {completed_holder['n']}/{len(samples)} done "
            f"(scenario={session['scenario_type']}, hit={session['hit']}, {elapsed_s}s)",
            file=sys.stderr,
        )

    _write_run_summary(run_id, completed_holder["n"], len(samples), aborted, abort_reason)
    _events_writer.close()
    _sessions_writer.close()

    print(
        f"[run {run_id}] finished: {completed_holder['n']}/{len(samples)} sessions completed"
        + (f" (aborted early: {abort_reason})" if aborted else ""),
        file=sys.stderr,
    )
    print(f"[run {run_id}] output: {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
