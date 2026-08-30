# 灵活自然语言意图适配器

## Goal

在不改变官方 `Agent` 接口、不修改冻结商品目录、且保持无网络可运行的前提下，为当前 Shopping Copilot 增加一个可选、可验证、可降级的自然语言意图适配层。该层应把否定、局部修改、多条件和口语化表达转换为现有结构化约束与查询证据，重点改善 Intent Override 和潜在私有集 paraphrase 的鲁棒性，而不是扩展成通用购物助手。

## User Value

- 用户不必严格复述模拟器模板，也能被正确理解。
- 一次局部修改不会错误清空仍然有效的类别和约束。
- 模型不可用、超时或返回非法内容时，系统仍可使用现有确定性路径完成官方评测。
- 团队能够通过场景分层指标判断模型是否真正提分，而不是只增加“AI 感”。

## Confirmed Facts and Constraints

- 官方目标是在最多 10 轮内尽快、尽可能靠前地找到精确 `parent_asin`；语义相似但 ID 不同不算命中。
- 用户消息来自由商品 metadata 派生的隐藏 intent card，不是真实购物对话语料。
- 官方明确允许 query rewriting、structured constraint state、intent override、dynamic context、semantic reranking、合法 LLM API 与本地模型。
- 官方明确排除 catalog modification、完整模型训练、多模态系统和基础设施级重型向量数据库。
- `ask_attribute` 只能是 `category/material/color/size/style/brand/budget/feature/use_case/other` 或 `null`；模拟器依据该结构字段而不是回复文案决定后续信息。
- 正式评分可能禁用网络；在线 API 不能成为唯一或必需路径。
- 当前系统已有 `IntentUpdate`、`ConstraintMutation`、`StateReducer`、结构化候选池、澄清策略、可选 LLM/Qwen reranker 和分层诊断。
- 当前规则解析主要依赖固定英文模板和词表；检测到 override 后会清空全部约束与 query terms，粒度过粗。
- 2026-08-28 使用当前 `starter.agent.Agent`、冻结 catalog 与 200 条公开集复跑的无模型基线为：Hit Rate@10 `1.000000`、MRR `0.805024`、MTTC `3.005000`、TechnicalScore `0.901407`、usage `0`。四个场景均为 Hit Rate@10 `1.000000`；Intent Override 的 MRR 为 `0.821429`、MTTC 为 `4.733333`。可复核产物为 `research/baseline-2026-08-28.json`；仓库根目录 `results.json` 仍是旧产物，不能作为当前基线。
- 已有整体技术调研位于 `.trellis/tasks/08-25-shopping-copilot-research/`；本任务只承接其中与意图解析、状态更新、模型降级和评测纪律有关的结论。

## Requirements

### R1. 保持官方协议和目录边界

- `Agent.reset(...)` 与 `Agent.respond(...)` 的输入输出契约保持不变。
- 不生成目录外 ID，不读取隐藏标签，不修改 evaluator，不修改或扩写冻结 catalog。
- 内部解析结果最终必须映射到现有属性枚举和现有候选检索/排序链路。

### R2. 增加可验证的意图增量契约

- 自然语言解析结果复用或兼容现有 `IntentUpdate` 与 `ConstraintMutation`。
- 支持 `upsert`、`replace`、`remove`，以及 `prefer/avoid/require` 极性和 `soft/hard` 强度。
- 支持类别锚点、无偏好、全局穷尽、边界信号、澄清重开和有界查询改写。
- 解析结果必须包含来源与置信度，供 reducer、诊断和降级逻辑使用。

### R3. 将 override 从全局清空收敛为有来源的局部更新

- 明确类别不变时保留 `category_anchor`。
- 用户只修改一个属性时，只 supersede 被引用或冲突的旧偏好。
- 仍兼容的、后续明确披露的硬约束不得被无条件清除。
- query evidence 需要具备足够的轮次/来源信息，以便移除被覆盖的旧偏好而不是清空全部历史。
- 只有用户明确表达“全部重来/换一个完全不同的需求”时才允许全局 reset。

### R4. 扩展高价值自然语言行为

至少覆盖以下英文评测相关表达类别：

- 否定：`not red`、`anything but leather`。
- 局部替换：`keep the style, but change the budget to under $50`。
- 删除偏好：`color no longer matters`。
- 多条件：`black, under $80, and suitable for running`。
- 口语化硬软约束：`must be XL`、`preferably waterproof`。
- 指代/延续：`like the second direction, but lighter`；无法安全落槽时保留为软 query evidence，不编造硬约束。
- 现有官方模板与可能的自然语言 paraphrase。

MVP 只要求官方评测语言与当前 catalog metadata 可支撑的语义；中文、多语种和真实场景本体不作为本任务验收项。

### R5. 采用规则优先、模型可选的混合解析

- 高置信度、已知模板继续走确定性解析，不产生额外模型调用。
- 仅在否定、修改、指代、多条件、冲突或低置信度时允许调用模型。
- 模型只能返回严格、白名单化的 JSON 意图增量，不能返回商品 ID 或直接修改 session state。
- 模型关闭、无网络、超时、异常、非法 JSON、未知属性或越界内容时必须回退到规则结果。
- 模型推断默认不得把低置信度语义变成会排除 target 的硬过滤条件。

### R6. 复用现有模型后端和安全边界

- 优先复用现有 tiered model client、超时、usage 和 failure diagnostics 机制，避免建立第二套凭证与 HTTP 实现。
- 意图模型与语义 reranker 必须可独立开关，以支持 `rules_only`、`intent_model_only`、`reranker_only` 和 `both` 消融。
- 默认无环境变量时不得发生网络请求或隐式下载模型。

### R7. 可观察、可复现、可消融

- 每轮诊断记录解析路径、置信度、结构化增量、被接受/拒绝的字段、fallback 原因和状态变更摘要；不得记录密钥或无界 prompt。
- 评测必须按 Buying、Browsing、Intent Override、Boundary 分层报告。
- 对规则、意图模型和 reranker 的组合进行可复现消融，并记录延迟、token usage、失败率与核心指标。

## Acceptance Criteria

- [ ] 现有官方 Agent JSON contract 保持通过，响应仍只包含允许字段和值域。
- [ ] 默认无模型配置、无网络环境下完整 evaluator 和全部现有测试通过，且不会尝试联网或下载模型。
- [ ] 官方已知模板的解析行为不退化；新增测试覆盖否定、局部替换、删除偏好、多条件、强弱约束和安全指代降级。
- [ ] scoped override 测试证明：旧的被替换偏好失效，类别锚点与兼容硬约束保留，旧 query evidence 不再污染检索。
- [ ] 只有明确全局重置表达才增加 intent epoch 并清空完整意图；普通 `actually/change/instead` 不再自动等同全局 reset。
- [ ] 模型返回未知属性、非法 action、商品 ID、非法 JSON、异常或超时时，解析器拒绝不可信部分并回退到确定性结果。
- [ ] 高置信度规则消息不调用模型；触发模型的条件有单元测试和诊断证据。
- [ ] 意图模型与现有 reranker 可分别开关，四种消融配置可运行。
- [ ] 实现开始时用当前代码重新生成并冻结独立基线产物；复跑结果应与 2026-08-28 锚点一致：Hit Rate@10 `1.000000`、MRR `0.805024`、MTTC `3.005000`、TechnicalScore `0.901407`、usage `0`。若不一致，先定位环境或代码差异，不进入模型消融。
- [ ] 公共集总体 Hit Rate@10、MRR 与 TechnicalScore 不得低于重新冻结的当前基线；若 MTTC 有权衡，必须在报告中解释并由综合分非退化约束兜底。
- [ ] Intent Override Hit Rate@10 不低于当前 `1.000000`，并提供 scoped override 对候选池/目标排名影响的逐会话诊断。
- [ ] 所有模型成功调用报告真实非负 token usage；失败 tier 的 usage 不计入成功响应。
- [ ] 输出一份简短消融报告，包含总体/分场景指标、p50/p95 turn latency、模型调用率、fallback 率和已知限制。

## Out of Scope

- 为 50,000 商品生成新的场景语义卡或修改 catalog 内容。
- 新建知识图谱、场景 ontology 或重型向量数据库。
- Dense retrieval、embedding 资产建设或新的商品侧语义索引。
- 模型训练、微调、强化学习、私有标签重建。
- 多模态、UI、真实交易、通用购物助手或推荐文案生成系统。
- 将中文或多语种支持作为官方评分承诺。
- 依赖在线 API 才能完成正式评分。

## Risks and Deferred Items

- 私有集 paraphrase 范围未知；模型可能增强泛化，也可能引入不确定性，必须以 fallback 和消融控制风险。
- 本地模型资产大小及官方 CPU/GPU/内存限制未被文档明确承诺；本任务不把本地权重打包作为阻塞条件。
- 公共集只有 200 会话，Boundary 仅 10 条；不能以单一公开总分过度调参。
- “指代上一批推荐”缺少产品级对话语义和 evaluator 反馈结构，MVP 仅做安全 query evidence，不承诺完整共指解析。

## Planning Status

- Blocking open questions: none for MVP planning.
- Implementation requires a separate explicit approval after review of this plan.
