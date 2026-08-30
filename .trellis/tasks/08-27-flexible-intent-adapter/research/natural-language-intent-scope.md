# Research: Natural-Language Intent Scope for the Competition

## Question

在当前 Agent 已能处理 metadata-derived 模板的情况下，是否应加入模型以支持更灵活的自然语言意图识别；如果加入，怎样避免超出 `docs/` 约束或损害正式评分可复现性？

## Evidence Reviewed

- `docs/competition_specification.md`
- `docs/submission_rules.md`
- `docs/agent_api_contract.json`
- `docs/evaluation_config.json`
- `evaluator/local_evaluator.py`
- `starter/agent.py`
- `starter/shopping_agent/state.py`
- `starter/shopping_agent/response.py`
- `starter/shopping_agent/semantic_ranking.py`
- `starter/shopping_agent/model.py`
- `starter/shopping_agent/config.py`
- `tests/`
- `results.json`
- `.trellis/tasks/08-25-shopping-copilot-research/`

## Confirmed Competition Boundary

1. 目标是 exact `parent_asin`，最多 10 轮；自然语言体验本身不产生正确性分数。
2. `ask_attribute` 是模拟器的交互控制面，只能使用十类属性或 `null`。
3. 用户消息来自 catalog metadata 派生的隐藏 intent card；公开资料不承诺真实开放世界需求。
4. query rewriting、structured state、intent override、dynamic context、semantic reranking 和合法模型都在范围内。
5. catalog modification、full-model training、multimodal 和 heavy vector database 超出范围。
6. 最终评分可能无网络，任何 API 路径都必须有声明和 fallback。

## Current Implementation Findings

- `parse_intent_update` 已能处理固定 marker、预算、少量颜色/材质词、no-preference、boundary 和 override 模板。
- 解析词表有限，否定 polarity、删除偏好、跨属性局部替换和指代没有形成完整规则路径。
- `ConstraintMutation` 已具备 `upsert/replace/remove` 与 `prefer/avoid/require` 数据能力，但 parser 尚未充分利用。
- `StateReducer` 将普通 override 当作 global reset；类别被保留，但约束、query terms、asked state、进度和推荐历史都会被清空。
- `query_terms` 缺少轮次和来源，无法只移除已被覆盖的旧偏好。
- 现有 LLM 只在候选生成后做 Top-30 semantic rerank；它不能修复 parser/reducer 已经错误丢失的意图证据。
- 结构化候选池通常仍包含目标；当前低分更集中在候选缩小、排序和 commit 时机，而不是目录合法性。

## Metric Evidence

当前 `results.json`：

| Metric | Value |
|---|---:|
| Hit Rate@10 | 0.990000 |
| MRR | 0.775411 |
| MTTC | 3.135000 |
| TechnicalScore | 0.884923 |
| Model usage | 0 |

场景 Hit Rate@10：

| Scenario | Hit Rate@10 |
|---|---:|
| Browsing | 1.000000 |
| Buying | 1.000000 |
| Boundary | 1.000000 |
| Intent Override | 0.933333 |

最新归档评测只有 `public_0013` 与 `public_0071` 两个 miss，且都属于 Intent Override，因此它仍是最明确的剩余改进方向。代表性诊断中，普通 override 清空已收集的细粒度条件后，会让候选池重新扩大，例如从 29 增至 244。模型不是唯一解决方案；provenance-aware scoped override 是更低风险的首要修复。

这些数值来自已完成任务 `.trellis/tasks/archive/2026-08/08-26-deterministic-dialogue-ranking/implement.md` 和 workspace journal 的一致记录。仓库根目录 `results.json` 仍保存更早的 `0.743854` 结果，属于 stale artifact；实施 Phase 1 必须重新生成独立基线文件确认当前锚点。

## Options Considered

### A. 不加模型，只扩展规则

优点：离线、快速、确定、最容易保持分数。

缺点：面对未知 paraphrase、多子句和指代时覆盖有限。

结论：必须作为基础和 fallback，但不完全满足“灵活自然语言”目标。

### B. 每轮调用 LLM 解析完整意图

优点：实现直观、开放表达覆盖广。

缺点：增加延迟、token、格式错误与状态漂移；正式评分可能禁网；简单模板也支付成本。

结论：不采用。

### C. 规则优先、难例触发模型、严格 JSON 校验

优点：把模型用于规则最弱的作用域/否定/指代问题；保留离线 fallback；能独立消融。

缺点：需要 trigger、merge、validator 和 provenance 设计。

结论：推荐。

### D. 商品语义卡、场景本体和新向量系统

优点：更像通用真实购物助手。

缺点：偏离 metadata-derived evaluator；可能接近 catalog modification/heavy infrastructure；资产、延迟和复现成本高。

结论：本任务明确排除，赛后或独立产品路线再评估。

## Recommendation

把功能命名和实现边界限定为 `Flexible Natural-Language Intent Adapter`：

- 只理解用户消息；
- 只输出现有允许属性的结构化增量与 query rewrite；
- 先修 scoped override 和 provenance；
- 模型只处理低置信度难例；
- 模型不可用时无缝回退；
- 不改变商品目录、候选 ID 边界或响应协议。

该范围同时覆盖官方鼓励的 structured state、intent override、dynamic context、query rewriting、low latency/fallback，并避免把比赛项目扩张成通用购物平台。

## Validation Strategy

1. 用 unit fixtures 验证语言行为，不使用 ground truth ID 训练 parser。
2. 用公共 evaluator 验证总体与场景非退化。
3. 对 scoped override 做逐会话候选池与 rank 诊断。
4. 对 `rules_only / intent_model_only / reranker_only / both` 做固定配置消融。
5. 报告延迟、调用率、fallback 和 usage；不要只报告最高 TechnicalScore。

## Residual Unknowns

- 私有 paraphrase 的具体语言分布。
- 官方最终硬件、网络、模型资产大小和 turn timeout。
- 本地小模型是否值得打包；需在后续实现阶段通过基准而不是计划阶段预设。
- 当前架构尚未运行 dense/LLM 的同口径完整实验。外部对照架构的 `structured ≈ 0.9435`、`LLM+dense ≈ 0.9445` 只能说明该外部实现中的边际差异，不能用于预测本架构加入 embedding 或模型后的收益。
