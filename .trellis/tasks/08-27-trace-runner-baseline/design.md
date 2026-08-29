# Independent Trace Runner Baseline — Design

## 1. Architecture and Boundaries

Runner 位于官方 evaluator 与真实 Agent 之外，不改变二者公开合同：

```text
catalog + public_set
        |
        v
existing evaluator.local_evaluator.evaluate
        |
        v
TracingAgentProxy ------> JsonlRecorder ------> events.jsonl
        |
        v
starter.agent.Agent
        |
        +---- last_diagnostics / bounded state projection

official result -------------------------------> evaluation.json
events + official result + post-hoc targets ---> analysis.json
run metadata ----------------------------------> manifest.json
```

`TracingAgentProxy` 只代理 `reset/respond`。它在调用前后抓取时间、公开输入/输出、受限状态投影与 `last_diagnostics`，然后原样返回 Agent 响应或重新抛出异常。官方 evaluator 继续独占响应容错、对话推进、命中终止和评分语义。

为了支持逐 planner step 成本和推荐来源，当前 Agent 的内部 diagnostics 做向后兼容增强，但不引入新的公开 response 字段要求：

- `TrajectoryEntry` 增加可选 planner metadata/context summary；旧构造调用继续有效。
- orchestrator 在每个 action/error entry 上记录 backend、合法 token usage、失败摘要和决策前的有界 context，不保存自由文本 rationale。
- Agent 在 diagnostics 中标记每个最终推荐 ID 的 provenance；公开 `recommendations` 不变。

## 2. Trace Contracts

### 2.1 Event envelope

每行 JSON 对象包含：

```json
{
  "schema_version": 1,
  "run_id": "...",
  "sequence": 1,
  "event_type": "turn_completed",
  "sample_index": 0,
  "sample_id": "...",
  "session_id": "...",
  "turn": 1,
  "timestamp": "UTC ISO-8601",
  "payload": {}
}
```

Recorder 为每次 append 分配单调 sequence，写入单行 UTF-8 JSON 后 flush。Recorder 错误在 Runner 边界显式失败，不能静默返回不完整报告；如果未来把 recorder 注入生产 Agent，则必须使用 no-op/safe wrapper，不能破坏公开响应。

### 2.2 State projection

状态投影仅保留分析所需的非敏感、有界字段：intent epoch、category、active constraints、no preference、profile loaded、pending attribute、asked attributes、候选/推荐 ID 上限、计数和 fingerprint。完整 reset profile 不进入 events；reset 只记录允许字段名、是否存在和稳定 hash。

`state_diff` 由 before/after 投影确定性计算；列表以稳定 JSON 值比较，输出 `added/removed/changed`，不依赖对象 repr。

### 2.3 Diagnostics normalization

Proxy 不假设每个 Agent 都实现内部状态或 diagnostics：

- 当前新 Agent：读取 `store.get(session_id)` 与 `last_diagnostics`。
- 其他/fixture Agent：状态投影和 diagnostics 可为空，但公共 I/O Trace 仍有效。
- diagnostics 通过递归有界/JSON-safe 清洗；异常文本经过 secret redaction 和长度限制。

### 2.4 Ground-truth separation

运行期 events 可带 `sample_id/scenario_type` 以便关联，但不写 target、intent card、behavior 或 official hit。Runner 不向 Agent 传递 samples 对象。完成 `evaluate` 后，Analyzer 单独从 dataset 与 evaluation result 读取 target 并生成 `analysis.json`。

## 3. Recommendation Provenance

工具终止路径先记录 planner 请求的 IDs，再执行现有 catalog validation 和 Top-K fill。diagnostics 为最终每个 ID 生成：

```json
{"parent_asin": "...", "rank": 1, "source": "planner_selected"}
```

来源优先级：

1. planner 选择且通过 validation → `planner_selected`
2. deterministic ranker 补齐 → `deterministic_fill`
3. popular fallback 补齐 → `popular_fill`
4. 整个 tool loop fallback 后由旧路径生成 → `fallback_pipeline`

同一 ID 只保留首次来源。ask-user 轮的 preview recommendations 记录为 planner preview 或现有候选，不视为 terminal planner recommendation。Analyzer 以 target 在首次命中轮的 provenance 判定 pure/assisted hit。

## 4. Runner and Analyzer

推荐实现为可测试模块 `evaluator/trace_runner.py`，提供：

- `JsonlRecorder`
- `TracingAgentProxy`
- state/diagnostic sanitizers
- `analyze_trace(...)`
- CLI `main()`

CLI 参数：

```text
--catalog data/catalog.jsonl
--dataset data/public_set.jsonl
--output-dir <required>
--baseline-result <optional evaluator JSON>
```

Runner 使用当前 checkout 的 `starter.agent.Agent`。环境配置仍由 `AgentConfig.from_env()` 读取；Runner 只把 allowlist 中的非敏感配置写入 manifest。benchmark 命令负责在进程启动前 source `.env`。

Analyzer 从 `events.jsonl` 和 official result 计算：

- 官方总体/分场景指标（原样引用）；
- action count/distribution/error rate；
- fallback session/turn rate 与 reason 分布；
- prompt/completion tokens per turn/session 和 reported usage 对账；
- pure planner hit、deterministic-fill-assisted hit、fallback-assisted hit；
- baseline 官方指标的 absolute/relative delta（若提供 baseline result）。

## 5. Compatibility and Failure Handling

- 不修改 `evaluator/local_evaluator.py`；只 import 其公开模块函数。
- 新 diagnostics 字段为可选，不改变 Agent response guard 或 evaluator usage 累计。
- trace 输出路径不得位于 `data/`；CLI 在运行前拒绝明显的数据目录目标。
- 运行中断保留已 flush JSONL；manifest 写 `status=running`，正常结束再原子替换为 `completed`。失败时尽力更新为 `failed` 和清洗后的错误。
- evaluator 捕获的 Agent 异常仍由 evaluator 处理；Proxy 记录后重新抛出。
- benchmark 缺少 backend/credential 时必须在 manifest/analysis 显示实际 `execution_mode`，不能把 offline deterministic run 标为 DeepSeek run。

## 6. Validation and Rollback

- 用 `TemporaryDirectory` 和确定性 fake/scripted planner 覆盖 JSONL、透明性、状态 diff、fallback、usage、provenance 和 target separation。
- 全量现有 unittest 防止 Agent/官方 evaluator 合同回归。
- 对 `evaluator/local_evaluator.py` 和 data hash 做前后检查。
- benchmark 工件只写 `/tmp`，可直接删除；产品代码回滚点是 Runner 新模块、测试以及少量可选 diagnostics 增强。

## 7. Trade-offs

- 选择 proxy + diagnostics，而不是复制 evaluator：保证分数完全沿用官方实现，但 proxy 需要对当前 Agent 的内部状态做可选 introspection。
- 选择结构化 context summary，而不是记录 rationale/完整 prompt：牺牲自由文本解释性，换取隐私、提交安全和更可靠的行为证据。
- 首次只跑一次完整 DeepSeek benchmark：降低成本并快速建立 baseline；稳定性和显著性留给后续优化实验。
