# Independent Trace Runner Baseline — Implementation Plan

## Current Execution Handoff

Snapshot: `2026-08-27 14:31:48 +08`  
Branch: `feat/ecom-agent-architecture`  
Task start HEAD: `254abd07754ffeced73bfbe228616b2e82af80d2`

### Completed

- The independent Trace Runner and its tests/documentation are implemented in
  `evaluator/trace_runner.py`, `tests/test_trace_runner.py`, `README.md`, and
  the task-scoped Agent/orchestrator diagnostics files.
- The final Trellis Phase 2.2 review completed and fixed three findings:
  planner timeouts now retain step/timing evidence and use `planner.timeout`;
  repository provenance and the `data/` output guard no longer depend on the
  caller's working directory; boolean values are rejected as token counts.
- Verification after those fixes: `77/77` unittests passed, prescribed Ruff
  checks passed, `compileall` passed, scoped mypy passed, and
  `git diff --check` passed.
- Frozen catalog/public-set/evaluator/config hashes matched the planning
  evidence, and the official evaluator, frozen data, and evaluator config have
  no task diff. Secret-redaction and target-leakage tests passed.

### Benchmark Currently Paused

- Do **not** start a second benchmark while the paused process below is alive.
- Command:

  ```bash
  python3 -m evaluator.trace_runner \
    --catalog data/catalog.jsonl \
    --dataset data/public_set.jsonl \
    --output-dir /tmp/techjam-trace-agent-baseline-20260827 \
    --baseline-result /tmp/ecom-agent-after-deterministic.json
  ```

- Runtime configuration was checked without printing secrets:
  tool planning is enabled, a DeepSeek key is present, and the selected model
  is `deepseek-v4-flash`.
- The process was paused with `SIGSTOP` after the earlier running snapshot.
  Current PID: `274076`; process state should contain `T`; manifest remains
  `running` because the run has not ended.
- Paused progress: sample `68/200`, `633` flushed events; sample 68 is paused
  after its turn 3 `turn_started` event.
- Run started at `2026-08-27T06:10:56.456532+00:00`.
- Output directory:
  `/tmp/techjam-trace-agent-baseline-20260827`.
- A paused point-in-time copy is stored inside the repository at
  `artifacts/trace-runs/techjam-trace-agent-baseline-20260827/`. This project
  path is Git-ignored and is the preferred place for human inspection.
- The paused process still owns and writes the original `/tmp` directory after
  `SIGCONT`; the project copy will not update automatically. Re-copy/sync the
  original directory after any resumed run makes progress or completes.
- The historical baseline input is copied to
  `artifacts/trace-runs/reference/ecom-agent-after-deterministic.json`.
- Early trace evidence shows that DeepSeek is being called, but many responses
  do not parse as valid tool actions and therefore lead to planner errors and
  deterministic fallback. Treat this as preliminary until all 200 sessions
  finish and `analysis.json` is available.
- Stage report: `research/partial-trace-report.md` contains the current Token,
  latency, action/fallback statistics, representative structured traces, and
  the resume limitation.

Resume the same in-memory process only after the user decides to continue:

```bash
kill -CONT 274076
```

The current Runner does not support restarting from sample 68 after this PID
exits: it requires an empty output directory, creates `events.jsonl`
exclusively, and starts the official evaluator at sample 1.

Resume checks:

```bash
pgrep -af '^python3 -m evaluator\.trace_runner '
ps -p 274076 -o pid=,stat=,etime=,cmd=
python3 -c 'import json, pathlib; p=pathlib.Path("/tmp/techjam-trace-agent-baseline-20260827"); m=json.load((p/"manifest.json").open()); rows=[json.loads(x) for x in (p/"events.jsonl").open() if x.strip()]; r=rows[-1]; print(m.get("status"), len(rows), r.get("sample_index", -1)+1, r.get("turn"), r.get("event_type"))'
```

Completion must be determined using all of these checks:

1. No `python3 -m evaluator.trace_runner` process remains for this output
   directory.
2. `manifest.json` contains `"status": "completed"` (process absence alone is
   not success).
3. Both `evaluation.json` and `analysis.json` exist and parse as JSON.

Use this exact handoff check:

```bash
python3 -c 'import json, pathlib, subprocess; p=pathlib.Path("/tmp/techjam-trace-agent-baseline-20260827"); manifest=json.load((p/"manifest.json").open()) if (p/"manifest.json").exists() else {}; running=subprocess.run(["pgrep", "-af", r"^python3 -m evaluator\.trace_runner "], capture_output=True, text=True).returncode==0; required={name:(p/name).exists() for name in ("evaluation.json", "analysis.json")}; print({"running": running, "manifest_status": manifest.get("status"), "required_results": required, "complete": (not running and manifest.get("status")=="completed" and all(required.values()))})'
```

Interpretation:

- `complete: true` means the benchmark finished successfully.
- `running: true` means a process still owns the run; inspect `ps` status.
  A status containing `T` means it is intentionally paused and will not make
  progress until the user approves `kill -CONT 274076`; `R`/`S` means it is
  actively running or waiting on I/O. Never start a second run in either case.
- `running: false` with manifest status `running` or `failed` means the run was
  interrupted or failed; preserve the partial artifacts and investigate it.

Result files under `/tmp/techjam-trace-agent-baseline-20260827`:

- `evaluation.json`: unchanged official evaluator metrics and per-scenario
  results; this is the source of the official score.
- `analysis.json`: baseline deltas, planner/fallback attribution, action/error,
  token, and latency summaries; this is the primary diagnostic report.
- `manifest.json`: completion status, model/config allowlist, Git/input hashes,
  and run timestamps; use it to verify which configuration produced the run.
- `events.jsonl`: flushed turn-by-turn raw trace for deeper debugging; do not
  summarize final metrics directly from a partial file while the run is active.

### Remaining Work After the Benchmark

1. If PID `274076` is alive with status containing `T`, keep it paused until
   the user decides whether to resume. If it is alive with `R`/`S`, monitor it.
   Do not infer final metrics from partial events.
2. On success, inspect `manifest.json`, `events.jsonl`, `evaluation.json`, and
   `analysis.json`; report the official score, delta from historical
   TechnicalScore `0.743854`, token/latency totals, planner-vs-fallback hit
   attribution, action/error distribution, and scenario breakdown.
3. If the run fails because of credentials, network, quota, or backend
   behavior, preserve the failed manifest and partial events and report the
   exact external blocker. Do not label an offline fallback result as the
   DeepSeek baseline.
4. Run `trellis-update-spec` for any reusable trace/evaluator contracts learned
   from the completed run, then perform Phase 3.4 commit and
   `trellis-finish-work`.

### Worktree Boundary

- Task-related changes include `README.md`, `evaluator/trace_runner.py`, the
  Agent/orchestrator diagnostic files, related tests, and this task directory.
- Existing unrelated dirty items must remain untouched and must not enter the
  task commit: `.trellis/.template-hashes.json`, `.agents/`, `.codex/`, and
  `.trellis/tasks/08-27-robust-evaluator-feasibility/`.
- Benchmark artifacts under `/tmp` are intentionally untracked and must not be
  committed. Future benchmark artifacts should use the Git-ignored project
  path `artifacts/trace-runs/` instead of `/tmp` when no live process already
  owns the old path.

## Phase 1 — Freeze Evidence and Contracts

- [x] 记录起始 commit、dirty worktree、catalog/public-set hash、main 历史最好指标与 `.env` 非敏感配置键。
- [x] 确认 `evaluator/local_evaluator.py`、数据文件和现有无关未跟踪文件不在任务修改范围。
- [x] 以 PRD 的 schema、ground-truth separation、provenance 和 fallback taxonomy 为实现合同。

Rollback gate: 若实现需要改变官方 evaluator 或把 target 传入 Agent，停止并回到设计阶段。

## Phase 2 — Add Internal Diagnostic Granularity

- [x] 向后兼容扩展 `TrajectoryEntry` 的 planner metadata/context summary JSON 表达。
- [x] 在 orchestrator 每个 planner/action/error step 记录 backend、usage、failure、剩余预算和候选/profile 状态，不保存自由文本 rationale。
- [x] 在 Agent tool-loop terminal/fallback 路径记录 planner IDs、最终 IDs 和逐 ID provenance。
- [x] 保持公开 response schema、usage 汇总、fallback 行为和现有 trajectory 字段兼容。

Targeted validation:

```bash
python3 -m unittest tests.test_action_orchestrator tests.test_agent_tool_loop -v
```

Rollback gate: diagnostics 增强若改变推荐顺序、usage 或 fallback 结果，回滚该增强并由 Runner 使用现有字段降级。

## Phase 3 — Implement Independent Runner

- [x] 新增 `evaluator/trace_runner.py`：event envelope、recorder、sanitizer/redaction、state projection/diff、proxy、manifest、Analyzer 和 CLI。
- [x] 直接调用 `load_jsonl`、`catalog_index` 和 `evaluate`；不复制 customer simulator 或指标公式。
- [x] 显式要求 output dir，拒绝 `data/`，逐事件 flush，正常/失败均更新 manifest status。
- [x] 将 official evaluation 原样写入 `evaluation.json`，派生分析写入 `analysis.json`。
- [x] 支持可选 baseline evaluator JSON，并输出官方指标 absolute/relative delta。

## Phase 4 — Tests and Documentation

- [x] 新增 `tests/test_trace_runner.py`，使用 `TemporaryDirectory` 和最小 catalog/sample fixture。
- [x] 覆盖 proxy 透明性、异常重抛、JSONL 可解析/flush、secret redaction、target separation、state diff、recommendation provenance、pure vs assisted hit、baseline delta 和无 diagnostics Agent。
- [x] 扩展 tool-loop tests，验证逐 step usage/context 与 terminal/fallback provenance。
- [x] README 增加 traced evaluation 命令、工件说明、隐私边界和 DeepSeek/offline mode 检查。

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall evaluator starter tests
ruff check evaluator/trace_runner.py starter/shopping_agent/actions.py starter/shopping_agent/orchestrator.py starter/agent.py tests/test_trace_runner.py tests/test_action_orchestrator.py tests/test_agent_tool_loop.py
```

## Phase 5 — Public Benchmark Baseline

- [x] 在不输出值的情况下确认 `.env` 包含 tool-planning/DeepSeek 键。
- [ ] source `.env` 后验证 tool planning 实际启用，运行一次 traced 200-session public evaluator。
- [x] 输出目录使用 `/tmp/techjam-trace-<run-id>`；如存在，`/tmp/ecom-agent-after-deterministic.json` 作为可选 baseline result。
- [ ] 核对 manifest hash/commit/backend、evaluation official metrics、reported token usage 和 analysis token totals。
- [ ] 报告总体/分场景差值、fallback/action/provenance/token 指标，并明确是否建立 baseline；不在本任务中优化 Prompt/策略。

Planned command shape:

```bash
set -a
source .env
set +a
python3 -m evaluator.trace_runner \
  --catalog data/catalog.jsonl \
  --dataset data/public_set.jsonl \
  --output-dir /tmp/techjam-trace-agent-baseline \
  --baseline-result /tmp/ecom-agent-after-deterministic.json
```

External gate: 网络、凭据或配额失败时保留 failed manifest/partial events，记录可复跑命令并停止冒充完整 DeepSeek baseline。

## Phase 6 — Final Review

- [x] 运行 Trellis check，复核 spec、lint、tests、数据流、target leakage、secret redaction 和官方 evaluator hash。
- [ ] 检查 `git diff` 只包含任务相关代码、测试、文档和 Trellis 工件；不纳入 `.env`、data、benchmark outputs 或原有无关未跟踪文件。
- [ ] 根据实现中新学到的跨任务合同更新 `.trellis/spec/`，或明确现有 spec 已覆盖无需更新。
- [ ] 提交前向用户交付工件路径、指标、限制和下一步优化建议。
