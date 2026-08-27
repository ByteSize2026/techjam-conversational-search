# 项目指南

## 适用范围

适用于跨越 Agent、评估器、数据合同和测试的变更评审，以及本地与提交环境的离线复现。指南补充领域规范，不覆盖或替代实际接口合同。

## 真实来源

- 总体目标、四大架构支柱、范围限制与评审维度：`docs/problem_statement.md`。
- 实际数据流：`starter/agent.py`、`evaluator/local_evaluator.py`。
- 数据与评分限制：`docs/competition_specification.md`、`docs/evaluation_config.json`、`data/README.md`。
- 提交与网络限制：`docs/submission_rules.md`。
- 可执行测试基线：`tests/test_evaluator.py`。

## 开发前检查

- [ ] 从修改的符号追踪到受影响的 Agent、evaluator、JSONL、测试和提交说明。
- [ ] 判断变更是否会影响有效推荐的前 10 个过滤、Intent Override、Boundary 或评分指标。
- [ ] 确认所需 catalog、Python 版本、依赖和环境变量，并识别网络前提。
- [ ] 对跨层变更先阅读影响指南；对任何本地运行路径阅读离线复现指南。

## 专题链接

- [跨层变更影响](./change-impact.md)
- [离线复现](./offline-reproducibility.md)
- [Agent 合同](../agent/contract-and-state.md)
- [评估协议](../evaluator/protocol-and-scoring.md)
- [数据与提交边界](../data-contracts/jsonl-privacy-submission.md)
- [测试 fixture](../testing/unittest-fixtures.md)

## 质量检查

- [ ] 变更后以真实入口运行 unittest；在 catalog 存在时再运行本地 evaluator。
- [ ] 记录或声明网络、模型凭据、依赖与离线 fallback 的实际行为。
- [ ] 不通过修改评估器、冻结数据或测试来掩盖 Agent 合同问题。
- [ ] 提交前复查只包含允许工件，且复现命令能从提交包和说明推出。

## 交叉引用

具体评分由 [Evaluator](../evaluator/index.md) 定义；本地数据和秘密的处理由 [Data Contracts](../data-contracts/index.md) 定义。
