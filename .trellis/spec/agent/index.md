# Agent 规范

## 适用范围

适用于 `starter/agent.py` 及提交包中实现同一 `Agent` 接口的本地辅助模块。这里规定参赛 Agent 的可编辑边界、会话生命周期和输出合同；不改变 `evaluator/local_evaluator.py` 的评分语义。

## 真实来源

- Agent 总体能力目标与范围限制：`docs/problem_statement.md` 的四大架构支柱及 Constraints & Scope。
- 接口与字段上限：`docs/agent_api_contract.json` 的 `reset_request`、`turn_request`、`turn_response`。
- 当前基线与状态前置条件：`starter/agent.py` 的 `Agent.__init__`、`Agent.reset`、`Agent.respond`。
- 实际调用顺序和异常处理：`evaluator/local_evaluator.py` 的 `evaluate`。
- 比赛协议：`docs/competition_specification.md` 的“Session Protocol”和“Required Agent Interface”。

## 开发前检查

- [ ] 确认导出的类名为 `Agent`，并保留 `reset(session_id, user_profile)` 与 `respond(session_id, user_message, turn, top_k)` 签名。
- [ ] 明确新增状态按 `session_id` 隔离，且 `reset` 先于同会话的 `respond`。
- [ ] 按 `top_k=10` 和至多 10 轮设计；不要把未发送给 Agent 的目标、意图卡或模拟器状态当作输入。
- [ ] 输出前按本专题的字段、枚举和推荐 ID 规则检查。
- [ ] 若修改工具规划，检查 action 闭集、已观察 ID 来源、profile 显式读取、ask-user 暂停/恢复和有界 fallback。

## 专题链接

- [接口、状态与输出规则](./contract-and-state.md)
- [有界工具循环](./tool-loop.md)
- [评估器的容错与评分](../evaluator/protocol-and-scoring.md)
- [JSONL、隐私和提交边界](../data-contracts/jsonl-privacy-submission.md)
- [最小回归 fixture](../testing/unittest-fixtures.md)

## 质量检查

- [ ] 对每个会话先调用 `reset`；未知会话的处理明确且不会串用其他会话状态。
- [ ] `message` 为字符串，`ask_attribute` 为允许值或 `None`，推荐按最佳到最差排序。
- [ ] 推荐只使用冻结 catalog 中的 `parent_asin`，并在 Agent 侧尽早去重。
- [ ] 任何网络、凭据或本地资源前提均在提交说明中写明，并提供或说明离线路径。
- [ ] 工具模式关闭或模型不可用时，默认 deterministic 路径仍可离线运行且旧 evaluator 指标可对照。

## 交叉引用

会话协议与命中限制由 [Evaluator](../evaluator/index.md) 执行；数据可见性和提交限制见 [Data Contracts](../data-contracts/index.md)。
