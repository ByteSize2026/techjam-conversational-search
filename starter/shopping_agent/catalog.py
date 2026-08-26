"""Catalog records and the bounded, in-process retrieval repository.

The competition catalog is immutable at runtime.  This module keeps the
catalog as the single source of valid product IDs and builds an SQLite FTS5
index for inexpensive lexical recall.  It intentionally has no dependency on
the evaluator (or on a model backend), so it is also useful in small fixture
tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any


# ``[^\W_]`` means a unicode word character other than underscore.  Splitting
# into plain tokens before constructing MATCH expressions prevents user text
# from injecting FTS operators, column selectors, or an unbalanced quote.
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "please",
    "some",
    "that",
    "the",
    "this",
    "to",
    "want",
    "with",
    "would",
    "you",
    "looking",
    "need",
    "prefer",
    "preference",
    "product",
}


def text_value(value: object) -> str:
    """Flatten a catalog JSON value into deterministic searchable text."""

    if value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(
            f"{key} {text_value(item)}"
            for key, item in value.items()
            if item not in (None, "", [])
        )
    if isinstance(value, (list, tuple, set)):
        return " ".join(text_value(item) for item in value if item not in (None, ""))
    return str(value)


def safe_terms(value: object, *, limit: int = 64) -> list[str]:
    """Return normalized FTS-safe tokens from arbitrary user/catalog text."""

    tokens: list[str] = []
    seen: set[str] = set()
    for raw in TOKEN_RE.findall(text_value(value).lower()):
        token = raw.strip()
        if len(token) < 2 or token in STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= max(int(limit), 1):
            break
    return tokens


def safe_match_expression(value: object, *, limit: int = 64) -> str:
    """Build a quoted OR expression safe to pass as an FTS5 MATCH value."""

    return " OR ".join(f'"{token}"' for token in safe_terms(value, limit=limit))


def _as_tuple(value: object) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, Mapping):
        return tuple(
            f"{key}: {text_value(item)}"
            for key, item in value.items()
            if item not in (None, "", [])
        )
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _as_details(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    details: dict[str, str] = {}
    for key, item in value.items():
        if item in (None, "", []):
            continue
        details[str(key)] = text_value(item).strip()
    return details


def _as_float(value: object) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        match = re.search(r"-?\d+(?:[,.]\d+)*", str(value))
        if not match:
            return None
        try:
            parsed = float(match.group(0).replace(",", ""))
        except ValueError:
            return None
    return parsed if math.isfinite(parsed) else None


def _as_int(value: object) -> int | None:
    parsed = _as_float(value)
    if parsed is None:
        return None
    return int(parsed) if math.isfinite(parsed) else None


@dataclass(frozen=True)
class ProductRecord:
    """Normalized immutable catalog row.

    ``parent_asin`` is deliberately the first field: it is the only value
    that may cross the final recommendation boundary.  ``compressed`` keeps
    model prompts bounded while retaining enough evidence for listwise
    semantic ranking.
    """

    parent_asin: str
    title: str = ""
    categories: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    description: tuple[str, ...] = ()
    details: dict[str, str] = field(default_factory=dict)
    store: str | None = None
    price: float | None = None
    rating: float | None = None
    rating_count: int | None = None
    canonical_text: str = ""

    def compressed(self, *, max_text: int = 900) -> dict[str, object]:
        """Return a small JSON-friendly representation for a ranker prompt."""

        detail_items = [f"{key}: {value}" for key, value in self.details.items()]
        evidence = " ".join(
            item
            for item in (
                *self.features[:6],
                *detail_items[:6],
                *self.description[:2],
            )
            if item
        )
        return {
            "parent_asin": self.parent_asin,
            "title": self.title[:320],
            "categories": list(self.categories[:6]),
            "price": self.price,
            "store": self.store,
            "evidence": evidence[:max(int(max_text), 100)],
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ProductRecord":
        parent_asin = str(raw.get("parent_asin", "")).strip()
        if not parent_asin:
            raise ValueError("catalog product is missing parent_asin")
        title = text_value(raw.get("title")).strip()
        categories = _as_tuple(raw.get("categories"))
        features = _as_tuple(raw.get("features"))
        description = _as_tuple(raw.get("description"))
        details = _as_details(raw.get("details"))
        store_value = text_value(raw.get("store")).strip()
        price = _as_float(raw.get("price"))
        rating = _as_float(raw.get("rating"))
        if rating is None:
            rating = _as_float(raw.get("average_rating"))
        rating_count = _as_int(raw.get("rating_count"))
        if rating_count is None:
            rating_count = _as_int(raw.get("rating_number"))
        if rating_count is None:
            # A few catalog exports keep this field in details under one of
            # several common spellings.
            for key, value in details.items():
                if "rating" in key.lower() and "count" in key.lower():
                    rating_count = _as_int(value)
                    if rating_count is not None:
                        break
        canonical = " ".join(
            part
            for part in (
                title,
                text_value(categories),
                text_value(features),
                text_value(details),
                store_value,
                text_value(description),
            )
            if part
        )
        return cls(
            parent_asin=parent_asin,
            title=title,
            categories=categories,
            features=features,
            description=description,
            details=details,
            store=store_value or None,
            price=price,
            rating=rating,
            rating_count=rating_count,
            canonical_text=canonical,
        )


@dataclass(frozen=True)
class RetrievedProduct:
    """A product plus retrieval provenance used by the deterministic ranker."""

    product: ProductRecord
    score: float = 0.0
    source: str = "keyword"
    rank: int = 0

    @property
    def parent_asin(self) -> str:
        return self.product.parent_asin

    def compressed(self) -> dict[str, object]:
        return self.product.compressed()


class CatalogRepository:
    """In-memory SQLite FTS5 repository with deterministic popular fallback."""

    def __init__(
        self,
        catalog_path: str | Path | None = "data/catalog.jsonl",
        *,
        records: Iterable[ProductRecord | Mapping[str, object]] | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path) if catalog_path is not None else None
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.products: dict[str, ProductRecord] = {}
        self._build_schema()
        if records is not None:
            self._load_records(records)
        elif self.catalog_path is not None and self.catalog_path.exists():
            self._load_jsonl(self.catalog_path)
        self.connection.commit()

    def _build_schema(self) -> None:
        self.connection.execute(
            "CREATE VIRTUAL TABLE products USING fts5("
            "parent_asin UNINDEXED, title, categories, features, details, "
            "store, description, canonical_text, "
            "tokenize='unicode61 remove_diacritics 2')"
        )

    def _load_jsonl(self, path: Path) -> None:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, Mapping):
                        continue
                    self._add_record(ProductRecord.from_mapping(raw))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    # A malformed row should not make the whole agent
                    # unusable.  Keep the failure diagnostic for callers that
                    # choose to expose it, while retaining valid rows.
                    self.load_warnings.append(f"line {line_number}: {exc}")

    def _load_records(self, records: Iterable[ProductRecord | Mapping[str, object]]) -> None:
        for raw in records:
            try:
                record = raw if isinstance(raw, ProductRecord) else ProductRecord.from_mapping(raw)
                self._add_record(record)
            except (ValueError, TypeError) as exc:
                self.load_warnings.append(str(exc))

    @property
    def load_warnings(self) -> list[str]:
        warnings = getattr(self, "_load_warnings", None)
        if warnings is None:
            warnings = []
            self._load_warnings = warnings
        return warnings

    def _add_record(self, record: ProductRecord) -> None:
        # The first occurrence wins, keeping duplicate source rows from
        # producing duplicate recommendation IDs.
        if record.parent_asin in self.products:
            return
        self.products[record.parent_asin] = record
        self.connection.execute(
            "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.parent_asin,
                record.title,
                text_value(record.categories),
                text_value(record.features),
                text_value(record.details),
                record.store or "",
                text_value(record.description),
                record.canonical_text,
            ),
        )

    @property
    def ids(self) -> set[str]:
        return set(self.products)

    @property
    def records(self) -> tuple[ProductRecord, ...]:
        return tuple(self.products.values())

    def get(self, parent_asin: object) -> ProductRecord | None:
        return self.products.get(str(parent_asin).strip())

    def __len__(self) -> int:
        return len(self.products)

    def _popularity_key(self, record: ProductRecord) -> tuple[float, float, str]:
        count = max(record.rating_count or 0, 0)
        rating = max(record.rating or 0.0, 0.0)
        # Stable ID tie-break makes fallback reproducible across processes.
        return (math.log1p(count), rating, record.parent_asin)

    def popular(self, limit: int = 10, *, exclude_ids: set[str] | None = None) -> list[ProductRecord]:
        excluded = exclude_ids or set()
        ordered = sorted(
            (record for record in self.products.values() if record.parent_asin not in excluded),
            key=self._popularity_key,
            reverse=True,
        )
        return ordered[: max(int(limit), 0)]

    def _row_product(self, row: sqlite3.Row) -> ProductRecord | None:
        return self.products.get(str(row["parent_asin"]).strip())

    def search_with_scores(
        self,
        query: object,
        limit: int = 100,
        *,
        source: str = "keyword",
    ) -> list[RetrievedProduct]:
        """Search safely and return BM25 scores/provenance.

        Empty or punctuation-only queries and zero-result queries deliberately
        use the same popularity fallback.  This ensures every turn can still
        return valid products when the user supplies little text.
        """

        bounded_limit = max(int(limit), 0)
        if bounded_limit <= 0:
            return []
        expression = safe_match_expression(query)
        if not expression:
            return [
                RetrievedProduct(record, 0.0, source, index)
                for index, record in enumerate(self.popular(bounded_limit), 1)
            ]
        try:
            rows = self.connection.execute(
                "SELECT rowid, parent_asin, bm25(products, 0.0, 7.0, 4.0, "
                "3.0, 2.0, 1.5, 1.0, 2.0) AS rank_score "
                "FROM products WHERE products MATCH ? "
                "ORDER BY rank_score ASC, rowid ASC LIMIT ?",
                (expression, bounded_limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 is present in the supported runtime, but a conservative
            # lexical fallback keeps fixture environments without it usable.
            rows = []
        results: list[RetrievedProduct] = []
        for index, row in enumerate(rows, 1):
            product = self._row_product(row)
            if product is None:
                continue
            raw_score = row["rank_score"]
            score = -float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
            results.append(RetrievedProduct(product, score, source, index))
        if results:
            return results
        return [
            # Keep the requested source on safe popularity fallbacks.  This
            # lets route-level diagnostics distinguish a query with no lexical
            # hit from an intentionally popularity-only retrieval.
            RetrievedProduct(record, 0.0, source, index)
            for index, record in enumerate(self.popular(bounded_limit), 1)
        ]

    def search(
        self,
        query: object,
        limit: int = 100,
        *,
        source: str = "keyword",
    ) -> list[ProductRecord]:
        return [item.product for item in self.search_with_scores(query, limit, source=source)]

    def category_records(self, category: object, limit: int = 100) -> list[ProductRecord]:
        """Find a category lexically, with popularity as a safe fallback."""

        return self.search(category, limit, source="category")

    def estimate_count(self, query: object) -> int:
        """Return a cheap candidate-count estimate for CandidateGate."""

        expression = safe_match_expression(query)
        if not expression:
            return len(self.products)
        try:
            row = self.connection.execute(
                "SELECT count(*) FROM products WHERE products MATCH ?",
                (expression,),
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        try:
            return max(int(row[0]), 0) if row is not None else 0
        except (TypeError, ValueError):
            return 0

    def materialize(self, ids: Sequence[object], limit: int = 10) -> list[ProductRecord]:
        """Materialize only valid, unique catalog IDs in input order."""

        output: list[ProductRecord] = []
        seen: set[str] = set()
        for value in ids:
            parent_asin = str(value).strip()
            if not parent_asin or parent_asin in seen:
                continue
            record = self.products.get(parent_asin)
            if record is None:
                continue
            seen.add(parent_asin)
            output.append(record)
            if len(output) >= max(int(limit), 0):
                break
        return output


__all__ = [
    "CatalogRepository",
    "ProductRecord",
    "RetrievedProduct",
    "safe_match_expression",
    "safe_terms",
    "text_value",
]
