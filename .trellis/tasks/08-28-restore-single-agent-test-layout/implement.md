# Implementation Plan

1. 恢复唯一入口
   - 将 `LegacyAgent` 恢复为 `Agent`。
   - 删除 Contest facade/import/export。
   - 将所有 `LegacyAgent as Agent` 导入恢复为直接 `Agent`。
   - 增加构造注入与 evaluator facade 回归，锁定正式入口。

2. 删除比赛特供实现与工件
   - 删除 `contest_*`、专属 holdout helper、contest/holdout tests。
   - 删除 contest/holdout/shard/retrieval-combo 评测脚本。
   - 删除 tracked holdout、报告和结果快照。
   - 搜索并清除活跃代码/文档中的残留引用。

3. 整理测试模块
   - 创建 `tests/benchmarks` 包。
   - 迁移 Qwen benchmark 和 adaptive recall diagnostic。
   - 修正 repo-root、模块 imports、测试 imports 和 CLI 示例。
   - 删除迁移后为空的 `scripts/` tracked 内容。

4. 同步文档与规范
   - README 恢复单 Agent 架构说明并更新目录树/运行命令。
   - 更新 evaluator spec 中 benchmark 的新模块路径。
   - 更新 agent spec，移除 Legacy/Contest 双入口表述，保留 scoped override 合同。

5. 验证
   - `python3 -m unittest discover -s tests -v`。
   - `python3 -m tests.benchmarks.adaptive_recall --help`。
   - `python3 -m tests.benchmarks.qwen_reranker --help`。
   - `git diff --check` 与残留符号/文件扫描。
   - 若 `data/catalog.jsonl` 存在，运行官方公开集 evaluator，并保存输出到 `/tmp`。

6. Review / rollback gate
   - 检查 staged 前的完整删除清单和未跟踪文件。
   - 不自动提交或推送；按 Trellis Phase 3.4 单独向用户展示提交计划。

## Validation Result

- Independent review: PASS，未发现剩余 correctness/scope 问题。
- Focused tests: 66/66；full unittest: 92/92。
- Qwen/adaptive benchmark CLI smoke、py_compile、Notebook JSON、task validate、
  `git diff --check` 和残留引用扫描均通过。
- 官方 unchanged evaluator/public 200：Hit@10 1.000000，MRR 0.805024，
  MTTC 3.005000，TechnicalScore 0.901407，token usage 0。
