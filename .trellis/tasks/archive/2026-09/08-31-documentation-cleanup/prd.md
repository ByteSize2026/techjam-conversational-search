# 整理项目文档并清理已确认的无用文件

## Goal

在不改变 Agent、evaluator、公开比赛合同和用户仍在使用的评测数据的前提下，整理仓库文档入口与目录职责，修复失效链接，并删除用户已经确认不再需要的本地缓存、旧评测产物、Colab 打包文件、重复图表和旧 Claude worktree。

## Background

- 现有长期文档已经按 Tutorial、How-to、Explanation、Reference 建立基本导航，不需要整体重写。
- 2026-08-31 新增的 evaluator 对比报告和 Devpost 中文稿尚未提交；两者目前分类不准确，Devpost 稿还包含失效的 Obsidian 图片占位符。
- evaluator 图表同时存在完整图、paper 版、拆分图以及 PNG/SVG 多套输出，需要收敛为可复现的权威版本。
- 根 README 链接到被 Git 忽略的本地 `SHA256SUMS`，该链接在干净 checkout 中不可用。
- 旧 `.claude/worktrees/agent-ad48316d4e0ef0588` 的提交已包含在 `main`，但 Git 仍将其记录为 locked worktree。
- 用户已明确批准“全部清理，包括旧 worktree，并按建议整理文档”。

## Requirements

- 保留根 README、文档索引、架构说明、开发指南、公开比赛参考、数据说明和 Trellis 工作流记录。
- 将 evaluator 对比报告归入 `docs/reports/`，将 Devpost 中文稿归入 `docs/submissions/`，并更新所有内部链接。
- Devpost 稿增加一级标题，用现有的官方成绩进展图和自然语言 DeepSeek 收益图替换失效的 Obsidian 图片引用。
- evaluator 报告只保留每套 evaluator 一张独立完整图；每张图保留 PNG/SVG 一对，并保留可复现生成脚本。删除 paper/standalone 等不再引用的替代版本，同时同步简化脚本输出。
- 修正 README 的 checksum 说明，使其不依赖被 Git 忽略的本地文件。
- 删除 `.DS_Store`、所有项目内 `__pycache__`/`*.pyc`、旧根目录 `results.json` 和两个 `techjam-colab-*.zip`。
- 通过 Git worktree 命令删除旧 Claude worktree 及其 worktree metadata；不删除对应 Git 分支。
- 保留 `artifacts/`、`holdout/`、catalog、checksum 本地文件、当前 `.env`、Trellis 任务与其他用户改动。
- 不修改 Agent、evaluator、测试行为、比赛合同 JSON 或数据内容。

## Out of Scope

- 删除 `artifacts/`、`holdout/`、catalog、`SHA256SUMS` 或 `.env`。
- 清理或重写 Git 历史、删除旧 worktree 对应分支。
- 重写现有架构、模型后端、benchmark 或比赛规则正文。
- 运行联网评测或重新生成业务结果。

## Acceptance Criteria

- [x] 根 README 和 `docs/README.md` 能正确导航到移动后的报告与提交材料。
- [x] 所有仓库内 Markdown 链接和图片引用存在，Devpost 稿不再包含 Obsidian 占位符。
- [x] evaluator 报告只引用两张互不混合 evaluator 的完整图；每张有 PNG/SVG 可复现版本。
- [x] 不再保留未引用的 evaluator 替代图表；生成脚本只生成保留的权威资产。
- [x] 已确认的缓存、旧结果、Colab ZIP 和旧 worktree 被删除，旧 worktree 分支仍保留。
- [x] `artifacts/`、`holdout/`、catalog、checksum、`.env` 与无关用户改动保持不变。
- [x] 文档链接检查、图表生成检查、`git diff --check` 和相关文档引用检查通过。
- [x] 最终 Git diff 不包含 Agent、evaluator、测试逻辑或公开合同变化。

## Notes

- 文档模型：README 为 Tutorial 入口；development 为 How-to；reports 为 Explanation/Reference；submissions 为交付材料。
- 删除操作以本 PRD 的显式路径和模式为边界，不把“整理”解释为删除未列出的本地数据。
- Spec review：本次没有新增或修改命令/API/数据合同，也没有形成需要写入 `.trellis/spec/` 的长期代码约定，因此无需更新 code-spec。
