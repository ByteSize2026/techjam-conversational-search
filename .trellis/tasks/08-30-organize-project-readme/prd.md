# 统一双 Evaluator 协议与项目说明

## Goal

让同一个 `starter.agent.Agent` 通过显式协议参数兼容官方 evaluator 与自研自然语言 evaluator，同时保持正式接口、召回、排序和推荐提交策略唯一；README 为新开发者说明主代码目录、协议 profile 和其他目录职责。

## Background

- 当前分支 `codex/deepseek-canonicalizer-experiment` 直接建立在本地 `main` 最新提交之上，目前领先一个大型提交。
- `main` 与当前分支的 `evaluator/local_evaluator.py`、`docs/agent_api_contract.json` 以及公开 `reset` / `respond` 签名相同；主要差异位于自然语言意图解释和相关策略实现。
- 独立项目 `techjam-natural-language-benchmark` 已通过相同合同加载当前 Agent。官方侧合同/evaluator 测试 12 项通过，自研 benchmark 测试 34 项通过，已有 7 场景结果全部 Hit@10 且无运行错误。
- 用户确认 evaluator 类型必须由显式参数配置，Agent 不得根据输入文本、样本分布或隐藏字段猜测 evaluator。

## Requirements

- `starter.agent.Agent` 增加可选的 `protocol_profile` 构造参数，并支持通过 `AgentConfig` / 明确命名的环境变量配置同一值。
- 官方与自研 evaluator 的现有命令行入口必须分别支持同名 `--protocol-profile` 参数，并在一条 `python -m ...` 命令中直接传递 profile，不要求用户预先导出环境变量或修改源码。
- profile 只允许 `official` 与 `natural_language`；零参数构造必须使用 `official`，保持官方 evaluator 兼容。
- `official` 使用确定性协议解析路径和 `protocol_aware` 澄清策略；不得因为存在 API key 而自动切换为自然语言 profile。
- `natural_language` 使用当前 `IntentInterpreter` 路径，并使用基于候选信息量的具体属性澄清策略；模型后端仍必须显式启用且无模型时可确定性降级。
- 显式注入的 `intent_interpreter` 与 `clarification_policy` 优先于 profile 默认组件，保留现有测试和实验依赖注入边界。
- 两种 profile 产生统一的 `IntentUpdate` / `SessionState` 后，必须共享 `StateReducer`、`StructuredCandidatePool`、`RetrievalEngine`、`RankingEngine`、`RecommendationCommitPolicy`、语义排序和响应守卫。
- profile 名称进入初始化和逐轮 diagnostics，便于确认实际运行模式；不得暴露 evaluator 隐藏状态或目标。
- 不修改 `reset(session_id, user_profile)`、`respond(session_id, user_message, turn, top_k)` 和响应 schema。
- README 明确 `starter/` 是正式主代码目录、`starter.agent.Agent` 是唯一入口，并说明 profile 用法和主要顶层目录职责。

## Out of Scope

- 不根据消息格式、样本内容、session ID 或 evaluator 特征自动识别 profile。
- 不为两个 profile 复制或分叉召回、排序、结构化候选池、推荐提交或响应合同实现。
- 不修改官方 `evaluator/` 的评分语义。
- 自研 benchmark 的改动只限于 `--protocol-profile` CLI、子进程 loader/worker 透传、相关测试和 README 示例；不修改数据生成、模拟器或评分语义。
- 本任务不执行 `main` 与当前分支的 merge、rebase 或 cherry-pick；是否将验证后的统一实现推进 `main` 是后续发布决策。
- 不移动、重命名或删除现有顶层目录。

## Acceptance Criteria

- [x] `Agent()` 与 `Agent(protocol_profile="official")` 行为等价，官方 evaluator 的调用方式无需改变。
- [x] `Agent(protocol_profile="natural_language")` 选择自然语言意图解释与信息量澄清默认组件。
- [x] `AgentConfig` 和环境配置能显式选择 profile，非法值以明确错误失败或安全回退到 documented default，不会静默选择另一策略。
- [x] 显式 interpreter/policy 注入覆盖 profile 默认值，现有兼容委托继续有效。
- [x] 测试证明两种 profile 共享同一召回、排序和推荐提交实现，并且 profile 不改变公开响应合同。
- [x] 当前仓库完整 unittest 通过；官方 evaluator 契约测试单独通过。
- [x] 自研 benchmark 完整 unittest 通过，并能以 `natural_language` profile 加载当前 Agent 完成至少一个离线 smoke evaluation。
- [x] 以下两种形式均可直接执行，且 profile 进入 Agent diagnostics：官方 `python3 -m evaluator.local_evaluator --protocol-profile official ...`；自研 `python3 -m nl_benchmark evaluate --protocol-profile natural_language ...`。
- [x] README 说明正式入口、profile 配置、目录职责以及本任务没有执行分支合并。

## Notes

- 这是复杂任务，需要 `design.md` 和 `implement.md`。
- 实现跨越当前 Agent 仓库与独立自研 benchmark 仓库，但 profile 适配核心只存在于 Agent 仓库；benchmark 只负责显式透传。
