# 实现灵活自然语言意图解析器

## Goal

在现有 Shopping Agent 中实现一个规则优先、模型可选、严格校验、可离线降级的意图解析器，把用户自然语言转换为现有 `IntentUpdate` / `ConstraintMutation`，重点正确处理否定、局部修改、多条件、强弱约束与 Intent Override，不改变 catalog、retrieval、ranking 或官方响应协议。

## Current State

- 当前解析入口为 `starter/shopping_agent/state.py:parse_intent_update`，主要依赖固定英文模板、正则与词表。
- 数据结构已经支持 `upsert/replace/remove`、`prefer/avoid/require` 和 `soft/hard`，但解析器没有完整利用。
- 普通 override 当前被处理成全局 reset，容易丢失仍有效的约束和 query evidence。
- 2026-08-28 使用当前 `starter.agent.Agent`、冻结 catalog 与 200 条公开集复跑的确定性基线为：TechnicalScore `0.901407`、Hit@10 `1.000000`、MRR `0.805024`、MTTC `3.005000`、usage `0`；四个场景均为 Hit@10 `1.000000`，其中 Intent Override 的 MRR 为 `0.821429`、MTTC 为 `4.733333`。
- 先前提到的 `LLM + dense ≈ 0.9445` 来自外部对照架构，不是当前代码的实验结果。当前架构尚未完成 dense/LLM 同口径评测，本任务不得据此推断模型或 embedding 收益。

## Requirements

### R1. 稳定解析契约

- 新增窄职责 `IntentInterpreter`，输入当前消息、轮次和有界 session snapshot，输出 validated `IntentUpdate` 与解析诊断。
- 保留 `parse_intent_update(...)` 兼容入口。
- `StateReducer` 继续作为唯一会修改 session state 的组件。

### R2. 确定性自然语言覆盖

- 识别否定与排除：`not red`、`anything but leather`。
- 识别删除偏好：`color no longer matters`。
- 识别局部替换：`keep the style, change the budget to under $50`。
- 识别多条件：`black, under $80, suitable for running`。
- 区分硬约束与软偏好：`must` / `need` / `require` 对比 `preferably` / `ideally`。
- 对无法安全解析的指代保留为软 query evidence，不编造硬约束。

### R3. Scoped override

- 区分 `attribute_replace`、`referenced_preference_replace` 与 `global_reset`。
- 官方 override 模板默认只替换被引用的旧偏好，保留 category 和兼容硬约束。
- 只有明确表示“全部重来/完全换需求”才执行 global reset 并进入新 epoch。
- query evidence 必须具备来源/轮次信息，使被覆盖的旧词可以单独失效。

### R4. 可选模型解析

- 高置信度规则结果不调用模型。
- 仅在作用域不清、多子句冲突、指代或规则低置信度时触发模型。
- 复用现有 tiered model client；默认无配置时完全离线且不下载模型。
- 模型只能返回严格白名单 JSON；禁止输出商品 ID、候选排序或直接状态变更。
- 非法 JSON、未知字段、越界内容、超时和 backend 异常必须回退规则结果。

### R5. 安全合并与可观察性

- 明确用户文本与高置信度规则证据优先于模型推断。
- 低置信度 model-only 结果只能成为软 evidence，不能直接成为 target-excluding hard filter。
- 诊断记录解析路径、触发原因、scope、接受/拒绝 mutation、state diff、backend、failure 和 usage。

## Acceptance Criteria

- [ ] 现有 75 个测试与新增解析测试全部通过。
- [ ] 默认无模型、无网络时不发生任何模型请求或下载。
- [ ] 否定、删除、局部替换、多条件、强弱约束和安全指代均有独立单元测试。
- [ ] scoped override 保留 category 与兼容约束，只失效被替换的 constraint/query evidence。
- [ ] 明确 global reset 才增加 epoch 并清空完整旧意图。
- [ ] 高置信度官方模板不会调用模型。
- [ ] 模型非法输出、未知属性、商品 ID、异常和超时均安全回退。
- [ ] Agent 输出继续满足 `docs/agent_api_contract.json`，recommendation 仍只来自 catalog whitelist。
- [ ] 实现开始时重新冻结当前公共基线；完成后 Hit@10、MRR、TechnicalScore 和 Intent Override HitRate 均不得低于该基线。
- [ ] 输出 rules-only 与 optional-model 的调用率、fallback 率、p50/p95 延迟及总体/分场景指标。

## Out of Scope

- Dense retrieval、embedding 生成或语义向量索引。
- 商品语义卡、catalog 扩写或场景 ontology。
- 修改 evaluator、ranking、commit policy 或官方 API。
- 模型训练、微调、多模态或在线 API 强依赖。

## Planning Status

- Blocking open questions: none.
- Implementation begins only after explicit approval of this child-task plan.
