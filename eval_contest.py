#!/usr/bin/env python3
"""Public-200 eval for ContestAgent variants (default: PUBLIC).

Writes results_contest_public.json when the public variant runs.
``python -m evaluator.local_evaluator`` loads the same Agent.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl
from starter.shopping_agent.contest_agent import ContestAgent
from starter.shopping_agent.contest_config import CLASSMATE, HYBRID, KHANNA, PUBLIC, ContestConfig
from starter.shopping_agent.contest_index import ContestIndex


VARIANTS: list[tuple[str, ContestConfig]] = [
    ("contest_public", PUBLIC),
    ("contest_khanna", KHANNA),
    ("contest_hybrid", HYBRID),
    ("contest_classmate_gate", CLASSMATE),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Contest-agent public-set eval")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--dataset", default="data/public_set.jsonl")
    parser.add_argument("--output", default="results_contest.json")
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated variant names (khanna, hybrid, classmate_gate)",
    )
    args = parser.parse_args()
    wanted = {part.strip() for part in args.only.split(",") if part.strip()}
    catalog_ids, categories, products = catalog_index(args.catalog)
    samples = load_jsonl(args.dataset)
    print("loading contest index", flush=True)
    started = time.perf_counter()
    index = ContestIndex(args.catalog)
    print("index_seconds", round(time.perf_counter() - started, 2), "n", len(index), flush=True)

    results: list[dict] = []
    for name, config in VARIANTS:
        short = name.removeprefix("contest_")
        if wanted and short not in wanted and name not in wanted:
            continue
        print("evaluating", name, "gate", config.gate_size, "hard", config.hard_filter, flush=True)
        agent = ContestAgent(args.catalog, config=config, index=index)
        combo_started = time.perf_counter()
        payload = evaluate(agent, samples, catalog_ids, categories, products)
        elapsed = round(time.perf_counter() - combo_started, 2)
        summary = {
            "name": name,
            "gate_size": config.gate_size,
            "hard_filter": config.hard_filter,
            "seconds": elapsed,
            "hit_rate_at_10": payload["hit_rate_at_10"],
            "mrr": payload["mrr"],
            "mttc": payload["mttc"],
            "efficiency": payload["efficiency"],
            "recommended_technical_score": payload["recommended_technical_score"],
            "scenario_metrics": payload["scenario_metrics"],
            "misses": [item["sample_id"] for item in payload["sessions"] if not item.get("hit")],
        }
        results.append(summary)
        print(
            json.dumps(
                {
                    key: summary[key]
                    for key in (
                        "name",
                        "hit_rate_at_10",
                        "mrr",
                        "mttc",
                        "recommended_technical_score",
                        "seconds",
                    )
                }
            ),
            flush=True,
        )
        Path(args.output).write_text(json.dumps({"combos": results}, indent=2) + "\n", encoding="utf-8")
        if name == "contest_public":
            Path("results_contest_public.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print("contest table", flush=True)
    print(f"{'name':24} {'Hit@10':>7} {'MRR':>7} {'MTTC':>6} {'score':>7}")
    for item in results:
        print(
            f"{item['name']:24} {item['hit_rate_at_10']:7.3f} {item['mrr']:7.3f} "
            f"{item['mttc']:6.2f} {item['recommended_technical_score']:7.3f}"
        )


if __name__ == "__main__":
    main()
