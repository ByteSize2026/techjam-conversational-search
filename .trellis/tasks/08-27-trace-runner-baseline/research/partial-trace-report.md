# DeepSeek Trace Benchmark — 阶段性技术报告

快照时间：2026-08-27  
运行目录：`/tmp/techjam-trace-agent-baseline-20260827`  
仓库内快照：`artifacts/trace-runs/techjam-trace-agent-baseline-20260827`  
状态：运行已中断；保留 633 条已 flush 事件，尚未形成官方最终分数

## 一句话结论

当前工具链本身很快，但 DeepSeek 经常没有返回合法工具动作，导致约九成对话轮次转入确定性兜底；继续按原配置跑满 200 条会继续消耗较多 Token，但得到的很可能仍是一份“兜底主导”的基线。

## 当前样本范围

- 已写入 `633` 条 Trace 事件。
- 已进入第 `68/200` 条样本；第 68 条停在第 3 轮开始处。
- 已完成并可分析 `282` 个对话轮次，覆盖 68 条已开始的样本。
- `evaluation.json` 和 `analysis.json` 尚未生成，因此本文不报告官方 HitRate、MRR 或 TechnicalScore。
- Trace 不记录隐藏 chain-of-thought；下文的“推理样本”是可审计的结构化动作、状态、耗时、Token、错误和推荐来源。

## 核心数据

| 项目 | 阶段结果 | 人话解释 |
| --- | ---: | --- |
| Prompt Token | 211,029 | 输入给模型的 Token |
| Completion Token | 81,867 | 模型输出的 Token |
| 总 Token | 292,896 | 平均每个已完成轮次约 1,039 |
| Planner 调用 | 775 | 平均每轮约 2.75 次模型规划 |
| Planner 错误 | 496（64.0%） | 多数是没有返回合法工具动作 |
| 直接走完工具循环 | 28/282（9.9%） | 少量轮次按计划成功执行 |
| 转入兜底 | 254/282（90.1%） | 绝大多数轮次最终靠旧逻辑返回 |
| 平均每轮耗时 | 16.6 秒 | 用户等待时间偏高 |
| P95 每轮耗时 | 24.6 秒 | 约 5% 的轮次超过这个水平 |
| 平均 Planner 调用 | 5.0 秒 | 主要延迟来源 |
| 平均工具执行 | 14 毫秒 | 检索/过滤工具不是瓶颈 |

若后续样本保持相近用量，粗略外推跑满 200 条可能达到约 `86 万` Token。该数字只是按当前进度线性估算，不是最终账单。

## 为什么频繁兜底

254 个兜底轮次中：

- `240` 次：连续收到非法动作，达到错误上限。
- `12` 次：用完规划步数仍未完成。
- `2` 次：工具错误达到上限。

这说明当前首要问题不是商品检索慢或工具执行失败，而是模型输出与动作协议的匹配度不足。

## 推荐来源

当前记录到的推荐位来源：

- `fallback_pipeline`：2,540 个。
- `deterministic_fill`：189 个。
- `planner_selected`：31 个。
- `tool_preview`：6 个。

这些是“推荐位数量”，不是命中数量。因为完整评测尚未结束，现在不能据此声称 Planner 或兜底贡献了多少官方命中。

## 三个代表性推理样本

### 1. 工具循环成功：识别新需求并推荐

用户把需求改成 `leather` 后，Planner 依次执行：

1. `search_products`
2. `get_product_details`
3. `recommend_products`

整轮约 `7.86` 秒，消耗 `2,449` Prompt Token 和 `564` Completion Token。第一件商品由 Planner 直接选择，其余结果由确定性排序补齐。

结论：模型在部分场景中能够正确使用搜索、详情和推荐工具，但单轮 Token 仍然较高。

### 2. 工具循环成功：主动追问属性

用户表示现有结果不合适，并要求询问一个具体属性。Planner 选择 `ask_user`，追问腰带风格，例如休闲、正式或礼服风格。

整轮约 `4.87` 秒，消耗 `599` Prompt Token 和 `475` Completion Token，没有触发兜底。

结论：`ask_user` 路径可正常工作，能够产生合理的澄清问题。

### 3. 典型失败：非法动作后转入兜底

用户寻找合金材质项链。执行过程为：

1. Planner 返回非法动作。
2. 成功调用 `get_user_profile`。
3. Planner 再次返回非法动作。
4. 达到错误上限，转入确定性兜底。

整轮约 `21.31` 秒，消耗 `571` Prompt Token 和 `364` Completion Token。最终展示的推荐全部来自 `fallback_pipeline`。

结论：即使最终能给出商品，用户也付出了两次无效模型调用的时间和 Token，最终结果却主要来自旧逻辑。

## 暂停与恢复能力

生成本阶段报告时，PID `274076` 曾通过 `SIGSTOP` 暂停在内存中。后续复核时该 PID
和其他 Trace Runner 进程均已不存在；`manifest.json` 仍为 `running`，633 条事件仍在，
但 `evaluation.json` 和 `analysis.json` 均未生成。因此原运行已经中断，不能再通过
`SIGCONT` 恢复，也不能将该目录视为完整 benchmark。

当前 Runner **不支持进程退出后的磁盘断点续跑**：

- 输出目录必须为空。
- `events.jsonl` 使用独占新建模式。
- evaluator 每次启动都从第 1 条样本开始。

因此不能直接从第 68 条重启；需要先实现显式 checkpoint/resume，或换新目录从头运行。

## Agent 架构审计

现有实现已经拆出 `state.py`、`policy.py`、`planner.py`、`actions.py`、
`tools.py` 和 `orchestrator.py` 等文件，但文件拆分并不等于职责边界已经成立。
当前真实调用链更接近：

```text
Agent.respond
  -> regex intent update + scattered state mutation
  -> ModelActionPlanner asks a chat model to write JSON text
  -> generic PlannedAction(name, dict)
  -> ActionOrchestrator mixes retry, execution, state mutation and response assembly
  -> ShoppingToolbox or the separate deterministic Agent pipeline
  -> Agent._guard patches the final public response
```

目标应调整为以下有明确输入输出合同的分层：

```text
Input / Session
  -> State & Memory
  -> Intent / Policy
  -> Planner
  -> Structured Action Runtime
  -> Tool / Service Executor
  -> Typed Observation
  -> Evaluator / Guardrail
  -> Response Renderer
```

### 逐层差距

| 层级 | 当前实现 | 主要问题 |
| --- | --- | --- |
| Input / Session | `Agent.reset/respond`、内存 `SessionStore` | 公开合同存在，但 `Agent` 同时负责路由、检索、排序、fallback、guard 和回复组装；缺少 session 版本、并发控制、持久化/恢复和显式 missing-reset policy。 |
| State & Memory | `SessionState`、`StateReducer` | 状态模型较完整，但 Reducer 并非唯一写入口；Toolbox、Orchestrator 和 Agent 都直接修改候选、profile、pending task、asked attributes 和推荐历史，难以原子回放和审计。 |
| Intent / Policy | `parse_intent_update`、`IntentRouter`、`CandidateGate`、`ClarificationPolicy` | 工具型路径在进入 Planner 前没有运行 `IntentRouter`，也没有产生 browsing/buying/clarification/intent-override 的统一类型化决策；Policy 不生成允许动作集合，Planner 每一步都看到全部动作。 |
| Planner | `ModelActionPlanner` | 同时负责 prompt 拼装、动作 schema 的文字描述、模型调用和结果解码；它不是只做“下一步决策”，还被要求生成 JSON、推荐 ID、澄清问题和 message。 |
| Structured Action | `PlannedAction(name, arguments: dict)` | 只有通用容器和 action 名白名单，没有每个 action 的严格输入类型、JSON Schema、版本、discriminator 或 provider-native tool calling；action-specific 校验被推迟到工具内部。 |
| Tool / Service | `ShoppingToolbox`、`CatalogRepository`，以及 `Agent` 内另一套 retrieve/rank 流程 | 工具循环和确定性 pipeline 重复实现检索/候选处理；Planner 工具集中没有独立 `rank`/rerank action，LLM 实际承担了本应由确定性 rank service 完成的一部分选择。 |
| Observation | 通用 `ActionObservation(payload: dict)` | 缺少按 action 定义的 observation schema、质量/截断元数据和稳定错误类型；Planner prompt 直接携带最近通用 payload，边界不清晰且可能产生不必要 Token。 |
| Evaluator / Guardrail | JSON decoder、action parser、Toolbox 校验、Orchestrator retry、`Agent._guard` 分散存在 | 没有一个统一 runtime guardrail；backend、JSON、schema、参数、工具和质量错误被压成 `planner_error`，retry 只得到笼统的“invalid action”，无法有针对性修正。 |
| Response | Toolbox、Orchestrator、deterministic pipeline 和 `Agent._guard` 多处拼装 | 没有独立 Response Renderer；`ask_user` 直接采用 LLM 文本，`recommend_products.message` 又会被工具丢弃，最终 message 策略不一致。 |

## 已确认的问题与待改进清单

以下条目属于后续 Agent Runtime/架构任务，不在本次 Trace Runner baseline
任务中顺手调整 Prompt 或策略。

### P0 — 先修复模型到 Action 的硬边界

- [ ] **使用 provider-native Structured Output / Tool Calling。** Agent Runtime
  必须把模型限制在注册过的 action schema 中；不能继续只在 system prompt 里写
  `Return JSON only`，再期待自由文本模型自觉遵守。
- [ ] **为每个 action 定义真正的类型和 JSON Schema。** 至少包括
  `SearchProducts`、`RetrieveDetails`、`RankProducts`、`Clarify`、
  `Recommend/Answer`；字段类型、枚举、长度、ID 来源和 required 字段必须可机器验证。
- [ ] **Planner 只返回下一步决策。** Planner 不直接执行工具，不直接拼最终自然语言，
  不承担手写 JSON 的正确性；Runtime 负责 provider tool-call 与内部 Action 对象的转换。
- [ ] **增加 capability/action registry。** Policy 根据 intent、state 和已完成 observation
  生成本步骤允许的 action mask；例如还未 search 时不能 recommend 未观察 ID，
  profile 已加载时默认不再开放重复 profile action。
- [ ] **修复同类的 Semantic Ranker 边界。** `LLMSemanticRanker` 目前也只是通过 prompt
  要求 `Return JSON only`，并调用相同的 `complete_json` parser；它必须使用严格 ranking
  schema，或由本地 Qwen/cross-encoder 等确定性 rank service 替代。
- [ ] **做小规模 structured-action canary 后再跑 200 条。** Canary 必须证明每次模型调用
  都可归类为 request、structured decode、schema validation、policy rejection、tool error
  或 success，不能再出现无法解释的统一 `planner_error`。

验收标准：模型回复前后即使包含自然语言，也由 provider/runtime 的结构化通道隔离；
Planner 业务代码只接收已验证的 Action 对象，不再自行从 message content 提取 JSON。

### P0 — 修复 Intent / Policy 输入质量

- [ ] **建立类型化 `IntentDecision`。** 显式区分 browsing、buying、clarification answer、
  intent override 和 boundary/no-preference，并携带 confidence、约束变更和 allowed actions。
- [ ] **让工具路径真正经过 Policy。** 当前 `IntentRouter` 主要服务确定性 pipeline；
  tool loop 应在每轮规划前消费同一个 PolicyDecision，而不是直接把通用 state 交给 Planner。
- [ ] **修复已由 Trace 证明的 slot 错配。** `Material:alloy` 被记录为
  `feature="Material:alloy"`，用户已给出 alloy 后 fallback 仍追问 material；
  `Buckle closure` 又没有进入 requirements。需要覆盖 `Attribute:value`、catalog vocabulary、
  同义词、未知材料以及置信度不足时的降级策略。
- [ ] **让 clarification 与已有约束一致。** Guardrail 必须阻止系统追问用户已经明确给出的
  attribute，也不能因为抽取失败把同一需求再次问回去。

验收标准：上述两个公开 Trace 句子分别稳定产生 `material=alloy` 和可查询的
`feature/closure=buckle`，且 Policy 不允许重复询问已满足的 slot。

### P1 — 解耦 Runtime、Tool、Observation 和 Response

- [ ] **把 `Agent` 缩回 facade。** `Agent.respond` 只负责 session lookup、调用 Runtime、
  转换公开 contract；当前约 1100 行中的 retrieval、rank、fallback、provenance、guard 和
  message 逻辑应下沉到独立组件。
- [ ] **把 Orchestrator 拆成明确状态机。** Runtime 负责 step budget、timeout、action mask、
  transition 和终止条件；Executor 只执行 action；Response Renderer 只消费最终决定和证据。
- [ ] **统一检索与排序服务。** Tool loop 和 deterministic pipeline 应调用同一套
  Search/Retrieve/Rank services，避免当前 Toolbox 简单搜索与 Agent 高级 route/rerank
  两套路径产生行为漂移。
- [ ] **增加独立 `RankProducts` action/service。** LLM 可以决定何时排序及排序目标，
  但真正的 feature rank、Qwen rerank、去重、Top-K 和 catalog-valid 检查由确定性服务完成。
- [ ] **为每个工具定义 typed observation。** Observation 包含 action version、status、
  bounded result、quality signals、error code、latency 和 provenance；传回 Planner 前由专门的
  observation projector 压缩，而不是直接传通用字典。
- [ ] **状态写入集中化。** Tool 和 Planner 不直接修改 SessionState；Action + Observation
  交给 Reducer/transition function 原子生成新状态，并保存可回放的 state diff。
- [ ] **建立统一 Runtime Guardrail。** 分层处理 schema、policy、argument、catalog、tool、
  timeout 和 result-quality 错误；retry 必须收到具体、可修复的结构化错误，重复同类错误时
  采用降级策略而不是盲目重发近似 prompt。
- [ ] **建立独立 Response Renderer。** 澄清、推荐和 fallback 都从已验证的
  Decision + Observation + provenance 生成回复；Planner 不再提供可直接面向用户的 message。

验收标准：每一层都有可单测的输入/输出类型；替换 Planner backend 不需要改 Tool、
State 或 Response；替换检索/排序实现不需要改 Planner prompt。

### P1 — 修复 Trace 与成本核算盲点

- [ ] **保留非敏感失败分类。** 当前嵌套 sanitizer 把 `backend/stage/error` 全部变为
  `[TRUNCATED]`。应在浅层写入稳定字段，例如 `failure_stage=json`、
  `error_code=invalid_json`、`top_level_type=list`、`action_name=...`。
- [ ] **记录安全的结构化 I/O 证据。** 不保存 chain-of-thought 或完整 profile，但应记录
  action schema/version、允许动作、prompt/context hash、response content type、长度、
  finish reason、JSON decoder 位置、decoded keys 和经过清洗的短错误摘要。
- [ ] **失败调用也要核算 Token。** 当前 usage 只在 JSON 解码和 validator 成功后读取；
  已付费但格式错误的 completion 可能完全不进入报告。Usage 应属于 backend attempt，
  与业务 action 是否有效分开记录。
- [ ] **区分 retry 成本。** 报告分别统计首次成功、schema retry、policy retry、tool retry、
  fallback 前浪费的 Token/延迟，避免只看最终 response usage。
- [ ] **保留 crash-tolerant benchmark checkpoint。** 这次 PID 消失后无法从第 68 条续跑；
  Runner 应按 sample 边界保存官方 evaluator 所需的可验证 checkpoint，或明确采用可合并的
  shard 运行协议。

验收标准：任意一次失败无需读取秘密或 chain-of-thought，就能回答“请求是否到达 backend、
返回是否为 JSON、违反哪个 schema/policy、是否执行工具、花了多少 Token 和时间”。

### P2 — Session、质量评估与可扩展性

- [ ] 为 SessionStore 增加版本、TTL、并发/幂等语义以及可选持久化，不再依赖单进程内存。
- [ ] 将“缺少 reset”从静默建空 session 改为显式、可配置的 contract policy。
- [ ] 在 Runtime Guardrail 增加结果质量检查：约束覆盖、候选数量、重复/已见商品、
  provenance、推荐依据和 fallback assisted 标记，而不只检查 catalog ID 与 Top-K。
- [ ] 为 action、observation、state snapshot 和 trace event 建立独立 schema version，
  支持后续迁移而不是依赖通用 dict 的隐式兼容。
- [ ] 建立分层评测：action validity、policy correctness、tool determinism、state transition、
  response grounding、端到端 official score 分开测试，避免最终分数掩盖中间层失败。

## 对当前阶段结果的决策建议

当前进程已经消失，不能再通过 `SIGCONT` 恢复。现有 68 条样本应保留为架构诊断证据，
不应称为完整 DeepSeek baseline。建议不要在原架构上直接重跑 200 条：

1. 先完成 P0 的 Structured Action Runtime、失败分类和 intent slot 修复。
2. 用 10–20 条 canary 验证 action validity、fallback、Token 和 latency。
3. Canary 证明 Planner 确实主导有效动作后，再从空目录运行完整 200-session baseline。

如果完整重构暂不进入当前任务，至少应先修复 Trace 失败分类与 usage 核算，确保下一次运行
不会再次得到“知道失败很多，但看不到模型究竟违反了哪条合同”的结果。
