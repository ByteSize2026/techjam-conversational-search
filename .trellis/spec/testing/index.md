# 测试规范

## 适用范围

适用于本仓库的标准库 `unittest` 回归、临时 JSONL catalog fixture 和确定性 Agent 替身。当前测试基线为 `tests/test_evaluator.py`，不假设存在额外测试框架。

## 真实来源

- 测试结构、断言风格和临时目录：`tests/test_evaluator.py`。
- 需锁定的纯函数和评估循环：`evaluator/local_evaluator.py`。
- 可构造的最小数据字段：`data/README.md` 与 `docs/competition_specification.md`。

## 开发前检查

- [ ] 使用 `unittest.TestCase` 与 `self.assertEqual` 等标准库断言，遵循现有文件风格。
- [ ] 外部文件 fixture 放在 `tempfile.TemporaryDirectory()` 内，不写入仓库数据目录。
- [ ] 替身 Agent 实现实际会被调用的 `reset` 和 `respond`，响应保持确定性。
- [ ] 每个测试仅构造 evaluator 读取所需的最小 catalog/sample 字段。

## 专题链接

- [unittest 与 fixture 模式](./unittest-fixtures.md)
- [评估器的标准化和指标](../evaluator/protocol-and-scoring.md)
- [JSONL 数据规则](../data-contracts/jsonl-privacy-submission.md)
- [变更影响检查](../guides/change-impact.md)

## 质量检查

- [ ] 运行 `python3 -m unittest discover -s tests -v`。
- [ ] 覆盖正常路径及至少一个协议边界：重复/无效 ID、miss=11、Intent Override 或 Boundary。
- [ ] fixture 在上下文退出后自动清理，测试之间不依赖运行顺序、真实 catalog 或网络。
- [ ] 测试验证可观察合同，而非复制被测实现的内部步骤。

## 交叉引用

指标含义以 [Evaluator](../evaluator/index.md) 为准；提交不得携带测试生成物的边界见 [Data Contracts](../data-contracts/index.md)。
