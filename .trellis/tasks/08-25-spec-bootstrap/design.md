# Trellis 规范初始化设计

## 设计目标

让 `.trellis/spec/` 的目录和内容直接反映当前项目，而不是沿用不适用的通用模板。未来开发者或 Agent 应能根据修改目标快速找到真实约束，并通过源码路径验证规则。

## 架构判断

当前仓库是单仓库 Python 3.10+ 竞赛包，没有前端、HTTP 服务、ORM、迁移、第三方依赖清单或现成日志框架。真实职责边界为：

- `starter/`：参赛者可编辑的 Agent 实现。
- `evaluator/`：公开集对话模拟、响应容错与评分。
- `tests/`：标准库 `unittest` 回归。
- `data/`：公开集和本地下载的冻结 catalog。
- `docs/`：机器可读接口、评分配置和比赛/提交规则。

因此采用单仓库一级领域规范，而不是伪造 package/frontend/backend 层级。

## 最终规范结构

```text
.trellis/spec/
├── agent/
│   ├── index.md
│   └── contract-and-state.md
├── evaluator/
│   ├── index.md
│   └── protocol-and-scoring.md
├── data-contracts/
│   ├── index.md
│   └── jsonl-privacy-submission.md
├── testing/
│   ├── index.md
│   └── unittest-fixtures.md
└── guides/
    ├── index.md
    ├── change-impact.md
    └── offline-reproducibility.md
```

每个领域采用“一个 index + 一个核心专题”的最小拆分；横切 guides 仅保留影响分析与离线复现两个高频主题，避免重复架构总览或泛化 style guide。

## 事实来源优先级

当来源描述不完全一致时，按以下顺序解释当前行为：

1. `evaluator/local_evaluator.py`：本地运行和评分的实际语义。
2. `starter/agent.py`：当前可编辑基线及项目内既有 Python 模式。
3. `tests/test_evaluator.py`：被回归测试锁定的行为。
4. `docs/agent_api_contract.json`、`docs/evaluation_config.json`：机器可读合同。
5. `docs/competition_specification.md`、`docs/submission_rules.md`、`data/README.md`：比赛、数据和提交边界。

规范应显式记录合同与实现的边界，例如 response schema 允许最多 100 个 recommendations，但 evaluator 只计前 10 个有效唯一 ID。

## 层间契约

核心影响链：

```text
catalog/public-set JSONL
  → Agent.reset / Agent.respond
  → evaluator.evaluate
  → normalize_recommendations
  → Hit@10 / MRR / MTTC / scenario metrics
```

- Agent 层拥有策略、会话状态、检索和输出构造。
- Evaluator 层拥有模拟协议、标准化和评分语义；Agent 开发默认不修改它。
- Data Contracts 层拥有 JSONL、标识、隐私、网络和提交边界。
- Testing 层用最小确定性 fixture 锁定协议行为。
- Guides 负责跨层影响半径和离线可复现性。

## 删除与替换策略

- 删除 `.trellis/spec/frontend/` 和 `.trellis/spec/backend/`，因为内容全部是占位模板且技术栈不存在。
- 删除旧 `code-reuse-thinking-guide.md` 与 `cross-layer-thinking-guide.md`，以项目专用的 `change-impact.md` 和 `offline-reproducibility.md` 替换。
- 不保留兼容性重定向文件，避免 Trellis discovery 继续暴露错误层级。

## 兼容性与范围控制

不修改 `.trellis/workflow.md` 或 Trellis 脚本。当前 `get_context.py --mode packages` 在无 packages 配置时列出 `.trellis/spec/` 下除 `guides` 外的一级领域；`guides` 是该脚本在多仓输出中单独处理的共享目录，但单仓简略输出不会逐项列出它。五个目录仍由最终 spec 文件集合、各 index 的交叉链接和任务校验共同验证。若未来需要在该命令的单仓输出中显式列出 `guides`，应另立任务修改 workflow runtime，而不是在本任务中改动它。

不修改产品源码、测试和竞赛文档；本任务只改变开发规范和当前任务上下文。

## 回滚

若新规范发现或校验失败：

1. 先修正 index、文件命名或 manifest 路径。
2. 若发现一级领域结构不受 Trellis 支持，暂停实施并回到规划，不直接修改 workflow runtime。
3. 删除新建规范并恢复旧模板仅作为最后回滚手段；旧模板不视为可交付状态。
