# 修复仓库维护债务

## Goal

清理当前主线中已经确认的文档和架构维护债务，使 README 只引用真实工件、Trellis
Agent 规范准确描述现有组件，并在不改变正式 Agent 行为和公共导入路径的前提下缩小
`starter/shopping_agent/state.py` 的职责范围。

## Background

- `README.md:163-166` 引用 `docs/participant_release_checklist.md` 和三个
  `organizer/` 文件；这些路径不在当前树中，也未曾由主线提交提供。
- `.trellis/spec/agent/contract-and-state.md:19` 只列出 retrieval、ranking、response，
  但正式管道还包含 catalog、config、state、policy、structured pool、model、LLM 和
  Qwen reranker。
- `starter/shopping_agent/state.py` 同时包含状态模型、session store、state reducer、
  正则规则和消息意图解析，约 1350 行。现有调用方直接从 `state` 导入
  `parse_intent_update`，因此拆分必须保留兼容入口。
- 当前 `main` 与 `origin/main` 一致；任务开始前工作树干净；基线为 92 个 unittest
  全部通过。

## Requirements

1. 删除 README 中指向不存在工件的 judging/release 链接，保留真实存在的
   `docs/submission_rules.md` 入口，不虚构 organizer 文档。
2. 更新 Agent Trellis 规范中的实现模块边界，覆盖当前正式管道的实际职责和依赖
   方向，并明确 `starter.agent.Agent` 仍是唯一正式入口。
3. 将确定性的用户消息意图解析从 `state.py` 移到独立模块；状态模型、session
   store 和 reducer 保留在 `state.py`，避免数据模型循环依赖。
4. `starter.shopping_agent.state.parse_intent_update` 继续可导入并保持相同签名与
   返回类型；`starter.shopping_agent` 包级导出也保持兼容。
5. 不改变 evaluator、评分语义、公开数据、Agent `reset/respond` 合同、召回排序
   行为或模型后端策略。

## Acceptance Criteria

- [x] README 中所有仓库内 Markdown 路径均存在，已删除四个已确认的死链。
- [x] Agent 规范准确列出 catalog/config、state/intent、policy、retrieval、structured
      pool、ranking、model/reranker、response 的边界。
- [x] 意图解析实现位于独立模块，`state.py` 不再拥有正则解析规则和解析辅助函数。
- [x] 旧的 `state.parse_intent_update` 和包级导入保持工作，现有调用方无需迁移。
- [x] Intent Override、Boundary、无偏好和替换约束回归继续通过。
- [x] `python3 -m unittest discover -s tests -v` 全部通过。
- [x] `git diff --check` 通过，并且没有 evaluator、数据或任务外文件变更。

## Out of Scope

- 恢复已经删除的 ContestAgent、holdout、调参报告或 organizer 私有工件。
- 改写状态机语义、优化公开集分数或引入新的意图识别能力。
- 进一步拆分 `SessionState` 与 `StateReducer`；二者共享大量状态不变量，留待有明确
  行为需求时单独设计。
- 下载完整 catalog 或运行需要外部模型资产的 benchmark。
