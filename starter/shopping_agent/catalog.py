"""Catalog records and the bounded, in-process retrieval repository.

The competition catalog is immutable at runtime.  This module keeps the
catalog as the single source of valid product IDs and builds an SQLite FTS5
index for inexpensive lexical recall.  It intentionally has no dependency on
the evaluator (or on a model backend), so it is also useful in small fixture
tests.
"""

from __future__ import annotations

from collections import defaultdict
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


@dataclass(frozen=True)
class CategoryResolution:
    """A trustworthy category match for a bounded session anchor.

    The resolver deliberately returns the product IDs only to the retrieval
    layer.  Production diagnostics use the status and counts, never a hidden
    target ID.  ``ambiguous`` and ``unknown`` resolutions carry no IDs so a
    caller cannot accidentally turn an uncertain natural-language anchor into
    an unbounded category scan.
    """

    anchor: str
    status: str
    category: str | None = None
    normalized_category: str | None = None
    matched_alias: str | None = None
    path: tuple[str, ...] = ()
    product_ids: tuple[str, ...] = ()
    reason: str = "unknown_anchor"

    @property
    def resolved(self) -> bool:
        return self.status in {"resolved", "resolved_union"} and bool(self.product_ids)

    @property
    def category_size(self) -> int:
        return len(self.product_ids)

    def as_dict(self) -> dict[str, object]:
        """Return target-free metadata suitable for a diagnostics record."""

        return {
            "anchor": self.anchor,
            "status": self.status,
            "category": self.category,
            "normalized_category": self.normalized_category,
            "matched_alias": self.matched_alias,
            "path": list(self.path),
            "category_size": self.category_size,
            "reason": self.reason,
        }


@dataclass
class _CategoryGroup:
    """Internal path-aware category group used by ``CategoryResolution``."""

    labels: tuple[str, ...]
    label: str
    normalized_label: str
    path_key: tuple[str, ...]
    aliases: tuple[tuple[str, ...], ...]
    product_ids: set[str] = field(default_factory=set)


def normalize_category(value: object) -> str:
    """Normalize a category phrase without substring matching.

    Categories are compared as exact word tokens.  Thus ``men`` cannot match
    ``women`` and punctuation/order differences such as ``A & B`` versus
    ``B A`` are handled by the resolver's token-set comparison.
    """

    tokens = TOKEN_RE.findall(text_value(value).lower())
    return " ".join(dict.fromkeys(token for token in tokens if token))


def normalize_attribute_value(value: object) -> str:
    """Normalize a catalog value for exact, field-scoped validation.

    This intentionally shares the catalog's tokenization rules with category
    resolution, but does not apply substring matching.  A value is admitted
    to a structured slot only when its normalized form is present in that
    slot's catalog-derived index.
    """

    return normalize_category(value)


_DETAIL_ATTRIBUTE_ALIASES: dict[str, str] = {
    "brand": "brand",
    "label": "brand",
    "maker": "brand",
    "manufacturer": "brand",
    "color": "color",
    "colour": "color",
    "shade": "color",
    "material": "material",
    "fabric": "material",
    "composition": "material",
    "size": "size",
    "sizing": "size",
    "fit": "size",
    "style": "style",
    "pattern": "style",
    "design": "style",
    "closure": "style",
}

_COMMON_TITLE_ATTRIBUTE_VALUES: dict[str, frozenset[str]] = {
    "color": frozenset(
        {
            "black",
            "white",
            "blue",
            "red",
            "pink",
            "green",
            "brown",
            "gray",
            "grey",
            "purple",
            "yellow",
            "orange",
            "navy",
            "beige",
        }
    ),
    "material": frozenset(
        {
            "cotton",
            "polyester",
            "nylon",
            "leather",
            "wool",
            "spandex",
            "silk",
            "rayon",
            "fabric",
            "linen",
            "denim",
            "cashmere",
        }
    ),
}


def _detail_attribute(key: object) -> str | None:
    normalized = normalize_attribute_value(key)
    if not normalized:
        return None
    if normalized in _DETAIL_ATTRIBUTE_ALIASES:
        return _DETAIL_ATTRIBUTE_ALIASES[normalized]
    # Multi-word detail labels are common in Amazon exports.  Only map a
    # label when the semantic word is unambiguous; arbitrary detail keys stay
    # out of the strict state-machine slots and remain searchable text.
    tokens = set(normalized.split())
    for token, attribute in _DETAIL_ATTRIBUTE_ALIASES.items():
        if token in tokens:
            return attribute
    return None


def _constraint_value(constraint: object) -> tuple[str, str]:
    """Read a hard-constraint-like object without importing session state."""

    if isinstance(constraint, Mapping):
        value = constraint.get("value", "")
        polarity = constraint.get("polarity", "prefer")
    else:
        value = getattr(constraint, "value", constraint)
        polarity = getattr(constraint, "polarity", "prefer")
    return str(value or ""), str(polarity or "prefer").lower()


def category_relevance(
    record: ProductRecord,
    *,
    constraints: Sequence[object] = (),
    query_evidence: Sequence[object] = (),
) -> tuple[float, float, float]:
    """Score category members before applying a bounded category budget.

    The first component captures required/avoided constraint evidence, the
    second captures deterministic conversational evidence, and the final
    component is intentionally tiny so catalog popularity only breaks a
    complete relevance tie.
    """

    text = record.canonical_text.lower()
    constraint_score = 0.0
    for constraint in constraints or ():
        if not isinstance(constraint, (str, bytes)):
            hardness = getattr(constraint, "hardness", None)
            polarity = getattr(constraint, "polarity", None)
            if isinstance(constraint, Mapping):
                hardness = constraint.get("hardness", hardness)
                polarity = constraint.get("polarity", polarity)
            if str(hardness or "hard").lower() != "hard" and str(polarity or "prefer").lower() != "avoid":
                continue
        value, polarity = _constraint_value(constraint)
        terms = safe_terms(value)
        if not terms:
            continue
        match_ratio = sum(term in text for term in terms) / len(terms)
        constraint_score += -match_ratio if polarity == "avoid" else match_ratio

    evidence_terms: set[str] = set()
    for evidence in query_evidence or ():
        evidence_terms.update(safe_terms(evidence))
    evidence_score = (
        sum(term in text for term in evidence_terms) / len(evidence_terms)
        if evidence_terms
        else 0.0
    )
    popularity = math.log1p(max(record.rating_count or 0, 0)) + 0.01 * max(record.rating or 0.0, 0.0)
    return constraint_score, evidence_score, popularity


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
        self._coarse_anchor_index: dict[str, tuple[str, ...]] = {}
        self._category_groups: tuple[_CategoryGroup, ...] = ()
        self._category_label_index: dict[str, tuple[str, ...]] = {}
        self._category_counts: dict[str, int] = {}
        self._attribute_value_index: dict[str, dict[str, tuple[str, ...]]] = {}
        self._build_schema()
        if records is not None:
            self._load_records(records)
        elif self.catalog_path is not None and self.catalog_path.exists():
            self._load_jsonl(self.catalog_path)
        self._build_category_index()
        self._build_attribute_value_index()
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

    def _build_category_index(self) -> None:
        """Build deterministic, path-aware category groups after catalog load.

        A label may occur in multiple branches (for example ``Shoes`` under
        both ``Men`` and ``Women``).  Keeping the complete path as the group
        key lets ``resolve_category`` reject such an anchor as ambiguous,
        while labels that occur in one branch can still be resolved directly.
        Suffix aliases make combined anchors such as ``Accessories Wallets``
        resolve to the leaf group without requiring the user to reproduce the
        catalog's full root path.
        """

        groups: dict[tuple[str, ...], _CategoryGroup] = {}
        coarse_alias_ids: dict[str, set[str]] = defaultdict(set)
        coarse_excluded = {
            "clothing",
            "clothing shoes & jewelry",
            "clothing, shoes & jewelry",
        }
        ordered_records = sorted(self.products.values(), key=lambda item: item.parent_asin)
        for record in ordered_records:
            labels = tuple(str(value).strip() for value in record.categories if str(value).strip())
            if not labels:
                continue

            # Match the public protocol's coarse category construction: split
            # each label on commas, remove generic roots, and keep the final
            # two fragments.  A coarse alias may cover several branches, so
            # retain the complete union of IDs already present in the catalog.
            coarse_fragments: list[str] = []
            for value in labels:
                for fragment in value.split(","):
                    fragment = fragment.strip()
                    if fragment and fragment.lower() not in coarse_excluded:
                        coarse_fragments.append(fragment)
            if coarse_fragments:
                coarse_alias = normalize_category(" ".join(coarse_fragments[-2:]))
                if coarse_alias:
                    coarse_alias_ids[coarse_alias].add(record.parent_asin)

            normalized_labels = tuple(normalize_category(value) for value in labels)
            for depth, label in enumerate(labels):
                normalized_label = normalized_labels[depth]
                if not normalized_label:
                    continue
                path_key = normalized_labels[: depth + 1]
                group = groups.get(path_key)
                if group is None:
                    aliases: list[tuple[str, ...]] = []
                    # All suffixes are bounded by the catalog row's category
                    # depth and remain cheap for the released data.
                    for start in range(depth + 1):
                        alias_tokens = tuple(
                            token
                            for part in normalized_labels[start : depth + 1]
                            for token in part.split()
                            if token
                        )
                        if alias_tokens and alias_tokens not in aliases:
                            aliases.append(alias_tokens)
                    group = _CategoryGroup(
                        labels=labels[: depth + 1],
                        label=label,
                        normalized_label=normalized_label,
                        path_key=path_key,
                        aliases=tuple(aliases),
                    )
                    groups[path_key] = group
                group.product_ids.add(record.parent_asin)

        self._category_groups = tuple(
            sorted(
                groups.values(),
                key=lambda group: (group.normalized_label, len(group.path_key), group.path_key),
            )
        )
        self._coarse_anchor_index = {
            alias: tuple(sorted(parent_asins))
            for alias, parent_asins in sorted(coarse_alias_ids.items())
        }
        label_ids: dict[str, set[str]] = defaultdict(set)
        for group in self._category_groups:
            label_ids[group.normalized_label].update(group.product_ids)
        self._category_label_index = {
            label: tuple(sorted(values)) for label, values in sorted(label_ids.items())
        }
        self._category_counts = {
            label: len(values) for label, values in self._category_label_index.items()
        }

    def _build_attribute_value_index(self) -> None:
        """Build field-scoped value -> product indexes from loaded records.

        FTS remains the broad lexical index.  This second index is deliberately
        narrower: it is a catalog-grounded admission check for values that
        are about to become structured state.  Title tokens are kept in their
        own slot so a token such as ``mojo`` cannot silently become a generic
        ``feature`` constraint.
        """

        buckets: dict[str, defaultdict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )

        def add(attribute: str, value: object, parent_asin: str) -> None:
            normalized = normalize_attribute_value(value)
            add_normalized(attribute, normalized, parent_asin)

        def add_normalized(
            attribute: str, normalized: str, parent_asin: str
        ) -> None:
            if normalized:
                buckets[attribute][normalized].add(parent_asin)

        for record in self.products.values():
            for category in record.categories:
                add("category", category, record.parent_asin)
            if record.store:
                add("brand", record.store, record.parent_asin)
            for key, value in record.details.items():
                attribute = _detail_attribute(key)
                if attribute is not None:
                    add(attribute, value, record.parent_asin)
                else:
                    # Short, human-facing detail values (for example
                    # ``Special Feature: Adjustable``) are valid feature
                    # evidence even when the export uses an unfamiliar key.
                    # Keep long metadata sentences and identifiers lexical-only
                    # so catalog admission remains bounded and field-scoped.
                    detail_tokens = normalized = normalize_attribute_value(value)
                    if detail_tokens and len(detail_tokens.split()) <= 4 and len(detail_tokens) <= 80:
                        add_normalized("feature", detail_tokens, record.parent_asin)
                        for token in detail_tokens.split():
                            if len(token) >= 2 and token not in STOPWORDS:
                                add_normalized("feature", token, record.parent_asin)
            for feature in record.features:
                normalized_feature = normalize_attribute_value(feature)
                feature_tokens = normalized_feature.split()
                # Concise catalog feature phrases are valid slot values.  Long
                # marketing sentences remain searchable text; admitting them
                # as state-machine labels would recreate the old catch-all.
                if len(feature_tokens) <= 4:
                    add_normalized("feature", normalized_feature, record.parent_asin)
                # Reuse the already-tokenized normalized feature instead of
                # running the Unicode tokenizer a second time for every one
                # of the catalog's feature sentences.
                for token in feature_tokens:
                    if len(token) >= 2 and token not in STOPWORDS:
                        add_normalized("feature", token, record.parent_asin)
            for token in safe_terms(record.title):
                add("title_token", token, record.parent_asin)
                for attribute, values in _COMMON_TITLE_ATTRIBUTE_VALUES.items():
                    if token in values:
                        add_normalized(attribute, token, record.parent_asin)

        self._attribute_value_index = {
            attribute: {
                value: tuple(sorted(parent_asins))
                for value, parent_asins in values.items()
            }
            for attribute, values in buckets.items()
        }

    @property
    def ids(self) -> set[str]:
        return set(self.products)

    @property
    def records(self) -> tuple[ProductRecord, ...]:
        return tuple(self.products.values())

    @property
    def category_index(self) -> dict[str, tuple[str, ...]]:
        """Return normalized label -> sorted product IDs for diagnostics/tests."""

        return dict(self._category_label_index)

    @property
    def category_counts(self) -> dict[str, int]:
        """Return normalized category sizes without exposing mutable indexes."""

        return dict(self._category_counts)

    @property
    def attribute_value_index(self) -> dict[str, dict[str, tuple[str, ...]]]:
        """Return a defensive copy of strict catalog value indexes."""

        return {
            attribute: dict(values)
            for attribute, values in self._attribute_value_index.items()
        }

    def attribute_values(self, attribute: object) -> tuple[str, ...]:
        """Return normalized values observed for one structured attribute."""

        key = str(attribute or "").strip().lower()
        return tuple(sorted(self._attribute_value_index.get(key, {})))

    def resolve_attribute_value(
        self,
        attribute: object,
        value: object,
        *,
        candidate_ids: Iterable[str] | None = None,
    ) -> tuple[str, tuple[str, ...]] | None:
        """Resolve a value against a catalog-derived, field-scoped index.

        The returned value is ``(normalized_value, matching_ids)``.  When a
        candidate subset is supplied, the value must occur in that subset as
        well; this lets callers prefer category-local enumerations without
        changing the global catalog ground truth.
        """

        key = str(attribute or "").strip().lower()
        normalized = normalize_attribute_value(value)
        if not key or not normalized:
            return None
        values = self._attribute_value_index.get(key, {})
        ids = values.get(normalized)
        if not ids:
            return None
        if candidate_ids is not None:
            allowed = {str(item).strip() for item in candidate_ids if str(item).strip()}
            filtered = tuple(parent_asin for parent_asin in ids if parent_asin in allowed)
            if not filtered:
                return None
            ids = filtered
        return normalized, tuple(ids)

    def resolve_category(self, anchor: object) -> CategoryResolution:
        """Resolve the most specific unambiguous catalog category.

        Matching is token based and bounded to the precomputed category index.
        An exact alias wins over a strict subset; ties with different product
        sets are intentionally reported as ``ambiguous`` rather than guessed.
        """

        raw_anchor = text_value(anchor).strip()
        anchor_tokens = tuple(dict.fromkeys(TOKEN_RE.findall(raw_anchor.lower())))
        if not anchor_tokens:
            return CategoryResolution(
                anchor=raw_anchor,
                status="unknown",
                reason="empty_anchor",
            )
        coarse_alias = normalize_category(raw_anchor)
        coarse_product_ids = self._coarse_anchor_index.get(coarse_alias)
        if coarse_product_ids:
            return CategoryResolution(
                anchor=raw_anchor,
                status="resolved_union",
                category=raw_anchor,
                normalized_category=coarse_alias,
                matched_alias=coarse_alias,
                product_ids=coarse_product_ids,
                reason="coarse_anchor_union",
            )
        anchor_set = frozenset(anchor_tokens)

        # Keep only the strongest alias for each path group.  A group may have
        # both a leaf label and a longer suffix alias that match the anchor.
        matches: list[tuple[tuple[int, int, int], _CategoryGroup, tuple[str, ...]]] = []
        for group in self._category_groups:
            best: tuple[tuple[int, int, int], tuple[str, ...]] | None = None
            label_tokens = tuple(group.normalized_label.split())
            # When the user names a catalog label directly, do not let a
            # repeated parent token (e.g. ``Shirts -> T-Shirts``) outrank
            # other branches carrying the same leaf label.  Direct leaf-label
            # matches are unioned below; longer suffix aliases are reserved
            # for genuinely combined anchors.
            direct_label = tuple(dict.fromkeys(label_tokens)) == anchor_tokens
            for alias in group.aliases:
                alias_set = frozenset(alias)
                if not alias_set:
                    continue
                if direct_label and alias != label_tokens:
                    continue
                if alias_set == anchor_set:
                    strength = 2
                elif alias_set < anchor_set:
                    strength = 1
                else:
                    continue
                # Path depth is deliberately not part of the match strength:
                # the same leaf label can appear at different depths, and all
                # branches sharing that strongest alias should be unioned.
                # Keep repeated words in the alias length.  A category path
                # such as ``Wallets, Card Cases & Money Organizers ->
                # Wallets`` contains ``wallets`` twice; retaining that signal
                # lets the combined anchor select the leaf path rather than
                # collapsing back to its parent label.
                rank = (strength, len(alias), 0)
                if best is None or rank > best[0]:
                    best = (rank, alias)
            if best is not None:
                matches.append((best[0], group, best[1]))

        if not matches:
            return CategoryResolution(
                anchor=raw_anchor,
                status="unknown",
                reason="no_category_alias_match",
            )

        best_rank = max(match[0] for match in matches)
        best_matches = [match for match in matches if match[0] == best_rank]
        distinct_aliases = {frozenset(match[2]) for match in best_matches}
        if len(distinct_aliases) > 1:
            return CategoryResolution(
                anchor=raw_anchor,
                status="ambiguous",
                reason="incomparable_category_matches",
            )

        distinct_sets = {frozenset(match[1].product_ids) for match in best_matches}

        # The same product set can be represented by multiple equivalent path
        # groups.  Pick the deepest/lexically stable one for diagnostics.
        _, group, alias = sorted(
            best_matches,
            key=lambda match: (
                -len(match[1].path_key),
                match[1].path_key,
                match[1].normalized_label,
            ),
        )[0]
        if len(distinct_sets) == 1:
            product_ids = tuple(sorted(group.product_ids))
            status = "resolved"
            reason = "exact_or_specific_alias"
        else:
            # Repeated labels such as ``Shoes`` can legitimately occur under
            # several catalog branches.  Once the strongest alias is the
            # same normalized phrase, union those branch members instead of
            # silently selecting one and losing recall.  Ambiguous *different*
            # aliases were rejected above.
            union_ids: set[str] = set()
            for _, candidate_group, _ in best_matches:
                union_ids.update(candidate_group.product_ids)
            product_ids = tuple(sorted(union_ids))
            status = "resolved_union"
            reason = "shared_alias_union"
        return CategoryResolution(
            anchor=raw_anchor,
            status=status,
            category=group.label,
            normalized_category=group.normalized_label,
            matched_alias=" ".join(alias),
            path=group.labels,
            product_ids=product_ids,
            reason=reason,
        )

    def category_with_scores(
        self,
        anchor: object,
        limit: int = 100,
        *,
        hard_constraints: Sequence[object] = (),
        query_evidence: Sequence[object] = (),
        resolution: CategoryResolution | None = None,
    ) -> tuple[CategoryResolution, list[RetrievedProduct]]:
        """Return a resolved category route in deterministic relevance order.

        Every returned record is a member of the resolved category.  Supplied
        hard-constraint values are matched before popularity is used, so a
        relevant low-rating product is not discarded by an earlier popularity
        truncation; unrelated products cannot enter this route.
        """

        resolution = resolution or self.resolve_category(anchor)
        bounded_limit = max(int(limit), 0)
        if not resolution.resolved or bounded_limit <= 0:
            return resolution, []
        records = [
            self.products[parent_asin]
            for parent_asin in resolution.product_ids
            if parent_asin in self.products
        ]
        # Constraint and conversational evidence determine the category-route
        # prefix before popularity is consulted.  This keeps a low-rating but
        # semantically matching product in the allocated quota instead of
        # losing it during an earlier popularity truncation.
        records.sort(
            key=lambda record: (
                -category_relevance(
                    record,
                    constraints=hard_constraints,
                    query_evidence=query_evidence,
                )[0],
                -category_relevance(
                    record,
                    constraints=hard_constraints,
                    query_evidence=query_evidence,
                )[1],
                -category_relevance(
                    record,
                    constraints=hard_constraints,
                    query_evidence=query_evidence,
                )[2],
                record.parent_asin,
            )
        )
        output: list[RetrievedProduct] = []
        for rank, record in enumerate(records[:bounded_limit], 1):
            # The route's order is category-constrained first; this small
            # monotonically decreasing score only makes the order stable when
            # later route scores are merged.
            score = 100.0 / (60.0 + rank)
            output.append(RetrievedProduct(record, score, "category:exact", rank))
        return resolution, output

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

    def search_column_with_scores(
        self,
        query: object,
        column: str = "title",
        limit: int = 100,
        *,
        source: str | None = None,
    ) -> list[RetrievedProduct]:
        """FTS5 column MATCH without popularity fallback.

        Extra recall routes should return empty rather than injecting popular
        items that never matched the query.  Unknown column names fall back
        to title.
        """

        bounded_limit = max(int(limit), 0)
        if bounded_limit <= 0:
            return []
        allowed = {
            "title",
            "categories",
            "features",
            "details",
            "store",
            "description",
            "canonical_text",
        }
        if column not in allowed:
            column = "title"
        terms = safe_terms(query)
        if not terms:
            return []
        expression = " OR ".join(f'{column}:"{token}"' for token in terms)
        labeled = source or column
        try:
            rows = self.connection.execute(
                "SELECT rowid, parent_asin, bm25(products) AS rank_score "
                "FROM products WHERE products MATCH ? "
                "ORDER BY rank_score ASC, rowid ASC LIMIT ?",
                (expression, bounded_limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        results: list[RetrievedProduct] = []
        for index, row in enumerate(rows, 1):
            product = self._row_product(row)
            if product is None:
                continue
            raw_score = row["rank_score"]
            score = -float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
            results.append(RetrievedProduct(product, score, labeled, index))
        return results

    def search(
        self,
        query: object,
        limit: int = 100,
        *,
        source: str = "keyword",
    ) -> list[ProductRecord]:
        return [item.product for item in self.search_with_scores(query, limit, source=source)]

    def category_records(self, category: object, limit: int = 100) -> list[ProductRecord]:
        """Find a category exactly, with lexical fallback when unresolved."""

        resolution, found = self.category_with_scores(category, limit)
        if resolution.resolved:
            return [item.product for item in found]
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
    "CategoryResolution",
    "CatalogRepository",
    "ProductRecord",
    "RetrievedProduct",
    "normalize_attribute_value",
    "normalize_category",
    "safe_match_expression",
    "safe_terms",
    "text_value",
]
