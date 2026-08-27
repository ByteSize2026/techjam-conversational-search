"""Seeded holdout sessions with the public-set row shape, disjoint asins."""

from __future__ import annotations

import json
import math
import random
from collections.abc import Mapping, Sequence
from pathlib import Path

SCENARIO_MIX: dict[str, int] = {
    "buying": 80,
    "browsing": 80,
    "intent_override": 30,
    "boundary": 10,
}
DIFFICULTY = {
    "buying": "easy",
    "browsing": "medium",
    "intent_override": "hard",
    "boundary": "medium",
}
_DEFAULT_PROFILE = {
    "average_prior_rating": 4.0,
    "preference_tags": ["fit", "comfort"],
    "purchase_frequency": "3-4 prior purchases",
    "rating_style": "usually positive",
    "summary": "Prior purchases emphasize fit, comfort; ratings are usually positive.",
}


def load_jsonl(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: str | Path, rows: Sequence[Mapping]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return target


def public_asins(rows: Sequence[Mapping]) -> set[str]:
    found: set[str] = set()
    for row in rows:
        ground = row.get("ground_truth") if isinstance(row.get("ground_truth"), Mapping) else {}
        asin = str(ground.get("parent_asin") or "").strip()
        if asin:
            found.add(asin)
    return found


def _popularity(product: Mapping) -> int:
    try:
        return int(product.get("rating_number") or product.get("rating_count") or 0)
    except (TypeError, ValueError):
        return 0


def _eligible(products: Sequence[Mapping], exclude: set[str]) -> list[tuple[str, int]]:
    best: dict[str, int] = {}
    for product in products:
        asin = str(product.get("parent_asin") or "").strip()
        if not asin or asin in exclude:
            continue
        pop = _popularity(product)
        if asin not in best or pop > best[asin]:
            best[asin] = pop
    return list(best.items())


def _weighted_sample(items: list[tuple[str, int]], k: int, rng: random.Random) -> list[str]:
    pool = list(items)
    weights = [math.log1p(max(pop, 0)) + 1.0 for _asin, pop in pool]
    chosen: list[str] = []
    for _ in range(k):
        total = sum(weights)
        pick = rng.random() * total
        acc = 0.0
        index = len(pool) - 1
        for i, weight in enumerate(weights):
            acc += weight
            if acc >= pick:
                index = i
                break
        chosen.append(pool[index][0])
        pool.pop(index)
        weights.pop(index)
    return chosen


def build_holdout(
    products: Sequence[Mapping],
    *,
    exclude: set[str],
    mix: Mapping[str, int] | None = None,
    seed: int = 2026,
    sample_prefix: str = "holdout",
    profiles: Sequence[Mapping] | None = None,
) -> list[dict]:
    """Return public_set-shaped sessions. Does not bake intent_card or behavior."""

    counts = dict(mix or SCENARIO_MIX)
    needed = sum(int(counts[name]) for name in counts)
    eligible = _eligible(products, exclude)
    if len(eligible) < needed:
        raise ValueError(f"need {needed} unused catalog asins, have {len(eligible)}")
    rng = random.Random(seed)
    asins = _weighted_sample(eligible, needed, rng)
    rng.shuffle(asins)
    scenarios: list[str] = []
    for name, count in counts.items():
        scenarios.extend([str(name)] * int(count))
    rng.shuffle(scenarios)
    rows: list[dict] = []
    for i, (asin, scenario) in enumerate(zip(asins, scenarios, strict=True), start=1):
        if profiles:
            profile = dict(profiles[(i - 1) % len(profiles)])
        else:
            profile = dict(_DEFAULT_PROFILE)
        rows.append(
            {
                "sample_id": f"{sample_prefix}_{i:04d}",
                "scenario_type": scenario,
                "category_bucket": "clothing",
                "difficulty_bucket": DIFFICULTY.get(scenario, "medium"),
                "user_profile": profile,
                "ground_truth": {"parent_asin": asin},
            }
        )
    return rows
