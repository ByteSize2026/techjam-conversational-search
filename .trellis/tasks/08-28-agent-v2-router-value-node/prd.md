# Router/Value-Node shopping agent architecture (v2)

## Goal

把 Agent 从"自由 tool-loop"（模型决定下一步调用什么工具）重写为 Router（纯代码路由）与 Value Node（产出单一值，可以是 LLM 也可以是算法，但永远不能决定下一步去哪）严格分离的静态有限状态机架构。

目的不是刷 evaluator 分数——官方 evaluator 只看最终推荐结果，不读内部过程，很容易被固定流水线刷高分。真正的目标是让整套系统在 Technical Execution、Innovation & Problem Insight、Feasibility & Practicality 这几项 judging criteria 上站得住脚：把"聪明"完全从模型手里收回控制权，换成可枚举、可测试、可讲清楚的状态机，同时保留模型在具体判断点上的语义理解能力。

## Background and Confirmed Facts

- 官方 `Agent.reset/respond` 接口签名和 `evaluator/local_evaluator.py` 打分公式不能改，10 轮硬上限不变。
- 上一次尝试（`08-27-ecom-agent-architecture`，已提交 `c891de8`/`b24a40d`）实现了一个"模型选 action"的自由 tool-loop（`ModelActionPlanner` 每步自己决定调用哪个工具）。这被判定为设计错误：模型仍然握有"下一步去哪"的决定权，与新原则冲突。该任务已标记 `superseded_by` 本任务，其 tool-loop 代码不复用。
- 新设计原则见 `.trellis/workspace/RayneYael/agent-v2-first-principles-design.md`（第一性设计草案）：系统里每一步只能是 Router 或 Value Node 二选一；LLM 只活在 Value Node 里，回答单一结构化问题，不参与"下一步去哪"的判断；主循环的所有权（要不要继续、走哪条分支）永远在纯代码 harness 手里，不构成链式 ReAct 循环，从结构上排除模型失控风险。
- 可复用的既有代码（判定未违反新原则，直接沿用）：
  - `starter/shopping_agent/policy.py`：`IntentRouter.decide`、`CandidateGate.evaluate`、`ClarificationPolicy` —— 已经是"输入 context → 输出决策"的纯函数写法，直接沿用为新架构的 Router 实现。
  - `starter/shopping_agent/state.py`：`StateReducer.apply` + `SessionState` + `intent_epoch` override 语义 —— 单一状态写入口，直接沿用；需要清掉旧 tool-loop 专属字段（`profile_loaded`/`pending_task`/`tool_candidate_ids`/`tool_trajectory`/`last_tool_error`）。
  - `starter/shopping_agent/recommendation.py`：`RecommendationEngine` Protocol 及 `SearchRequest`/`RankRequest`/`CandidateSearchResult`/`CandidateRankResult` —— 已经是 Value Node 形状（输入结构→输出结构，无控制权），可直接抬入新节点表作为 Search/Rank 的实现基础。
  - `evaluator/trace_runner.py`：`TracingAgentProxy` 靠 state diff 记录 JSONL trace，不关心内部节点结构；新架构只需在 state 里加一个有界的节点轨迹字段，`trace_runner.py` 基本不用改。
- 需要整体重写、不复用的代码：`starter/shopping_agent/actions.py`、`planner.py`、`orchestrator.py`、`tools.py`（属于被否定的自由 tool-loop 模式），以及它们在 `starter/agent.py` 构造函数里的接线。
- 官方 `evaluator/local_evaluator.py` 完全不读 diagnostics/trace 字段，只认 `reset`/`respond` 的公开返回值算分；trace 设计因此不受评测契约约束，完全服务于可解释性和 demo storytelling，也是后续 Robust Evaluator 任务需要的真实 trajectory 来源。
- 题目原文（`docs/problem_statement.md`）四大支柱与本次设计的对照结论：
  1. **Dual-Track Routing + Hybrid Pipeline**（多路召回 → LLM 语义排序）：现有 `IntentRouter` 覆盖买/逛分流，直接沿用；新增一个独立的 LLM 语义排序 Value Node（复用 `semantic_ranking.py` 已有能力），排序阶段不能只有纯代码,否则对不上题目原文对"LLM Semantic Ranking"的明确要求。
  2. **Dynamic State Machine**（信息累加 + 意图覆盖 + 过泛化澄清）：`StateReducer` 的 override 语义 + `CandidateGate` 的过泛化截断已覆盖，直接复用，不重新发明。
  3. **Self-Evolution / Dynamic Context Programming**：拆成"参数自适应"（检索权重随轮次/已确认槽位数变化、澄清阈值随剩余轮次收紧以逼近 MTTC 最优——这是合法的自适应）与"流程结构自适应"（模型改写节点图本身——明确不做）。这个区分用于正面回应题目"运行时重新编排"这句话与新架构原则的表面冲突,而不是回避它。用户画像蒸馏做轻量占位版本,本任务不深化(见下 Out of Scope)。
  4. **Evaluation Matrix**（Hit Rate@10 / MRR / MTTC）：主循环由 harness（纯代码）完全拥有循环和终止权,模型每次只回答单一结构化问题,不构成链式 ReAct 循环,从结构上排除失控风险,不依赖运行时步数计数器去"兜底"。

## Requirements

- 主循环是一张静态 Router 表 + Node 表(不拆子树),harness 独占循环和终止决定权;`respond()` 内每一步"要不要继续 / 走哪条分支"必须能从当前 state 用纯函数算出来,不依赖模型自我判断。
- Value Node 输入输出用 pydantic model 定义并校验;解析失败重试固定 1 次,仍失败则 Router 走确定性降级分支;model_json_schema() 导出的 schema 直接用于模型的结构化输出请求。
- 状态只有一个写入口(`StateReducer`);新增的 Value Node(约束抽取、意图分类、画像蒸馏等)产出的更新都通过同一路径提交;移除旧 tool-loop 专属字段。
- 节点表至少覆盖:约束抽取、槽位检查/追问、搜索、候选池判断(沿用 `CandidateGate`,不接模型)、排序(含独立的 LLM 语义排序节点)、意图分类(比较/精炼/新搜索/确认)、查详情、生成文案、渲染。
- 候选池是否过泛化的判断维持纯代码(`CandidateGate`),本任务不引入模型参与这一步。
- 用户画像蒸馏做轻量版:数据流是"上一轮 `session_profile` 摘要 + 本轮结构化增量 → 蒸馏 → 经 `StateReducer` 写回";只在本轮确实产生新信息时触发(由 Router 判断是否触发,不是每轮都跑);字段设计不深入优化,只要数据流跑通、能证明架构留有长期画像的口子即可。
- Trace:`respond()` 内每执行一个节点,向 state 的有界节点轨迹字段追加一条 `{step, node, type: router|value_node, input_summary, output_summary, elapsed_ms}`;`trace_runner.py` 的改动仅限于把这个字段纳入投影范围。
- 官方 `Agent.reset/respond` 接口签名、响应 schema、旧 evaluator 计分公式不变。
- 旧 tool-loop 模块(`actions.py`/`planner.py`/`orchestrator.py`/`tools.py`)不复用;新架构中移除它们在 `starter/agent.py` 构造函数里的全部接线。

## Acceptance Criteria

- [ ] 完整对话流程可以画成一张静态 Router/Node 表,节点数量和分支在设计阶段已知、可枚举,不存在运行时才能确定的分支。
- [ ] 每个 LLM Value Node 的输入输出都有 pydantic model 定义;单元测试覆盖解析失败重试与确定性降级路径。
- [ ] `StateReducer` 保持唯一写入口;旧 tool-loop 专属字段从 `SessionState` 移除,新架构中不存在死字段。
- [ ] 排序阶段包含一个独立可测的 LLM 语义排序 Value Node,且该节点的输出不允许携带"下一步去哪"这类控制信号。
- [ ] 候选池过泛化判断继续走纯代码路径,有测试证明该判断不会触发任何模型调用。
- [ ] 画像蒸馏节点仅在"本轮产生新信息"时触发(有测试验证未变化时不触发),产出通过 `StateReducer` 写入,不存在第二条写状态路径。
- [ ] 新增节点轨迹字段可以还原一次 `respond()` 内的完整节点路径;`trace_runner.py` 现有测试和至少一个新架构下的 trace 用例通过。
- [ ] 旧 `evaluator/local_evaluator.py` 在新架构(无模型 key,纯确定性降级路径)下可正常跑完一轮 public benchmark,产出与改造前一致的 metric schema;分数如有变化需报告差值和原因,不得通过修改 evaluator 掩盖回归。
- [ ] 现有测试套件全部通过;新增覆盖主循环、各 Router 分支表、State 写入口、trace 记录的确定性测试。
- [ ] `starter/shopping_agent/actions.py`/`planner.py`/`orchestrator.py`/`tools.py` 及其在 `starter/agent.py` 中的接线被移除。

## Out of Scope

- 用户画像的深度个性化设计(跨 session 记忆、复杂 schema 优化)——本任务只做轻量占位数据流,深化留给后续任务。
- Robust Evaluator / rubric 生成——独立后续任务,不在本任务范围内(对应 `08-27-robust-evaluator-feasibility`)。
- 评论(review)相关能力——当前 catalog 无评论文本,继续排除。
- UI、外部数据抓取、多模态处理——同赛题 Out of scope 约束。
- 每个 Router 的具体分支表(边界情况,如搜索为空、约束冲突)——留给 `design.md` 逐节点展开,不在 PRD 里空转。

## Notes

- `08-27-ecom-agent-architecture` 任务已标记 `superseded_by` 本任务,其 tool-loop 代码不复用,但其 catalog 工具函数中的确定性部分(search/filter/detail 的纯代码实现)如与新节点表的 Search/FetchDetails 节点等价,可作为参考起点评估,不代表整体复用其接口设计。
- 复杂任务,需要 `design.md` + `implement.md`,`task.py start` 前需完成两者并经用户审阅。
