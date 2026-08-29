# Independent Trace Runner Baseline

## Goal

在完全保持官方 `Agent.reset/respond` 合同、`evaluator/local_evaluator.py`、公开 benchmark 和评分公式不变的前提下，实现一套独立 Trace Runner，持久化新工具型 Agent 的逐轮状态变化、planner 决策、工具调用、推荐来源、fallback 和 token/耗时信息，并运行一次 200-session public benchmark，形成可复现的新 Agent baseline 诊断报告。

本任务的用户价值不是立即证明新架构优于 main，而是先确认新 Agent 确实在主动规划、明确官方分数由 planner 还是 deterministic fallback 贡献，并为后续策略、Prompt、token 成本优化和 Robust Evaluator 提供统一 trajectory 基础。

## Background and Confirmed Facts

- 当前分支为 `feat/ecom-agent-architecture`，起始 HEAD 为 `254abd07754ffeced73bfbe228616b2e82af80d2`。现有工具循环已经把逐轮 `tool_trajectory` 暴露在 `Agent.last_diagnostics`，但记录仍停留在内存/最近一轮，缺少独立持久化、状态 diff、逐 planner step usage 和最终推荐来源归因。
- 官方 evaluator 每个 sample 先 `reset`，再最多调用 10 次 `respond`；首次目标命中即终止。官方指标为 HitRate@10、MRR、MTTC、Efficiency 和 recommended TechnicalScore，且按 buying、browsing、intent_override、boundary 分组。
- 官方 evaluator 和 benchmark 是冻结的主要 score proxy，不得为了 Trace Runner 改动。Agent Trace 中不得包含 ground truth、hidden intent card 或 simulator behavior；这些信息只允许在运行结束后的 evaluation annotation/报告中关联。
- 当前真实 catalog 和 public set 已存在，SHA256 分别为 `da979b05a68af864cb0dcf9ee6a81c010c7e66a57978ad286c7a2e005fc69a67` 和 `857259f7a438e6188ac63e18995b6ff4489bfcfc4a716a798b9a2aa0ee8f7579`。
- main 历史最好结果来自无 LLM 的确定性 Agent，不是本任务要拆解的对照架构。已记录的 200-session 结果为 HitRate@10 `0.9`、MRR `0.545181`、MTTC `4.485`、Efficiency `0.6515`、TechnicalScore `0.743854`。本次只把它作为性能回归参考线，不做模型或架构因果实验。
- `.env` 已声明 DeepSeek 和 tool-planning 配置键，但凭据只可在运行时从环境读取，不进入 Trace、报告、fixture 或提交。

## Requirements

### R1 — Transparent official-evaluator execution

- 新 Runner 必须调用现有 `evaluator.local_evaluator.evaluate`、`load_jsonl` 和 `catalog_index`，不得复制或修改对话循环、customer simulator、推荐标准化或评分公式。
- 通过保持 `reset/respond` 签名一致的 tracing proxy 包裹真实 Agent。除观测开销外，proxy 不修复、不重排、不增删 Agent 响应，也不吞掉原本会由 evaluator 处理的异常。
- Runner 接受 catalog、dataset、输出目录和可选 baseline result；默认只运行当前 checkout 的新 Agent，不实现 main ref 加载或 main 内部 instrumentation。

### R2 — Versioned, crash-tolerant trace artifacts

- 每次运行生成唯一 run ID，并在调用过程中逐条写入、flush JSONL，避免长 benchmark 失败后丢失全部轨迹。
- 输出至少包含 `manifest.json`、`events.jsonl`、原样的 `evaluation.json` 和派生的 `analysis.json`；所有格式携带 schema version。
- manifest 记录 Git commit/dirty 状态、Python 版本、catalog/dataset/evaluator-config hash、非敏感 Agent 配置、模型/backend 名称、动作/超时预算和开始/结束时间。不得记录 API key、Authorization header 或 `.env` 原文。
- 输出目录必须由调用方显式指定；测试使用临时目录，benchmark 产物写入 `/tmp` 或其他不提交位置，不写入 `data/`。

### R3 — Agent-visible trajectory without target leakage

- 每个 session/turn 记录 sample identity、用户输入、Agent 原始输出、公开响应字段、调用耗时和合法 usage；sample identity 只用于关联，ground truth、intent card 和 behavior 不得进入 Agent 调用或 agent-visible event payload。
- 对当前 Agent 做有界状态投影，至少覆盖 intent epoch、category anchor、active constraint 摘要、no-preference、profile-loaded、pending attribute、asked attributes、候选数量/有界 ID、已推荐数量和 state fingerprint；保存每轮 before/after 及结构化 diff。
- 记录 planner 选择的 action、受限参数、执行状态、工具 observation 摘要、错误、工具耗时、planner backend、逐 step token usage/失败摘要和剩余预算。禁止记录模型自由文本 chain-of-thought 或未经清洗的完整 profile。
- pause/resume、intent override、invalid action、tool error、time/step budget exhaustion 和 deterministic fallback 必须可从事件序列还原。

### R4 — Recommendation and fallback attribution

- 每轮同时记录 planner 提议/工具验证的推荐 ID、Agent guard 后的公开推荐以及 evaluator 标准化后的 Top-10。
- 最终推荐按 ID 标注来源，至少区分 `planner_selected`、`deterministic_fill`、`popular_fill` 和 `fallback_pipeline`；不得把 fallback 补齐产生的命中算作 pure planner hit。
- fallback 使用稳定 reason taxonomy，至少区分 planner invalid/backend/timeout/budget、tool validation/execution、orchestrator step/time budget、deterministic fallback 和 pipeline/guard error。
- 运行结束后才使用 dataset target 和官方结果生成 evaluation annotation，计算 pure planner hit、fallback-assisted hit、fallback rate、action 分布、无效动作、平均 actions/turn、tokens/session 和场景分组摘要。

### R5 — One diagnostic baseline run

- 完成自动测试后，从本地 `.env` 显式导出配置，确认 tool planning 与 DeepSeek backend 实际启用，再运行一次完整 200-session public benchmark。
- 报告官方总体/分场景指标、token usage、动作/失败/推荐来源指标，并与 main 历史最好结果给出绝对差值和相对差值。
- 本次 benchmark 是诊断性 baseline；不在运行前设定任意“不得下降 X%”阈值。是否接受为后续优化 baseline 由用户根据报告决定。
- 若外部 API 凭据、网络或配额阻止完整运行，Runner、测试和离线 smoke 仍需完成；报告必须准确记录阻塞点，不用 deterministic 结果冒充 DeepSeek baseline。

### R6 — Competition and repository boundaries

- 最多 10 轮、Top-10、catalog-valid unique `parent_asin`、session 隔离、intent override 和 ask-attribute 行为继续由现有 Agent/evaluator 合同约束。
- catalog 严格只读，不生成或注入 mock ASIN；测试 catalog 只存在于 `TemporaryDirectory`。
- 不新增重型框架、数据库或网络遥测服务；实现使用标准库和现有项目依赖。
- Trace 开关/Recorder 失败不得破坏公开 Agent 响应；本任务不得通过 evaluator 或 frozen data 改动掩盖回归。

## Acceptance Criteria

- [ ] `evaluator/local_evaluator.py` 与 `data/catalog.jsonl`、`data/public_set.jsonl` 无任务引入的修改，Runner 直接复用官方 evaluator 入口和指标结果。
- [ ] tracing proxy 在确定性 fixture 上与未包裹 Agent 产生相同的官方 evaluation 结果；异常与非法响应仍按官方 evaluator 原行为处理。
- [ ] `events.jsonl` 可逐行解析、序号稳定、session/turn 可关联；中途异常时已 flush 的事件仍存在。
- [ ] manifest 的 hash、commit、模型/预算配置完整且不包含已知 API key；测试覆盖 secret redaction。
- [ ] 至少一个确定性工具循环 fixture 证明状态 before/after、planner action、工具 observation、逐 step usage、pause/resume 或 fallback 被完整记录。
- [ ] 最终推荐的 planner、fill 和 fallback 来源可以逐 ID 还原；测试证明 assisted hit 不会被计入 pure planner hit。
- [ ] analysis 同时保留官方总体/分场景指标，并报告 action、fallback、token、pure/assisted hit 和可选 baseline delta。
- [ ] `python3 -m unittest discover -s tests -v` 全部通过；相关 Python 文件通过现有 Ruff/compile 检查。
- [ ] 在 DeepSeek 可访问时完成一次 200-session traced benchmark，并产出 `/tmp` 下未提交的四类工件；否则产出明确的外部阻塞证据和可直接复跑的命令。
- [ ] 报告明确判断 tool loop 是否实际启用、多少命中来自 planner/fallback，以及新 Agent 相对 `0.743854` 历史 TechnicalScore 的差值，但不在本任务中调 Prompt 或策略。

## Out of Scope

- 修改官方 evaluator、benchmark、评分权重、customer simulator 或 catalog。
- 给 main 分支增加 Trace、从 Git ref 动态加载 Agent，或设计 main/DeepSeek/其他模型的消融矩阵。
- 调整 planner Prompt、动作策略、检索/排序算法、token budget 以追求分数提升。
- 实现 Robust Evaluator、rubric case generation、LLM judge 或额外 benchmark。
- 记录原始 chain-of-thought、完整模型 prompt/response、API key、私有评测数据或不必要的原始 profile。

## Deferred Decisions

- 新 Agent 是否正式成为后续优化 baseline，以及允许相对 main 下降多少，在首次 traced benchmark 报告后由用户决定。
- 多次重复运行、显著性评估、策略/Prompt/token 消融和 Robust Evaluator 接入属于后续独立任务。
