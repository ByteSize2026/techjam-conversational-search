from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from starter.shopping_agent.contest_agent import ContestAgent
from starter.shopping_agent.contest_config import CLASSMATE, KHANNA, PUBLIC, ContestConfig
from starter.shopping_agent.contest_dense import PoolDenseEncoder, set_encoder
from starter.shopping_agent.contest_rerank import PoolReranker, set_reranker
from starter.shopping_agent.contest_dialogue import parse_opening, parse_reply
from starter.shopping_agent.contest_index import ContestIndex
from starter.shopping_agent.contest_response import guard_response
from starter.shopping_agent.contest_rank import conjunction_asins, hard_pool, rank
from starter.shopping_agent.contest_slots import ContestState
from starter.shopping_agent.contest_text import constraint_matches, product_search_text


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


class ContestAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.catalog_path = _write_catalog(Path(self.tempdir.name))

    def tearDown(self) -> None:
        set_encoder(None)
        set_reranker(None)
        self.tempdir.cleanup()

    def test_evaluator_facade_is_contest_public(self) -> None:
        from starter.agent import Agent as Facade

        self.assertTrue(issubclass(Facade, ContestAgent))
        agent = Facade(self.catalog_path)
        self.assertEqual(agent.config.gate_size, PUBLIC.gate_size)
        self.assertTrue(agent.config.dense_skip_generic)
        self.assertEqual(agent.config.w_title, 0.0)
        self.assertEqual(agent.config.distinctive_early_cap, 0)
        self.assertEqual(agent.config.dense_generic_cap, 0)
        self.assertEqual(agent.config.w_dense_tiny, 0.12)
        self.assertEqual(agent.config.dense_tiny_cap, 6)
        self.assertEqual(agent.config.dense_tie_margin, 0.0)
        self.assertEqual(agent.config.w_bm25, 0.0)
        self.assertEqual(agent.config.w_uniq, 0.0)
        self.assertEqual(agent.config.w_field, 0.35)
        self.assertFalse(agent.config.dense_skip_field_flat)
        self.assertEqual(agent.config.w_phrase, 0.15)

    def test_opening_templates(self) -> None:
        lookup = {"shirts": "Shirts", "boots": "Boots"}
        buying = parse_opening("I'm looking for shirts. A key requirement is: cotton.", lookup)
        self.assertEqual(buying.scenario, "buying")
        self.assertEqual(buying.category, "Shirts")
        self.assertEqual(buying.constraints, ["cotton"])
        browsing = parse_opening("I'm looking for shirts, but I'm still exploring.", lookup)
        self.assertEqual(browsing.scenario, "browsing")
        override = parse_reply(
            "Actually, ignore my earlier preference. What I need is: leather."
        )
        self.assertEqual(override.kind, "override")
        self.assertEqual(override.constraints, ["leather"])
        self.assertEqual(override.scope, "referenced_preference_replace")
        opening_override = parse_opening(
            "I'm looking for Underwear Undershirts. Imported",
            {"underwear undershirts": "Underwear Undershirts", "shirts": "Shirts"},
        )
        self.assertEqual(opening_override.scenario, "intent_override")
        self.assertEqual(opening_override.category, "Underwear Undershirts")
        self.assertEqual([item.lower() for item in opening_override.constraints], ["imported"])

    def test_khanna_always_asks_other_and_fills_top_k(self) -> None:
        agent = ContestAgent(self.catalog_path, config=KHANNA)
        agent.reset("s", {"preference_tags": ["comfort"]})
        response = agent.respond("s", "I'm looking for shirts. A key requirement is: cotton.", 1, 10)
        self.assertEqual(response["ask_attribute"], "other")
        ids = [item["parent_asin"] for item in response["recommendations"]]
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids, list(dict.fromkeys(ids)))
        self.assertTrue(set(ids) <= {"A", "B", "C"})
        self.assertIn("A", ids)

    def test_override_keeps_opening_value_at_decay(self) -> None:
        agent = ContestAgent(self.catalog_path, config=KHANNA)
        agent.reset("s", {})
        agent.respond("s", "I'm looking for boots. A key requirement is: cotton.", 1, 10)
        agent.respond(
            "s",
            "Actually, ignore my earlier preference. What I need is: leather.",
            2,
            10,
        )
        state = agent._sessions["s"]
        self.assertTrue(state.override_applied)
        texts = {item.text.lower(): item.weight for item in state.active}
        self.assertIn("leather", texts)
        self.assertEqual(texts.get("cotton"), 0.5)
        self.assertEqual(state.intent_scope, "referenced_preference_replace")
        self.assertEqual(state.intent_epoch, 1)

    def test_attribute_replace_deactivates_same_typed_slot(self) -> None:
        agent = ContestAgent(self.catalog_path, config=KHANNA)
        agent.reset("s", {})
        agent.respond("s", "I'm looking for shirts. A key requirement is: black.", 1, 10)
        agent.respond("s", "Please change the color to blue.", 2, 10)
        state = agent._sessions["s"]
        self.assertEqual(state.intent_scope, "attribute_replace")
        active = {item.text.lower(): item.attribute for item in state.active}
        self.assertIn("blue", active)
        self.assertNotIn("black", active)
        self.assertEqual(state.category, "Shirts")

    def test_global_reset_clears_constraints_keeps_category(self) -> None:
        agent = ContestAgent(self.catalog_path, config=KHANNA)
        agent.reset("s", {})
        agent.respond("s", "I'm looking for shirts. A key requirement is: cotton.", 1, 10)
        agent.respond("s", "Forget everything. What I need is: leather.", 2, 10)
        state = agent._sessions["s"]
        self.assertEqual(state.intent_scope, "global_reset")
        texts = {item.text.lower() for item in state.active}
        self.assertEqual(texts, {"leather"})
        self.assertEqual(state.category, "Shirts")

    def test_guard_drops_invalid_ids_and_illegal_ask(self) -> None:
        index = ContestIndex(self.catalog_path)
        guarded = guard_response(
            index,
            {
                "message": "ok",
                "ask_attribute": "not-a-slot",
                "recommendations": [
                    {"parent_asin": "A"},
                    {"parent_asin": "ZZZ"},
                    "A",
                    {"parent_asin": "B"},
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            },
            10,
        )
        self.assertEqual(guarded["message"], "ok")
        self.assertIsNone(guarded["ask_attribute"])
        self.assertEqual(
            [item["parent_asin"] for item in guarded["recommendations"]],
            ["A", "B"],
        )
        self.assertEqual(guarded["usage"], {"prompt_tokens": 0, "completion_tokens": 0})

    def test_message_remembers_intent_and_still_asks_other(self) -> None:
        agent = ContestAgent(self.catalog_path, config=KHANNA)
        agent.reset("s", {"preference_tags": ["comfort"]})
        response = agent.respond(
            "s",
            "I'm looking for shirts. A key requirement is: cotton.",
            1,
            10,
        )
        self.assertEqual(response["ask_attribute"], "other")
        lowered = response["message"].lower()
        self.assertIn("cotton", lowered)
        self.assertIn("shirts", lowered)
        agent.respond(
            "s",
            "Actually, ignore my earlier preference. What I need is: leather.",
            2,
            10,
        )
        state = agent._sessions["s"]
        self.assertEqual(state.intent_snippets()[-1].lower(), "leather")
        self.assertTrue(any(event["kind"] == "referenced_preference_replace" for event in state.intent_log))

    def test_distinctive_early_commit_ranks_small_hard_pool(self) -> None:
        rows = []
        for idx in range(8):
            rows.append(
                {
                    "parent_asin": f"R{idx}",
                    "title": f"Trail shoe {idx}",
                    "features": ["rubber sole", "leather"],
                    "description": ["hiking"],
                    "categories": ["Shoes"],
                    "details": {"department": "unisex"},
                    "store": f"Store{idx}",
                    "price": 40.0 + idx,
                    "average_rating": 4.0,
                    "rating_number": 20 + idx,
                }
            )
        path = Path(self.tempdir.name) / "distinctive_early.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        config = ContestConfig(
            gate_size=5,
            hard_filter=True,
            gate_before_override=True,
            pad_to_top_k=False,
            distinctive_early_cap=10,
        )
        agent = ContestAgent(path, config=config)
        agent.reset("s", {})
        first = agent.respond(
            "s",
            "I'm looking for shoes. A key requirement is: rubber sole.",
            1,
            10,
        )
        self.assertEqual(first["ask_attribute"], "other")
        self.assertEqual(len(first["recommendations"]), 8)
        self.assertIn("rubber", first["message"].lower())

    def test_generic_cotton_does_not_early_commit(self) -> None:
        rows = []
        for idx in range(8):
            rows.append(
                {
                    "parent_asin": f"C{idx}",
                    "title": f"Cotton tee {idx}",
                    "features": ["cotton", "imported"],
                    "description": ["shirt"],
                    "categories": ["Shirts"],
                    "details": {"department": "unisex"},
                    "store": f"Store{idx}",
                    "price": 15.0 + idx,
                    "average_rating": 4.0,
                    "rating_number": 20 + idx,
                }
            )
        path = Path(self.tempdir.name) / "generic_early.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        config = ContestConfig(
            gate_size=5,
            hard_filter=True,
            gate_before_override=True,
            pad_to_top_k=False,
            distinctive_early_cap=10,
        )
        agent = ContestAgent(path, config=config)
        agent.reset("s", {})
        first = agent.respond(
            "s",
            "I'm looking for shirts. A key requirement is: cotton.",
            1,
            10,
        )
        self.assertEqual(first["recommendations"], [])
        self.assertEqual(first["ask_attribute"], "other")

    def test_product_own_feature_detail_and_budget_stay_in_conjunction_pool(self) -> None:
        rows = [
            {
                "parent_asin": "PUNCT",
                "title": "Kids novelty tee",
                "features": ["100% Cotton, pre-shrunk; ribbed crew neck."],
                "description": ["fun graphic"],
                "categories": ["Clothing", "Boys", "Novelty T-Shirts"],
                "details": {"Color": "Hot Pink", "department": "boys"},
                "store": "Zed",
                "price": 12.5,
                "average_rating": 1.0,
                "rating_number": 1,
            },
            {
                "parent_asin": "OTHER",
                "title": "Plain black cotton shirt",
                "features": ["Machine wash cold"],
                "description": ["everyday tee"],
                "categories": ["Clothing", "Boys", "Novelty T-Shirts"],
                "details": {"Color": "Black", "department": "boys"},
                "store": "Other",
                "price": 40.0,
                "average_rating": 4.9,
                "rating_number": 9000,
            },
        ]
        source = rows[0]
        corpus = product_search_text(source)
        feature = "100% Cotton, pre-shrunk; ribbed crew neck."
        self.assertTrue(constraint_matches(feature, corpus, source["price"]))
        self.assertTrue(constraint_matches("color: pink", corpus, source["price"]))
        self.assertTrue(constraint_matches("budget around $12.5", corpus, source["price"]))
        ids = conjunction_asins(
            rows,
            "Boys Novelty T-Shirts",
            [feature, "color: pink", "budget around $12.5"],
        )
        self.assertIn("PUNCT", ids)
        self.assertNotIn("OTHER", ids)

    def test_generic_and_keeps_cold_source_when_popularity_ranks_it_out(self) -> None:
        """Holdout Hit leaks are this class, not skipped matching.

        Cotton / 100% cotton / color:white / imported match the 1-rating
        source and 16 hotter clones. Conjunction keeps COLD; a leather
        clone drops. Popularity-first Top-10 then excludes COLD. Raising
        COLD into Top-10 would require inverting popularity.
        """

        rows = []
        for idx in range(16):
            rows.append(
                {
                    "parent_asin": f"HOT{idx:02d}",
                    "title": f"Hot cotton tee {idx}",
                    "features": ["cotton", "100% cotton", "Imported"],
                    "description": ["everyday tee"],
                    "categories": ["Clothing", "Men", "T-Shirts"],
                    "details": {"Color": "White", "department": "mens"},
                    "store": "HotBrand",
                    "price": 18.0,
                    "average_rating": 4.9,
                    "rating_number": 9000 - idx,
                }
            )
        cold = {
            "parent_asin": "COLD",
            "title": "Quiet cotton tee",
            "features": ["cotton", "100% cotton", "Imported"],
            "description": ["everyday tee"],
            "categories": ["Clothing", "Men", "T-Shirts"],
            "details": {"Color": "White", "department": "mens"},
            "store": "QuietBrand",
            "price": 18.0,
            "average_rating": 1.0,
            "rating_number": 1,
        }
        other = {
            "parent_asin": "OTHER",
            "title": "Black leather boot",
            "features": ["leather", "rubber sole"],
            "description": ["boot"],
            "categories": ["Clothing", "Men", "T-Shirts"],
            "details": {"Color": "Black", "department": "mens"},
            "store": "Other",
            "price": 80.0,
            "average_rating": 4.9,
            "rating_number": 8000,
        }
        rows.extend([cold, other])
        constraints = ["cotton", "color: white", "100% cotton", "imported"]
        corpus = product_search_text(cold)
        for text in constraints:
            self.assertTrue(constraint_matches(text, corpus, cold["price"]))
        ids = conjunction_asins(rows, "Men T-Shirts", constraints)
        self.assertIn("COLD", ids)
        self.assertNotIn("OTHER", ids)
        self.assertGreaterEqual(len(ids), 17)

        path = Path(self.tempdir.name) / "generic_and.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Men T-Shirts"
        state.add_constraints(constraints, turn=1)
        cfg = replace(PUBLIC, w_dense=0.0, w_rerank=0.0, w_title=0.0)
        hard = hard_pool(index, state, list(range(len(index))))
        self.assertIn("COLD", [index.ids[idx] for idx in hard])
        ranked_ids = [index.ids[idx] for idx in rank(index, state, cfg, hard, limit=10)]
        self.assertNotIn("COLD", ranked_ids)
        self.assertTrue(all(item.startswith("HOT") for item in ranked_ids))

    def test_title_tie_break_ranks_distinctive_title_above_hotter_clone(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["ribbed crew neck", "pre-shrunk"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 9000,
            },
            {
                "parent_asin": "TARGET",
                "title": "Ribbed crew neck cotton shirt",
                "features": ["ribbed crew neck", "pre-shrunk"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 12,
            },
        ]
        path = Path(self.tempdir.name) / "title_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        pool = list(range(len(index)))
        titled = ContestConfig(
            gate_size=5,
            hard_filter=True,
            pad_to_top_k=False,
            w_popularity=1.0,
            w_constraint=0.35,
            w_lexical=0.55,
            w_title=0.55,
            title_pool_limit=24,
        )
        popular_first = [index.ids[idx] for idx in rank(index, state, KHANNA, pool, limit=2)]
        titled_first = [index.ids[idx] for idx in rank(index, state, titled, pool, limit=2)]
        self.assertEqual(popular_first[0], "HOT")
        self.assertEqual(titled_first[0], "TARGET")
        generic = ContestState(session_id="g")
        generic.category = "Shirts"
        generic.add_constraints(["cotton"], turn=1)
        generic_first = [index.ids[idx] for idx in rank(index, generic, titled, pool, limit=2)]
        self.assertEqual(generic_first[0], "HOT")
        public_first = [
            index.ids[idx]
            for idx in rank(index, state, replace(PUBLIC, w_dense=0, w_phrase=0.0), pool, limit=2)
        ]
        self.assertEqual(public_first[0], "HOT")

    def test_tiny_pool_title_tie_breaks_near_popularity_clones(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["ribbed crew neck", "pre-shrunk"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.0,
                "rating_number": 40,
            },
            {
                "parent_asin": "TARGET",
                "title": "Ribbed crew neck cotton shirt",
                "features": ["ribbed crew neck", "pre-shrunk"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.0,
                "rating_number": 25,
            },
        ]
        for idx in range(4):
            rows.append(
                {
                    "parent_asin": f"CLONE{idx}",
                    "title": f"Cotton layer {idx}",
                    "features": ["ribbed crew neck", "pre-shrunk"],
                    "description": ["basic tee"],
                    "categories": ["Shirts"],
                    "details": {"department": "mens"},
                    "store": f"Store{idx}",
                    "price": 18.0,
                    "average_rating": 4.0,
                    "rating_number": 10 + idx,
                }
            )
        path = Path(self.tempdir.name) / "tiny_title.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        pool = list(range(len(index)))
        self.assertLessEqual(len(pool), 6)
        tiny = replace(PUBLIC, w_dense=0.0, w_title=0.12, title_pool_limit=6, w_phrase=0.0)
        off = replace(PUBLIC, w_dense=0.0, w_title=0.0, title_pool_limit=6, w_phrase=0.0)
        self.assertEqual(index.ids[rank(index, state, off, pool, limit=1)[0]], "HOT")
        self.assertEqual(index.ids[rank(index, state, tiny, pool, limit=1)[0]], "TARGET")
        wide = replace(PUBLIC, w_dense=0.0, w_title=0.12, title_pool_limit=6, w_phrase=0.0)
        big_rows = rows + [
            {
                "parent_asin": f"EXTRA{idx}",
                "title": f"Spare cotton {idx}",
                "features": ["ribbed crew neck"],
                "description": ["tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "X",
                "price": 18.0,
                "average_rating": 4.0,
                "rating_number": 8,
            }
            for idx in range(3)
        ]
        big_path = Path(self.tempdir.name) / "wide_title.jsonl"
        big_path.write_text("".join(json.dumps(row) + "\n" for row in big_rows), encoding="utf-8")
        big_index = ContestIndex(big_path)
        big_pool = list(range(len(big_index)))
        self.assertGreater(len(big_pool), 6)
        self.assertEqual(
            big_index.ids[rank(big_index, state, wide, big_pool, limit=1)[0]],
            "HOT",
        )

    def test_three_slots_recommend_inside_evidence_cap_despite_gate(self) -> None:
        rows = []
        for idx in range(8):
            rows.append(
                {
                    "parent_asin": f"P{idx}",
                    "title": f"Cotton pullover {idx}",
                    "features": ["cotton", "pull on closure", "machine wash"],
                    "description": ["layer"],
                    "categories": ["Shirts"],
                    "details": {"department": "unisex"},
                    "store": f"Store{idx}",
                    "price": 20.0 + idx,
                    "average_rating": 4.0,
                    "rating_number": 10 + idx,
                }
            )
        path = Path(self.tempdir.name) / "evidence_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        gated = ContestConfig(
            gate_size=5,
            hard_filter=True,
            gate_before_override=True,
            pad_to_top_k=False,
            min_slots_to_recommend=0,
        )
        early = ContestConfig(
            gate_size=5,
            hard_filter=True,
            gate_before_override=True,
            pad_to_top_k=False,
            min_slots_to_recommend=3,
            evidence_pool_cap=20,
        )
        closed = ContestAgent(path, config=gated)
        closed.reset("s", {})
        closed.respond("s", "I'm looking for shirts. A key requirement is: cotton.", 1, 10)
        still_closed = closed.respond(
            "s",
            "For that, what matters is: pull on closure; machine wash.",
            2,
            10,
        )
        self.assertEqual(still_closed["recommendations"], [])
        opened = ContestAgent(path, config=early)
        opened.reset("s", {})
        first = opened.respond("s", "I'm looking for shirts. A key requirement is: cotton.", 1, 10)
        self.assertEqual(first["recommendations"], [])
        second = opened.respond(
            "s",
            "For that, what matters is: pull on closure; machine wash.",
            2,
            10,
        )
        ids = [item["parent_asin"] for item in second["recommendations"]]
        self.assertEqual(second["ask_attribute"], "other")
        self.assertEqual(len(ids), 8)
        self.assertEqual(ids, list(dict.fromkeys(ids)))
        self.assertTrue(set(ids) <= {f"P{idx}" for idx in range(8)})

    def test_four_slots_dump_inside_cap_despite_large_gate(self) -> None:
        rows = []
        for idx in range(12):
            rows.append(
                {
                    "parent_asin": f"P{idx}",
                    "title": f"Cotton pullover {idx}",
                    "features": ["cotton", "pull on closure", "machine wash", "imported"],
                    "description": ["layer"],
                    "categories": ["Shirts"],
                    "details": {"department": "unisex"},
                    "store": f"Store{idx}",
                    "price": 20.0 + idx,
                    "average_rating": 4.0,
                    "rating_number": 10 + idx,
                }
            )
        path = Path(self.tempdir.name) / "dump_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        config = ContestConfig(
            gate_size=5,
            hard_filter=True,
            gate_before_override=True,
            pad_to_top_k=False,
            min_slots_to_recommend=3,
            evidence_pool_cap=8,
            dump_slots=4,
            dump_pool_cap=20,
        )
        agent = ContestAgent(path, config=config)
        agent.reset("s", {})
        agent.respond("s", "I'm looking for shirts. A key requirement is: cotton.", 1, 10)
        mid = agent.respond(
            "s",
            "For that, what matters is: pull on closure; machine wash.",
            2,
            10,
        )
        self.assertEqual(mid["recommendations"], [])
        last = agent.respond("s", "For that, what matters is: imported.", 3, 10)
        ids = [item["parent_asin"] for item in last["recommendations"]]
        self.assertEqual(len(ids), 10)
        self.assertEqual(last["ask_attribute"], "other")

    def test_dense_tie_break_on_hard_pool_can_outrank_a_hotter_clone(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["ribbed crew neck"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 9000,
            },
            {
                "parent_asin": "TARGET",
                "title": "Ribbed crew neck cotton shirt",
                "features": ["ribbed crew neck"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 12,
            },
        ]
        path = Path(self.tempdir.name) / "dense_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        pool = list(range(len(index)))

        def encode(texts: list[str]) -> list[list[float]]:
            vectors = []
            for text in texts:
                lowered = text.lower()
                if "ribbed crew neck" in lowered and "everyday" not in lowered:
                    vectors.append([1.0, 0.0])
                elif "ribbed crew" in lowered:
                    vectors.append([0.2, 0.8])
                else:
                    vectors.append([0.0, 1.0])
            return vectors

        set_encoder(PoolDenseEncoder(encode=encode))
        dense = ContestConfig(
            gate_size=5,
            hard_filter=True,
            pad_to_top_k=False,
            w_popularity=1.0,
            w_constraint=0.35,
            w_lexical=0.55,
            w_dense=0.85,
            dense_pool_limit=80,
        )
        popular_first = [
            index.ids[idx]
            for idx in rank(index, state, replace(PUBLIC, w_dense=0, w_phrase=0.0), pool, limit=2)
        ]
        dense_first = [index.ids[idx] for idx in rank(index, state, dense, pool, limit=2)]
        self.assertEqual(popular_first[0], "HOT")
        self.assertEqual(dense_first[0], "TARGET")

    def test_near_tie_minilm_extra_skips_popularity_blowout(self) -> None:
        def rows(hot_n: int, cold_n: int) -> list[dict]:
            data = [
                {
                    "parent_asin": "HOT",
                    "title": "Everyday cotton shirt",
                    "features": ["ribbed crew neck"],
                    "description": ["basic tee"],
                    "categories": ["Shirts"],
                    "details": {"department": "mens"},
                    "store": "HotBrand",
                    "price": 18.0,
                    "average_rating": 4.8,
                    "rating_number": hot_n,
                },
                {
                    "parent_asin": "TARGET",
                    "title": "Ribbed crew neck cotton shirt",
                    "features": ["ribbed crew neck"],
                    "description": ["basic tee"],
                    "categories": ["Shirts"],
                    "details": {"department": "mens"},
                    "store": "QuietBrand",
                    "price": 18.0,
                    "average_rating": 4.8,
                    "rating_number": cold_n,
                },
            ]
            for idx in range(6):
                data.append(
                    {
                        "parent_asin": f"C{idx}",
                        "title": f"Cotton layer {idx}",
                        "features": ["ribbed crew neck"],
                        "description": ["tee"],
                        "categories": ["Shirts"],
                        "details": {"department": "mens"},
                        "store": f"S{idx}",
                        "price": 18.0,
                        "average_rating": 4.0,
                        "rating_number": 10 + idx,
                    }
                )
            return data

        def encode(texts: list[str]) -> list[list[float]]:
            vectors = []
            for text in texts:
                lowered = text.lower()
                if "ribbed crew neck" in lowered and "everyday" not in lowered and "layer" not in lowered:
                    vectors.append([1.0, 0.0])
                else:
                    vectors.append([0.0, 1.0])
            return vectors

        set_encoder(PoolDenseEncoder(encode=encode))
        trial = replace(
            PUBLIC,
            w_dense=0.01,
            w_dense_tiny=0.35,
            dense_tiny_cap=6,
            dense_tie_margin=0.05,
            dense_tie_cap=20,
            dense_skip_generic=False,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
        )
        tied_path = Path(self.tempdir.name) / "tie_dense.jsonl"
        tied_path.write_text("".join(json.dumps(row) + "\n" for row in rows(50, 45)), encoding="utf-8")
        tied_index = ContestIndex(tied_path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        tied_pool = list(range(len(tied_index)))
        self.assertGreater(len(tied_pool), 6)
        self.assertEqual(
            tied_index.ids[rank(tied_index, state, trial, tied_pool, limit=1)[0]],
            "TARGET",
        )
        blow_path = Path(self.tempdir.name) / "blow_dense.jsonl"
        blow_path.write_text("".join(json.dumps(row) + "\n" for row in rows(400, 20)), encoding="utf-8")
        blow_index = ContestIndex(blow_path)
        blow_pool = list(range(len(blow_index)))
        self.assertEqual(
            blow_index.ids[rank(blow_index, state, trial, blow_pool, limit=1)[0]],
            "HOT",
        )

    def test_minilm_loader_uses_cache_then_hub(self) -> None:
        encoder = PoolDenseEncoder()
        calls: list[bool] = []

        class DummyModel:
            def eval(self) -> object:
                return self

        def fake_load(local_files_only: bool):
            calls.append(local_files_only)
            if local_files_only:
                raise OSError("cache miss")
            return "tok", DummyModel(), object()

        encoder._load_transformers = fake_load  # type: ignore[method-assign]
        self.assertTrue(encoder.available())
        self.assertEqual(calls, [True, False])

    def test_minilm_loader_stays_offline_when_flag_set(self) -> None:
        previous = os.environ.get("TECHJAM_DENSE_OFFLINE")
        os.environ["TECHJAM_DENSE_OFFLINE"] = "1"
        self.addCleanup(
            lambda: os.environ.pop("TECHJAM_DENSE_OFFLINE", None)
            if previous is None
            else os.environ.__setitem__("TECHJAM_DENSE_OFFLINE", previous)
        )
        encoder = PoolDenseEncoder()
        calls: list[bool] = []

        def fake_load(local_files_only: bool):
            calls.append(local_files_only)
            raise OSError("cache miss")

        encoder._load_transformers = fake_load  # type: ignore[method-assign]
        self.assertFalse(encoder.available())
        self.assertEqual(calls, [True])

    def test_tiny_generic_pool_can_use_minilm_without_pop_floor(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["cotton", "imported"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"Color": "Black", "department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 80,
            },
            {
                "parent_asin": "TARGET",
                "title": "Soft cotton shirt",
                "features": ["cotton", "imported"],
                "description": ["soft cotton shirt"],
                "categories": ["Shirts"],
                "details": {"Color": "Black", "department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 40,
            },
        ]
        path = Path(self.tempdir.name) / "tiny_generic_dense.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["cotton", "imported"], turn=1)
        pool = list(range(len(index)))

        def encode(texts: list[str]) -> list[list[float]]:
            vectors = []
            for text in texts:
                lowered = text.lower()
                if "everyday" in lowered:
                    vectors.append([0.0, 1.0])
                else:
                    vectors.append([1.0, 0.0])
            return vectors

        set_encoder(PoolDenseEncoder(encode=encode))
        trial = replace(
            PUBLIC,
            dense_skip_generic=True,
            dense_generic_cap=6,
            w_dense=0.1,
            w_dense_tiny=0.25,
            dense_tiny_cap=6,
        )
        popular_first = [
            index.ids[idx] for idx in rank(index, state, replace(PUBLIC, w_dense=0), pool, limit=2)
        ]
        trial_first = [index.ids[idx] for idx in rank(index, state, trial, pool, limit=2)]
        self.assertEqual(popular_first[0], "HOT")
        self.assertEqual(trial_first[0], "TARGET")
        blocked = replace(trial, dense_generic_cap=0, w_dense_tiny=0.0)
        blocked_first = [index.ids[idx] for idx in rank(index, state, blocked, pool, limit=2)]
        self.assertEqual(blocked_first[0], "HOT")

    def test_idf_rare_token_outranks_hotter_generic_clone(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Cotton tee imported",
                "features": ["cotton", "100% cotton", "Imported"],
                "description": ["everyday tee"],
                "categories": ["Clothing", "Men", "T-Shirts"],
                "details": {"Color": "White", "department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 200,
            },
            {
                "parent_asin": "TARGET",
                "title": "Gemma cotton tee",
                "features": ["cotton", "100% cotton", "Imported", "Gemma"],
                "description": ["everyday tee"],
                "categories": ["Clothing", "Men", "T-Shirts"],
                "details": {"Color": "White", "department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 50,
            },
        ]
        path = Path(self.tempdir.name) / "idf_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Men T-Shirts"
        state.add_constraints(["cotton", "color: white", "imported", "gemma"], turn=1)
        pool = list(range(len(index)))
        ids = conjunction_asins(rows, "Men T-Shirts", ["cotton", "color: white", "imported", "gemma"])
        self.assertIn("TARGET", ids)
        baseline = replace(
            PUBLIC,
            w_idf=0.0,
            w_exclusive=0.0,
            w_dense=0.0,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
            dense_skip_generic=False,
        )
        idf_cfg = replace(
            PUBLIC,
            w_idf=0.25,
            w_exclusive=0.0,
            w_dense=0.0,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
            dense_skip_generic=False,
        )
        popular_first = [index.ids[idx] for idx in rank(index, state, baseline, pool, limit=2)]
        idf_first = [index.ids[idx] for idx in rank(index, state, idf_cfg, pool, limit=2)]
        self.assertEqual(popular_first[0], "HOT")
        self.assertEqual(idf_first[0], "TARGET")

    def test_exclusive_rare_token_outranks_hotter_generic_clone(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Cotton tee imported",
                "features": ["cotton", "100% cotton", "Imported"],
                "description": ["everyday tee"],
                "categories": ["Clothing", "Men", "T-Shirts"],
                "details": {"Color": "White", "department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 200,
            },
            {
                "parent_asin": "TARGET",
                "title": "Gemma cotton tee",
                "features": ["cotton", "100% cotton", "Imported", "Gemma"],
                "description": ["everyday tee"],
                "categories": ["Clothing", "Men", "T-Shirts"],
                "details": {"Color": "White", "department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 50,
            },
        ]
        path = Path(self.tempdir.name) / "exclusive_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Men T-Shirts"
        state.add_constraints(["cotton", "color: white", "imported", "gemma"], turn=1)
        pool = list(range(len(index)))
        ids = conjunction_asins(rows, "Men T-Shirts", ["cotton", "color: white", "imported", "gemma"])
        self.assertIn("TARGET", ids)
        quiet = dict(
            w_idf=0.0,
            w_dense=0.0,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
            dense_skip_generic=False,
        )
        popular_first = [
            index.ids[idx]
            for idx in rank(index, state, replace(PUBLIC, w_exclusive=0.0, **quiet), pool, limit=2)
        ]
        exclusive_first = [
            index.ids[idx]
            for idx in rank(index, state, replace(PUBLIC, w_exclusive=0.25, **quiet), pool, limit=2)
        ]
        self.assertEqual(popular_first[0], "HOT")
        self.assertEqual(exclusive_first[0], "TARGET")

    def test_hard_pool_bm25_prefers_title_hit_over_long_hotter_clone(self) -> None:
        filler = " ".join(f"token{idx}" for idx in range(80))
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["ribbed crew neck", filler],
                "description": [filler],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 80,
            },
            {
                "parent_asin": "TARGET",
                "title": "Ribbed crew neck cotton shirt",
                "features": ["ribbed crew neck"],
                "description": ["tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 40,
            },
        ]
        path = Path(self.tempdir.name) / "bm25_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        pool = list(range(len(index)))
        off = replace(
            PUBLIC,
            w_dense=0.0,
            w_bm25=0.0,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
            w_phrase=0.0,
        )
        on = replace(off, w_bm25=0.25)
        self.assertEqual(index.ids[rank(index, state, off, pool, limit=1)[0]], "HOT")
        self.assertEqual(index.ids[rank(index, state, on, pool, limit=1)[0]], "TARGET")

    def test_hard_pool_title_uniqueness_outranks_hotter_generic_clone(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Cotton everyday shirt",
                "features": ["ribbed crew neck"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 80,
            },
            {
                "parent_asin": "TARGET",
                "title": "Alpine gemma trail shirt",
                "features": ["ribbed crew neck"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 40,
            },
        ]
        path = Path(self.tempdir.name) / "uniq_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        pool = list(range(len(index)))
        off = replace(
            PUBLIC,
            w_dense=0.0,
            w_uniq=0.0,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
            w_bm25=0.0,
        )
        on = replace(off, w_uniq=0.35)
        self.assertEqual(index.ids[rank(index, state, off, pool, limit=1)[0]], "HOT")
        self.assertEqual(index.ids[rank(index, state, on, pool, limit=1)[0]], "TARGET")

    def test_hard_pool_exact_field_line_outranks_hotter_blob_clone(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["soft cotton jersey"],
                "description": ["The ribbed crew neck is comfortable"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 80,
            },
            {
                "parent_asin": "TARGET",
                "title": "Cotton shirt",
                "features": ["ribbed crew neck"],
                "description": ["tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 40,
            },
        ]
        path = Path(self.tempdir.name) / "field_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        pool = list(range(len(index)))
        off = replace(
            PUBLIC,
            w_dense=0.0,
            w_field=0.0,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
            w_bm25=0.0,
            w_uniq=0.0,
        )
        on = replace(off, w_field=0.35)
        self.assertEqual(index.ids[rank(index, state, off, pool, limit=1)[0]], "HOT")
        self.assertEqual(index.ids[rank(index, state, on, pool, limit=1)[0]], "TARGET")

    def test_hard_pool_field_line_strips_trailing_punct(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["soft cotton jersey."],
                "description": ["The ribbed crew neck is comfortable"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 80,
            },
            {
                "parent_asin": "TARGET",
                "title": "Cotton shirt",
                "features": ["ribbed crew neck."],
                "description": ["tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 40,
            },
        ]
        path = Path(self.tempdir.name) / "field_punct.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        pool = list(range(len(index)))
        off = replace(
            PUBLIC,
            w_dense=0.0,
            w_field=0.0,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
            w_bm25=0.0,
            w_uniq=0.0,
        )
        on = replace(off, w_field=0.35)
        self.assertEqual(index.ids[rank(index, state, off, pool, limit=1)[0]], "HOT")
        self.assertEqual(index.ids[rank(index, state, on, pool, limit=1)[0]], "TARGET")

    def test_field_flat_skips_minilm_keeps_hotter_clone_first(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["ribbed crew neck"],
                "description": ["plain tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 80,
            },
            {
                "parent_asin": "CLONE",
                "title": "Cotton shirt",
                "features": ["ribbed crew neck"],
                "description": ["everyday unique rubber sole comfort"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 40,
            },
        ]
        path = Path(self.tempdir.name) / "field_flat_dense.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        pool = list(range(len(index)))

        def encode(texts: list[str]) -> list[list[float]]:
            vectors = []
            for text in texts:
                lowered = text.lower()
                if "plain tee" in lowered:
                    vectors.append([0.0, 1.0])
                else:
                    vectors.append([1.0, 0.0])
            return vectors

        set_encoder(PoolDenseEncoder(encode=encode))
        base = replace(
            PUBLIC,
            w_dense=0.8,
            w_dense_tiny=0.5,
            dense_tiny_cap=6,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
            w_field=0.25,
            dense_skip_generic=True,
            dense_skip_field_flat=False,
        )
        shuffled = rank(index, state, base, pool, limit=1)
        locked = rank(index, state, replace(base, dense_skip_field_flat=True), pool, limit=1)
        self.assertEqual(index.ids[shuffled[0]], "CLONE")
        self.assertEqual(index.ids[locked[0]], "HOT")

    def test_hard_pool_distinctive_title_phrase_outranks_hotter_blob_clone(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["soft cotton jersey"],
                "description": ["The ribbed crew neck is comfortable"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 80,
            },
            {
                "parent_asin": "TARGET",
                "title": "Ribbed crew neck cotton shirt",
                "features": ["everyday tee"],
                "description": ["ribbed crew neck"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 40,
            },
        ]
        path = Path(self.tempdir.name) / "phrase_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)
        pool = list(range(len(index)))
        off = replace(
            PUBLIC,
            w_dense=0.0,
            w_field=0.0,
            w_phrase=0.0,
            w_lexical=0.0,
            w_constraint=0.0,
            w_profile=0.0,
            w_bm25=0.0,
            w_uniq=0.0,
            w_title=0.0,
        )
        on = replace(off, w_phrase=0.35)
        self.assertEqual(index.ids[rank(index, state, off, pool, limit=1)[0]], "HOT")
        self.assertEqual(index.ids[rank(index, state, on, pool, limit=1)[0]], "TARGET")

    def test_dense_pop_floor_keeps_eighth_popular_in_top10(self) -> None:
        rows = []
        for idx in range(7):
            rows.append(
                {
                    "parent_asin": f"HOT{idx:02d}",
                    "title": f"Cotton tee {idx}",
                    "features": ["cotton", "100% cotton", "Imported"],
                    "description": ["everyday tee"],
                    "categories": ["Clothing", "Men", "T-Shirts"],
                    "details": {"Color": "Black", "department": "mens"},
                    "store": "HotBrand",
                    "price": 18.0,
                    "average_rating": 4.5,
                    "rating_number": 120 - idx,
                }
            )
        rows.append(
            {
                "parent_asin": "COLD",
                "title": "Quiet cotton tee",
                "features": ["cotton", "100% cotton", "Imported"],
                "description": ["everyday tee"],
                "categories": ["Clothing", "Men", "T-Shirts"],
                "details": {"Color": "Black", "department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.5,
                "rating_number": 113,
            }
        )
        for idx in range(8):
            rows.append(
                {
                    "parent_asin": f"TAIL{idx:02d}",
                    "title": f"Cotton tee tail {idx}",
                    "features": ["cotton", "100% cotton", "Imported"],
                    "description": ["everyday tee"],
                    "categories": ["Clothing", "Men", "T-Shirts"],
                    "details": {"Color": "Black", "department": "mens"},
                    "store": "TailBrand",
                    "price": 18.0,
                    "average_rating": 4.5,
                    "rating_number": 112 - idx,
                }
            )
        path = Path(self.tempdir.name) / "pop_floor.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Men T-Shirts"
        state.add_constraints(["cotton", "color: black", "100% cotton", "imported"], turn=1)
        pool = list(range(len(index)))

        def encode(texts: list[str]) -> list[list[float]]:
            vectors = []
            for text in texts:
                vectors.append([0.0, 1.0] if "quiet" in text.lower() else [1.0, 0.0])
            return vectors

        set_encoder(PoolDenseEncoder(encode=encode))
        open_cfg = replace(PUBLIC, dense_pop_floor=0, dense_rrf_k=0, dense_skip_generic=False)
        floor_cfg = replace(PUBLIC, dense_pop_floor=10, dense_rrf_k=0, dense_skip_generic=False)
        skip_cfg = replace(PUBLIC, dense_pop_floor=0, dense_rrf_k=0, dense_skip_generic=True)
        open_ids = [index.ids[idx] for idx in rank(index, state, open_cfg, pool, limit=10)]
        floor_ids = [index.ids[idx] for idx in rank(index, state, floor_cfg, pool, limit=10)]
        skip_ids = [index.ids[idx] for idx in rank(index, state, skip_cfg, pool, limit=10)]
        self.assertNotIn("COLD", open_ids)
        self.assertIn("COLD", floor_ids)
        self.assertIn("COLD", skip_ids)
        self.assertEqual(set(floor_ids), {f"HOT{idx:02d}" for idx in range(7)} | {"COLD", "TAIL00", "TAIL01"})
        leather = ContestState(session_id="leather")
        leather.category = "Men T-Shirts"
        leather.add_constraints(["leather", "rubber sole"], turn=1)
        distinctive = [index.ids[idx] for idx in rank(index, leather, skip_cfg, pool, limit=10)]
        self.assertNotIn("COLD", distinctive)

    def test_price_bonus_ranks_exact_budget_above_hotter_clone(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Cotton pullover",
                "features": ["cotton"],
                "description": ["layer"],
                "categories": ["Shirts"],
                "details": {"department": "unisex"},
                "store": "HotBrand",
                "price": 24.0,
                "average_rating": 4.9,
                "rating_number": 9000,
            },
            {
                "parent_asin": "TARGET",
                "title": "Cotton pullover",
                "features": ["cotton"],
                "description": ["layer"],
                "categories": ["Shirts"],
                "details": {"department": "unisex"},
                "store": "QuietBrand",
                "price": 20.0,
                "average_rating": 4.8,
                "rating_number": 12,
            },
        ]
        path = Path(self.tempdir.name) / "price_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["cotton", "budget around $20"], turn=1)
        pool = list(range(len(index)))
        priced = ContestConfig(
            gate_size=5,
            hard_filter=True,
            pad_to_top_k=False,
            w_popularity=1.0,
            w_constraint=0.35,
            w_lexical=0.55,
            w_price=0.85,
        )
        popular_first = [index.ids[idx] for idx in rank(index, state, replace(PUBLIC, w_dense=0), pool, limit=2)]
        priced_first = [index.ids[idx] for idx in rank(index, state, priced, pool, limit=2)]
        self.assertEqual(popular_first[0], "HOT")
        self.assertEqual(priced_first[0], "TARGET")

    def test_overlap_margin_defers_close_clones_but_not_a_popularity_blowout(self) -> None:
        def write_rows(counts: list[int], name: str) -> Path:
            rows = []
            for idx, count in enumerate(counts):
                rows.append(
                    {
                        "parent_asin": f"P{idx}",
                        "title": f"Cotton pullover {idx}",
                        "features": ["cotton", "pull on closure", "machine wash"],
                        "description": ["layer"],
                        "categories": ["Shirts"],
                        "details": {"department": "unisex"},
                        "store": f"Store{idx}",
                        "price": 20.0,
                        "average_rating": 4.0,
                        "rating_number": count,
                    }
                )
            path = Path(self.tempdir.name) / name
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            return path

        config = ContestConfig(
            gate_size=5,
            hard_filter=True,
            gate_before_override=True,
            pad_to_top_k=False,
            min_slots_to_recommend=3,
            evidence_pool_cap=20,
            overlap_margin=0.08,
        )
        opening = "I'm looking for shirts. A key requirement is: cotton."
        follow = "For that, what matters is: pull on closure; machine wash."
        close = ContestAgent(write_rows([100] * 8, "close.jsonl"), config=config)
        close.reset("s", {})
        close.respond("s", opening, 1, 10)
        close_second = close.respond("s", follow, 2, 10)
        self.assertEqual(close_second["recommendations"], [])
        self.assertEqual(close_second["ask_attribute"], "other")
        blowout = ContestAgent(write_rows([20000] + [12] * 7, "blowout.jsonl"), config=config)
        blowout.reset("s", {})
        blowout.respond("s", opening, 1, 10)
        blow_second = blowout.respond("s", follow, 2, 10)
        ids = [item["parent_asin"] for item in blow_second["recommendations"]]
        self.assertGreaterEqual(len(ids), 1)
        self.assertEqual(ids[0], "P0")

    def test_strict_override_gate_skips_dump_until_pool_hits_gate(self) -> None:
        rows = []
        for idx in range(12):
            rows.append(
                {
                    "parent_asin": f"P{idx}",
                    "title": f"Cotton pullover {idx}",
                    "features": ["cotton", "pull on closure", "machine wash", "imported"],
                    "description": ["layer"],
                    "categories": ["Shirts"],
                    "details": {"department": "unisex"},
                    "store": f"Store{idx}",
                    "price": 20.0,
                    "average_rating": 4.0,
                    "rating_number": 10 + idx,
                }
            )
        path = Path(self.tempdir.name) / "override_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        dumped = ContestConfig(
            gate_size=5,
            hard_filter=True,
            gate_before_override=True,
            pad_to_top_k=False,
            min_slots_to_recommend=3,
            evidence_pool_cap=8,
            dump_slots=4,
            dump_pool_cap=20,
            strict_override_gate=False,
        )
        strict = ContestConfig(
            gate_size=5,
            hard_filter=True,
            gate_before_override=True,
            pad_to_top_k=False,
            min_slots_to_recommend=3,
            evidence_pool_cap=8,
            dump_slots=4,
            dump_pool_cap=20,
            strict_override_gate=True,
        )
        opening = "I'm looking for shirts. imported"
        follow = "For that, what matters is: cotton; pull on closure."
        override = "Actually, ignore my earlier preference. What I need is: machine wash."

        loose = ContestAgent(path, config=dumped)
        loose.reset("s", {})
        loose.respond("s", opening, 1, 10)
        loose.respond("s", follow, 2, 10)
        loose_hit = loose.respond("s", override, 3, 10)
        self.assertGreaterEqual(len(loose_hit["recommendations"]), 1)

        tight = ContestAgent(path, config=strict)
        tight.reset("s", {})
        tight.respond("s", opening, 1, 10)
        tight.respond("s", follow, 2, 10)
        tight_hit = tight.respond("s", override, 3, 10)
        self.assertEqual(tight_hit["recommendations"], [])
        self.assertEqual(tight_hit["ask_attribute"], "other")

    def test_rerank_tie_break_on_hard_pool_can_outrank_a_hotter_clone(self) -> None:
        rows = [
            {
                "parent_asin": "HOT",
                "title": "Everyday cotton shirt",
                "features": ["ribbed crew neck"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "HotBrand",
                "price": 18.0,
                "average_rating": 4.9,
                "rating_number": 9000,
            },
            {
                "parent_asin": "TARGET",
                "title": "Ribbed crew neck cotton shirt",
                "features": ["ribbed crew neck"],
                "description": ["basic tee"],
                "categories": ["Shirts"],
                "details": {"department": "mens"},
                "store": "QuietBrand",
                "price": 18.0,
                "average_rating": 4.8,
                "rating_number": 12,
            },
        ]
        path = Path(self.tempdir.name) / "rerank_catalog.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        index = ContestIndex(path)
        state = ContestState(session_id="s")
        state.category = "Shirts"
        state.add_constraints(["ribbed crew neck"], turn=1)

        def score(query: str, docs: list[str]) -> list[float]:
            del query
            return [0.1 if "everyday" in doc else 0.9 for doc in docs]

        set_reranker(PoolReranker(score=score))
        reranked = ContestConfig(
            gate_size=5,
            hard_filter=True,
            pad_to_top_k=False,
            w_popularity=1.0,
            w_constraint=0.35,
            w_lexical=0.55,
            w_rerank=0.85,
            rerank_pool_limit=80,
        )
        popular_first = [
            index.ids[idx]
            for idx in rank(
                index,
                state,
                replace(PUBLIC, w_dense=0, w_rerank=0, w_phrase=0.0),
                list(range(len(index))),
                limit=2,
            )
        ]
        rerank_first = [index.ids[idx] for idx in rank(index, state, reranked, list(range(len(index))), limit=2)]
        self.assertEqual(popular_first[0], "HOT")
        self.assertEqual(rerank_first[0], "TARGET")

    def test_classmate_gate_can_withhold_recommendations(self) -> None:
        agent = ContestAgent(self.catalog_path, config=CLASSMATE)
        agent.reset("s", {})
        response = agent.respond("s", "I'm looking for shirts, but I'm still exploring.", 1, 10)
        self.assertEqual(response["ask_attribute"], "other")
        # Tiny fixture pools are small enough that the gate may still fire
        # recs; the contract is only that IDs stay valid when present.
        ids = [item["parent_asin"] for item in response["recommendations"]]
        self.assertTrue(set(ids) <= {"A", "B", "C"})
