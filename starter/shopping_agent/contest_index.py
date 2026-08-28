"""In-memory catalog: coarse-category buckets, blobs, popularity."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .contest_text import (
    catalog_field_lines,
    coarse_category,
    normalise,
    parse_price,
    searchable_blob,
    terms,
)


class ContestIndex:
    def __init__(self, catalog_path: str | Path) -> None:
        self.path = Path(catalog_path)
        self.ids: list[str] = []
        self.blobs: list[str] = []
        self.titles: list[str] = []
        self.categories: list[str] = []
        self.prices: list[float | None] = []
        self.ratings: list[float] = []
        self.rating_counts: list[int] = []
        self.token_sets: list[set[str]] = []
        self.field_lines: list[frozenset[str]] = []
        self.buckets: dict[str, list[int]] = {}
        self.bucket_lookup: dict[str, str] = {}
        self._load()
        self.id_set = set(self.ids)
        max_count = max(self.rating_counts) if self.rating_counts else 1
        self.log_max = math.log1p(max_count) or 1.0
        self._popular = sorted(range(len(self.ids)), key=lambda idx: -self.popularity(idx))

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                product = json.loads(line)
                idx = len(self.ids)
                parent = str(product.get("parent_asin", "")).strip()
                if not parent:
                    continue
                self.ids.append(parent)
                blob = searchable_blob(product)
                self.blobs.append(blob)
                self.titles.append(normalise(product.get("title") or ""))
                category = coarse_category(product.get("categories") or [])
                self.categories.append(category)
                self.prices.append(parse_price(product.get("price")))
                try:
                    self.ratings.append(float(product.get("average_rating") or product.get("rating") or 0.0))
                except (TypeError, ValueError):
                    self.ratings.append(0.0)
                try:
                    self.rating_counts.append(int(product.get("rating_number") or product.get("rating_count") or 0))
                except (TypeError, ValueError):
                    self.rating_counts.append(0)
                self.token_sets.append(set(terms(blob, limit=256)))
                self.field_lines.append(catalog_field_lines(product))
                self.buckets.setdefault(category, []).append(idx)
        self.bucket_lookup = {normalise(name): name for name in self.buckets}

    def __len__(self) -> int:
        return len(self.ids)

    def popularity(self, idx: int) -> float:
        count = math.log1p(max(self.rating_counts[idx], 0)) / self.log_max
        quality = max(min(self.ratings[idx], 5.0), 0.0) / 5.0
        return 0.65 * count + 0.35 * quality

    def bucket(self, category: str | None) -> list[int]:
        if not category:
            return []
        name = self.bucket_lookup.get(normalise(category), category)
        return list(self.buckets.get(name, ()))

    def popular(self, limit: int, *, exclude: set[int] | None = None) -> list[int]:
        skipped = exclude or set()
        output: list[int] = []
        for idx in self._popular:
            if idx in skipped:
                continue
            output.append(idx)
            if len(output) >= limit:
                break
        return output

    def lexical_hits(self, tokens: list[str], *, limit: int) -> list[int]:
        if not tokens:
            return self.popular(limit)
        unique = list(dict.fromkeys(tokens))[:16]
        scored: list[tuple[int, int]] = []
        for idx, bag in enumerate(self.token_sets):
            hits = sum(1 for token in unique if token in bag)
            if hits:
                scored.append((hits, idx))
        scored.sort(key=lambda item: (-item[0], -self.popularity(item[1]), self.ids[item[1]]))
        return [idx for _hits, idx in scored[:limit]]
