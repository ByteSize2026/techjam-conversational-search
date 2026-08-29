# Robust evaluator feasibility analysis

## Goal

基于当前仓库与相邻 `../EComAgentBench_` 仓库的真实代码、数据合同和评分实现，判断新增 Robust Evaluator 是否技术可行、评估上有效、成本合理；如果关键数据或协议无法支撑，应给出明确的否定或降级建议，而不是仅凭问题背景肯定方案。

## Requirements

- 保持当前 benchmark 与 evaluator 完整不变，作为 baseline 和主要 score proxy。
- 审计当前仓库的 catalog / benchmark 数据类型、multi-turn agent/evaluator 协议、trajectory 表达、ranking 指标定义和报告产物。
- 审计 `../EComAgentBench_` 的 requirement/rubric 数据类型、case/user simulation 生成方式、商品事实来源和评分方式。
- 逐项评估 Robust Case Generator、Robust User Simulator、Ranking Scorer、Rubric Scorer、Reporter 的可实现性与复用边界。
- 特别验证当前 catalog metadata 是否足以可靠构造与判定 `attribute match`、`numeric range`、`negative constraint`，以及 target-product sampling 是否会产生伪难例或泄漏。
- 分析 robust ranking 指标与 existing ranking 指标能否真正横向比较，并指出语义不一致处。
- 给出 Go / Conditional Go / No-Go 结论、最小验证实验、风险、粗略工作量和停止条件。
- 研究任务只产出分析文档，不修改产品代码、当前 evaluator 或 benchmark。

## Acceptance Criteria

- [x] 所有关键结论均有仓库文件/符号/数据样例证据。
- [x] 给出两个仓库的数据类型与评分方式对照，而非只描述架构设想。
- [x] 对五个拟议模块分别给出可行性、依赖、风险与必要改动。
- [x] 区分可直接复用、接口适配、重新实现和当前数据无法支持的部分。
- [x] 接口/schema 不完全匹配不构成否决理由；进一步判断是否能通过确定性修复、现有数据增强/离线预处理、或由使用方提供外部 API key 的可控模型步骤实现逻辑复用。
- [x] 只有无法通过上述可控手段修复、或修复后仍会破坏评估有效性的差异，才作为 No-Go 证据。
- [x] 识别 validity threats：benchmark bias、rubric 可验证性、target leakage、单一 target 与多解需求、simulator pattern bias、LLM judge 依赖等。
- [x] 给出明确决策建议及一个低成本、可证伪的 pilot 方案。

## Out of Scope

- 本轮不实现 Robust Evaluator。
- 不改变当前 evaluator、benchmark、agent 或 competition score。
- 不以实际运行大规模模型评测替代静态可行性分析；若凭据或外部服务缺失，只设计验证方法。
