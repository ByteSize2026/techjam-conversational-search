# Shopping Copilot 实现方案与架构

## Goal

为 TechJam Conversational E-Commerce Search Challenge 定义一套可实现、可验证、可离线复现的 Shopping Copilot 架构，使后续开发能在不引入通用 Agent 编排框架或常驻中间件的前提下，系统性提升 HitRate@10、MRR 与 MTTC。

本任务先完成需求、技术设计和实施计划；用户评审通过后进入代码实现。2026-08-26 用户已确认按三级模型后端方案开始实现。

## Background

当前 `starter/agent.py` 只是无状态 SQLite FTS5/BM25 baseline：它只检索当前一条消息，不使用 `user_profile`，不保存约束，不追问，也不处理 Intent Override 或 Boundary。公开基线为 HitRate@10 `0.125`、MRR `0.068034`、MTTC `9.81`、TechnicalScore `0.10671`；本任务规划阶段已在现有 50,000 行 catalog 上精确复现这些数值。

评测器为单进程、本地 Python 调用。每个会话最多 10 轮，只有前 10 个合法且唯一的 `parent_asin` 被评分。自然语言问题不会驱动模拟器，`ask_attribute` 才是顾客披露更多信息的控制面；Agent 可在同一轮同时提问和推荐。

## Confirmed Decisions

- 不采用 LangGraph、LlamaIndex 或其他通用 Agent/RAG 编排框架。
- 使用纯 Python 模块和显式状态机；模块边界保留未来迁移到图编排的可能，但当前不为此增加运行时依赖。
- 不引入 Redis、消息队列、独立数据库服务、向量数据库或容器编排。
- 保留 `starter.agent.Agent.reset/respond` 作为唯一公开入口。
- 每轮通过显式 `IntentRouter` 在 Buying 与 Browsing 两条检索路线之间选择或混合，不把首轮路由永久锁死。
- 目标 MVP 仍包含 LLM 能力，但不需要 LLM/Agent 框架。LLM 承担受约束的意图理解和候选集语义精排；商品 ID、状态归并、硬约束、候选边界和响应 schema 由确定性代码控制。
- `CandidateGate` 必须在昂贵模型阶段前识别 over-generality；候选过宽时限制检索/精排预算，并优先返回结构化澄清问题。
- 模型能力采用显式三级后端：首选 `deepseek-v4-flash` API；不可用时尝试用户声明的 3B–8B 本地 OpenAI-compatible 服务；两者都失败时使用确定性 parser/Feature Ranker。
- API 与本地模型均为可选能力；无凭据、无网络、无本地服务时，Agent 仍须完成会话、检索和合法 Top-K 输出。
- 最终提交不得假设组织方提供足以承载 DeepSeek-V4-Flash 权重的 GPU；开放权重部署是独立的服务器级选项，不作为评测环境前提。

## Functional Requirements

### Agent contract

- 保持 `Agent.reset(session_id, user_profile)` 与 `Agent.respond(session_id, user_message, turn, top_k)` 的签名。
- 输出始终包含合法字符串 `message`、允许值或 `None` 的 `ask_attribute`、最多 `top_k` 个按优先级排序的合法唯一推荐。
- 模型或内部模块失败时不得向评测器抛出未处理异常；必须返回合法降级响应并记录内部诊断。

### Session and intent state

- 每个 `session_id` 独立保存 profile、类目锚点、活动约束、拒绝/无偏好信息、已问属性、推荐历史和当前意图 epoch。
- 支持约束新增、同属性替换、显式移除、全局/局部 override、无偏好和否定表达。
- Intent Override 后不得继续使用被覆盖的旧偏好；新 intent epoch 必须重新检索，并重置仅对旧意图有效的“已推荐排除”信息。
- Boundary 的“没有偏好”应标记该属性已穷尽，不能反复追问或臆造约束。

### Retrieval and ranking

- 以冻结的 50,000 商品 catalog 为唯一商品 ID 来源。
- `IntentRouter` 至少输出 `buying`、`browsing` 和混合权重：Buying 强调硬约束、类目和 lexical precision；Browsing 强调 dense recall、跨措辞/场景匹配与结果多样性。
- Multi-Route Retrieval 必须支持 keyword、category 与 vector 三类信号，并通过可解释的候选合并进入统一排序。
- Dense retrieval 是 Browsing 目标路线的一部分，但必须同时满足离线资产、CPU 延迟、内存和 candidate-recall 收益门槛；若官方资源不允许，必须保留可运行 fallback 并披露偏差。
- 硬约束采用 match / contradict / unknown 三值判断。只有可靠矛盾才能硬过滤；metadata 缺失时应 fail open，避免误杀目标。
- 确定性 Feature Ranker 先把 union 压缩到 20–30 个候选，再由 `LLMSemanticRanker` 执行一次受控 listwise semantic ranking；LLM 只能重排 catalog-valid 候选。
- 排序综合文本相关性、结构化约束、profile 弱先验、流行度/质量先验、本 intent epoch 内的已推荐覆盖和 LLM semantic score。
- 每轮都尽量返回 Top-10；提问不应导致空推荐。

### Clarification policy

- `CandidateGate` 根据 cheap-probe candidate count、候选 entropy、活动约束数量和剩余轮次判断 over-generality。
- over-generality 成立时，跳过昂贵 LLM semantic ranking，限制本轮召回规模，返回廉价但多样的 Top-10，并立即提出一个高信息量 `ask_attribute`。
- 根据未确认属性、候选分布、预计可披露信息、剩余轮次和已问历史选择一个 `ask_attribute`。
- 支持协议感知的 `other` 兜底策略，但必须在实验中与更自然的属性信息增益策略分开评估，防止把模拟器捷径误当成通用能力。
- 当继续追问的预期收益低或已无可用属性时，允许返回 `ask_attribute=None`，但仍必须给出最佳候选。

### Dynamic context programming

- 每轮把消息历史、活动约束、provided profile、intent epoch、候选统计和剩余轮次蒸馏为有限的 `RuntimeContext`，不把完整聊天历史无界塞入 prompt。
- `AdaptiveOrchestrator` 只根据 `RuntimeContext` 选择 route、召回预算、是否运行 Dense/LLM Ranker 和澄清策略；它是确定性策略控制器，不是自由循环 Agent。
- 由于合同没有稳定 user ID，不实现跨 session 身份记忆；provided profile 是长期先验，当前对话只更新 session-local distilled profile。

### Model assistance

- 定义薄 `ModelAdapter`，不允许模型自由循环调用检索工具。
- 模型输出必须解析为受控的结构化意图更新；任何未知字段、无效 action 或低置信结果均由确定性规则拒绝或降级。
- `LLMSemanticRanker` 只接收已验证的 20–30 个候选及压缩商品文本，只能输出这些 ID 的 permutation/score，不得召回新商品。
- 本地模型后端、模型资产大小、冷启动和 CPU 延迟必须在选型实验中量化；云端后端必须通过环境变量配置并报告真实 usage。
- 默认配置按 `DeepSeek API -> local OpenAI-compatible endpoint -> deterministic` 顺序选择模型能力；仅当对应环境变量显式启用时才访问网络或本地服务。
- deterministic parser/Feature Ranker 是完整的离线执行路径；它不伪装成模型推理，也不报告虚假 token usage。

### Experimentation and diagnostics

- 除官方指标外，记录 candidate recall@N、逐场景指标、首次命中轮、排名、每轮询问、状态变更、fallback 次数、初始化时间和响应延迟。
- 公共集必须按 scenario 与 difficulty 分层，保留 locked holdout；不得用 200 个公开 target ID 制作运行时查表捷径。
- 每个实验只改变一个主要模块，并保留配置、结果和逐会话输出以便回归定位。

## Non-Functional Requirements

- Python 3.10+；默认进程内运行，不要求 HTTP 服务。
- 配置集中、版本固定、无明文密钥；正式运行需有一条清晰命令。
- 离线核心路径不得访问网络；所有可选模型和预计算资产需有来源、版本、校验及资源说明。
- 优先保持可测试和可解释，避免隐藏状态、隐式全局单例及不可控 Agent 循环。
- 在官方未给出硬件/超时上限前，所有 dense/reranker 决策必须同时报告 CPU cold/warm 性能，并保留 lexical-only 开关。

## In Scope

- 会话状态、意图更新、检索、过滤、融合、排序、提问策略、模型适配、响应构造与诊断的模块边界。
- 分阶段实施顺序、验证门槛、回滚路径和实验纪律。
- 后续需要新增的测试、配置、依赖与提交说明。

## Out of Scope

- 不修改评测器或公开数据。
- 不训练大型基础模型，不恢复隐私字段，不使用 ground truth 作为 Agent 运行时输入。
- 不设计 Web UI、交易流程、分布式服务或长期用户画像平台。
- 不承诺未经 benchmark 的具体 embedding、reranker 或 LLM 型号为最终选型。

## Acceptance Criteria

- [x] 明确是否使用通用 Agent 框架及其边界。
- [x] 给出从 `reset/respond` 到最终 Top-10 的完整组件和数据流设计。
- [x] 定义 SessionState、约束更新、Intent Override、Boundary 与推荐历史的语义。
- [x] 定义 lexical、可选 dense、结构化过滤、融合与可选 rerank 的职责和启用门槛。
- [x] 定义澄清问题策略以及 `other` 的协议感知实验边界。
- [x] 定义模型适配与无效输出降级边界。
- [x] 显式覆盖 Dual-Track Routing、Over-Generality Cutoff、Dynamic Context Programming 与 LLM Semantic Ranking。
- [x] 给出模块布局、测试策略、指标、实验切分、资源验证与回滚方案。
- [x] 给出有序实施计划，并在实现前设置用户评审 gate。

## Open Decisions for Later Optimization

- 本地模型是否随提交打包，以及组织方允许的模型/向量资产大小上限。
- 最终 embedding、reranker 和 3B–8B 本地 LLM 的具体 checkpoint/runtime；首版只规定 OpenAI-compatible 接口。
- 官方 CPU、内存、单轮 timeout、网络和 GPU 条件。
- `other` 优先策略与自然属性策略在 locked holdout 上的最终取舍。
