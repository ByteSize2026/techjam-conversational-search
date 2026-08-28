# Implementation Plan

1. 修复 `README.md` judging/submission 区域的死链，仅保留真实提交规则。
2. 将 `state.py` 的意图解析正则、辅助函数和 `parse_intent_update` 机械迁移到
   `starter/shopping_agent/intent.py`。
3. 在 `state.py` 保留兼容导出；核对 `starter/shopping_agent/__init__.py` 和
   `starter/agent.py` 的公共导入不变。
4. 更新 `.trellis/spec/agent/contract-and-state.md` 的模块边界及依赖约束。
5. 运行针对性测试：
   `python3 -m unittest tests.test_agent_contract tests.test_deterministic_policy -v`。
6. 运行完整验证：`python3 -m unittest discover -s tests -v`、
   `python3 -m compileall -q starter evaluator tests`、`git diff --check`。
7. 检查最终 diff 只覆盖任务范围，特别确认 evaluator 和数据未改变。

## Review Gates

- 迁移后旧导入路径必须可用，才继续文档规范更新。
- 任一行为测试失败时先比较迁移前后的解析实现，不借机调整策略语义。
- 不运行完整公开集 evaluator，因为仓库当前缺少 `data/catalog.jsonl`。
