# 实施计划

## Checklist

- [x] 再次记录 dirty tree 与明确删除目标，验证旧 worktree 没有 main 之外的提交。
- [x] 建立 `docs/reports/` 与 `docs/submissions/`，移动两份新增文档并更新 README 导航。
- [x] 修复 Devpost 标题、图片和移动后的相对链接；修正 checksum 说明。
- [x] 收敛 evaluator 图表为两套完整 PNG/SVG，更新报告引用和生成脚本。
- [x] 删除不再引用的 evaluator 图表变体。
- [x] 删除 `.DS_Store`、项目内 Python 缓存、旧 `results.json` 和两个 Colab ZIP。
- [x] 解锁并移除旧 Claude worktree，保留对应分支。
- [x] 检查 Markdown 本地链接、图片引用、生成脚本、Git diff 和保留边界。
- [x] 更新 PRD acceptance 状态并执行 Trellis 收尾流程。

## Validation

```bash
python3 docs/diagrams/generate_evaluator_comparison_charts.py
python3 docs/diagrams/generate_submission_charts.py
git worktree list --porcelain
git diff --check
git status --short --ignored
```

另使用只读本地链接检查，确保所有 Markdown 相对路径存在，并搜索 `![[`、旧文档路径和已删除图表 basename。

## Risk and Rollback Points

- 移动文档前保留当前 dirty-tree 清单，避免覆盖用户的未提交正文。
- 图表删除必须在新引用和生成脚本验证后执行。
- worktree 删除前再次验证分支没有独有提交；仅移除 worktree，不删除分支。
- 不运行 `git reset`、`git checkout --` 或历史重写。
