# 整理仓库架构与项目文档

## Goal

在不改变 Agent、evaluator、测试与公开比赛合同的前提下，重新建立清晰的仓库入口、
目录职责和文档导航，让团队成员能够快速理解项目、完成本地运行，并找到架构、模型、
benchmark 与比赛规则的权威说明。

## Background

- 根 `README.md` 同时承载项目介绍、比赛规则、开发命令、模型配置和文件清单，内容过长，
  且中英文段落交错。
- `README.md` 引用了当前仓库不存在的 participant/organizer checklist 与 runbook。
- 当前源码结构是近期有意恢复并验证的单一正式入口：`starter.agent.Agent` 负责整轮编排，
  `starter/shopping_agent/` 按状态、召回、排序、模型适配与响应拆分职责。
- `tests/benchmarks/` 是近期明确确定的 benchmark 位置，不应再次迁移。
- `docs/competition_specification.md`、`docs/submission_rules.md` 和三个 JSON 文件是公开合同
  或机器可读参考，现有路径被 README 与 Trellis spec 广泛引用。
- 工作区已有未跟踪的 `.claude/worktrees/`、`holdout/` 和两个 Colab ZIP；它们是本地工作
  或私有/生成产物，不属于本任务的可删除、可提交内容。

## Requirements

- 根 `README.md` 作为中文项目入口，只保留项目定位、快速开始、核心入口、常用命令和
  文档导航；不在其中重复完整规则、模型配置或 benchmark 参数。
- 新增中文 `docs/README.md`，按教程、操作指南、解释和参考四类组织文档入口。
- 新增中文架构说明，准确描述 `Agent -> state/retrieval/ranking/model/response -> evaluator`
  的职责、数据流、稳定接口和修改影响面。
- 将本地开发、可选模型后端和 benchmark 使用说明拆成独立中文操作指南；命令必须来自
  当前 CLI、环境变量和模块路径，不写未经验证的启动方式。
- 保留公开比赛规则、提交规则、数据说明和机器可读合同的英文内容及稳定路径；只允许为
  准确性、导航或失效引用做必要修正，不改变比赛语义。
- 保留 `starter/`、`evaluator/`、`tests/benchmarks/`、`data/`、`notebooks/` 的当前代码/资产
  布局，不借文档整理再次重构正式 Agent。
- 更新 `.gitignore`，明确隔离本地 Claude worktree、私有 holdout、临时 report 和当前命名
  的 Colab ZIP 产物；不得删除、移动或提交任务开始前的未跟踪内容。
- `docs/baseline_results.json` 继续表示已发布的 weak starter 参考结果；文档不得把它误称为
  当前已演进 Agent 的实时成绩。
- 本任务不新增团队开发规范，不修改 `.trellis/spec/` 的领域开发规则。

## Documentation Model

- 文档类型：快速开始使用 Tutorial；本地评测、模型和 benchmark 使用 How-to；系统架构
  使用 Explanation；比赛规则、API 合同、评测配置和基线结果使用 Reference。
- 目标读者：首次接手仓库的团队开发者，以及查阅公开规则的参赛者。
- 读者目标：从根目录在数分钟内找到正确入口、运行命令和权威规则，并能判断一次修改应
  落在哪个模块。
- 明确排除：团队协作规范、分支/提交规范、代码风格制度、源码重构、评分策略修改、私有
  organizer 文档恢复、实验结果重跑与提交包制作。

## Acceptance Criteria

- [x] 根 README 可在不展开实现细节的情况下完成项目介绍、catalog 准备、测试与本地评测
      的首次引导，并链接到其余文档。
- [x] `docs/README.md` 能清楚区分教程、操作指南、架构解释和公开参考，所有入口均存在。
- [x] 中文架构文档与 `starter/agent.py`、`starter/shopping_agent/`、
      `evaluator/local_evaluator.py` 的实际职责一致，且明确 `starter.agent.Agent` 是唯一正式
      入口、`tests/benchmarks/` 是既定 benchmark 位置。
- [x] 本地开发、模型后端和 benchmark 文档中的命令、参数、环境变量均可由当前代码验证；
      无效或过时示例被删除。
- [x] `docs/competition_specification.md`、`docs/submission_rules.md`、`data/README.md`、
      `DATA_ATTRIBUTION.md` 保持英文，公开合同/配置文件路径不变。
- [x] README 不再引用仓库中不存在的 organizer/checklist 文件，也不把历史 weak baseline
      冒充当前 Agent 结果。
- [x] `.gitignore` 覆盖本任务确认的本地私有/生成产物；任务开始前的未跟踪文件仍原地存在，
      没有被纳入提交。
- [x] `python3 -m unittest discover -s tests -v`、文档本地链接检查和 `git diff --check`
      全部通过；若完整 catalog 可用，再用临时输出路径运行一次 evaluator smoke/完整验证。
- [x] 最终 diff 不包含 Agent/evaluator 行为变更、团队开发规范或 `.trellis/spec/` 修改。

## Constraints

- 所有事实以当前代码、CLI help、测试和现有公开合同为准，不访问外部资料补写项目行为。
- 不删除或覆盖用户的未跟踪文件，不执行历史重写、reset 或强制 checkout。
- 文档应通过链接引用权威内容，避免 README、开发指南和公开规则之间维护多份同义副本。
