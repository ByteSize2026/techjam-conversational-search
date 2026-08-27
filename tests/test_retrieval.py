from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from starter.shopping_agent.catalog import CatalogRepository


def _write_catalog(root: Path) -> Path:
    rows = [
        {
            "parent_asin": "A",
            "title": "Blue cotton running shirt",
            "features": ["comfortable", "durable"],
            "description": ["lightweight walking top"],
            "categories": ["Clothing", "Shirts"],
            "details": {"department": "womens"},
            "store": "Alpha",
            "price": 29.0,
            "average_rating": 4.8,
            "rating_number": 100,
        },
        {
            "parent_asin": "B",
            "title": "Black leather winter boot",
            "features": ["warm", "waterproof"],
            "description": ["outdoor hiking boot"],
            "categories": ["Clothing", "Boots"],
            "details": {"department": "mens"},
            "store": "Beta",
            "price": 89.0,
            "average_rating": 4.5,
            "rating_number": 80,
        },
        {
            "parent_asin": "C",
            "title": "White polyester casual jacket",
            "features": ["hood", "pockets"],
            "description": ["comfortable outdoor layer"],
            "categories": ["Clothing", "Jackets"],
            "details": {"department": "unisex"},
            "store": "Gamma",
            "price": 59.0,
            "average_rating": 4.2,
            "rating_number": 60,
        },
    ]
    path = root / "catalog.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.catalog_path = _write_catalog(Path(self.tempdir.name))
        self.repository = CatalogRepository(self.catalog_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_search_with_scores_stays_inside_catalog(self) -> None:
        hits = self.repository.search_with_scores("shirt", 3, source="a")
        ids = [item.parent_asin for item in hits]
        self.assertEqual(ids, list(dict.fromkeys(ids)))
        self.assertTrue(set(ids) <= {"A", "B", "C"})
        self.assertTrue(hits)

    def test_title_column_search_does_not_fall_back_to_popularity(self) -> None:
        hits = self.repository.search_column_with_scores("xyzzy-no-such-token", "title", 10, source="title")
        self.assertEqual(hits, [])
        shirts = self.repository.search_column_with_scores("running shirt", "title", 10, source="title")
        self.assertTrue(shirts)
        self.assertEqual(shirts[0].parent_asin, "A")
        self.assertEqual(shirts[0].source, "title")
