# Technical Design

## Current Problem

`starter.agent.Agent` 当前是 `ContestAgent` 的 PUBLIC facade，原完整管道被改名为
`LegacyAgent`。官方 evaluator 只导入 `Agent`，但大多数合同测试被改成导入
`LegacyAgent as Agent`，导致测试通过与正式入口行为脱节。随后实现的 scoped Intent
Override 也只接入了 Legacy 路径。

## Target Structure

```text
evaluator/local_evaluator.py
        -> starter/agent.py::Agent
        -> starter/shopping_agent/{state,retrieval,ranking,response,...}.py

tests/
  test_*.py                 unittest 回归
  benchmarks/
    __init__.py
    adaptive_recall.py      原 scripts/diagnose_adaptive_recall.py
    qwen_reranker.py        原 scripts/benchmark_qwen_reranker.py
```

仓库根目录不再保留实验性的 `eval_*.py`。官方 evaluator 仍位于 `evaluator/`，因为它
是生产合同实现，不属于可移动的测试辅助代码。

## Removal Boundary

删除：

- `starter/shopping_agent/contest_*.py`、`starter/shopping_agent/holdout.py`；
- `tests/test_contest_agent.py`、`tests/test_holdout.py`；
- `eval_contest.py`、`eval_holdout.py`、`eval_shard.py`；
- `eval_deepseek_parallel.py`（一次性在线 API 跑分脚本，含个人 Desktop `.env`
  路径假设且无独立回归）；
- 已失效且只服务该轮消融的 `eval_retrieval_combos.py`；
- tracked `holdout/`、`report/`、`results_contest*.json`、
  `results_retrieval_combos.json`。

保留：

- `evaluator/` 与 `data/public_set.jsonl`；
- 原架构、Qwen/LLM 和 adaptive recall 代码；
- 通用 `test_model_fallback.py`、`test_retrieval.py`；
- Qwen benchmark 和 adaptive recall diagnostic，但迁入 `tests.benchmarks`。

## Entry Restoration

将当前 `LegacyAgent` 类原位恢复命名为 `Agent`，删除文件末尾 Contest facade 及
imports，并把 `__all__` 恢复为仅导出 `Agent`。这是最小变更，可以完整保留
18e7d02 之后写入该类的 Intent Override 诊断和状态处理。

所有测试和 benchmark 直接导入 `starter.agent.Agent`。新增/保留一个 facade 合同
测试，断言构造注入参数确实生效，避免未来再次用吞掉 `**kwargs` 的 wrapper 替换
正式入口。

## Test Tool Relocation

使用 `git mv` 保留历史：

- `scripts/benchmark_qwen_reranker.py` -> `tests/benchmarks/qwen_reranker.py`
- `scripts/diagnose_adaptive_recall.py` -> `tests/benchmarks/adaptive_recall.py`

模块通过 `python -m` 从仓库根运行，因此 repo-root 计算从 `parents[1]` 调整为
`parents[2]`，单元测试导入同步更新。命令文档与 Trellis evaluator spec 同步新路径。

## Compatibility and Rollback

- 外部稳定边界仍为 `from starter.agent import Agent`；这是恢复而非新增 API。
- 不改 evaluator、响应 schema、catalog 或公开集。
- 文件删除和移动均由单个任务提交承载；如验收失败，可在提交前按 diff 精确回退，
  不需要 reset 或历史重写。
- 本任务不碰开始前的 untracked holdout、zip 或其他 Trellis 任务目录。
