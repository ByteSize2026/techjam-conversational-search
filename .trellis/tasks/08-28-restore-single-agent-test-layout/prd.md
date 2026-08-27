# 恢复单一 Agent 并整理测试模块

## Goal

恢复 4b71160 之前的单一正式 Agent 架构，让现有检索、状态、可选 LLM/Qwen
重排与 scoped Intent Override 都通过 `starter.agent.Agent` 进入官方 evaluator；移除
ContestAgent 比赛特供实现及其调参产物，并将保留的测试/benchmark 工具统一收纳在
`tests` Python 包下。

## Requirements

- `starter.agent` 只保留一个正式 `Agent` 类；不得再通过别名、继承或 facade 将
  evaluator 路由到第二套 Agent。
- 保留当前原架构已经完成的 scoped Intent Override、结构化候选池、确定性排序、
  自适应召回和可选模型重排能力。
- 删除 ContestAgent 专属实现、配置、测试、评测入口、tracked holdout、结果快照和
  调参报告；不得保留隐藏的运行时 import 或文档入口。
- 保留官方 `evaluator/`、官方公开集和通用的模型解析/检索回归；不得为了 Agent
  改动 evaluator 的评分语义。
- 将仍有价值的 Qwen benchmark 和 adaptive recall diagnostic 收纳为
  `tests.benchmarks` 子包，修正单元测试、命令示例和导入路径。DeepSeek 并行跑分
  及失效、只服务比赛特供消融的脚本直接删除。
- 不删除或提交本任务开始前无法归属的 untracked 文件；tracked 清理后遗留的
  untracked holdout 输出单独报告给用户。
- 不执行 push、reset、force checkout 或历史重写。

## Acceptance Criteria

- [x] `evaluator.local_evaluator` 导入的 `starter.agent.Agent` 是原检索/状态管道，
      并接受 `config`、`repository`、`model_client`、`semantic_ranker` 等既有注入参数。
- [x] 活跃源码和测试中不再定义或引用 `ContestAgent` / `LegacyAgent`；比赛特供的
      `contest_*`、tracked holdout、结果和报告文件已删除。
- [x] 原来导入 `LegacyAgent as Agent` 的脚本与测试全部恢复为直接导入 `Agent`。
- [x] Qwen benchmark 和 adaptive recall diagnostic 可通过
      `python -m tests.benchmarks.<name>` 调用，测试不再从根目录 `eval_*.py` 或
      `scripts.*` 导入；DeepSeek 并行跑分脚本已删除。
- [x] `python3 -m unittest discover -s tests -v` 全部通过。
- [x] 使用临时小 catalog 的 Agent 构造、reset/respond、依赖注入和 Intent Override
      回归全部通过；若完整 catalog 可用，再运行官方公开集 evaluator 并报告结果。
- [x] `git diff --check` 通过，且任务外 untracked 文件保持不变。

## Notes

- “删除比赛特供版”指删除 tracked ContestAgent 实现和专属实验工件，不进行 Git
  历史重写；旧提交仍可从 Git 历史审计。
- 通用的 `model.py` JSON 容错、DeepSeek/local JSON response 支持，以及
  `catalog.py` 的通用检索回归不因与 4b71160 同批提交而机械回滚。
