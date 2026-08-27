"""Category lock, soft constraint scoring, optional hard conjunction, padding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .contest_config import ContestConfig
from .contest_dense import dense_doc, dense_query, get_encoder
from .contest_rerank import get_reranker
from .contest_index import ContestIndex
from .contest_slots import ContestState, Slot
from .contest_text import (
    CHROME,
    COLORS,
    MATERIALS,
    STOPWORDS,
    coarse_category,
    constraint_matches,
    fold_punct,
    normalise,
    parse_price,
    product_search_text,
    terms,
)

# Tokens that appear in many titles and should not drive the title tie-break.
_DENSE_GENERIC = STOPWORDS | CHROME | set(MATERIALS) | set(COLORS) | {
    "imported",
    "machine",
    "wash",
    "cold",
    "made",
    "china",
    "usa",
    "color",
    "colour",
    "material",
    "percent",
    "department",
    "brand",
    "style",
    "feature",
    "men",
    "women",
    "mens",
    "womens",
    "unisex",
    "size",
    "small",
    "medium",
    "large",
    "100",
}

_TITLE_SKIP = STOPWORDS | CHROME | set(MATERIALS) | set(COLORS) | {
    "size",
    "small",
    "medium",
    "large",
    "xl",
    "xxl",
    "men",
    "man",
    "women",
    "woman",
    "mens",
    "womens",
    "boys",
    "girls",
    "kid",
    "kids",
    "unisex",
    "pair",
    "pack",
    "set",
    "new",
    "one",
    "two",
    "three",
    "percent",
    "imported",
    "machine",
    "wash",
    "cold",
    "made",
    "china",
    "usa",
    "color",
    "colour",
    "material",
    "department",
    "brand",
    "style",
    "feature",
}


def _title_tokens(item: Slot) -> list[str]:
    return [
        token
        for token in item.tokens
        if token not in _TITLE_SKIP and len(token) >= 3 and not token.isdigit()
    ]


def title_bonus(title: str, slots: Sequence[Slot]) -> float:
    """How much of each disclosed constraint is visible in the product title.

    After a hard conjunction, clones share feature bullets. The title is the
    remaining visibility signal: a less-popular product that advertises the
    disclosed phrase should outrank a hotter clone that only has it in details.
    """

    if not title or not slots:
        return 0.0
    folded_title = fold_punct(title)
    total = 0.0
    weight_sum = 0.0
    for item in slots:
        if item.kind == "budget":
            continue
        weight = item.weight
        weight_sum += weight
        text = normalise(item.text)
        folded = fold_punct(text)
        if len(text) >= 12 and (text in title or (len(folded) >= 8 and folded in folded_title)):
            total += weight
            continue
        tokens = _title_tokens(item)
        if len(tokens) < 2:
            continue
        hits = sum(1 for token in tokens if token in title)
        if hits >= 2 and hits / len(tokens) >= 0.5:
            total += weight * (hits / len(tokens))
    return total / weight_sum if weight_sum else 0.0


def in_category(product: Mapping[str, object], category_text: str) -> bool:
    query = normalise(category_text)
    if not query:
        return True
    coarse = normalise(coarse_category(product.get("categories") or []))
    if coarse == query:
        return True
    joined = normalise(", ".join(str(value) for value in (product.get("categories") or [])))
    return all(token in joined for token in query.split() if token not in {"&"})


def conjunction_asins(
    products: Sequence[Mapping[str, object]],
    category_text: str,
    constraints: Sequence[str],
) -> list[str]:
    """Category lock then constraint AND. A filter that would empty the pool is skipped."""

    selected = [row for row in products if in_category(row, category_text)]
    if not selected:
        selected = list(products)
    for raw in constraints:
        text = str(raw or "").strip()
        if not text:
            continue
        kept = [
            row
            for row in selected
            if constraint_matches(text, product_search_text(row), parse_price(row.get("price")))
        ]
        if kept:
            selected = kept
    return [str(row.get("parent_asin", "")).strip() for row in selected if str(row.get("parent_asin", "")).strip()]


def satisfies(index: ContestIndex, idx: int, item: Slot) -> float:
    blob = index.blobs[idx]
    price = index.prices[idx]
    if constraint_matches(item.text, blob, price):
        return 1.0
    if item.kind == "budget" and item.value:
        try:
            target = float(item.value)
        except (TypeError, ValueError):
            return 0.25
        if price is None:
            return 0.25
        if target <= 0:
            return 0.25
        return max(0.0, 1.0 - abs(price - target) / target)
    tokens = item.tokens
    if not tokens:
        return 0.0
    bag = index.token_sets[idx]
    hits = sum(1 for token in tokens if token in bag)
    return 0.7 * (hits / len(tokens))


def hard_match(index: ContestIndex, idx: int, item: Slot) -> bool:
    return constraint_matches(item.text, index.blobs[idx], index.prices[idx])


def constraint_score(index: ContestIndex, idx: int, slots: list[Slot]) -> float:
    if not slots:
        return 0.0
    total = 0.0
    weight_sum = 0.0
    for item in slots:
        weight = (1.0 + min(2.0, len(item.tokens) / 6.0)) * item.weight
        total += weight * satisfies(index, idx, item)
        weight_sum += weight
    return total / weight_sum if weight_sum else 0.0


def candidate_pool(index: ContestIndex, state: ContestState, config: ContestConfig) -> list[int]:
    pool: list[int] = []
    if config.use_category_lock and state.category:
        pool = index.bucket(state.category)
    if len(pool) >= config.min_candidates:
        return pool
    tokens, _weights = state.query_tokens()
    extra = index.lexical_hits(tokens or terms(state.category or ""), limit=config.global_fallback_limit)
    seen = set(pool)
    for idx in extra:
        if idx not in seen:
            pool.append(idx)
            seen.add(idx)
        if len(pool) >= max(config.min_candidates, 80):
            break
    return pool or index.popular(config.global_fallback_limit)


def hard_pool(index: ContestIndex, state: ContestState, pool: list[int]) -> list[int]:
    narrowed = list(pool)
    for item in state.active:
        filtered = [idx for idx in narrowed if hard_match(index, idx, item)]
        if filtered:
            narrowed = filtered
        if len(narrowed) <= 1:
            break
    return narrowed


def price_bonus(index: ContestIndex, idx: int, slots: Sequence[Slot]) -> float:
    """1.0 if price matches the disclosed budget exactly, 0 at the hard-match edge."""

    budget = next((item for item in slots if item.kind == "budget" and item.value), None)
    if budget is None:
        return 0.0
    try:
        target = float(budget.value)
    except (TypeError, ValueError):
        return 0.0
    price = index.prices[idx]
    if price is None:
        return 0.0
    width = max(2.0, 0.05 * abs(target) if target else 2.0)
    return max(0.0, 1.0 - abs(price - target) / width)


def rank(
    index: ContestIndex,
    state: ContestState,
    config: ContestConfig,
    pool: list[int],
    *,
    limit: int,
) -> list[int]:
    if not pool:
        return []
    tokens, weights = state.query_tokens()
    unique = list(dict.fromkeys(tokens))[:24]
    slots = state.active if config.use_constraint_scoring else []
    apply_title = bool(
        config.w_title
        and slots
        and (config.title_pool_limit <= 0 or len(pool) <= config.title_pool_limit)
    )
    tags: list[str] = []
    if config.use_profile_prior and not (config.profile_cold_start_only and slots):
        tags = state.profile_tags()
    dense_map: dict[int, float] = {}
    skip_dense = bool(config.dense_skip_generic and slots_are_generic(slots))
    if config.w_dense and not skip_dense and 2 <= len(pool) <= config.dense_pool_limit:
        encoder = get_encoder()
        if encoder.available():
            try:
                values = encoder.pool_scores(
                    dense_query(state),
                    [dense_doc(index, idx) for idx in pool],
                )
            except Exception:
                values = None
            if values is not None and len(values) == len(pool):
                dense_map = {idx: value for idx, value in zip(pool, values, strict=False)}
    rerank_map: dict[int, float] = {}
    if config.w_rerank and 2 <= len(pool) <= config.rerank_pool_limit:
        reranker = get_reranker()
        if reranker.available():
            try:
                values = reranker.pool_scores(
                    dense_query(state),
                    [dense_doc(index, idx) for idx in pool],
                )
            except Exception:
                values = None
            if values is not None and len(values) == len(pool):
                rerank_map = {idx: value for idx, value in zip(pool, values, strict=False)}
    scored: list[tuple[float, str, int]] = []
    for idx in pool:
        score = 0.0
        if unique:
            bag = index.token_sets[idx]
            score += config.w_lexical * sum(weights.get(token, 1.0) for token in unique if token in bag) / (
                sum(weights.get(token, 1.0) for token in unique) or 1.0
            )
        if slots:
            score += config.w_constraint * constraint_score(index, idx, slots)
        if config.use_popularity_prior:
            score += config.w_popularity * index.popularity(idx)
        if apply_title:
            score += config.w_title * title_bonus(index.titles[idx], slots)
        if config.w_price and slots:
            score += config.w_price * price_bonus(index, idx, slots)
        if dense_map:
            score += config.w_dense * dense_map.get(idx, 0.0)
        if rerank_map:
            score += config.w_rerank * rerank_map.get(idx, 0.0)
        if tags:
            blob = index.blobs[idx]
            score += config.w_profile * (sum(1 for tag in tags if tag and tag in blob) / len(tags))
        scored.append((score, index.ids[idx], idx))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if skip_dense:
        # Lexical noise can still drop a pop-rank-8 generic target; lock the
        # popularity head when MiniLM was skipped for catalog chrome.
        scored = apply_pop_floor(scored, index, config.dense_pop_floor or 10)
    elif dense_map or rerank_map:
        if config.dense_rrf_k:
            scored = merge_pop_dense_rrf(scored, index, config.dense_rrf_k)
        elif config.dense_pop_floor:
            scored = apply_pop_floor(scored, index, config.dense_pop_floor)
    return [idx for _score, _asin, idx in scored[: max(limit, 0)]]


def slots_are_generic(slots: Sequence[Slot]) -> bool:
    """True when every slot is color/material/imported-style catalog chrome."""

    if not slots:
        return True
    for item in slots:
        if item.kind == "budget":
            return False
        distinctive = [
            token
            for token in item.tokens
            if token not in _DENSE_GENERIC and len(token) >= 3 and not token.isdigit()
        ]
        if distinctive:
            return False
    return True


def apply_pop_floor(
    scored: Sequence[tuple[float, str, int]],
    index: ContestIndex,
    floor: int,
) -> list[tuple[float, str, int]]:
    """Keep the popularity top-``floor`` items at the head, blended-score order.

    Dense/rerank may reorder those items (MRR) but cannot replace one of them
    with a lower-popularity clone. Pools no larger than ``floor`` are unchanged.
    """

    rows = list(scored)
    if floor <= 0 or len(rows) <= floor:
        return rows
    protected = {
        idx
        for _score, _asin, idx in sorted(rows, key=lambda item: (-index.popularity(item[2]), item[1]))[:floor]
    }
    head = [item for item in rows if item[2] in protected]
    tail = [item for item in rows if item[2] not in protected]
    return head + tail


def merge_pop_dense_rrf(
    scored: Sequence[tuple[float, str, int]],
    index: ContestIndex,
    k: int,
    *,
    rrf_k: int = 10,
) -> list[tuple[float, str, int]]:
    """Put RRF(pop top-k ∪ blended top-k) first so MiniLM cannot drop a
    popular item or bury a dense-promoted clone that already made blended top-k.
    """

    rows = list(scored)
    if k <= 0 or len(rows) <= k:
        return rows
    by_idx = {item[2]: item for item in rows}
    pop_ids = [idx for _score, _asin, idx in sorted(rows, key=lambda item: (-index.popularity(item[2]), item[1]))]
    blend_ids = [idx for _score, _asin, idx in rows]
    pop_rank = {idx: rank for rank, idx in enumerate(pop_ids, 1)}
    blend_rank = {idx: rank for rank, idx in enumerate(blend_ids, 1)}
    seen: set[int] = set()
    union: list[int] = []
    for idx in pop_ids[:k] + blend_ids[:k]:
        if idx not in seen:
            seen.add(idx)
            union.append(idx)
    union.sort(
        key=lambda idx: (
            -(1.0 / (rrf_k + pop_rank[idx]) + 1.0 / (rrf_k + blend_rank[idx])),
            index.ids[idx],
        )
    )
    head = [by_idx[idx] for idx in union]
    tail = [item for item in rows if item[2] not in seen]
    return head + tail


def pad(index: ContestIndex, ranked: list[int], pool: list[int], limit: int) -> list[int]:
    if len(ranked) >= limit:
        return ranked[:limit]
    chosen = list(ranked)
    seen = set(chosen)
    for idx in sorted(pool, key=lambda item: -index.popularity(item)):
        if idx in seen:
            continue
        chosen.append(idx)
        seen.add(idx)
        if len(chosen) >= limit:
            return chosen
    for idx in index.popular(limit * 4, exclude=seen):
        chosen.append(idx)
        seen.add(idx)
        if len(chosen) >= limit:
            break
    return chosen[:limit]


def should_withhold(
    state: ContestState,
    config: ContestConfig,
    soft_n: int,
    hard_n: int,
) -> bool:
    if config.gate_size <= 0:
        return False
    if state.turn >= 9:
        return False
    if "other" in state.exhausted and state.turn >= 2:
        return False
    if config.gate_before_override and state.scenario == "intent_override" and not state.override_applied:
        return True
    if config.hard_filter and hard_n == 0:
        return False
    working = hard_n if (config.hard_filter and hard_n > 0) else soft_n
    early_ok = not (config.strict_override_gate and state.scenario == "intent_override")
    if (
        early_ok
        and config.min_slots_to_recommend > 0
        and len(state.active) >= config.min_slots_to_recommend
        and 0 < working <= config.evidence_pool_cap
    ):
        return False
    if (
        early_ok
        and config.dump_slots > 0
        and len(state.active) >= config.dump_slots
        and 0 < working <= config.dump_pool_cap
    ):
        return False
    return working > config.gate_size


def defer_for_overlap(
    index: ContestIndex,
    state: ContestState,
    config: ContestConfig,
    working: list[int],
) -> bool:
    """True when an early recommend should wait: top-two popularity overlap.

    Translates D2D's top-overlapping-item test to this protocol.  Does not
    fire once the working pool is already at most gate_size, after ``other``
    is exhausted, on the last turns, or before an override can score.
    """

    if config.overlap_margin <= 0 or config.gate_size <= 0:
        return False
    if state.turn >= 9:
        return False
    if "other" in state.exhausted:
        return False
    if config.gate_before_override and state.scenario == "intent_override" and not state.override_applied:
        return False
    if len(working) <= config.gate_size or len(working) < 2:
        return False
    top = sorted(working, key=lambda idx: -index.popularity(idx))[:2]
    gap = index.popularity(top[0]) - index.popularity(top[1])
    return gap < config.overlap_margin
