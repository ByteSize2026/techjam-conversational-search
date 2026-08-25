# Trellis 规范初始化实施计划

## 执行清单

1. [ ] 复核当前任务、现有 spec 树和真实源码/合同。
2. [ ] 完成 `prd.md`、`design.md`、`implement.md`，运行规划阶段校验。
3. [ ] 运行 `task.py start` 激活任务后再修改规范文件。
4. [ ] 删除 `.trellis/spec/frontend/`、`.trellis/spec/backend/` 和两个旧 guides。
5. [ ] 创建 `agent` 规范，覆盖 Agent 合同、会话状态、检索与反模式。
6. [ ] 创建 `evaluator` 规范，覆盖模拟、标准化、场景和评分。
7. [ ] 创建 `data-contracts` 规范，覆盖 JSONL、隐私、冻结数据和提交限制。
8. [ ] 创建 `testing` 规范，覆盖 unittest、替身 Agent 和临时最小 fixture。
9. [ ] 重写 `guides` 为影响分析和离线可复现指南。
10. [ ] 更新 `implement.jsonl` 与 `check.jsonl`，移除 `_example`。
11. [ ] 检查 index 链接、源码引用、枚举和评分公式。
12. [ ] 运行全部验证命令；发现问题时修正规范，不修改产品代码来绕过验证。
13. [ ] 运行 `trellis-check` 做最终规范质量复核。

## 验证命令

```bash
python3 ./.trellis/scripts/task.py validate 08-25-spec-bootstrap
python3 ./.trellis/scripts/get_context.py --mode packages
find .trellis/spec -type f -name '*.md' -print | sort
grep -RInE 'TBD|To be filled|To fill|placeholder|Fill in|React|TypeScript|ORM|migration|SWR|React Query' .trellis/spec
grep -RInE 'ask_attribute|intent_override|normalize_recommendations|hit_rate_at_10|mrr|mttc|TemporaryDirectory|catalog\.jsonl|标准库|离线' .trellis/spec
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --help
```

## 质量门槛

- 任务校验成功，manifests 不含示例行。
- spec discovery 只列出计划中的五个领域。
- 占位/错误技术栈搜索无输出。
- 关键契约搜索能覆盖所有核心主题。
- 所有 unittest 通过，CLI help 可运行。
- 未修改范围外文件。

## 回滚点

- 删除旧模板前：规划产物已完成并校验。
- 新建规范后：先校验目录发现和链接，再更新 manifests。
- 最终验证失败时：只回滚或修正规范文件与任务上下文；不触碰产品源码、评估器或测试。
