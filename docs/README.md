# 项目文档

这里是 TechJam 多轮对话电商搜索项目的文档入口。中文文档面向项目开发者；公开比赛规则、
提交规则、数据说明和机器可读合同保留英文及稳定路径。

## 教程：第一次运行项目

- [根 README 快速开始](../README.md)：准备 catalog、运行测试，并完成一次公开集评测。

教程以“成功跑通”为目标。已经熟悉项目、只想解决一个具体问题时，请直接查看下面的操作
指南。

## 操作指南：完成具体任务

- [本地开发与评测](development/local-evaluation.md)：运行测试、公开集评测并理解输出。
- [配置可选模型后端](development/model-backends.md)：启用 DeepSeek、本地兼容端点或 Qwen。
- [运行离线 benchmark](development/benchmarks.md)：执行 adaptive recall 诊断和 Qwen 实验。

## 架构解释：理解系统

- [系统架构](architecture.md)：唯一正式入口、组件职责、每轮数据流和变更影响面。

## 公开参考：查询合同与规则

- [Competition Specification](competition_specification.md)：比赛目标、会话协议和评分说明。
- [Submission Rules](submission_rules.md)：提交内容、网络限制与复现要求。
- [Agent API Contract](agent_api_contract.json)：请求和响应的机器可读 JSON Schema。
- [Evaluation Config](evaluation_config.json)：Top K、轮数、指标和权重配置。
- [Weak Starter Baseline](baseline_results.json)：已发布弱基线的历史参考结果。
- [Competition Data](../data/README.md)：公开集和 catalog 字段说明。
- [Data Attribution](../DATA_ATTRIBUTION.md)：数据来源与使用边界。

## 如何判断哪个文件是权威来源

同一事实只应有一个权威来源：

- 接口字段、类型和枚举以 `agent_api_contract.json` 为准。
- 评测常量和权重以 `evaluation_config.json` 与 evaluator 实现为准。
- 实际运行行为以 `starter/`、`evaluator/` 和回归测试为准。
- 参赛范围、数据政策和提交限制以英文公开规则为准。
- README 和中文操作指南负责引导与解释，不复制整份公开合同。

如果说明与实现不一致，先确认是否属于代码缺陷、规则文档漂移或历史基线差异，不要为了让
文档“看起来一致”而直接修改评分合同。
