#!/usr/bin/env python3
"""Combinatorial retrieval ablations on the TechJam public set.

Reuses the official evaluate() loop.  Extra wheels are optional: missing
imports are skipped instead of failing the run.  Default Agent behavior is
unchanged when this script is not used.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from evaluator.local_evaluator import (
    catalog_index,
    coarse_category,
    customer_reply,
    evaluate,
    initial_message,
    load_jsonl,
    materialize_hidden_fields,
    MAX_TURNS,
    metric_summary,
    normalize_recommendations,
    TOP_K,
)
from starter.agent import LegacyAgent as Agent
from starter.shopping_agent.catalog import CatalogRepository
from starter.shopping_agent.config import AgentConfig
from starter.shopping_agent.retrieval import (
    ExtraRoutes,
    FlashRankReranker,
    available_optional_backends,
)


BASELINE_MISS_PATH = Path("results_rules.json")
CACHE_DIR = Path("data/.retrieval_cache")


def _load_baseline_misses(path: Path) -> list[str]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [item["sample_id"] for item in payload.get("sessions", []) if not item.get("hit")]


def _config(*, routes: str, reranker: str, retrieval_limit: int, trace: bool) -> AgentConfig:
    return AgentConfig(
        retrieval_routes=routes,
        reranker=reranker,
        retrieval_limit=retrieval_limit,
        candidate_limit=30,
        rerank_limit=80,
        trace_retrieval=trace,
    )


def _make_agent(
    repository: CatalogRepository,
    *,
    routes: str,
    reranker: str,
    retrieval_limit: int,
    extra: ExtraRoutes | None,
    flash: FlashRankReranker | None,
    trace: bool,
) -> Agent:
    extra_names = tuple(
        part.strip()
        for part in routes.split(",")
        if part.strip() in {"title", "bm25s", "fuzzy", "minilm"}
    )
    if extra is not None:
        extra.activate(extra_names)
        extra_arg = extra if extra_names else None
    else:
        extra_arg = None
    flash_arg = flash if reranker == "flashrank" and flash is not None and flash.ready else None
    return Agent(
        repository.catalog_path or "data/catalog.jsonl",
        config=_config(routes=routes, reranker=reranker, retrieval_limit=retrieval_limit, trace=trace),
        repository=repository,
        extra_routes=extra_arg,
        lexical_reranker=flash_arg,
    )


def _gold_rank(ids: list[str], target: str) -> int | None:
    try:
        return ids.index(target) + 1
    except ValueError:
        return None


def probe_sessions(
    agent: Agent,
    samples: list[dict],
    catalog_ids: set[str],
    categories: dict[str, list[str]],
    products: dict[str, dict],
) -> list[dict]:
    """Replay the official simulator while recording gold ranks in the pool."""

    traces: list[dict] = []
    for sample in samples:
        session_id = f"probe_{sample['sample_id']}"
        agent.reset(session_id, sample["user_profile"])
        target = str(sample["ground_truth"]["parent_asin"])
        card, behavior = materialize_hidden_fields(sample, products)
        effective = {**sample, "intent_card": card, "behavior": behavior}
        disclosed: set[str] = set()
        boundary_used = False
        override_applied = sample["scenario_type"] != "intent_override"
        user_message = initial_message(effective, coarse_category(categories.get(target, [])), disclosed)
        best_retrieved: int | None = None
        best_feature: int | None = None
        hit_turn: int | None = None
        best_rank: int | None = None
        turns: list[dict] = []
        for turn in range(1, MAX_TURNS + 1):
            response = agent.respond(session_id, user_message, turn, TOP_K)
            ranked = normalize_recommendations(response.get("recommendations"), catalog_ids)
            diagnostics = agent.last_diagnostics or {}
            retrieved_ids = [str(item) for item in diagnostics.get("retrieved_ids") or []]
            feature_ids = [str(item) for item in diagnostics.get("feature_ids") or []]
            retrieved_rank = _gold_rank(retrieved_ids, target)
            feature_rank = _gold_rank(feature_ids, target)
            top_rank = _gold_rank(ranked, target)
            if retrieved_rank is not None:
                best_retrieved = retrieved_rank if best_retrieved is None else min(best_retrieved, retrieved_rank)
            if feature_rank is not None:
                best_feature = feature_rank if best_feature is None else min(best_feature, feature_rank)
            if override_applied and top_rank is not None:
                best_rank = top_rank
                hit_turn = turn
            turns.append(
                {
                    "turn": turn,
                    "retrieved_rank": retrieved_rank,
                    "feature_rank": feature_rank,
                    "top10_rank": top_rank,
                    "sources": diagnostics.get("retrieval_sources"),
                }
            )
            if override_applied and top_rank is not None:
                break
            if turn == MAX_TURNS:
                break
            override = effective.get("behavior", {}).get("override") or {}
            if not override_applied and turn + 1 == int(override.get("turn", 3)):
                override_applied = True
                new_value = str(override.get("new_value", ""))
                if new_value:
                    disclosed.add(new_value)
                user_message = str(override.get("message", "Actually, please ignore my earlier preference."))
            else:
                user_message, boundary_used = customer_reply(
                    effective, response.get("ask_attribute"), disclosed, boundary_used
                )
        traces.append(
            {
                "sample_id": sample["sample_id"],
                "scenario_type": sample["scenario_type"],
                "target": target,
                "hit": hit_turn is not None,
                "first_hit_turn": hit_turn,
                "best_rank": best_rank,
                "best_retrieved_rank": best_retrieved,
                "best_feature_rank": best_feature,
                "in_retrieval_pool": best_retrieved is not None,
                "in_feature_window": best_feature is not None,
                "turns": turns,
            }
        )
    return traces


def summarize_probe(traces: list[dict]) -> dict:
    n = len(traces) or 1
    return {
        "n": len(traces),
        "hit": sum(item["hit"] for item in traces),
        "in_retrieval_pool": sum(item["in_retrieval_pool"] for item in traces),
        "in_feature_window": sum(item["in_feature_window"] for item in traces),
        "hit_rate": round(sum(item["hit"] for item in traces) / n, 4),
        "recall_pool_rate": round(sum(item["in_retrieval_pool"] for item in traces) / n, 4),
    }


def backend_oracle(
    extra: ExtraRoutes,
    repository: CatalogRepository,
    samples: list[dict],
    categories: dict[str, list[str]],
    products: dict[str, dict],
    *,
    limit: int = 200,
) -> dict[str, dict]:
    """Search each backend with the first-turn query of every miss."""

    names = ("fts", "title", "bm25s", "fuzzy", "minilm")
    stats = {name: {"found": 0, "ranks": []} for name in names}
    details: dict[str, dict[str, int | None]] = {}
    for sample in samples:
        target = str(sample["ground_truth"]["parent_asin"])
        card, behavior = materialize_hidden_fields(sample, products)
        disclosed: set[str] = set()
        query = initial_message(
            {**sample, "intent_card": card, "behavior": behavior},
            coarse_category(categories.get(target, [])),
            disclosed,
        )
        if card.get("hard_constraints"):
            query = f"{query} {card['hard_constraints'][0]}"
        ranks: dict[str, int | None] = {}
        fts = repository.search_with_scores(query, limit, source="fts")
        ranks["fts"] = _gold_rank([item.parent_asin for item in fts], target)
        extra.activate(("title", "bm25s", "fuzzy", "minilm"))
        extras = {name: items for items, name, _weight in extra.search_all(query, limit)}
        for name in ("title", "bm25s", "fuzzy", "minilm"):
            ranks[name] = _gold_rank([item.parent_asin for item in extras.get(name, [])], target)
        details[sample["sample_id"]] = ranks
        for name, rank in ranks.items():
            if rank is not None:
                stats[name]["found"] += 1
                stats[name]["ranks"].append(rank)
    summary = {}
    for name, payload in stats.items():
        found = payload["found"]
        ranks = payload["ranks"]
        summary[name] = {
            "found": found,
            "n": len(samples),
            "recall": round(found / (len(samples) or 1), 4),
            "median_rank": sorted(ranks)[len(ranks) // 2] if ranks else None,
            "hits_at_10": sum(rank <= 10 for rank in ranks),
            "hits_at_50": sum(rank <= 50 for rank in ranks),
            "hits_at_100": sum(rank <= 100 for rank in ranks),
        }
    return {"summary": summary, "per_sample": details}


def planned_combos(flags: dict[str, bool]) -> list[dict]:
    combos = [
        {"name": "baseline_fts", "routes": "fts", "reranker": "feature", "retrieval_limit": 100},
        {"name": "fts_title", "routes": "fts,title", "reranker": "feature", "retrieval_limit": 100},
        {"name": "fts_k250", "routes": "fts", "reranker": "feature", "retrieval_limit": 250},
    ]
    if flags.get("bm25s"):
        combos.append({"name": "fts_bm25s", "routes": "fts,bm25s", "reranker": "feature", "retrieval_limit": 100})
    if flags.get("fuzzy"):
        combos.append({"name": "fts_fuzzy", "routes": "fts,fuzzy", "reranker": "feature", "retrieval_limit": 100})
    if flags.get("bm25s") and flags.get("fuzzy"):
        combos.append(
            {
                "name": "fts_title_bm25s_fuzzy",
                "routes": "fts,title,bm25s,fuzzy",
                "reranker": "feature",
                "retrieval_limit": 100,
            }
        )
        combos.append(
            {
                "name": "lex_k250",
                "routes": "fts,title,bm25s,fuzzy",
                "reranker": "feature",
                "retrieval_limit": 250,
            }
        )
    if flags.get("minilm"):
        combos.append({"name": "fts_minilm", "routes": "fts,minilm", "reranker": "feature", "retrieval_limit": 100})
        if flags.get("bm25s") and flags.get("fuzzy"):
            combos.append(
                {
                    "name": "lex_minilm",
                    "routes": "fts,title,bm25s,fuzzy,minilm",
                    "reranker": "feature",
                    "retrieval_limit": 100,
                }
            )
    if flags.get("flashrank") and flags.get("bm25s") and flags.get("fuzzy"):
        combos.append(
            {
                "name": "lex_flashrank",
                "routes": "fts,title,bm25s,fuzzy",
                "reranker": "flashrank",
                "retrieval_limit": 100,
            }
        )
        if flags.get("minilm"):
            combos.append(
                {
                    "name": "all_flashrank",
                    "routes": "fts,title,bm25s,fuzzy,minilm",
                    "reranker": "flashrank",
                    "retrieval_limit": 100,
                }
            )
    return combos


def _combo_summary(combo: dict, result: dict, elapsed: float, baseline_misses: set[str], baseline_hits: set[str]) -> dict:
    recovered = recovered_misses(result["sessions"], baseline_misses)
    lost = lost_hits(result["sessions"], baseline_hits)
    return {
        "name": combo["name"],
        "routes": combo["routes"],
        "reranker": combo["reranker"],
        "retrieval_limit": combo["retrieval_limit"],
        "seconds": elapsed,
        "hit_rate_at_10": result["hit_rate_at_10"],
        "mrr": result["mrr"],
        "mttc": result["mttc"],
        "efficiency": result["efficiency"],
        "recommended_technical_score": result["recommended_technical_score"],
        "scenario_metrics": result["scenario_metrics"],
        "recovered_misses": recovered,
        "lost_hits": lost,
        "recovered_n": len(recovered),
        "lost_n": len(lost),
    }


def _print_combo(summary: dict) -> None:
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
                    "recovered_n",
                    "lost_n",
                    "seconds",
                )
            }
        ),
        flush=True,
    )


def recovered_misses(sessions: list[dict], baseline_misses: set[str]) -> list[str]:
    return sorted(
        item["sample_id"]
        for item in sessions
        if item.get("hit") and item.get("sample_id") in baseline_misses
    )


def lost_hits(sessions: list[dict], baseline_hits: set[str]) -> list[str]:
    return sorted(
        item["sample_id"]
        for item in sessions
        if not item.get("hit") and item.get("sample_id") in baseline_hits
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieval combo ablation on the public set")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--dataset", default="data/public_set.jsonl")
    parser.add_argument("--output", default="results_retrieval_combos.json")
    parser.add_argument("--skip-minilm", action="store_true")
    parser.add_argument("--skip-flashrank", action="store_true")
    parser.add_argument("--only-probe", action="store_true")
    args = parser.parse_args()

    started = time.perf_counter()
    flags = available_optional_backends()
    if args.skip_minilm:
        flags["minilm"] = False
    if args.skip_flashrank:
        flags["flashrank"] = False

    samples = load_jsonl(args.dataset)
    catalog_ids, categories, products = catalog_index(args.catalog)
    repository = CatalogRepository(args.catalog)
    baseline_misses = _load_baseline_misses(BASELINE_MISS_PATH)
    miss_samples = [sample for sample in samples if sample["sample_id"] in set(baseline_misses)]
    baseline_hit_ids = {
        item["sample_id"]
        for item in (json.loads(BASELINE_MISS_PATH.read_text(encoding="utf-8")).get("sessions", []) if BASELINE_MISS_PATH.exists() else [])
        if item.get("hit")
    }

    print("catalog", len(repository), "samples", len(samples), "baseline_misses", len(baseline_misses))
    print("backends", flags)

    baseline_agent = _make_agent(
        repository,
        routes="fts",
        reranker="feature",
        retrieval_limit=100,
        extra=None,
        flash=None,
        trace=True,
    )
    print("probing baseline recall on", len(miss_samples), "misses...")
    miss_probe = probe_sessions(baseline_agent, miss_samples, catalog_ids, categories, products)
    print("miss probe", summarize_probe(miss_probe))

    extra = ExtraRoutes(repository, (), cache_dir=CACHE_DIR)
    lexical_routes = ["title"]
    if flags.get("bm25s"):
        lexical_routes.append("bm25s")
    if flags.get("fuzzy"):
        lexical_routes.append("fuzzy")
    extra.activate(lexical_routes)
    print("preparing lexical indexes", extra.routes)
    prepare_started = time.perf_counter()
    extra.prepare()
    print("lexical_prepare_seconds", round(time.perf_counter() - prepare_started, 2), "warnings", extra.warnings)
    flags["bm25s"] = extra.ready("bm25s")
    flags["fuzzy"] = extra.ready("fuzzy")

    oracle = {"summary": {}, "per_sample": {}}
    if miss_samples:
        oracle = backend_oracle(extra, repository, miss_samples, categories, products)
        print("lexical backend oracle on misses:")
        print(json.dumps(oracle["summary"], indent=2))

    combo_results: list[dict] = []

    def dump_partial() -> None:
        Path(args.output).write_text(
            json.dumps(
                {
                    "backends": flags,
                    "prepare_warnings": extra.warnings,
                    "baseline_misses": baseline_misses,
                    "miss_probe": {
                        "summary": summarize_probe(miss_probe),
                        "still_outside_pool": [
                            item["sample_id"] for item in miss_probe if not item["in_retrieval_pool"]
                        ],
                        "in_pool_not_top10": [
                            item["sample_id"]
                            for item in miss_probe
                            if item["in_retrieval_pool"] and not item["hit"]
                        ],
                    },
                    "backend_oracle": oracle,
                    "combos": combo_results,
                    "seconds": round(time.perf_counter() - started, 2),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def run_combo(combo: dict, flash: FlashRankReranker | None) -> None:
        print("evaluating", combo["name"], combo["routes"], combo["reranker"], "k", combo["retrieval_limit"], flush=True)
        agent = _make_agent(
            repository,
            routes=combo["routes"],
            reranker=combo["reranker"],
            retrieval_limit=combo["retrieval_limit"],
            extra=extra,
            flash=flash,
            trace=False,
        )
        combo_started = time.perf_counter()
        result = evaluate(agent, samples, catalog_ids, categories, products)
        summary = _combo_summary(
            combo,
            result,
            round(time.perf_counter() - combo_started, 2),
            set(baseline_misses),
            baseline_hit_ids,
        )
        combo_results.append(summary)
        _print_combo(summary)
        dump_partial()

    dump_partial()
    if not args.only_probe:
        lexical_flags = dict(flags)
        lexical_flags["minilm"] = False
        lexical_flags["flashrank"] = False
        for combo in planned_combos(lexical_flags):
            run_combo(combo, None)

        if flags.get("minilm"):
            extra.activate(lexical_routes + ["minilm"])
            print("preparing MiniLM on", len(repository), "titles (CPU)", flush=True)
            minilm_started = time.perf_counter()
            extra.prepare()
            print("minilm_prepare_seconds", round(time.perf_counter() - minilm_started, 2), "warnings", extra.warnings, flush=True)
            flags["minilm"] = extra.ready("minilm")
            if miss_samples and flags["minilm"]:
                oracle = backend_oracle(extra, repository, miss_samples, categories, products)
                print("oracle with MiniLM:", flush=True)
                print(json.dumps(oracle["summary"], indent=2), flush=True)
                dump_partial()

        flash = None
        if flags.get("flashrank"):
            flash = FlashRankReranker(cache_dir=CACHE_DIR / "flashrank", limit=80)
            print("flashrank ready", flash.ready, "warning", flash.warning, flush=True)
            flags["flashrank"] = bool(flash.ready)

        remaining = [
            combo
            for combo in planned_combos(flags)
            if combo["name"] not in {item["name"] for item in combo_results}
        ]
        for combo in remaining:
            run_combo(combo, flash)

    payload = {
        "backends": flags,
        "prepare_warnings": extra.warnings,
        "baseline_misses": baseline_misses,
        "miss_probe": {
            "summary": summarize_probe(miss_probe),
            "still_outside_pool": [
                item["sample_id"] for item in miss_probe if not item["in_retrieval_pool"]
            ],
            "in_pool_not_top10": [
                item["sample_id"]
                for item in miss_probe
                if item["in_retrieval_pool"] and not item["hit"]
            ],
            "sessions": miss_probe,
        },
        "backend_oracle": oracle,
        "combos": combo_results,
        "seconds": round(time.perf_counter() - started, 2),
    }
    Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print("wrote", args.output, "in", payload["seconds"], "s")
    if combo_results:
        print("combo table")
        print(
            f"{'name':28} {'Hit@10':>7} {'MRR':>7} {'MTTC':>6} {'score':>7} {'rec':>4} {'lost':>4}"
        )
        for item in combo_results:
            print(
                f"{item['name']:28} {item['hit_rate_at_10']:7.3f} {item['mrr']:7.3f} "
                f"{item['mttc']:6.2f} {item['recommended_technical_score']:7.3f} "
                f"{item['recovered_n']:4d} {item['lost_n']:4d}"
            )


if __name__ == "__main__":
    main()
