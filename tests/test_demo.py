from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from demo.run_demo import pick_sample, run_session
from starter.agent import Agent


class DemoReplayTests(unittest.TestCase):
    def test_pick_sample_uses_stable_scenario_defaults(self) -> None:
        samples = [
            {"sample_id": "public_0001", "scenario_type": "buying"},
            {"sample_id": "public_0002", "scenario_type": "intent_override"},
            {"sample_id": "public_0006", "scenario_type": "browsing"},
            {"sample_id": "public_0035", "scenario_type": "boundary"},
        ]
        self.assertEqual(pick_sample(samples, "public_0002", None)["sample_id"], "public_0002")
        self.assertEqual(pick_sample(samples, None, "browsing")["sample_id"], "public_0006")
        self.assertEqual(pick_sample(samples, None, "intent_override")["sample_id"], "public_0002")

    def test_run_session_hits_buying_target_and_reports_zero_tokens(self) -> None:
        rows = [
            {
                "parent_asin": "TARGET",
                "title": "Blue cotton running shirt",
                "features": ["cotton", "machine wash"],
                "description": ["lightweight"],
                "categories": ["Clothing", "Shirts"],
                "details": {"department": "womens"},
                "store": "Alpha",
                "price": 29.0,
                "average_rating": 4.8,
                "rating_number": 100,
            },
            {
                "parent_asin": "OTHER",
                "title": "Black leather boot",
                "features": ["leather"],
                "description": ["boot"],
                "categories": ["Clothing", "Boots"],
                "details": {"department": "mens"},
                "store": "Beta",
                "price": 89.0,
                "average_rating": 4.0,
                "rating_number": 10,
            },
        ]
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        path = Path(tempdir.name) / "catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        sample = {
            "sample_id": "demo_buy",
            "scenario_type": "buying",
            "user_profile": {
                "purchase_frequency": "3-4 prior purchases",
                "average_prior_rating": 5.0,
                "rating_style": "usually positive",
                "preference_tags": ["comfort"],
                "summary": "Prior purchases emphasize comfort.",
            },
            "ground_truth": {"parent_asin": "TARGET"},
        }
        products = {row["parent_asin"]: row for row in rows}
        categories = {row["parent_asin"]: row["categories"] for row in rows}
        agent = Agent(path)
        outcome = run_session(agent, sample, products, categories)
        self.assertTrue(outcome["hit"])
        self.assertEqual(outcome["tokens"], 0)
        self.assertEqual(outcome["rank"], 1)
        self.assertGreaterEqual(int(outcome["turn"] or 0), 1)
