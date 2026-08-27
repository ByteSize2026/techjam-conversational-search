#!/usr/bin/env python3
"""Build a disjoint 200-session holdout and compare ContestAgent vs classmate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl
from starter.shopping_agent.contest_agent import ContestAgent
from starter.shopping_agent.contest_config import PUBLIC
from starter.shopping_agent.contest_index import ContestIndex
from starter.shopping_agent.holdout import SCENARIO_MIX, build_holdout, public_asins, write_jsonl

ROOT = Path(__file__).resolve().parent
CLASSMATE_ROOT = ROOT.parent / "classmate-buyteSize" / "techjam-conversational-search"
PUBLIC_SCORE = {
    "source": "results_contest_public.json",
    "hit_rate_at_10": 1.0,
    "mrr": 0.946714,
    "mttc": 2.53,
    "efficiency": 0.847,
    "recommended_technical_score": 0.953414,
}
CLASSMATE_PUBLIC = {
    "source": "classmate-buyteSize/result/report.md (full LLM+bge on public 200)",
    "hit_rate_at_10": 0.995,
    "mrr": 0.9358,
    "mttc": 2.685,
    "efficiency": 0.8315,
    "recommended_technical_score": 0.9445,
}


def _summarize(name: str, payload: dict, extra: dict | None = None) -> dict:
    summary = {
        "name": name,
        "sample_count": payload.get("sample_count"),
        "hit_rate_at_10": payload["hit_rate_at_10"],
        "mrr": payload["mrr"],
        "mttc": payload["mttc"],
        "efficiency": payload["efficiency"],
        "recommended_technical_score": payload["recommended_technical_score"],
        "scenario_metrics": payload.get("scenario_metrics"),
        "misses": [item["sample_id"] for item in payload.get("sessions", []) if not item.get("hit")],
        "reported_token_usage": payload.get("reported_token_usage"),
    }
    if extra:
        summary.update(extra)
    return summary


def _load_classmate(catalog: Path):
    os.environ["TECHJAM_NO_EMBED"] = "1"
    os.environ["TECHJAM_NO_LLM"] = "1"
    os.environ.setdefault("TECHJAM_GATE_SIZE", "5")
    classmate_root = str(CLASSMATE_ROOT.resolve())
    if classmate_root not in sys.path:
        sys.path.insert(0, classmate_root)
    from src.agent import ShoppingAgent  # type: ignore

    return ShoppingAgent(catalog), "structured (TECHJAM_NO_LLM=1, TECHJAM_NO_EMBED=1, gate=5)"


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdout 200 compare")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--public", default="data/public_set.jsonl")
    parser.add_argument("--holdout", default="holdout/holdout_200.jsonl")
    parser.add_argument("--output", default="holdout/holdout_compare.json")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--skip-generate", action="store_true")
    args = parser.parse_args()

    catalog = Path(args.catalog)
    public_rows = load_jsonl(args.public)
    excluded = public_asins(public_rows)
    holdout_path = Path(args.holdout)
    if args.skip_generate and holdout_path.exists():
        holdout_rows = load_jsonl(holdout_path)
    else:
        print("building holdout from", catalog, "exclude", len(excluded), flush=True)
        products = []
        with catalog.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    products.append(json.loads(line))
        profiles = [row["user_profile"] for row in public_rows if isinstance(row.get("user_profile"), dict)]
        holdout_rows = build_holdout(
            products,
            exclude=excluded,
            mix=SCENARIO_MIX,
            seed=args.seed,
            sample_prefix="holdout",
            profiles=profiles or None,
        )
        write_jsonl(holdout_path, holdout_rows)
        print("wrote", holdout_path, "n", len(holdout_rows), flush=True)

    asins = [str(row["ground_truth"]["parent_asin"]) for row in holdout_rows]
    mix = {}
    for row in holdout_rows:
        mix[row["scenario_type"]] = mix.get(row["scenario_type"], 0) + 1
    print("mix", mix, "disjoint", set(asins).isdisjoint(excluded), flush=True)

    catalog_ids, categories, products_map = catalog_index(catalog)
    print("loading contest index", flush=True)
    started = time.perf_counter()
    index = ContestIndex(catalog)
    print("index_seconds", round(time.perf_counter() - started, 2), flush=True)

    ours_agent = ContestAgent(catalog, config=PUBLIC, index=index)
    print("evaluating ours PUBLIC", flush=True)
    ours_started = time.perf_counter()
    ours_payload = evaluate(ours_agent, holdout_rows, catalog_ids, categories, products_map)
    ours_summary = _summarize(
        "ours_holdout",
        ours_payload,
        {"seconds": round(time.perf_counter() - ours_started, 2), "variant": "ContestAgent PUBLIC"},
    )
    print(json.dumps({k: ours_summary[k] for k in (
        "hit_rate_at_10", "mrr", "mttc", "recommended_technical_score", "misses",
    )}), flush=True)

    print("loading classmate structured agent", flush=True)
    classmate_agent, classmate_variant = _load_classmate(catalog)
    print("evaluating classmate", classmate_variant, flush=True)
    class_started = time.perf_counter()
    class_payload = evaluate(classmate_agent, holdout_rows, catalog_ids, categories, products_map)
    class_summary = _summarize(
        "classmate_holdout",
        class_payload,
        {"seconds": round(time.perf_counter() - class_started, 2), "variant": classmate_variant},
    )
    print(json.dumps({k: class_summary[k] for k in (
        "hit_rate_at_10", "mrr", "mttc", "recommended_technical_score", "misses",
    )}), flush=True)

    ours_holdout_score = ours_summary["recommended_technical_score"]
    class_holdout_score = class_summary["recommended_technical_score"]
    better = "ours" if ours_holdout_score > class_holdout_score else (
        "classmate" if class_holdout_score > ours_holdout_score else "tie"
    )
    comparison = {
        "holdout_path": str(holdout_path),
        "seed": args.seed,
        "sample_count": len(holdout_rows),
        "mix": mix,
        "disjoint_from_public": set(asins).isdisjoint(excluded),
        "public_exclude_count": len(excluded),
        "rows": {
            "ours_public": PUBLIC_SCORE,
            "ours_holdout": ours_summary,
            "classmate_public": CLASSMATE_PUBLIC,
            "classmate_holdout": class_summary,
        },
        "better_holdout_agent": better,
        "ours_public_overstated": PUBLIC_SCORE["recommended_technical_score"] > ours_holdout_score,
        "classmate_public_overstated": CLASSMATE_PUBLIC["recommended_technical_score"] > class_holdout_score,
        "note": (
            "Holdout is ID-disjoint on the same frozen catalog and official simulator; "
            "it is not the organizer private 800. Classmate holdout uses structured "
            "fallback because LLM/dense are disabled on this host."
        ),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
    Path("holdout/ours_holdout.json").write_text(json.dumps(ours_payload), encoding="utf-8")
    Path("holdout/classmate_holdout.json").write_text(json.dumps(class_payload), encoding="utf-8")
    print("wrote", output, "better", better, flush=True)
    print(
        f"{'row':22} {'Hit':>7} {'MRR':>8} {'MTTC':>6} {'score':>8}\n"
        f"{'ours_public':22} {PUBLIC_SCORE['hit_rate_at_10']:7.3f} {PUBLIC_SCORE['mrr']:8.4f} "
        f"{PUBLIC_SCORE['mttc']:6.3f} {PUBLIC_SCORE['recommended_technical_score']:8.4f}\n"
        f"{'ours_holdout':22} {ours_summary['hit_rate_at_10']:7.3f} {ours_summary['mrr']:8.4f} "
        f"{ours_summary['mttc']:6.3f} {ours_summary['recommended_technical_score']:8.4f}\n"
        f"{'classmate_public':22} {CLASSMATE_PUBLIC['hit_rate_at_10']:7.3f} {CLASSMATE_PUBLIC['mrr']:8.4f} "
        f"{CLASSMATE_PUBLIC['mttc']:6.3f} {CLASSMATE_PUBLIC['recommended_technical_score']:8.4f}\n"
        f"{'classmate_holdout':22} {class_summary['hit_rate_at_10']:7.3f} {class_summary['mrr']:8.4f} "
        f"{class_summary['mttc']:6.3f} {class_summary['recommended_technical_score']:8.4f}"
    )


if __name__ == "__main__":
    main()
