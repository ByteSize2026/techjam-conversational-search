#!/usr/bin/env python3
"""Parallel public-set eval using the official evaluate() + one Agent per process.

Does not patch evaluator/local_evaluator.py.  DeepSeek key is read from
%USERPROFILE%\\Desktop\\.env (or SHOPPING_AGENT_DEEPSEEK_API_KEY) and never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from evaluator.local_evaluator import catalog_index, load_jsonl, metric_summary


def _desktop_key() -> str:
    env = (os.environ.get("SHOPPING_AGENT_DEEPSEEK_API_KEY") or "").strip()
    if env:
        return env
    path = Path.home() / "Desktop" / ".env"
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        raise SystemExit(f"empty key file: {path}")
    first = raw.splitlines()[0].strip()
    if "=" in first:
        first = first.split("=", 1)[1].strip().strip('"').strip("'")
    return first.strip().strip('"').strip("'")


def _chunks(items: list, n: int) -> list[list]:
    n = max(1, min(n, len(items) or 1))
    size = (len(items) + n - 1) // n
    return [items[i : i + size] for i in range(0, len(items), size)]


def _worker(payload: dict) -> dict:
    os.environ["SHOPPING_AGENT_DEEPSEEK_API_KEY"] = payload["api_key"]
    os.environ["SHOPPING_AGENT_DEEPSEEK_MODEL"] = payload["model"]
    os.environ["SHOPPING_AGENT_MODEL_TIMEOUT_SECONDS"] = str(payload["timeout"])
    os.environ.pop("SHOPPING_AGENT_LOCAL_BASE_URL", None)
    os.environ.pop("SHOPPING_AGENT_LOCAL_MODEL", None)
    os.environ.pop("DEEPSEEK_API_KEY", None)

    from evaluator.local_evaluator import evaluate
    from starter.agent import LegacyAgent as Agent

    catalog = payload["catalog"]
    catalog_ids, categories, products = catalog_index(catalog)
    agent = Agent(catalog)
    started = time.time()
    result = evaluate(agent, payload["samples"], catalog_ids, categories, products)
    result["worker_id"] = payload["worker_id"]
    result["worker_seconds"] = round(time.time() - started, 2)
    result["worker_n"] = len(payload["samples"])
    return result


def _merge(parts: list[dict]) -> dict:
    sessions: list[dict] = []
    prompt = 0
    completion = 0
    for part in parts:
        sessions.extend(part.get("sessions") or [])
        usage = part.get("reported_token_usage") or {}
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
    overall = metric_summary(sessions)
    efficiency = max(0.0, min(1.0, (11.0 - float(overall["mttc"])) / 10.0))
    technical_score = 0.50 * overall["hit_rate_at_10"] + 0.30 * overall["mrr"] + 0.20 * efficiency
    grouped: dict[str, list[dict]] = defaultdict(list)
    for session in sessions:
        grouped[session["scenario_type"]].append(session)
    return {
        **overall,
        "efficiency": round(efficiency, 6),
        "recommended_technical_score": round(technical_score, 6),
        "reported_token_usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
        "scenario_metrics": {name: metric_summary(grouped[name]) for name in sorted(grouped)},
        "sessions": sessions,
        "parallel": {
            "workers": len(parts),
            "worker_seconds": [part.get("worker_seconds") for part in parts],
            "worker_n": [part.get("worker_n") for part in parts],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Parallel DeepSeek public-set eval")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--dataset", default="data/public_set.jsonl")
    parser.add_argument("--output", default="results_deepseek_v4_flash.json")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=0, help="0 = all samples")
    args = parser.parse_args()

    samples = load_jsonl(args.dataset)
    if args.limit and args.limit > 0:
        samples = samples[: args.limit]
    key = _desktop_key()
    workers = max(1, min(args.workers, len(samples)))
    shards = _chunks(samples, workers)
    print(
        f"samples={len(samples)} workers={len(shards)} model={args.model} key_len={len(key)}",
        flush=True,
    )
    t0 = time.time()
    parts: list[dict] = []
    with ProcessPoolExecutor(max_workers=len(shards)) as pool:
        futs = [
            pool.submit(
                _worker,
                {
                    "worker_id": i,
                    "samples": shard,
                    "catalog": args.catalog,
                    "api_key": key,
                    "model": args.model,
                    "timeout": args.timeout,
                },
            )
            for i, shard in enumerate(shards)
        ]
        for fut in as_completed(futs):
            part = fut.result()
            usage = part.get("reported_token_usage") or {}
            print(
                f"worker {part.get('worker_id')} n={part.get('worker_n')} "
                f"hit={part.get('hit_rate_at_10')} s={part.get('worker_seconds')} "
                f"tokens={usage.get('total_tokens')}",
                flush=True,
            )
            parts.append(part)
    parts.sort(key=lambda item: int(item.get("worker_id") or 0))
    merged = _merge(parts)
    merged["wall_seconds"] = round(time.time() - t0, 2)
    Path(args.output).write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    printable = {k: v for k, v in merged.items() if k != "sessions"}
    print(json.dumps(printable, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
