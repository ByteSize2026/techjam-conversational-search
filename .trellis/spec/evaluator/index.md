# Evaluator 规范

## 适用范围

适用于理解、调用或为 `evaluator/local_evaluator.py` 编写兼容 Agent/测试替身的工作。评估器是公开集模拟、响应标准化和评分的事实执行者；通常不应为改进 Agent 而修改它。

## 真实来源

- 运行协议、容错、标准化、指标：`evaluator/local_evaluator.py` 的 `evaluate`、`normalize_recommendations`、`metric_summary`。
- 固定配置和权重：`docs/evaluation_config.json`。
- 场景定义和计分解释：`docs/competition_specification.md`。
- 已锁定的回归行为：`tests/test_evaluator.py`。

## 开发前检查

- [ ] 区分响应 schema 可接受的内容与 `normalize_recommendations` 实际计分的内容。
- [ ] 识别拟影响的场景：`buying`、`browsing`、`intent_override` 或 `boundary`。
- [ ] 检查改变是否影响 turn 1–10、miss=11、精确 ID 匹配或总分权重。
- [ ] 若只改 Agent，先用真实 evaluator 合同验证，而不是修改其容错逻辑。

## 专题链接

- [模拟协议、过滤与评分](./protocol-and-scoring.md)
- [Agent 接口与状态](../agent/contract-and-state.md)
- [测试 fixture 规则](../testing/unittest-fixtures.md)
- [跨层变更影响](../guides/change-impact.md)

## 质量检查

- [ ] 有效命中只来自前 10 个唯一 catalog-valid `parent_asin` 的精确匹配。
- [ ] Intent Override 在覆盖消息前不允许转化；Boundary 的“无偏好”回复可被处理。
- [ ] Hit@10、MRR、MTTC、Efficiency 和复合分严格按真实公式计算。
- [ ] 修改评估相关代码时，用临时小 catalog 和确定性 Agent 覆盖正常、重复/无效 ID 与 miss。

## 交叉引用

Agent 输出的构造规范见 [Agent](../agent/index.md)；指标使用的数据和离线命令边界见 [Guides](../guides/index.md)。
