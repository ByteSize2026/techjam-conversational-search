from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evaluator.local_evaluator import catalog_index, evaluate, materialize_hidden_fields
from starter.shopping_agent.holdout import build_holdout, public_asins, write_jsonl


class EchoTargetAgent:
    def reset(self, session_id: str, user_profile: dict) -> None:
        self.session_id = session_id

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        return {
            "message": "ok",
            "ask_attribute": "other",
            "recommendations": [{"parent_asin": "P00"}],
        }


class HoldoutBuilderTests(unittest.TestCase):
    def test_builder_mix_disjoint_catalog_and_evaluate_without_intent_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_rows = []
            for idx in range(22):
                catalog_rows.append(
                    {
                        "parent_asin": f"P{idx:02d}",
                        "title": f"Cotton tee {idx}",
                        "features": ["cotton", "machine wash"],
                        "details": {"department": "unisex"},
                        "description": ["everyday tee"],
                        "categories": ["Clothing", "Shirts"],
                        "store": "Example",
                        "average_rating": 4.0,
                        "rating_number": 10 + idx,
                        "price": 20.0 + idx,
                    }
                )
            catalog_path = root / "catalog.jsonl"
            catalog_path.write_text("".join(json.dumps(row) + "\n" for row in catalog_rows), encoding="utf-8")
            fake_public = [
                {"sample_id": "public_x", "scenario_type": "buying", "ground_truth": {"parent_asin": "P00"}},
                {"sample_id": "public_y", "scenario_type": "browsing", "ground_truth": {"parent_asin": "P01"}},
            ]
            products = catalog_rows
            mix = {"buying": 8, "browsing": 8, "intent_override": 3, "boundary": 1}
            rows = build_holdout(
                products,
                exclude=public_asins(fake_public),
                mix=mix,
                seed=7,
                sample_prefix="holdout",
            )
            self.assertEqual(len(rows), 20)
            counts = {name: 0 for name in mix}
            asins = []
            for row in rows:
                self.assertIn("sample_id", row)
                self.assertIn("scenario_type", row)
                self.assertIn("user_profile", row)
                self.assertNotIn("intent_card", row)
                self.assertNotIn("behavior", row)
                asin = row["ground_truth"]["parent_asin"]
                asins.append(asin)
                counts[row["scenario_type"]] += 1
            self.assertEqual(counts, mix)
            self.assertEqual(len(asins), len(set(asins)))
            catalog_ids = {row["parent_asin"] for row in catalog_rows}
            self.assertTrue(set(asins) <= catalog_ids)
            self.assertTrue(set(asins).isdisjoint(public_asins(fake_public)))

            holdout_path = write_jsonl(root / "holdout.jsonl", rows)
            loaded = [json.loads(line) for line in holdout_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(loaded), 20)

            catalog_ids, categories, product_map = catalog_index(catalog_path)
            card, behavior = materialize_hidden_fields(rows[0], product_map)
            self.assertIn("hard_constraints", card)
            self.assertIn("scenario_type", behavior)
            result = evaluate(EchoTargetAgent(), rows, catalog_ids, categories, product_map)
            self.assertEqual(result["sample_count"], 20)
            self.assertIn("hit_rate_at_10", result)
            self.assertIn("mrr", result)
            self.assertIn("mttc", result)

    def test_builder_scaled_mix_32_32_12_4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_rows = []
            for idx in range(90):
                catalog_rows.append(
                    {
                        "parent_asin": f"Q{idx:03d}",
                        "title": f"Item {idx}",
                        "features": ["cotton"],
                        "details": {"department": "unisex"},
                        "description": ["x"],
                        "categories": ["Clothing", "Shirts"],
                        "store": "S",
                        "average_rating": 4.0,
                        "rating_number": idx,
                        "price": 10.0,
                    }
                )
            fake_public = [
                {"ground_truth": {"parent_asin": "Q000"}},
                {"ground_truth": {"parent_asin": "Q001"}},
            ]
            # Parameterized 1/10 of the random-800 mix 320/320/120/40.
            mix = {"buying": 32, "browsing": 32, "intent_override": 12, "boundary": 4}
            rows = build_holdout(
                catalog_rows,
                exclude=public_asins(fake_public),
                mix=mix,
                seed=3,
                sample_prefix="r800",
            )
            self.assertEqual(len(rows), 80)
            counts = {name: 0 for name in mix}
            asins = [row["ground_truth"]["parent_asin"] for row in rows]
            for row in rows:
                counts[row["scenario_type"]] += 1
                self.assertNotIn("intent_card", row)
            self.assertEqual(counts, mix)
            self.assertEqual(len(set(asins)), 80)
            self.assertTrue(set(asins).isdisjoint(public_asins(fake_public)))
            catalog_ids, categories, product_map = catalog_index(_write(root, catalog_rows))
            result = evaluate(EchoTargetAgent(), rows, catalog_ids, categories, product_map)
            self.assertEqual(result["sample_count"], 80)


def _write(root: Path, rows: list[dict]) -> Path:
    path = root / "catalog.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path
