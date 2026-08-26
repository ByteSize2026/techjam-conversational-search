# Shopping Copilot 技术框架选型

## Goal

基于比赛接口、运行环境和提交约束，与用户共同确定最适合 Shopping Copilot Agent 的技术框架，为后续方案设计提供稳定、轻量、可复现的基础。

## Background

当前项目通过 `starter.agent.Agent` 暴露进程内 Python 接口，由 `python3 -m evaluator.local_evaluator` 直接加载并运行。用户明确要求：

- 本地 assets 和 config 必须轻量。
- 最好一条命令即可运行。
- 不使用 Redis、消息队列、独立数据库服务等中间件。
- 不为竞赛规模引入无必要的基础设施。
- 本阶段优先确定框架，不立即实现完整检索与对话方案。

## Confirmed Constraints

- Python 3.10+。
- 提交入口必须保持 `starter.agent.Agent` 的 `reset` / `respond` 合同。
- 评估器为单进程本地 Python 调用，不要求 HTTP 服务。
- catalog 是本地 JSONL；当前 baseline 使用进程内 SQLite FTS5。
- 官方环境可能限制网络；云端模型不可成为唯一运行路径。
- 必须提供能够完成完整多轮对话、约束抽取和推荐说明的本地模型降级路径；降级后不能退化为无 LLM 的纯规则 Agent。
- 外部模型、密钥、网络需求和本地 fallback 必须在提交说明中披露。
- 方案应能通过一个清晰命令安装/运行，不依赖常驻服务。
- LLM 是 MVP 的核心能力，必须参与 `respond()` 的对话理解、意图/约束抽取或回复生成，不能将纯规则检索器作为最终 Shopping Copilot。
- 商品 ID 合法性、硬约束过滤、候选排序边界和响应 schema 仍由确定性 Python 代码校验，不能完全交给 LLM。

## In Scope

- 确定是否需要 Agent 编排框架。
- 比较纯 Python、自建薄层及主流 Python Agent 框架的适配度。
- 确定框架与检索库、模型 SDK、配置方式之间的边界。
- 给出 MVP 推荐、采用条件和升级路径。

## Out of Scope

- 本轮不实现 Agent。
- 暂不确定最终模型供应商、embedding 模型、reranker 或完整检索算法。
- 暂不设计 Web UI、服务端 API、分布式任务、持久化中间件或部署集群。
- 不修改 evaluator、公开数据或评分配置。

## Requirements

- 框架不能要求 Redis、Postgres、消息队列、容器编排或独立服务。
- 核心会话状态可由进程内 Python 对象管理。
- 支持确定性测试和离线 fallback。
- 不得妨碍保持 `Agent.reset` / `Agent.respond` 的薄公开入口。
- 新依赖必须有明确收益；若纯 Python 足够，应优先减少依赖。
- 框架选型必须区分：Agent 编排、模型 SDK、结构化输出、检索/索引和配置管理。

## Acceptance Criteria

- [x] 从比赛限制和真实代码中提取框架选择约束。
- [x] 比较至少纯 Python 薄层、PydanticAI、LangGraph、LlamaIndex 等候选。
- [x] 给出一个首选方案和一个条件式备选方案。
- [x] 明确不采用中间件及重型服务框架的理由。
- [x] 明确单命令运行形态与依赖边界。
- [x] 记录用户对框架复杂度、模型供应商耦合或未来扩展性的最终偏好。

## Technical Findings

- LLM 是 MVP 的必需能力，但不需要通用多 Agent 或 RAG 编排。核心流程仍是显式的有限状态：LLM 负责对话理解、约束抽取与自然语言，Python 负责追问决策、本地检索、过滤、重排和响应校验。
- 50,000 条冻结 catalog 可由进程内 SQLite FTS5 处理；无需向量数据库或搜索服务。
- 若确实需要轻量模型编排，`pydantic-ai-slim` 是比图编排框架更贴近需求的候选；但当前固定流程不需要引入通用 Agent 框架。
- LangGraph 只有在出现复杂条件图、断点恢复或多工具循环时才值得采用；当前属于过度设计。
- LlamaIndex 更适合大量非结构化文档 RAG；本项目的固定商品 JSONL 和 exact `parent_asin` 评分不需要完整 LlamaIndex runtime。
- Ollama 可离线但通常需要额外服务进程；严格单进程时，本地模型应采用进程内 runtime，或将模型能力设为可选增强。

## Recommended Shortlist

1. **首选：纯 Python 显式状态机 + SQLite FTS5 + 薄模型适配层**。对话更新、检索、追问、排序和响应校验均保持可见、确定、可单元测试；模型调用可直接通过供应商 SDK 或本地 runtime 接入。
2. **条件式备选：`pydantic-ai-slim`**。仅在多模型适配、结构化输出重试和测试替身的重复代码已形成实际负担时引入。
3. **暂不采用：LangGraph / LlamaIndex**。保留为复杂条件图、长任务恢复、多工具循环或非结构化 RAG 真正出现后的升级路径。

## Final Decision

- 用户确认当前阶段不采用 LangGraph 或其他通用 Agent 编排框架，采用模块化纯 Python 显式状态机。
- LLM 运行后端、embedding 与 reranker 的最终选择转交后续“实现方案与架构”任务决定。
