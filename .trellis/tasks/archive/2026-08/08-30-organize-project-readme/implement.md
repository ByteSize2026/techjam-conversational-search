# Implementation Plan

## 1. Add profile configuration

- [x] Add the closed `official` / `natural_language` profile setting and normalization to `starter/shopping_agent/config.py`.
- [x] Read `SHOPPING_AGENT_PROTOCOL_PROFILE` in `AgentConfig.from_env()` with `official` as the safe default.
- [x] Add focused config tests for defaults, valid values and invalid values.

## 2. Select protocol adapters in the Agent facade

- [x] Add keyword-only `protocol_profile` to `Agent.__init__` without changing `reset` or `respond`.
- [x] Resolve explicit argument over `AgentConfig` and record the result in diagnostics.
- [x] Select the frozen official adapter, legacy-ordered `protocol_aware` clarification,
  official structured-filter fields, and no natural-language profile grounding for `official`.
- [x] Select `IntentInterpreter` plus `catalog_entropy` clarification for `natural_language`.
- [x] Preserve explicit interpreter and clarification-policy injection precedence.
- [x] Keep reset profile projection and the downstream reducer/retrieval/ranking/commit pipeline shared.
- [x] Add `--protocol-profile {official,natural_language}` to the official evaluator CLI and pass it into Agent construction.
- [x] Add the same option to the natural-language benchmark `evaluate` CLI, pass it through `SubprocessAgent` and the worker, and fail clearly when a non-official profile is requested from an Agent that does not support it.
- [x] Preserve legacy external-Agent loading for the `official` profile.

## 3. Add regression coverage

- [x] Test zero-argument equivalence to `official`.
- [x] Test natural-language component selection and diagnostics.
- [x] Test injection precedence and invalid profile handling.
- [x] Test that profile selection does not change retrieval/ranking/commit component types, config, thresholds or public response fields.
- [x] Test both CLI propagation paths and benchmark compatibility with a legacy Agent lacking the new constructor keyword.
- [x] Run focused agent contract, evaluator and intent-interpreter tests.

## 4. Update documentation

- [x] Update `README.md` with the main source directory, unique entry point and explicit profile examples.
- [x] Expand the repository structure section for evaluator, tests, data, docs, notebooks, holdout, report, scripts and Trellis.
- [x] State that profiles share retrieval/ranking/commit and that no branch merge is performed by this task.
- [x] Update model-backend or architecture docs only if required to keep configuration reference accurate.

## 5. Validate both evaluators

- [x] Run `python3 -m unittest discover -s tests -v` in the Agent repository.
- [x] Run `python3 -m unittest discover -s tests -v` in the natural-language benchmark repository.
- [x] Run a frozen offline smoke evaluation with `--protocol-profile natural_language`, writing output under `/tmp`.
- [x] Inspect git diff to confirm evaluator scoring, retrieval, ranking and commit implementations were not profile-forked.

## Rollback Points

- After step 1: remove the additive config field/parser if configuration compatibility fails.
- After step 2: remove only profile selection branches; shared pipeline modules remain untouched.
- Before documentation: require both profile-focused tests to pass.
- Before completion: do not merge or rebase `main`; only report whether the current branch is ready for a separate promotion decision.

## Validation Evidence

- Agent repository: `148` unittest cases passed after final edits.
- Natural-language benchmark: `37` unittest cases passed.
- Official public set (`200` samples, explicit `official`): Hit@10 `1.0`, MRR
  `0.805024`, MTTC `3.005`, recommended technical score `0.901407`, no evaluator
  errors. These exactly match the pre-natural-language reference in `results.json`.
- Natural-language frozen smoke (`7` samples, explicit `natural_language`): Hit@10
  `1.0`, exact Top-1 `0.429`, MRR `0.631`, MTTC `1.71`; turn diagnostics recorded
  `natural_language`.
- `git diff --check` passed in both repositories. Retrieval, ranking, commit-policy and
  evaluator scoring implementations remain shared and unchanged; clarification policy and
  structured-pool field admission were parameterized at the protocol boundary.
