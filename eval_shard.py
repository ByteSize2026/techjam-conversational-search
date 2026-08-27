#!/usr/bin/env python3
"""Evaluate ContestAgent PUBLIC on one JSONL shard. Used by parallel workers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl
from starter.shopping_agent.contest_agent import ContestAgent
from starter.shopping_agent.contest_config import PUBLIC
from starter.shopping_agent.contest_index import ContestIndex


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    catalog_ids, categories, products = catalog_index(args.catalog)
    samples = load_jsonl(args.dataset)
    index = ContestIndex(args.catalog)
    agent = ContestAgent(args.catalog, config=PUBLIC, index=index)
    payload = evaluate(agent, samples, catalog_ids, categories, products)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "dataset": args.dataset,
                "n": payload["sample_count"],
                "hit_rate_at_10": payload["hit_rate_at_10"],
                "mrr": payload["mrr"],
                "mttc": payload["mttc"],
                "recommended_technical_score": payload["recommended_technical_score"],
                "misses": [s["sample_id"] for s in payload["sessions"] if not s.get("hit")],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
