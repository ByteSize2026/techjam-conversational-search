from __future__ import annotations

import json
import unittest

from starter.shopping_agent import (
    AgentConfig,
    BackendResponse,
    CatalogRepository,
    ConstraintMutation,
    IntentUpdate,
    IntentInterpreter,
    ModelCompletionResult,
    SessionState,
    StateReducer,
    TieredModelClient,
    profile_intent_update,
    parse_intent_update,
    validate_canonical_json,
    validate_intent_json,
)


class _FakeBackend:
    name = "local-test"

    def __init__(self, payload: object, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0
        self.messages: list[object] = []

    def complete(self, messages, **_kwargs):
        self.calls += 1
        self.messages.append(messages)
        if self.error is not None:
            raise self.error
        return BackendResponse(
            content=json.dumps(self.payload),
            usage={"prompt_tokens": 4, "completion_tokens": 3},
        )


class IntentInterpreterTests(unittest.TestCase):
    @staticmethod
    def _catalog() -> CatalogRepository:
        return CatalogRepository(
            None,
            records=[
                {
                    "parent_asin": "A-CLOG",
                    "title": "Mojo walking clog",
                    "categories": ["Shoes", "Clogs & Mules"],
                    "features": ["Breathable mesh upper", "Lightweight sole"],
                    "details": {
                        "Color": "Blue",
                        "Material": "Mesh",
                        "Size": "8",
                    },
                    "store": "Roamans",
                },
                {
                    "parent_asin": "A-DRESS",
                    "title": "Everyday dress",
                    "categories": ["Clothing", "Dresses"],
                    "features": ["Machine washable"],
                    "details": {"Color": "Black"},
                    "store": "Skechers",
                },
            ],
        )

    def test_catalog_builds_field_scoped_attribute_indexes(self) -> None:
        repository = self._catalog()

        brand = repository.resolve_attribute_value("brand", "Roamans")
        feature = repository.resolve_attribute_value("feature", "breathable")

        self.assertEqual(brand, ("roamans", ("A-CLOG",)))
        self.assertEqual(feature, ("breathable", ("A-CLOG",)))
        self.assertIsNone(repository.resolve_attribute_value("feature", "mojo"))
        self.assertEqual(
            repository.resolve_attribute_value("title_token", "mojo"),
            ("mojo", ("A-CLOG",)),
        )

    def test_natural_language_numeric_and_title_facts_use_typed_feature_syntax(self) -> None:
        messages = {
            "Please look for something rated 4.6 stars or higher.": "rating: 4.6 stars or higher",
            "I prefer a product with 250+ reviews.": "rating_count: 250 or more",
            "I remember seeing mojo in the product name.": "title contains: mojo",
        }
        for message, expected in messages.items():
            with self.subTest(message=message):
                update = parse_intent_update(message, turn=1)
                self.assertTrue(
                    any(
                        item.attribute == "feature" and item.value == expected
                        for item in update.mutations
                    )
                )

    def test_profile_tags_become_one_catalog_grounded_soft_fact(self) -> None:
        repository = self._catalog()
        cases = {
            "Please prioritize the Roamans label": ("brand", "roamans"),
            "I remember seeing mojo in the product name": (
                "feature",
                "title contains: mojo",
            ),
            "I prefer a product with 250+ reviews": (
                "feature",
                "rating_count: 250 or more",
            ),
        }
        for tag, expected in cases.items():
            with self.subTest(tag=tag):
                update = profile_intent_update(
                    {"preference_tags": [tag]}, repository
                )
                self.assertEqual(
                    [(item.attribute, item.value) for item in update.mutations],
                    [expected],
                )
                self.assertEqual(update.mutations[0].source, "profile")
                self.assertEqual(update.mutations[0].hardness, "soft")

    def test_catalog_grounding_drops_unknown_rule_feature_from_state_update(self) -> None:
        result = IntentInterpreter(
            catalog_repository=self._catalog(),
            enabled=False,
        ).interpret("I need shoes that are moonstone aura.", turn=1)

        self.assertEqual(result.path, "rules")
        self.assertFalse(
            any(item.value == "shoes that are moonstone aura" for item in result.update.mutations)
        )
        self.assertIn("shoes that are moonstone aura", result.update.query_terms)

    def test_catalog_grounding_uses_preference_payload_not_marker_words(self) -> None:
        repository = CatalogRepository(
            None,
            records=[
                {
                    "parent_asin": "SNAP-STYLE",
                    "title": "Snap keychain",
                    "details": {"Closure Type": "Snap"},
                    "features": ["Modern look"],
                },
                {
                    "parent_asin": "LOOK-FEATURE",
                    "title": "Classic item",
                    "features": ["Look"],
                },
                {
                    "parent_asin": "SNAP-FEATURE-1",
                    "title": "Accessory one",
                    "features": ["Snap"],
                },
                {
                    "parent_asin": "SNAP-FEATURE-2",
                    "title": "Accessory two",
                    "features": ["Snap"],
                },
            ],
        )

        result = IntentInterpreter(
            catalog_repository=repository,
            enabled=False,
        ).interpret("Please look for Snap.", turn=1)

        self.assertEqual(
            [(item.attribute, item.value) for item in result.update.mutations],
            [("style", "snap")],
        )
        self.assertFalse(
            any(item.source == "model" for item in result.update.query_evidence)
        )
        self.assertNotIn(("feature", "look"), {
            (item.attribute, item.value) for item in result.update.mutations
        })

    def test_catalog_grounding_does_not_treat_yes_as_the_requested_brand(self) -> None:
        repository = CatalogRepository(
            None,
            records=[
                {
                    "parent_asin": "YES-BRAND",
                    "title": "Unrelated item",
                    "store": "Yes",
                },
                {
                    "parent_asin": "BOYS-DEPARTMENT",
                    "title": "Everyday shirt",
                    "details": {"Department": "Boys"},
                },
            ],
        )

        result = IntentInterpreter(
            catalog_repository=repository,
            enabled=False,
        ).interpret("Yes — please look for Boys.", turn=2)

        self.assertEqual(result.update.query_terms[0], "Boys")
        self.assertIn(
            ("feature", "boys"),
            {(item.attribute, item.value) for item in result.update.mutations},
        )
        self.assertNotIn(
            ("brand", "yes"),
            {(item.attribute, item.value) for item in result.update.mutations},
        )

    def test_referenced_override_parses_only_the_new_catalog_fact(self) -> None:
        repository = CatalogRepository(
            None,
            records=[
                {
                    "parent_asin": "OLD",
                    "title": "Old item",
                    "store": "RALUPOP",
                },
                {
                    "parent_asin": "NEW",
                    "title": "Golf accessory",
                    "features": ["Golf"],
                },
            ],
        )
        message = (
            "Actually, ignore that earlier preference for !RALUPOP. "
            "Please prioritize golf instead."
        )

        update = IntentInterpreter(
            catalog_repository=repository,
            enabled=False,
        ).interpret(message, turn=3).update

        self.assertEqual(update.scope, "referenced_preference_replace")
        self.assertEqual(update.query_terms, ("golf",))
        self.assertEqual(
            [(item.attribute, item.value, item.hardness) for item in update.mutations],
            [("feature", "golf", "hard")],
        )
        self.assertNotIn("ralupop", " ".join(update.query_terms).lower())

    def test_bare_numeric_override_does_not_become_an_unrelated_catalog_slot(self) -> None:
        repository = CatalogRepository(
            None,
            records=[
                {
                    "parent_asin": "COLOR-FIVE",
                    "title": "Numbered item",
                    "details": {"Color": "5"},
                }
            ],
        )
        message = (
            "Actually, ignore that earlier preference for red. "
            "Please prioritize 5.0 instead."
        )

        update = IntentInterpreter(
            catalog_repository=repository,
            enabled=False,
        ).interpret(message, turn=3).update

        self.assertEqual(update.query_terms, ("5.0",))
        self.assertFalse(any(item.attribute == "color" for item in update.mutations))

    def test_natural_rating_query_uses_one_typed_evidence_term(self) -> None:
        update = parse_intent_update(
            "Please look for something rated 4.7 stars or higher.", turn=1
        )

        self.assertEqual(update.query_terms, ("rating: 4.7 stars or higher",))

    def test_positive_value_stays_lexical_while_negated_value_is_removed(self) -> None:
        positive = parse_intent_update(
            "I'm looking for Leather & Faux Leather.", turn=1
        )
        negative = parse_intent_update(
            "I don't want black. Please look for Smiffys.", turn=1
        )

        self.assertIn("Leather", positive.query_terms[0])
        self.assertEqual(positive.category_anchor, "Leather & Faux Leather")
        self.assertFalse(
            any(item.attribute == "material" for item in positive.mutations)
        )
        self.assertNotIn("black", " ".join(negative.query_terms).lower())

    def test_explicit_rule_path_does_not_call_model(self) -> None:
        backend = _FakeBackend(
            {
                "mutations": [
                    {
                        "attribute": "feature",
                        "value": "hallucinated",
                    }
                ],
                "confidence": 1.0,
            }
        )
        interpreter = IntentInterpreter(
            client=TieredModelClient([backend]),
            config=AgentConfig(),
        )

        result = interpreter.interpret(
            "Actually, ignore my earlier preference. What I need is: leather.",
            SessionState("s"),
            turn=3,
        )

        self.assertEqual(result.path, "rules")
        self.assertEqual(result.update.scope, "referenced_preference_replace")
        self.assertEqual(backend.calls, 0)

    def test_ambiguous_message_uses_model_and_keeps_model_values_soft(self) -> None:
        backend = _FakeBackend(
            {
                "mutations": [
                    {
                        "action": "upsert",
                        "attribute": "feature",
                        "value": "breathable",
                        "polarity": "require",
                        "hardness": "hard",
                        "confidence": 1.0,
                    }
                ],
                "query_terms": ["breathable"],
                "confidence": 1.0,
            }
        )
        interpreter = IntentInterpreter(client=TieredModelClient([backend]))

        result = interpreter.interpret("I want shoes that are breathable", turn=1)

        self.assertEqual(result.path, "model")
        self.assertEqual(result.backend, "local-test")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(len(result.update.mutations), 1)
        self.assertEqual(result.update.mutations[0].source, "model")
        self.assertEqual(result.update.mutations[0].hardness, "soft")
        self.assertEqual(result.update.mutations[0].polarity, "prefer")
        self.assertEqual(result.usage, {"prompt_tokens": 4, "completion_tokens": 3})
        self.assertEqual(result.diagnostics["intent_usage"], {"prompt_tokens": 4, "completion_tokens": 3})

    def test_model_first_normalizes_explicit_brand_preference(self) -> None:
        backend = _FakeBackend(
            {
                "mutations": [
                    {
                        "action": "replace",
                        "attribute": "other",
                        "value": "Skechers",
                        "confidence": 0.9,
                    }
                ],
                "confidence": 0.9,
            }
        )
        interpreter = IntentInterpreter(
            client=TieredModelClient([backend]),
            config=AgentConfig(intent_model_enabled=True, intent_model_mode="model_first"),
        )
        result = interpreter.interpret("Yes, please look for Skechers.", turn=2)
        self.assertEqual(result.path, "model")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(result.update.mutations[0].attribute, "brand")
        self.assertEqual(result.update.mutations[0].action, "upsert")
        self.assertTrue(result.update.mutations[0].explicit)

    def test_catalog_accepts_known_model_feature(self) -> None:
        backend = _FakeBackend(
            {
                "mutations": [
                    {
                        "action": "upsert",
                        "attribute": "feature",
                        "value": "breathable",
                        "confidence": 0.9,
                    }
                ],
                "confidence": 0.9,
            }
        )
        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=self._catalog(),
            config=AgentConfig(intent_model_enabled=True, intent_model_mode="model_first"),
        ).interpret("I want a breathable clog.", turn=1)

        self.assertTrue(
            any(
                item.attribute == "feature" and item.value == "breathable"
                for item in result.update.mutations
            )
        )
        self.assertFalse(
            any(item.get("reason") == "catalog_value_not_supported" for item in result.rejected)
        )

    def test_catalog_rejects_unknown_model_value_but_keeps_lexical_evidence(self) -> None:
        backend = _FakeBackend(
            {
                "mutations": [
                    {
                        "action": "upsert",
                        "attribute": "feature",
                        "value": "moonstone aura",
                        "confidence": 0.9,
                    }
                ],
                "confidence": 0.9,
            }
        )
        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=self._catalog(),
            config=AgentConfig(intent_model_enabled=True, intent_model_mode="model_first"),
        ).interpret("Please look for moonstone aura.", turn=1)

        self.assertFalse(
            any(item.value == "moonstone aura" for item in result.update.mutations)
        )
        self.assertIn("moonstone aura", result.update.query_terms)
        self.assertTrue(
            any(item.get("reason") == "catalog_value_not_supported" for item in result.rejected)
        )

    def test_canonicalizer_mode_translates_then_uses_existing_parser(self) -> None:
        backend = _FakeBackend({"canonical_text": "For that, what matters is: category: Clogs & Mules.\nFor that, what matters is: brand: Skechers."})
        interpreter = IntentInterpreter(
            client=TieredModelClient([backend]),
            config=AgentConfig(intent_model_enabled=True, intent_model_mode="canonicalizer"),
        )
        result = interpreter.interpret("我想找 Skechers 的 Clogs & Mules", turn=1)
        self.assertEqual(result.path, "model")
        self.assertTrue(result.accepted[0]["canonical_text"].startswith("For that, what matters is:"))

    def test_canonicalizer_rejects_unknown_field_and_falls_back_atomically(self) -> None:
        with self.assertRaises(ValueError):
            validate_canonical_json(
                {
                    "canonical_text": (
                        "For that, what matters is: product name contains: mojo."
                    )
                }
            )
        backend = _FakeBackend(
            {
                "canonical_text": (
                    "For that, what matters is: product name contains: mojo."
                )
            }
        )
        message = "Please look for Roamans."
        expected = IntentInterpreter(
            catalog_repository=self._catalog(),
            enabled=False,
        ).interpret(message, turn=1).update
        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=self._catalog(),
            config=AgentConfig(intent_model_enabled=True, intent_model_mode="canonicalizer"),
        ).interpret(message, turn=1)

        self.assertEqual(result.path, "rules_fallback")
        self.assertEqual(result.update, expected)
        self.assertTrue(
            any(
                item.attribute == "brand" and item.value == "roamans"
                for item in result.update.mutations
            )
        )
        self.assertTrue(result.failures)

    def test_canonicalizer_rejects_unknown_catalog_value_atomically(self) -> None:
        backend = _FakeBackend(
            {
                "canonical_text": (
                    "For that, what matters is: brand: Roamans.\n"
                    "For that, what matters is: feature: moonstone aura."
                )
            }
        )
        message = "Please look for Roamans with a moonstone aura."
        expected = IntentInterpreter(
            catalog_repository=self._catalog(),
            enabled=False,
        ).interpret(message, turn=1).update
        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=self._catalog(),
            config=AgentConfig(intent_model_enabled=True, intent_model_mode="canonicalizer"),
        ).interpret(message, turn=1)

        self.assertEqual(result.path, "rules_fallback")
        self.assertEqual(result.update, expected)
        self.assertTrue(any(item.attribute == "brand" for item in result.update.mutations))

    def test_canonicalizer_request_failure_keeps_catalog_grounded_fallback(self) -> None:
        backend = _FakeBackend({}, error=TimeoutError("network unavailable"))
        message = "Please look for Roamans."
        expected = IntentInterpreter(
            catalog_repository=self._catalog(),
            enabled=False,
        ).interpret(message, turn=1).update

        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=self._catalog(),
            config=AgentConfig(
                intent_model_enabled=True,
                intent_model_mode="canonicalizer",
            ),
        ).interpret(message, turn=1)

        self.assertEqual(result.path, "rules_fallback")
        self.assertEqual(result.update, expected)
        self.assertTrue(any(item.attribute == "brand" for item in result.update.mutations))

    def test_canonicalizer_rejects_a_value_copied_from_an_earlier_turn(self) -> None:
        repository = CatalogRepository(
            None,
            records=[
                {
                    "parent_asin": "SNAP",
                    "title": "Snap closure item",
                    "features": ["Snap"],
                },
                {
                    "parent_asin": "BOYS",
                    "title": "Everyday shirt",
                    "details": {"Department": "Boys"},
                },
            ],
        )
        backend = _FakeBackend(
            {"canonical_text": "For that, what matters is: feature: Snap."}
        )

        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=repository,
            config=AgentConfig(
                intent_model_enabled=True,
                intent_model_mode="canonicalizer",
            ),
        ).interpret("Yes — please look for Boys.", turn=2)

        self.assertEqual(result.path, "rules_fallback")
        self.assertIn(
            ("feature", "boys"),
            {(item.attribute, item.value) for item in result.update.mutations},
        )
        self.assertFalse(any(item.value.casefold() == "snap" for item in result.update.mutations))

    def test_canonicalizer_fills_slots_without_deleting_local_facts(self) -> None:
        backend = _FakeBackend(
            {"canonical_text": "For that, what matters is: brand: Roamans."}
        )
        message = "Please look for Roamans and keep the price under $80."

        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=self._catalog(),
            config=AgentConfig(
                intent_model_enabled=True,
                intent_model_mode="canonicalizer",
            ),
        ).interpret(message, turn=1)

        self.assertEqual(result.path, "model")
        self.assertEqual(
            {item.attribute for item in result.update.mutations},
            {"brand", "budget"},
        )
        prompt_payload = json.loads(backend.messages[0][-1]["content"])
        self.assertNotIn("state", prompt_payload)

    def test_canonicalizer_does_not_retype_a_locally_grounded_value(self) -> None:
        repository = CatalogRepository(
            None,
            records=[
                {
                    "parent_asin": "SNAP-STYLE",
                    "title": "Snap accessory",
                    "details": {"Closure Type": "Snap"},
                }
            ],
        )
        backend = _FakeBackend(
            {
                "canonical_text": (
                    "For that, what matters is: feature: title contains: Snap."
                )
            }
        )

        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=repository,
            config=AgentConfig(
                intent_model_enabled=True,
                intent_model_mode="canonicalizer",
            ),
        ).interpret("Please look for Snap.", turn=1)

        self.assertEqual(
            [(item.attribute, item.value) for item in result.update.mutations],
            [("style", "snap")],
        )

    def test_canonicalizer_does_not_turn_demographics_into_use_case(self) -> None:
        backend = _FakeBackend(
            {"canonical_text": "For that, what matters is: use_case: mens."}
        )

        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=self._catalog(),
            config=AgentConfig(
                intent_model_enabled=True,
                intent_model_mode="canonicalizer",
            ),
        ).interpret("I'd prefer mens.", turn=1)

        self.assertEqual(result.path, "rules_fallback")
        self.assertFalse(any(item.attribute == "use_case" for item in result.update.mutations))

    def test_canonicalizer_does_not_promote_a_tentative_decoy_clause(self) -> None:
        repository = CatalogRepository(
            None,
            records=[
                {
                    "parent_asin": "CRAVAT",
                    "title": "Formal cravat",
                    "categories": ["Cravats"],
                },
                {
                    "parent_asin": "BACKPACK",
                    "title": "Day backpack",
                    "categories": ["Backpacks"],
                },
            ],
        )
        backend = _FakeBackend(
            {
                "canonical_text": (
                    "For that, what matters is: category: Cravats.\n"
                    "For that, what matters is: category: Backpacks.\n"
                    "For that, what matters is: budget: $50 and under."
                )
            }
        )
        message = (
            "I'm looking for Cravats. I initially thought I wanted $50 and "
            "Under Backpacks, but I am still deciding."
        )

        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            catalog_repository=repository,
            config=AgentConfig(
                intent_model_enabled=True,
                intent_model_mode="canonicalizer",
            ),
        ).interpret(message, turn=1)

        self.assertEqual(result.path, "rules_fallback")
        self.assertEqual(result.update.category_anchor, "Cravats")
        self.assertFalse(
            any(item.value.casefold() == "backpacks" for item in result.update.mutations)
        )

    def test_canonical_negative_slot_is_structured_without_positive_query(self) -> None:
        text = validate_canonical_json(
            {"canonical_text": "I do not want color: black."}
        )
        update = parse_intent_update(text, turn=2)

        self.assertEqual(len(update.mutations), 1)
        self.assertEqual(update.mutations[0].attribute, "color")
        self.assertEqual(update.mutations[0].polarity, "avoid")
        self.assertEqual(update.query_terms, ())

    def test_canonicalizer_calls_model_even_when_rules_are_confident(self) -> None:
        backend = _FakeBackend(
            {"canonical_text": "For that, what matters is: category: Dresses."}
        )
        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            config=AgentConfig(
                intent_model_enabled=True,
                intent_model_mode="canonicalizer",
            ),
        ).interpret("I'm looking for Dresses.", turn=1)
        self.assertEqual(result.path, "model")
        self.assertEqual(result.trigger_reason, "canonicalizer")
        self.assertEqual(backend.calls, 1)

    def test_canonicalizer_cannot_replace_explicit_rule_category_with_old_preference(self) -> None:
        backend = _FakeBackend(
            {
                "canonical_text": (
                    "For that, what matters is: category: Backpacks.\n"
                    "For that, what matters is: budget: $50."
                )
            }
        )
        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            config=AgentConfig(
                intent_model_enabled=True,
                intent_model_mode="canonicalizer",
            ),
        ).interpret(
            "I'm looking for Mid-Calf. I initially thought I wanted $50 "
            "and Under Backpacks, but I am still deciding.",
            turn=1,
        )
        self.assertEqual(result.update.category_anchor, "Mid-Calf")
        self.assertFalse(
            any(item.attribute == "category" for item in result.update.mutations)
        )
        self.assertNotIn("Backpacks", result.update.query_terms)
        self.assertTrue(
            any(item.attribute == "budget" for item in result.update.mutations)
        )

    def test_canonicalizer_multiline_clauses_are_collapsed_for_legacy_parser(self) -> None:
        update = parse_intent_update(
            "For that, what matters is: category: Clogs & Mules.\n"
            "For that, what matters is: brand: Skechers.",
            turn=2,
        )
        self.assertEqual(len(update.mutations), 2)
        self.assertEqual({item.attribute for item in update.mutations}, {"category", "brand"})
        self.assertEqual(update.category_anchor, "Clogs & Mules")
        values = {item.attribute: item.value for item in update.mutations}
        self.assertEqual(values["category"], "Clogs & Mules")
        self.assertEqual(values["brand"], "Skechers")
        self.assertEqual(update.query_terms, ("Clogs & Mules", "Skechers"))

    def test_canonicalizer_budget_and_rating_keep_clean_safe_evidence(self) -> None:
        update = parse_intent_update(
            "A key requirement is: budget: under $80.\n"
            "A key requirement is: rating: 4.5 stars or higher.",
            turn=2,
        )
        values = {item.attribute: item.value for item in update.mutations}
        self.assertEqual(values["budget"], "under $80")
        self.assertEqual(values["feature"], "rating: 4.5 stars or higher")
        self.assertTrue(all(item.hardness == "hard" for item in update.mutations))
        self.assertEqual(
            update.query_terms,
            ("under $80", "rating: 4.5 stars or higher"),
        )

    def test_canonicalizer_review_count_and_title_token_keep_closed_syntax(self) -> None:
        text = validate_canonical_json(
            {
                "canonical_text": (
                    "A key requirement is: rating_count: 250 or more.\n"
                    "For that, what matters is: feature: title contains: mojo."
                )
            }
        )
        update = parse_intent_update(text, turn=2)
        self.assertEqual(
            [(item.attribute, item.value) for item in update.mutations],
            [
                ("feature", "rating_count: 250 or more"),
                ("feature", "title contains: mojo"),
            ],
        )

    def test_canonical_slot_parser_does_not_change_ordinary_prose(self) -> None:
        message = "I prefer a brand: Skechers, but I am still browsing."
        update = parse_intent_update(message, turn=2)
        self.assertIsNone(update.category_anchor)
        self.assertEqual(update.mutations, ())
        self.assertEqual(
            update.query_terms,
            ("I prefer a brand: Skechers", "but I am still browsing"),
        )

    def test_marker_query_evidence_is_not_truncated_by_original_text_spans(self) -> None:
        update = parse_intent_update(
            "For that, what matters is: product name contains: mojo.", turn=2
        )

        self.assertEqual(update.query_terms, ("product name contains: mojo",))

    def test_explicit_model_fact_counts_as_confirmed_attribute(self) -> None:
        state = SessionState("s")
        reducer = StateReducer()
        reducer.apply(
            state,
            parse_intent_update("I prefer shoes.", turn=1),
            turn=1,
        )
        mutation = ConstraintMutation(
            action="upsert", attribute="brand", value="Skechers", source="model", explicit=True
        )
        reducer.apply(state, IntentUpdate(mutations=(mutation,)), turn=2)
        self.assertIn("brand", state.confirmed_attributes)

    def test_model_can_remove_existing_budget_after_explicit_override(self) -> None:
        backend = _FakeBackend(
            {
                "mutations": [
                    {"action": "remove", "attribute": "budget", "value": "", "confidence": 0.9},
                    {"action": "upsert", "attribute": "brand", "value": "Luoika", "confidence": 0.9},
                ],
                "confidence": 0.9,
            }
        )
        state = SessionState("s")
        reducer = StateReducer()
        reducer.apply(state, parse_intent_update("I need something under $50.", turn=1), turn=1)
        result = IntentInterpreter(
            client=TieredModelClient([backend]),
            config=AgentConfig(intent_model_enabled=True, intent_model_mode="model_first"),
        ).interpret(
            "Ignore that earlier budget and prioritize Luoika instead.", state=state, turn=2
        )
        self.assertTrue(any(item.action == "remove" for item in result.update.mutations))

    def test_model_failure_returns_deterministic_update(self) -> None:
        backend = _FakeBackend({}, error=TimeoutError("timed out"))
        interpreter = IntentInterpreter(client=TieredModelClient([backend]))

        result = interpreter.interpret("I want shoes that are breathable", turn=1)

        self.assertEqual(result.path, "rules_fallback")
        self.assertEqual(result.update, parse_intent_update("I want shoes that are breathable", turn=1))
        self.assertEqual(backend.calls, 1)
        self.assertTrue(result.failures)
        self.assertEqual(result.diagnostics["intent_model_failures"][0]["stage"], "request")

    def test_unknown_model_fields_and_catalog_ids_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            validate_intent_json({"evil": True})
        with self.assertRaises(ValueError):
            validate_intent_json(
                {
                    "mutations": [
                        {
                            "action": "upsert",
                            "attribute": "color",
                            "value": "B000123456",
                        }
                    ]
                }
            )

        backend = _FakeBackend({"unexpected": True})
        result = IntentInterpreter(client=TieredModelClient([backend])).interpret(
            "I want shoes that are breathable", turn=1
        )
        self.assertEqual(result.path, "rules_fallback")
        self.assertEqual(result.backend, None)

    def test_model_only_value_can_be_a_semantic_inference(self) -> None:
        backend = _FakeBackend(
            {
                "mutations": [
                    {
                        "attribute": "feature",
                        "value": "waterproof",
                        "polarity": "prefer",
                        "hardness": "soft",
                        "confidence": 1.0,
                    }
                ],
                "confidence": 1.0,
            }
        )
        result = IntentInterpreter(client=TieredModelClient([backend])).interpret(
            "I want shoes that are breathable", turn=1
        )
        self.assertEqual(len(result.update.mutations), 1)
        mutation = result.update.mutations[0]
        self.assertEqual(mutation.source, "model")
        self.assertEqual(mutation.value, "waterproof")
        self.assertEqual(mutation.hardness, "soft")
        self.assertEqual(mutation.polarity, "prefer")
        self.assertFalse(result.rejected)

    def test_remove_preference_retires_constraint_and_query_evidence(self) -> None:
        state = SessionState("s")
        reducer = StateReducer()
        reducer.apply(
            state,
            parse_intent_update("For that, what matters is: color: red.", turn=1),
            turn=1,
        )
        self.assertEqual(state.active_query_terms, ["color: red"])

        update = parse_intent_update("The color no longer matters.", turn=2)
        self.assertEqual(update.mutations[0].action, "remove")
        reducer.apply(state, update, turn=2)

        self.assertEqual(state.active_constraints, [])
        self.assertEqual(state.active_query_terms, [])
        self.assertIn("color", state.no_preference)

    def test_mapping_snapshot_is_bounded_and_does_not_require_session_object(self) -> None:
        backend = _FakeBackend(
            {
                "query_terms": ["breathable"],
                "confidence": 0.8,
            }
        )
        state = {
            "query_terms": ["x" * 1000] * 100,
            "asked_attributes": ["color"],
            "recent_turns": [{"turn": i, "message": "y" * 1000} for i in range(100)],
        }
        result = IntentInterpreter(client=TieredModelClient([backend])).interpret(
            "I want shoes that are breathable", state=state, turn=1
        )
        self.assertEqual(result.path, "model")
        user_payload = json.loads(backend.messages[0][1]["content"])
        self.assertLessEqual(len(user_payload["state"]["query_terms"]), 12)
        self.assertLessEqual(len(user_payload["state"]["recent_turns"]), 4)
        self.assertLessEqual(len(backend.messages[0][1]["content"]), 8000)


if __name__ == "__main__":
    unittest.main()
