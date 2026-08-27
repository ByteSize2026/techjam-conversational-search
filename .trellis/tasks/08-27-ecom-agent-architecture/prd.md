# ECom-style agent architecture upgrade

## Goal

在不改变 Hackathon 官方 `Agent.reset/respond` 接口和旧 evaluator 的前提下，把当前“每轮固定跑完整检索流水线”的 Agent，升级为能够主动决定何时读取 profile、搜索、过滤、查看商品详情、向用户澄清和输出推荐的工具型购物 Agent。第一阶段优先对标 EComAgentBench 中可由当前 catalog 支撑的基本能力，同时保留现有确定性检索/排序逻辑作为工具实现和离线 fallback。

用户价值：旧 benchmark 分数继续可比；Agent 内部开始具备可观察、可测试的主动信息获取和多轮任务执行能力，为后续 Robust Evaluator 提供真实的 tool trajectory，而不是只评估固定流水线对固定话术的适配。

## Background and Confirmed Facts

- 官方边界固定为 `reset(session_id, user_profile)` 与 `respond(session_id, user_message, turn, top_k)`；旧 evaluator 每个 session 最多调用 10 轮，并依赖结构化 `ask_attribute`。该公开合同不能改变。
- 当前 `starter/agent.py:Agent._respond_impl` 在每轮固定执行状态更新、route、retrieval、feature ranking、可选 semantic reranking、clarification policy 和 Top-K 输出；模型不能选择下一步动作。
- 当前已有能力可作为工具后端复用：内存 SQLite FTS/catalog repository、category recall、feature ranking、Qwen/兼容 API reranking、session state、override 和 `ask_attribute` 输出保护。
- EComAgentBench 的 Agent 使用普通 Python tool loop，并不要求 LangChain/LangGraph。其基础工具包括 search、attribute filter、product detail、profile、ask-user 和 recommend；评论相关工具依赖独立 review 数据。
- 当前 catalog 没有 review text，私有评测也不应假设会额外提供。第一阶段只使用 `data/catalog.jsonl`、reset profile 和对话消息，不实现评论能力。
- `reset()` 当前直接收到 profile。为测试主动 profile 获取，可以把 profile 保存在 session environment 中，而不是默认注入 planner context；planner 通过内部 `get_user_profile` action 显式读取。

## Requirements

- 保持 `starter.agent.Agent` 类名、构造兼容性、`reset/respond` 方法签名和响应 schema 不变；不修改现有 evaluator 和 benchmark。
- 新增一个有明确步数、超时和失败边界的内部 action loop。一次 `respond()` 内可执行多个不需要用户输入的动作；执行 `ask_user` 时暂停，转换成外部 `message + ask_attribute`，下一轮收到回复后恢复。
- 第一阶段 action 集至少包含：
  - `search_products`：复用当前 catalog/FTS/category recall 获得候选；
  - `filter_products`：按当前 catalog 中可验证的属性或数值收窄候选，并明确 missing/unknown 行为；
  - `get_product_details`：按 `parent_asin` 返回有界、可审计的商品 metadata；
  - `get_user_profile`：显式读取 reset 时保存的匿名 profile；
  - `ask_user`：提出问题并可靠映射到官方允许的 `ask_attribute`；
  - `recommend_products`：输出去重、catalog-valid、至多 `top_k` 个结果并结束本轮内部 loop。
- action schema、action result 和 trajectory 使用项目内的 Python 类型/JSON-compatible dict 表达，不强制引入 Agent framework、向量数据库或新服务。
- planner 必须可替换：允许模型驱动的 action selection，同时保留确定性策略作为无 API key、模型失败、超时或回归测试时的 fallback。
- session 必须隔离保存：已知需求、被覆盖/撤销的需求、profile 是否已读取、工具结果摘要、待回答澄清项、候选集、已推荐商品和完整 action trajectory。
- 旧 benchmark 路径默认可运行；任何模型/API/本地权重均为显式配置，不允许把密钥写入仓库。
- 记录可诊断的 trajectory：action 名称、受限参数、结果摘要、错误/耗时、暂停/恢复、最终推荐；不得记录 API key 或不必要的完整敏感输入。
- 对 tool loop、session pause/resume、profile gating、ask-attribute 映射、override、fallback 和官方响应合同增加确定性测试。
- 先以当前 catalog 能支撑的功能为准；不能从现有数据可靠实现的能力必须在结果与文档中明确标记，而不能伪造数据或把缺失当满足。
- 商品事实只允许来自当前 catalog 字段：title、categories、features、description、price、details、rating、rating count 和 store；不联网补商品知识。

## Acceptance Criteria

- [x] 未配置外部 API key 和新服务时，Agent 仍能初始化、完成官方 `reset/respond` 流程并返回合法推荐。
- [x] 配置一个兼容的 planner client 后，测试可观察到 planner 在单次 `respond()` 中按需执行至少两种非终止 action，再执行 `recommend_products`；action 选择不是写死的固定全流水线。
- [x] `ask_user` 会暂停内部 loop，返回合法 `ask_attribute`；下一轮用户回复只恢复同一 session 的待处理任务，不串 session。
- [x] profile 默认不出现在 planner 可见上下文；只有执行 `get_user_profile` 后，profile 内容和 `profile_loaded=true` 才进入该 session 的决策上下文。
- [x] search、filter、detail 工具只返回当前 catalog 内商品；最终 recommendations 去重、有效且不超过 `top_k`。
- [x] intent override 会废弃或降权被替换的旧约束，同时保留未被撤销的其他约束，并重新检索/推荐。
- [x] planner 返回未知 action、非法参数、超出 action/时间预算或抛出异常时，Agent 有界失败并走合法 fallback，不向 evaluator 抛异常。
- [x] trajectory 足以还原每轮执行了哪些 action、为什么暂停/终止、使用了哪些候选 ID 摘要和发生了什么错误。
- [x] 现有测试全部通过，并新增覆盖 action loop、工具合同、pause/resume、profile gating、fallback 和旧 evaluator 兼容性的测试。
- [x] 使用当前 public benchmark 跑一次旧 evaluator，产出与改造前相同指标 schema；若分数变化，报告差值和可追踪原因，不通过修改 evaluator 掩盖回归。

## Out of Scope

- 本任务不实现 Robust Evaluator、rubric case generation 或 rubric scoring；这些属于后续独立任务。
- 不改变旧 benchmark、旧 evaluator、competition Technical Score 或官方 response contract。
- 第一阶段不要求引入 LangChain、LangGraph、Redis、消息队列、微服务或向量数据库。
- 不允许用 benchmark target、hidden intent card 或 ground truth 作为 Agent 工具输入。
- 不实现 review search、review stats、review content 或任何依赖自由文本评论的判断。
- 不额外抓取、购买或生成商品数据；外部 API 只可用于规划/理解当前输入，不能作为商品事实来源。
