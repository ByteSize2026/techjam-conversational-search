# 优化确定性对话检索与动态提交策略

## Goal

在不依赖 LLM、稠密检索或网络服务的前提下，重构参赛 Agent 的确定性对话决策主链，使系统能够更快取得高价值约束、完整保留满足约束的目标候选、区分“继续提问”和“提交多少推荐”两个决策，并在对话轮数、HitRate、MRR 与 MTTC 之间形成可解释、可消融的平衡。

本任务以 2026-08-26 同口径无模型公开集结果为基线：TechnicalScore `0.743854`、HitRate@10 `0.900000`、MRR `0.545181`、MTTC `4.485`。竞争实现的 deterministic structured 结果 `0.943489` 仅作为架构研究证据，不作为需要逐行复制的实现。

## User Decisions

以下决策已由用户确认，属于本任务的硬要求：

1. 提高 `other` 的问询优先级，以更快取得跨属性约束。
2. Agent 可以在同一轮同时返回 recommendations 和 `ask_attribute`，但返回数量必须由提交策略动态控制，不能默认每轮固定 Top 10。
3. 增加明确的 `exhausted` 会话状态，区分属性级无偏好和全局信息耗尽。
4. 商品排名是独立问题；召回、排序、提问、提交必须分别诊断和验证。
5. 本轮不引入或运行 LLM；核心路径必须完全离线、确定性、可复现。

## Requirements

### R1. Exhaustion and dialogue state

- 会话状态必须区分：
  - `attribute_exhausted`：用户对某个属性没有偏好；
  - `global_exhausted`：用户没有任何更多偏好；
  - `boundary_seen`：Boundary 式“use your judgment”已经发生；
  - `no_progress_streak`：连续多少轮候选池或意图状态没有有效变化。
- Intent Override 必须进入新的 intent epoch，旧约束和旧推荐惩罚不得污染新意图。
- Boundary 的单属性拒绝不得错误设置 `global_exhausted`。
- `other` 必须允许重复询问，直到收到全局耗尽信号、无进展保护触发或轮次不足；不能因为它曾被问过一次就永久禁用。
- 所有可变状态继续按 `session_id` 隔离，`reset` 必须完全替换同名会话状态。

### R2. Clarification policy

- 候选池宽泛、仍可获得信息且未接近截止轮次时，优先询问 `other`。
- `other` 已经无效或不再有价值时，才能切换到可区分候选的具体属性。
- `global_exhausted`、最后两轮或连续无进展时必须停止继续等待。
- 用户同时否定当前推荐并明确要求继续提问时，可在当前轮临时绕过 `global_exhausted` 的提问禁令；不得清除持久耗尽状态，并应同轮继续返回 forced recommendations。
- 提问策略不得依赖 Agent 不可见的 scenario label、target、intent card 或 ground truth。

### R3. Target-preserving structured candidate pool

- 类目解析后必须先从完整类目成员集合构造 structured pool，再按高置信约束逐条收缩；不得在结构化过滤之前按固定 100/400 条预算截断。
- material、color、budget 和来自 catalog feature/detail 的高置信披露必须具备确定性匹配策略。
- 新约束若把此前非空候选池收缩为零，系统必须保留上一非空池，并将该约束降级为软排序证据，同时记录诊断原因。
- 只有 structured pool 构建完成后，才能生成排序 shortlist 或进入现有 lexical route。

### R4. Ranking as an independent stage

- 排序阶段不得承担“找回已被截断目标”的职责。
- 第一阶段仅使用确定性信号：完整约束满足、BM25/title coverage、popularity、rating 和低权重 profile prior。
- 排序结果必须提供可供提交策略使用的相对置信信号，至少包括 Top-1 margin、Top-K 稳定性和候选池规模。
- 排名权重必须通过消融验证；不得直接复制竞争实现公开集上调出的具体数值。

### R5. Dynamic recommendation commit policy

- 新建独立于现有 CandidateGate 的 commit policy。现有 Gate 可继续控制计算成本，但不得同时承担推荐提交决策。
- commit policy 必须支持动态列表长度：`0`、高置信前缀（例如 `1` 或 `3`）、完整小候选池、截止轮次 Top 10。
- 候选池宽泛且下一问题价值高时允许只提问、返回空 recommendations。
- Top-1/Top-3 稳定时允许同时推荐高置信前缀并继续提问。
- `global_exhausted`、无进展保护或第 9/10 轮必须提交可用的最佳候选。
- Intent Override 消息到达后必须立即进入新 epoch；在消息到达前不根据不可见的 scenario 状态推断覆盖。

### R6. Diagnostics and ablation

- Agent 侧每轮至少记录：structured pool size、constraint mutations、exhaustion flags、no-progress streak、rank margin、Top-K stability、commit mode、returned list size 和 ask attribute。
- 本地评测分析必须能区分：
  - target 不在 structured pool：召回/过滤失败；
  - target 在 pool 但 rank > 10：排序失败；
  - target 在低位且提前命中：提交过早；
  - 多轮未获得约束：提问或解析失败。
- 按顺序进行可归因消融：`other` 策略、exhausted 状态、structured pool、dynamic commit、deterministic ranking；禁止一次性合并后只比较总分。

## Constraints

- 不修改 `evaluator/local_evaluator.py` 的协议和评分语义。
- 不读取或利用 Agent 调用中不可见的 target、intent card、behavior 或 scenario label。
- 不引入网络、凭据、LLM、dense embedding 或新的重量级依赖。
- 保留 `Agent.reset` / `Agent.respond` 签名和输出合同。
- recommendations 只包含 catalog-valid、唯一、按最佳到最差排序的 `parent_asin`，最多 10 项。
- 公开 200 条仅用于开发与消融；阈值选择必须记录，不以单个总体分数掩盖场景退化。

## Acceptance Criteria

### Functional

- [ ] 属性级无偏好、全局耗尽、Boundary 与 Intent Override 的状态转换均有确定性单元测试。
- [ ] `other` 可以在有效信息仍可能到来时重复询问，并在全局耗尽/无进展/截止轮次正确停止。
- [ ] broad、partial-commit、small-pool、exhausted、late-turn 五类 commit 路径均有测试。
- [ ] 同一响应可以合法包含 `ask_attribute` 和 1–3 条高置信 recommendations。
- [ ] structured pool 在高置信约束过滤前不做固定预算截断，零交集时回退到上一非空池。
- [ ] 排序与 commit policy 通过明确的数据结构解耦，commit policy 不读取隐藏评测字段。
- [ ] 两个 session 并行使用时 exhaustion、候选池、排名稳定性和问询历史不串状态。

### Quality and reproducibility

- [ ] `python3 -m unittest discover -s tests -v` 全部通过。
- [ ] 无模型、无网络环境可运行完整本地 evaluator。
- [ ] 生成逐阶段 ablation 结果，至少包含 overall 和四种 scenario metrics。
- [ ] 相对当前 `0.743854` 基线，最终方案 TechnicalScore、HitRate、MRR 均不得退化。
- [ ] 第一阶段目标：TechnicalScore `>= 0.85`、HitRate@10 `>= 0.95`、MRR `>= 0.75`、MTTC `<= 3.8`；若未达到，必须用失败分类说明瓶颈，不得用 LLM 补救。
- [ ] Stretch goal：TechnicalScore `>= 0.90`、MRR `>= 0.85`，且 Intent Override HitRate `>= 0.90`。
- [ ] 记录最终阈值、选择依据和至少一个相邻阈值的对照结果，避免只报告单点最优。

## Out of Scope

- LLM reranking、cross-encoder、dense retrieval 或模型资产打包。
- 修改官方 evaluator、指标权重、catalog 或公开标签。
- UI、演示文案、提交包之外的产品化购物交互。
- 用真实用户长期偏好模型替代当前匿名 profile prior。

## Open Decisions for Planning Review

1. Dynamic commit 的首轮候选阈值是否采用 `commit_all <= 5`、`partial_commit <= 25`，还是只把它们作为待扫描值。
2. partial commit 初版是否只允许 Top 1，还是同时测试 Top 1/Top 3。
3. `other` 在收到一次 Boundary 拒绝后是否立即重试，还是先返回高置信前缀再重试。
4. 第一阶段 structured matcher 是否只覆盖 evaluator 明确生成的 material/color/budget/feature/detail，还是同步加入 size/style/use_case 的规范化匹配。
