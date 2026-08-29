from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evaluator.local_evaluator import catalog_index, evaluate
from evaluator.trace_runner import (
    JsonlRecorder,
    TracingAgentProxy,
    analyze_trace,
    run_trace_evaluation,
    sanitize,
    state_diff,
)

PRODUCT = {
    "parent_asin": "A",
    "title": "Blue cotton running shirt",
    "features": ["breathable"],
    "details": {"material": "cotton"},
    "categories": ["Clothing", "Shirts"],
    "price": 29.0,
}
SAMPLE = {
    "sample_id": "sample-1",
    "scenario_type": "buying",
    "user_profile": {"summary": "likes comfort"},
    "ground_truth": {"parent_asin": "A"},
    "intent_card": {
        "target_category": "running shirt",
        "hard_constraints": ["cotton"],
        "soft_preferences": ["blue"],
    },
    "behavior": {"scenario_type": "buying"},
}


class StaticAgent:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.last_diagnostics: dict[str, object] = {}
        self.config = SimpleNamespace(
            deepseek_api_key=None,
            deepseek_model="deepseek-v4-flash",
            local_base_url=None,
            local_model=None,
            model_timeout_seconds=8.0,
            model_max_tokens=512,
            temperature=0.0,
            tool_max_steps=4,
            tool_timeout_seconds=8.0,
            retrieval_limit=100,
            candidate_limit=30,
        )
        self.action_orchestrator = None
        self.action_planner = None

    def reset(self, session_id: str, user_profile: dict) -> None:
        self.session_id = session_id
        self.profile_fields = sorted(user_profile)

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        if self.raises:
            raise RuntimeError("expected failure")
        self.last_diagnostics = {"execution_mode": "deterministic", "turn": turn}
        return {
            "message": f"unchanged:{user_message}",
            "ask_attribute": None,
            "recommendations": [{"parent_asin": "A"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }


class TraceRunnerTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, set[str], dict, dict]:
        catalog_path = root / "catalog.jsonl"
        dataset_path = root / "public.jsonl"
        catalog_path.write_text(json.dumps(PRODUCT) + "\n", encoding="utf-8")
        dataset_path.write_text(json.dumps(SAMPLE) + "\n", encoding="utf-8")
        catalog_ids, categories, products = catalog_index(catalog_path)
        return catalog_path, dataset_path, catalog_ids, categories, products

    def test_proxy_is_transparent_and_runtime_events_exclude_hidden_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _catalog, _dataset, catalog_ids, categories, products = self._fixture(root)
            direct = evaluate(StaticAgent(), [SAMPLE], catalog_ids, categories, products)
            with JsonlRecorder(root / "events.jsonl", "run") as recorder:
                proxy = TracingAgentProxy(StaticAgent(), recorder, [SAMPLE], catalog_ids)
                traced = evaluate(proxy, [SAMPLE], catalog_ids, categories, products)
            self.assertEqual(traced, direct)
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [item["sequence"] for item in events], list(range(1, len(events) + 1))
            )
            runtime_text = json.dumps(events)
            self.assertNotIn("ground_truth", runtime_text)
            self.assertNotIn("intent_card", runtime_text)
            self.assertNotIn('"behavior"', runtime_text)
            completed = next(
                item for item in events if item["event_type"] == "turn_completed"
            )
            self.assertEqual(completed["payload"]["normalized_recommendations"], ["A"])

    def test_proxy_records_and_rethrows_agent_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _catalog, _dataset, catalog_ids, _categories, _products = self._fixture(root)
            with JsonlRecorder(root / "events.jsonl", "run") as recorder:
                proxy = TracingAgentProxy(
                    StaticAgent(raises=True), recorder, [SAMPLE], catalog_ids
                )
                proxy.reset("session", SAMPLE["user_profile"])
                with self.assertRaisesRegex(RuntimeError, "expected failure"):
                    proxy.respond("session", "hello", 1, 10)
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events[-1]["event_type"], "turn_failed")

    def test_recorder_failure_does_not_replace_current_agent_response(self) -> None:
        class FailingRecorder:
            def append(self, *_args, **_kwargs):
                raise OSError("disk full")

        proxy = TracingAgentProxy(
            StaticAgent(), FailingRecorder(), [SAMPLE, SAMPLE], {"A"}  # type: ignore[arg-type]
        )
        proxy.reset("session", SAMPLE["user_profile"])
        response = proxy.respond("session", "hello", 1, 10)
        self.assertEqual(response["recommendations"], [{"parent_asin": "A"}])
        self.assertIsInstance(proxy.fatal_recorder_error, OSError)
        with self.assertRaisesRegex(RuntimeError, "trace recorder failed"):
            proxy.reset("next", SAMPLE["user_profile"])

    def test_sanitize_redacts_secrets_without_redacting_usage_tokens(self) -> None:
        secret = "secret-value-123"
        value = sanitize(
            {
                "api_key": secret,
                "prompt_tokens": 12,
                "error": f"Authorization: Bearer {secret}",
            },
            secrets=(secret,),
        )
        self.assertEqual(value["api_key"], "[REDACTED]")
        self.assertEqual(value["prompt_tokens"], 12)
        self.assertNotIn(secret, json.dumps(value))

    def test_sanitize_preserves_structured_failure_fields_at_trajectory_depth(self) -> None:
        value = sanitize(
            {
                "diagnostics": {
                    "tool_trajectory": [
                        {
                            "planner": {
                                "failure_stage": "schema",
                                "error_code": "structured_arguments_schema_invalid",
                                "failures": [
                                    {
                                        "backend": "deepseek-api",
                                        "stage": "schema",
                                        "error_code": "structured_arguments_schema_invalid",
                                    }
                                ],
                            }
                        }
                    ]
                }
            }
        )
        planner = value["diagnostics"]["tool_trajectory"][0]["planner"]
        self.assertEqual(planner["failure_stage"], "schema")
        self.assertEqual(
            planner["failures"][0]["error_code"],
            "structured_arguments_schema_invalid",
        )
        self.assertNotIn("[TRUNCATED]", json.dumps(value))

    def test_state_diff_reports_added_removed_and_changed(self) -> None:
        diff = state_diff(
            {"same": 1, "old": 2, "remove": 3},
            {"same": 1, "old": 4, "add": 5},
        )
        self.assertEqual(diff["added"], {"add": 5})
        self.assertEqual(diff["removed"], {"remove": 3})
        self.assertEqual(diff["changed"]["old"], {"before": 2, "after": 4})

    def test_analyzer_separates_planner_fill_and_fallback_hits(self) -> None:
        sources = ("planner_selected", "deterministic_fill", "fallback_pipeline")
        samples = []
        sessions = []
        events = []
        for index, source in enumerate(sources):
            sample_id = f"s{index}"
            target = f"T{index}"
            samples.append(
                {"sample_id": sample_id, "ground_truth": {"parent_asin": target}}
            )
            sessions.append(
                {
                    "sample_id": sample_id,
                    "scenario_type": "buying",
                    "hit": True,
                    "first_hit_turn": 1,
                    "best_rank": 1,
                }
            )
            events.append(
                {
                    "event_type": "turn_completed",
                    "sample_id": sample_id,
                    "session_id": sample_id,
                    "turn": 1,
                    "payload": {
                        "raw_response": {
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1}
                        },
                        "diagnostics": {
                            "recommendation_provenance": [
                                {"parent_asin": target, "rank": 1, "source": source}
                            ]
                        },
                    },
                }
            )
        evaluation = {
            "sample_count": 3,
            "hit_rate_at_10": 1.0,
            "mrr": 1.0,
            "mttc": 1.0,
            "efficiency": 1.0,
            "recommended_technical_score": 1.0,
            "sessions": sessions,
        }
        analysis = analyze_trace(
            events,
            evaluation,
            samples,
            baseline={"recommended_technical_score": 0.75},
        )
        attribution = analysis["recommendation_attribution"]
        self.assertEqual(attribution["pure_planner_hit"], 1)
        self.assertEqual(attribution["deterministic_fill_assisted_hit"], 1)
        self.assertEqual(attribution["fallback_assisted_hit"], 1)
        self.assertEqual(
            analysis["baseline_delta"]["recommended_technical_score"]["absolute"],
            0.25,
        )

    def test_analyzer_classifies_planner_timeout(self) -> None:
        analysis = analyze_trace(
            [
                {
                    "event_type": "turn_completed",
                    "sample_id": "s",
                    "scenario_type": "buying",
                    "session_id": "session",
                    "turn": 1,
                    "payload": {
                        "diagnostics": {
                            "execution_mode": "fallback",
                            "tool_fallback_reason": "planner_timeout",
                        }
                    },
                }
            ],
            {"sessions": []},
            [],
        )
        execution = analysis["execution"]
        self.assertEqual(execution["fallback_reasons"], {"planner.timeout": 1})

    def test_analyzer_uses_flat_error_code_for_legacy_nested_failure(self) -> None:
        analysis = analyze_trace(
            [
                {
                    "event_type": "turn_completed",
                    "sample_id": "s",
                    "scenario_type": "buying",
                    "session_id": "session",
                    "turn": 1,
                    "payload": {
                        "diagnostics": {
                            "execution_mode": "fallback",
                            "tool_trajectory": [
                                {
                                    "action": "planner_error",
                                    "status": "planner_error",
                                    "planner": {
                                        "failure_stage": "structured_decode",
                                        "error_code": "structured_action_multiple",
                                        "failures": [
                                            {
                                                "backend": "deepseek-api",
                                                "stage": "structured_decode",
                                            }
                                        ],
                                    },
                                }
                            ],
                        }
                    },
                }
            ],
            {"sessions": []},
            [],
        )
        execution = analysis["execution"]
        self.assertEqual(
            execution["planner_error_code_distribution"],
            {"structured_action_multiple": 1},
        )

    def test_runner_writes_four_artifacts_and_rejects_data_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, dataset, _ids, _categories, _products = self._fixture(root)
            output = root / "trace"
            secret = "trace-secret-value"
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(
                    "os.environ", {"SHOPPING_AGENT_DEEPSEEK_API_KEY": secret}
                ):
                    result = run_trace_evaluation(
                        catalog_path=catalog,
                        dataset_path=dataset,
                        output_dir=output,
                        agent=StaticAgent(),
                    )
            finally:
                os.chdir(original_cwd)
            self.assertEqual(result["manifest"]["status"], "completed")
            self.assertEqual(
                {item.name for item in output.iterdir()},
                {"manifest.json", "events.jsonl", "evaluation.json", "analysis.json"},
            )
            self.assertEqual(
                json.loads((output / "manifest.json").read_text())["status"],
                "completed",
            )
            artifact_text = "\n".join(
                item.read_text(encoding="utf-8") for item in output.iterdir()
            )
            self.assertNotIn(secret, artifact_text)
            inputs = result["manifest"]["inputs"]
            self.assertIsNotNone(inputs["evaluation_config_sha256"])
            self.assertIsNotNone(inputs["evaluator_sha256"])
            self.assertNotEqual(result["manifest"]["git"]["commit"], "unknown")

        data_output = Path.cwd() / "data" / "trace-test-output"
        with self.assertRaisesRegex(ValueError, "must not be data"):
            run_trace_evaluation(
                catalog_path="unused",
                dataset_path="unused",
                output_dir=data_output,
                agent=StaticAgent(),
            )


if __name__ == "__main__":
    unittest.main()
