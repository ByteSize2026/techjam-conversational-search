# Implementation Plan

## 1. Verify Sources of Truth

- [x] 读取 `Agent`、shopping_agent 组件、evaluator 入口以及两个 benchmark CLI 的实际接口。
- [x] 运行相关模块的 `--help`，记录可复现命令、默认路径和输出参数。
- [x] 检查现有 Markdown 的本地链接、重复内容、失效引用和基线措辞。
- [x] 记录任务开始前的 tracked/untracked 边界，后续不触碰无关本地文件。

## 2. Establish Documentation Navigation

- [x] 新增 `docs/README.md`，用 Diátaxis 四类建立文档地图和权威来源说明。
- [x] 重写根 `README.md` 为精简中文入口，并链接到公开英文规则和中文开发文档。
- [x] 删除 README 中指向缺失 organizer/checklist 文件的引用。
- [x] 明确 `baseline_results.json` 是 weak starter 历史参考，不代表当前 Agent 实时成绩。

## 3. Write Verified Project Documentation

- [x] 新增 `docs/architecture.md`，说明唯一 Agent 入口、组件职责、每轮数据流和修改影响面。
- [x] 新增 `docs/development/local-evaluation.md`，覆盖 catalog、测试、评测和结果解释入口。
- [x] 新增 `docs/development/model-backends.md`，覆盖 tier、环境变量、离线与失败回退。
- [x] 新增 `docs/development/benchmarks.md`，覆盖 adaptive recall 与 Qwen benchmark 的用途和入口。
- [x] 必要时只为导航/准确性调整现有英文公开文档，不改变其语言、路径或规则语义。

## 4. Clarify Repository Artifact Boundaries

- [x] 更新 `.gitignore`：`.claude/worktrees/`、`holdout/`、`report/`、
      `techjam-colab-*.zip`，并保留现有规则。
- [x] 确认未删除、移动或暂存任务开始前的未跟踪文件。
- [x] 确认源码、测试包布局、notebook 与 `.trellis/spec/` 无变更。

## 5. Validate

- [x] 运行 `python3 -m unittest discover -s tests -v`。
- [x] 运行两个 benchmark 模块的 `--help`，确认文档入口有效。
- [x] 用本地脚本检查 Markdown 相对链接目标存在，忽略外部 URL 和代码示例。
- [x] 运行 `git diff --check`。
- [x] 若 `data/catalog.jsonl` 存在，以 `/tmp` 输出路径运行 evaluator 验证文档命令，不覆盖
      用户已有 `results.json`。
- [x] 审查 `git diff --stat`、`git status --short` 和完整 diff，确认范围与语言边界。

## Risk and Rollback Points

- README 精简可能漏掉唯一说明：删除内容前先映射到目标文档，验证每项仍有权威入口。
- 文档命令可能触发大模型或写出大文件：仅运行离线 help/unittest；evaluator 写到 `/tmp`。
- ignore 规则可能过宽：只使用明确目录和 `techjam-colab-*.zip`，不使用全局 `*.zip`。
- 发现公开合同与实现不一致时，不自行改变比赛语义；保留合同并在结果中报告差异。

## Final Review Gate

实现前确认 PRD、设计和本计划已获用户明确批准；批准后再启动 Trellis task。若实现期间需要
改变公开合同路径、源码布局或加入团队规范，返回规划阶段重新确认。
