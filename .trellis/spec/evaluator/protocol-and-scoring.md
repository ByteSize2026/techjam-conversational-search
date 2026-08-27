# 评估协议与评分

## 对话循环

`evaluator/local_evaluator.py:evaluate` 对每个样本执行：生成随机会话 ID，调用 `agent.reset(session_id, sample["user_profile"])`，生成首条顾客消息，再最多循环 `MAX_TURNS = 10` 次调用 `agent.respond(session_id, user_message, turn, TOP_K)`，其中 `TOP_K = 10`。首次有效命中即结束；否则由 `customer_reply` 构造下一条消息。

公开协议的四种场景是 `buying`、`browsing`、`intent_override`、`boundary`，配置来源为 `docs/evaluation_config.json:scenario_metrics`。Buying 会较早披露硬约束；Browsing 起始需求模糊；Intent Override 在第 3 或第 4 轮替换先前偏好；Boundary 可对所问属性回答没有偏好。`evaluate` 以 `override_applied` 阻止 Intent Override 在新意图到达前转化。

## 响应容错与推荐过滤

`evaluate` 捕获 `respond` 异常；非字典响应或 `message` 非字符串也会降级为 `{ "message": "", "ask_attribute": None, "recommendations": [] }`。不要将“评估器没有抛错”误认为输出合格。

`normalize_recommendations(payload, catalog_ids)` 的规则是：

1. 非列表直接返回空列表。
2. 列表项可以是字典（读取 `parent_asin`）或裸值；值会转成字符串并去除首尾空白。
3. 空 ID、重复 ID、catalog 中不存在的 ID 被丢弃。
4. 保留首次出现顺序，至多返回 `TOP_K` 即 10 个 ID。

因此 `docs/agent_api_contract.json` 的 recommendations 上限 100 与计分上限不同。`tests/test_evaluator.py:test_normalization_preserves_first_valid_unique_order` 锁定了“首个有效唯一项顺序保留”的行为。命中采用 `target in ranked`，即 `parent_asin` 的精确匹配，不按标题或语义相似性匹配。

## 指标与权重

`metric_summary` 和 `evaluate` 的实际定义与 `docs/evaluation_config.json` 一致：

- **Hit@10**：命中会话数除以总会话数。
- **MRR**：每个命中的 `1 / target_rank` 取均值；miss 为 0。
- **MTTC**：首命中轮数取均值；miss 必须按 **11** 轮计入，即 `MAX_TURNS + 1`。
- **Efficiency**：`clip((11 - MTTC) / 10, 0, 1)`。
- **recommended_technical_score**：`0.50 × Hit@10 + 0.30 × MRR + 0.20 × Efficiency`。

评分同时按四个场景分组报告。`reported_token_usage` 会累计合法的 usage 整数，但 `docs/competition_specification.md` 说明 token 用量与延迟是可行性信息，不改变上述核心技术分。`tests/test_evaluator.py:test_metric_summary_assigns_turn_11_to_miss` 明确验证 miss=11 的 MTTC 行为。

## 修改边界

只要变更涉及 `MAX_TURNS`、`TOP_K`、`ALLOWED_ATTRIBUTES`、`normalize_recommendations`、`customer_reply`、`metric_summary` 或 `evaluate`，就同时核查 API 合同、配置、测试和竞赛规则。普通 Agent 改动不应改变这些符号；影响分析见 [跨层影响](../guides/change-impact.md)。

## Scenario: manifest 子集结果比较

### 1. Scope / Trigger

使用 `python -m tests.benchmarks.qwen_reranker compare` 比较由 `--sample-limit` 产生的 smoke 子集时，comparison 必须使用同一个 limit；否则它会按完整 manifest split 校验并错误报告缺失样本。

### 2. Signatures

```text
compare --baseline <json> --reranked <json> \
  --manifest <json> --split <name> [--sample-limit <N>]
```

Python 接口 `compare_result_data(...)`、`compare_result_files(...)` 和内部 `_metrics_for_result(...)` 均接受 `sample_limit: int | None`。

### 3. Contracts

- `sample_limit=None`：要求结果覆盖所选 split 的全部 ID。
- `sample_limit=N`：按 manifest 中的稳定顺序只校验前 N 个 ID，并只聚合这些 session。
- baseline、reranked 和 compare 必须使用相同的 manifest、split 与 sample limit。
- comparison JSON 必须回写 `sample_limit`，便于审计结果范围。

### 4. Validation & Error Matrix

- `sample_limit < 0` → `ValueError("sample_limit must be non-negative")`。
- 结果缺少所选范围内的 ID → `ValueError("result is missing ... IDs")`。
- 命名 split 没有 session 明细 → 拒绝只从聚合指标推断。

### 5. Good/Base/Bad Cases

- Good：baseline、rerank、compare 都使用 validation + limit 5。
- Base：三者都使用完整 validation，不传 limit。
- Bad：baseline/rerank 使用 limit 5，而 compare 按完整 validation 40 校验。

### 6. Tests Required

- 断言 limit 1 只聚合 manifest 的首个 ID。
- 断言省略 limit 时同一单样本结果因缺少其余 split ID 而失败。
- 断言 CLI 能解析并传递 `--sample-limit`。

### 7. Wrong vs Correct

```bash
# Wrong: smoke 结果对完整 split 做 comparison
compare --manifest manifest.json --split validation

# Correct: comparison 与两个输入使用同一子集
compare --manifest manifest.json --split validation --sample-limit 5
```
