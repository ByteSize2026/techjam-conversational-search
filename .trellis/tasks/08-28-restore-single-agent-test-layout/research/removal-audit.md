# ContestAgent Removal Audit

## Git finding

`4b71160` 没有删除原 Agent 方法，而是把原类改名为 `LegacyAgent`，新增
`Agent(ContestAgent)` facade，并把合同测试改成 `LegacyAgent as Agent`。`7915e53`
随后在 README 明确固定 `main = ContestAgent PUBLIC`。`87eafba` 合并没有吞掉本地
业务代码；本地分支相对共同父提交只有 Trellis 归档和 journal。

## File classification

Contest-only：`contest_*.py`、`holdout.py`、contest/holdout/shard eval、tracked
holdout/result/report、`test_contest_agent.py`、`test_holdout.py`。

Generic and retained：原 Agent 管道、scoped Intent Override、Qwen benchmark、adaptive
recall diagnostic、模型 JSON 容错和通用 repository tests。

Explicitly removed after user review：`eval_deepseek_parallel.py`。它是一次性在线 API
跑分脚本，包含个人 Desktop `.env` 路径假设且没有独立单元测试，不迁入测试模块。

Stale：`eval_retrieval_combos.py` 构造 Agent 时仍传入当前构造函数不接受的
`extra_routes` / `lexical_reranker`，且输出属于 4b71160 的消融快照；删除而不迁移。

## Dirty-tree boundary

任务开始前存在未跟踪的其他 Trellis 任务、`.claude/worktrees/`、zip 和若干新的
holdout 输出。它们不属于 tracked ContestAgent 清理，不得由实现代理删除或提交。
