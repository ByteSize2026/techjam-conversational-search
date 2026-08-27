# Technical Design

## Target Repository Shape

```text
README.md                         中文项目入口与快速开始
DATA_ATTRIBUTION.md               英文公开数据归属
data/
  README.md                       英文公开数据参考
  public_set.jsonl                公开开发集
docs/
  README.md                       中文文档索引
  architecture.md                中文系统架构解释
  development/
    local-evaluation.md           中文本地开发与评测指南
    model-backends.md             中文可选模型后端指南
    benchmarks.md                 中文 benchmark 指南
  competition_specification.md    英文公开比赛规范（稳定路径）
  submission_rules.md             英文公开提交规则（稳定路径）
  agent_api_contract.json         机器可读接口合同（稳定路径）
  evaluation_config.json          机器可读评分配置（稳定路径）
  baseline_results.json           weak starter 历史参考（稳定路径）
starter/                          正式 Agent 与实现组件（不移动）
evaluator/                        官方本地评估器（不移动）
tests/
  test_*.py                       unittest 回归（不移动）
  benchmarks/                     诊断与离线 benchmark（不移动）
notebooks/                        可选实验 notebook（不移动）
```

仓库规模尚不需要把公开参考再嵌套到 `docs/reference/`。保留公开合同的现有路径可以避免
改动 README、Trellis spec 和外部使用者的链接；Diátaxis 分类通过 `docs/README.md` 的导航
表达，而不是用深目录强制表达。

## Documentation Boundaries

### Root README

根 README 是导航页和最短成功路径，不承担完整手册职责。它包含：项目一句话目标、代码
入口、准备 catalog、运行测试/评估器、文档地图、数据归属。模型环境变量、benchmark
参数、完整评分规则和提交细则只给摘要与链接。

### Documentation Index

`docs/README.md` 按读者任务而非文件扩展名导航：

- Tutorial：根 README 的快速开始。
- How-to：本地开发与评测、模型后端、benchmark。
- Explanation：系统架构。
- Reference：比赛规范、提交规则、API schema、评分配置、baseline、数据说明与归属。

### Architecture Explanation

`docs/architecture.md` 只解释当前稳定架构和变更影响，不重复每个类/函数的 API：

1. 唯一正式入口与 evaluator 调用顺序。
2. 会话状态、检索、结构化候选、确定性排序、可选语义排序和响应守卫的数据流。
3. `starter/shopping_agent/` 各模块职责。
4. 稳定合同与测试/benchmark 的兼容入口。
5. 常见改动应触达的文件与验证路径。

### How-to Guides

- `local-evaluation.md`：环境前提、catalog 准备、unittest、evaluator、结果位置与常见失败。
- `model-backends.md`：确定性默认路径、DeepSeek/local tier、环境变量、失败回退和 usage。
- `benchmarks.md`：两个 `tests.benchmarks` 模块的用途、help 入口、离线约束和产物边界。

这些指南只写当前代码能证实的命令。复杂参数以各 CLI 的 `--help` 为最终参考，减少参数
表与代码漂移。

## File and Compatibility Policy

- 不移动 Python 包或 notebook；本次“架构整理”是明确职责、入口和产物边界，不反转近期
  已验证的单 Agent 与 `tests/benchmarks` 决策。
- 不移动公开英文文档和 JSON；必要修改限于准确性与链接，不改 API/评分语义。
- `.gitignore` 只新增已确认的本地产物模式，使用窄模式匹配当前 Colab ZIP，避免屏蔽未来
  正式提交压缩包。
- 不删除现有未跟踪目录或 ZIP。ignore 规则只改变 Git 展示和误提交风险，不改变文件内容。

## Accuracy Strategy

- 模块职责从源码、现有 Trellis spec 和近期归档设计交叉验证。
- 命令与参数从 `python3 -m ... --help` 验证。
- 文档链接使用本地相对路径检查，不请求外网。
- `baseline_results.json` 保留为 weak starter 发布基准；如当前 evaluator 成绩不同，只调整
  文档措辞，不覆盖历史基线数据。

## Rollback

新增文档可逐文件移除，README 与 `.gitignore` 可按 diff 精确回退。由于不移动源码、不改
接口和数据，不需要代码迁移或数据回滚。任何意外行为 diff 都视为越界并在提交前撤销。
