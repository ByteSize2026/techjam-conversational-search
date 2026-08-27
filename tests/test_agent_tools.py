from __future__ import annotations

import unittest
from typing import cast

from starter.shopping_agent.actions import (
    ActionValidationError,
    PlannedAction,
    parse_planned_action,
)
from starter.shopping_agent.catalog import CatalogRepository
from starter.shopping_agent.state import SessionStore
from starter.shopping_agent.tools import ShoppingToolbox

PRODUCTS = [
    {
        "parent_asin": "A",
        "title": "Blue cotton running shirt",
        "features": ["breathable", "machine washable"],
        "description": ["A lightweight running top"],
        "categories": ["Clothing", "Shirts"],
        "details": {"material": "100% cotton", "size": "medium"},
        "store": "Alpha",
        "price": 29.0,
        "average_rating": 4.8,
        "rating_number": 100,
    },
    {
        "parent_asin": "B",
        "title": "Black leather hiking boot",
        "features": ["waterproof"],
        "categories": ["Clothing", "Boots"],
        "details": {"material": "leather", "size": "large"},
        "store": "Beta",
        "price": 89.0,
        "average_rating": 4.5,
        "rating_number": 80,
    },
    {
        "parent_asin": "C",
        "title": "Mystery accessory",
        "categories": ["Accessories"],
        "price": None,
    },
]


class AgentActionContractTests(unittest.TestCase):
    def test_parse_accepts_closed_action_and_rejects_unknown(self) -> None:
        action = parse_planned_action(
            {"name": "search_products", "arguments": {"query": "cotton"}, "rationale": "find candidates"}
        )
        self.assertEqual(action.name, "search_products")
        self.assertEqual(action.arguments["query"], "cotton")
        with self.assertRaises(ActionValidationError):
            parse_planned_action({"name": "search_reviews", "arguments": {}})
        with self.assertRaises(ActionValidationError):
            parse_planned_action({"name": "search_products", "arguments": []})


class ShoppingToolboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = CatalogRepository(catalog_path=None, records=PRODUCTS)
        self.profile = {
            "preference_tags": ["comfort"],
            "summary": "prefers simple clothing",
            "private_extra": "must not be exposed",
        }
        self.state = SessionStore().reset("s", self.profile)
        self.toolbox = ShoppingToolbox(self.repository, self.state, top_k=2)

    def test_search_detail_and_observed_recommendation(self) -> None:
        search = self.toolbox.execute(
            PlannedAction("search_products", {"query": "cotton running", "limit": 2})
        )
        self.assertTrue(search.ok)
        products = cast(list[dict[str, object]], search.payload["products"])
        ids = [item["parent_asin"] for item in products]
        self.assertEqual(ids[0], "A")
        self.assertTrue(set(ids) <= self.repository.ids)

        detail = self.toolbox.execute(
            PlannedAction("get_product_details", {"parent_asin": "A"})
        )
        self.assertTrue(detail.ok)
        details = cast(dict[str, object], detail.payload["details"])
        self.assertEqual(details["material"], "100% cotton")

        recommend = self.toolbox.execute(
            PlannedAction(
                "recommend_products",
                {"parent_asins": ["UNKNOWN", "A", "A"], "message": "Best match"},
            )
        )
        self.assertTrue(recommend.ok)
        self.assertEqual(recommend.payload["parent_asins"], ["A"])
        self.assertEqual(
            recommend.payload["message"],
            "Here are the closest catalog matches I found.",
        )

    def test_valid_but_unobserved_ids_cannot_be_probed_or_recommended(self) -> None:
        detail = self.toolbox.execute(
            PlannedAction("get_product_details", {"parent_asin": "B"})
        )
        self.assertFalse(detail.ok)
        self.assertNotIn("unknown", (detail.error or "").lower())

        filtered = self.toolbox.execute(
            PlannedAction(
                "filter_products",
                {
                    "product_ids": ["B"],
                    "attribute": "material",
                    "operator": "contains",
                    "value": "leather",
                },
            )
        )
        self.assertFalse(filtered.ok)

        recommendation = self.toolbox.execute(
            PlannedAction("recommend_products", {"parent_asins": ["B"]})
        )
        self.assertFalse(recommendation.ok)
        self.assertEqual(self.state.tool_candidate_ids, [])

    def test_planner_message_cannot_add_uncatalogued_product_claims(self) -> None:
        self.state.tool_candidate_ids = ["A"]
        result = self.toolbox.execute(
            PlannedAction(
                "recommend_products",
                {
                    "parent_asins": ["A"],
                    "message": "A has a lifetime warranty and magical powers.",
                },
            )
        )
        self.assertTrue(result.ok)
        self.assertNotIn("warranty", str(result.payload["message"]))

    def test_filter_reports_missing_numeric_values_as_unknown(self) -> None:
        self.state.tool_candidate_ids = ["A", "B", "C"]
        result = self.toolbox.execute(
            PlannedAction(
                "filter_products",
                {
                    "product_ids": ["A", "B", "C"],
                    "attribute": "price",
                    "operator": "lte",
                    "value": 50,
                },
            )
        )
        self.assertTrue(result.ok)
        products = cast(list[dict[str, object]], result.payload["products"])
        self.assertEqual(
            [item["parent_asin"] for item in products], ["A"]
        )
        self.assertEqual(result.payload["unknown_ids"], ["C"])

    def test_negative_text_filter_uses_catalog_evidence(self) -> None:
        self.state.tool_candidate_ids = ["A", "B"]
        result = self.toolbox.execute(
            PlannedAction(
                "filter_products",
                {
                    "product_ids": ["A", "B"],
                    "attribute": "feature",
                    "operator": "not_contains",
                    "value": "waterproof",
                },
            )
        )
        self.assertTrue(result.ok)
        products = cast(list[dict[str, object]], result.payload["products"])
        self.assertEqual(
            [item["parent_asin"] for item in products], ["A"]
        )

        self.state.tool_candidate_ids = ["C"]
        unknown = self.toolbox.execute(
            PlannedAction(
                "filter_products",
                {
                    "product_ids": ["C"],
                    "attribute": "material",
                    "operator": "not_contains",
                    "value": "leather",
                },
            )
        )
        self.assertEqual(unknown.payload["matched_count"], 0)
        self.assertEqual(unknown.payload["unknown_ids"], ["C"])

    def test_profile_is_hidden_until_explicit_tool_call(self) -> None:
        self.assertFalse(self.state.profile_loaded)
        result = self.toolbox.execute(PlannedAction("get_user_profile", {}))
        self.assertTrue(result.ok)
        self.assertTrue(self.state.profile_loaded)
        profile = cast(dict[str, object], result.payload["profile"])
        self.assertEqual(profile["preference_tags"], ["comfort"])
        self.assertNotIn("private_extra", profile)

    def test_invalid_ids_and_ask_attributes_are_bounded_errors(self) -> None:
        detail = self.toolbox.execute(
            PlannedAction("get_product_details", {"parent_asin": "UNKNOWN"})
        )
        self.assertFalse(detail.ok)
        ask = self.toolbox.execute(
            PlannedAction("ask_user", {"question": "Any preference?", "ask_attribute": "reviews"})
        )
        self.assertFalse(ask.ok)

    def test_typed_action_is_revalidated_at_execution_boundary(self) -> None:
        with self.assertRaises(ActionValidationError):
            parse_planned_action(
                PlannedAction("search_products", ["not", "an", "object"])  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
