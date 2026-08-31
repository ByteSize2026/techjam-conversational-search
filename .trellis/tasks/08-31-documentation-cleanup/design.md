# 文档整理与清理设计

## Target Structure

```text
README.md
docs/
  README.md
  architecture.md
  development/
    benchmarks.md
    local-evaluation.md
    model-backends.md
  reports/
    evaluator-comparison-2026-08-31.md
  submissions/
    devpost-zh.md
  diagrams/
    generate_evaluator_comparison_charts.py
    generate_submission_charts.py
    official_evaluator_comparison_200.{png,svg}
    smart_evaluator_comparison_100.{png,svg}
    official_score_progression.svg
    natural_language_deepseek_gain.svg
    system_diagrams_*.{mmd,svg}
```

公开合同与机器可读参考继续保留在 `docs/` 根层，避免改变稳定路径。目录只表达新增文档的职责，不迁移现有稳定参考。

## Asset Policy

- evaluator 报告使用 `official_evaluator_comparison_200` 与 `smart_evaluator_comparison_100` 两套完整图，两个 evaluator 不共享画布或坐标。
- 每套 evaluator 图保留 SVG 源渲染和 PNG 兼容渲染；生成脚本使用相同 basename。
- Devpost 使用 `official_score_progression.svg` 与 `natural_language_deepseek_gain.svg`，分别表达官方离线进展和自然语言增强收益。
- 删除 `_paper`、standalone quality/scenarios/resources 等不再引用的变体，避免同一结论维护多份图。

## Cleanup Boundaries

- 缓存清理只匹配仓库内 `.DS_Store`、`__pycache__` 和 `*.pyc`，排除 `.git`。
- 旧产物只删除根目录三个明确对象：`results.json` 与两个指定 Colab ZIP。
- worktree 删除前再次确认目标路径、锁状态与分支提交包含关系，然后使用 `git worktree unlock/remove`；保留 `worktree-agent-ad48316d4e0ef0588` 分支。
- `artifacts/` 等今天生成但未确认删除的内容保持原样。

## Compatibility and Rollback

- 文档移动会同步更新根 README、文档索引和内部相对链接。
- 文本变更可按 Git diff 回退；删除的缓存和 ZIP 为可再生成本地产物。
- 旧 `results.json` 已被项目记录标记为 stale，删除不会改变 evaluator 默认输出行为。
- worktree 分支保留，因此必要时仍可从该分支重新创建 worktree。
