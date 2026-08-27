from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from starter.shopping_agent import (
    AgentConfig,
    BackendResponse,
    DeepSeekAPIBackend,
    LLMSemanticRanker,
    LocalOpenAIBackend,
    TieredModelClient,
)


class _FakeBackend:
    def __init__(self, name: str, response: object = None, error: Exception | None = None):
        self.name = name
        self.response = response
        self.error = error
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class _Candidate:
    def __init__(self, parent_asin: str, title: str):
        self.parent_asin = parent_asin
        self.title = title

    def compressed(self):
        return {"parent_asin": self.parent_asin, "title": self.title}


class ModelFallbackTests(unittest.TestCase):
    def test_api_success_returns_backend_and_real_usage(self):
        api = _FakeBackend(
            "deepseek-api",
            BackendResponse(
                content=json.dumps({"ok": True}),
                usage={"prompt_tokens": 7, "completion_tokens": 3},
            ),
        )
        result = TieredModelClient([api]).complete_json([{"role": "user", "content": "x"}], lambda value: value)

        self.assertTrue(result.success)
        self.assertEqual(result.backend, "deepseek-api")
        self.assertEqual(result.value, {"ok": True})
        self.assertEqual(result.usage, {"prompt_tokens": 7, "completion_tokens": 3})
        self.assertEqual(result.failures, ())

    def test_invalid_api_json_falls_back_to_local(self):
        api = _FakeBackend(
            "deepseek-api",
            BackendResponse(
                content="not-json",
                usage={"prompt_tokens": 100, "completion_tokens": 100},
            ),
        )
        local = _FakeBackend(
            "local-model",
            BackendResponse(
                content=json.dumps({"source": "local"}),
                usage={"prompt_tokens": 5, "completion_tokens": 2},
            ),
        )
        result = TieredModelClient([api, local]).complete_json([], lambda value: value)

        self.assertEqual(result.backend, "local-model")
        self.assertEqual(result.value, {"source": "local"})
        self.assertEqual(result.usage, {"prompt_tokens": 5, "completion_tokens": 2})
        self.assertEqual(api.calls, 1)
        self.assertEqual(local.calls, 1)
        self.assertEqual(result.failures[0]["stage"], "json")

    def test_all_backends_fail_and_validator_failure_is_diagnostic(self):
        request_failure = _FakeBackend("deepseek-api", error=TimeoutError("timed out"))
        invalid = _FakeBackend("local-model", BackendResponse(content=json.dumps({"wrong": True})))
        result = TieredModelClient([request_failure, invalid]).complete_json(
            [], lambda value: value.get("required") if isinstance(value, dict) else False
        )

        self.assertFalse(result.success)
        self.assertIsNone(result.backend)
        self.assertIsNone(result.usage)
        self.assertEqual(result.attempted_backends, ("deepseek-api", "local-model"))
        self.assertEqual([failure.stage for failure in result.failures], ["request", "validator"])

    def test_no_backend_is_offline(self):
        with patch.dict(
            os.environ,
            {
                "SHOPPING_AGENT_DEEPSEEK_API_KEY": "",
                "SHOPPING_AGENT_LOCAL_BASE_URL": "",
                "SHOPPING_AGENT_LOCAL_MODEL": "",
            },
            clear=False,
        ):
            config = AgentConfig.from_env()
        client = TieredModelClient.from_config(config)
        self.assertEqual(client.backend_names, ())
        result = client.complete_json([], lambda value: value)
        self.assertFalse(result.success)
        self.assertIsNone(result.usage)
        self.assertEqual(result.failures[0]["stage"], "configuration")

    def test_ranker_repairs_unknown_duplicate_and_omitted_ids(self):
        backend = _FakeBackend(
            "deepseek-api",
            BackendResponse(
                content=json.dumps(
                    {
                        "ranked_parent_asins": ["unknown", "B", "B"],
                        "scores": {"B": 0.9, "unknown": 1.0},
                    }
                ),
                usage={"prompt_tokens": 9, "completion_tokens": 4},
            ),
        )
        ranker = LLMSemanticRanker(TieredModelClient([backend]), candidate_limit=3)
        candidates = [
            _Candidate("A", "first"),
            _Candidate("B", "second"),
            _Candidate("C", "third"),
        ]

        result = ranker.rank("shoes for walking", candidates)

        self.assertEqual(result.ordered_parent_asins, ("B", "A", "C"))
        self.assertEqual(result.scores, {"B": 0.9})
        self.assertEqual(result.backend, "deepseek-api")
        self.assertEqual(result.usage, {"prompt_tokens": 9, "completion_tokens": 4})
        self.assertEqual(backend.calls, 1)

    def test_ranker_accepts_products_list_schema(self):
        backend = _FakeBackend(
            "local-model",
            BackendResponse(
                content=json.dumps(
                    {
                        "products": [
                            {"parent_asin": "C", "title": "third"},
                            {"parent_asin": "A", "title": "first"},
                        ]
                    }
                ),
                usage={"prompt_tokens": 3, "completion_tokens": 2},
            ),
        )
        ranker = LLMSemanticRanker(TieredModelClient([backend]), candidate_limit=3)
        candidates = [_Candidate("A", "first"), _Candidate("B", "second"), _Candidate("C", "third")]
        result = ranker.rank("shoes", candidates)
        self.assertEqual(result.ordered_parent_asins, ("C", "A", "B"))
        self.assertEqual(result.backend, "local-model")

    def test_empty_content_uses_reasoning_content(self):
        from starter.shopping_agent.model import _decode_json, _response_content

        content, _usage = _response_content(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": '{"ranked_parent_asins":["A"]}',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }
        )
        self.assertEqual(_decode_json(content), {"ranked_parent_asins": ["A"]})

    def test_decode_json_extracts_object_from_prose(self):
        from starter.shopping_agent.model import _decode_json

        decoded = _decode_json('Sure.\n{"ranked_parent_asins":["B","A"],"scores":{"B":1}}\n')
        self.assertEqual(decoded["ranked_parent_asins"], ["B", "A"])

    def test_deepseek_payload_disables_thinking_and_forces_json(self):
        extra = DeepSeekAPIBackend("sk-test")._extra_body()
        self.assertEqual(extra["thinking"], {"type": "disabled"})
        self.assertEqual(extra["response_format"], {"type": "json_object"})

    def test_local_payload_forces_json_object(self):
        extra = LocalOpenAIBackend("http://127.0.0.1:11434/v1", "qwen2.5:3b")._extra_body()
        self.assertEqual(extra["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
