# 有界购物工具循环

## 1. Scope / Trigger

修改 `starter/shopping_agent/actions.py`、`tools.py`、`planner.py`、`orchestrator.py`，或把新工具接入 `starter/agent.py` 时，必须遵守本合同。目标是在不改变官方 `reset/respond` facade 的情况下允许 Agent 主动选择动作，同时阻止模型绕过 catalog、会话和执行预算。

## 2. Signatures

公开签名不变：

```python
Agent.reset(session_id: str, user_profile: dict) -> None
Agent.respond(session_id: str, user_message: str, turn: int, top_k: int) -> dict
ActionPlanner.next_action(context: PlannerContext) -> object
ShoppingToolbox.execute(action: PlannedAction) -> ActionObservation
ActionOrchestrator.run(state, toolbox, *, user_message, turn, ...) -> OrchestratorResult
```

允许的 action 名称是闭集：

```text
search_products
filter_products
get_product_details
get_user_profile
ask_user
recommend_products
```

## 3. Contracts

工具规划由 `SHOPPING_AGENT_TOOL_PLANNING_ENABLED` 显式开启；`SHOPPING_AGENT_TOOL_MAX_STEPS` 和 `SHOPPING_AGENT_TOOL_TIMEOUT_SECONDS` 控制每次 `respond` 的预算。没有可用模型 backend 时，即使开关为真也必须使用 deterministic 路径，不得隐式联网。

商品事实只来自 `CatalogRepository`。`search_products` 是模型获得候选 ID 的入口；`filter_products`、`get_product_details` 和 `recommend_products` 只能接受该 session 已观察到的 ID，或 Agent 明确注入的 deterministic candidate IDs。合法但未观察到的 catalog ID 也必须拒绝。

reset profile 保存在 session 内，但在 `get_user_profile` 成功前不得进入 `PlannerContext`。工具只返回匿名合同字段：`purchase_frequency`、`average_prior_rating`、`rating_style`、`preference_tags`、`summary`。

`ask_user` 不在一次 `respond` 内等待用户：它保存 `PendingTask`，返回合法 `message + ask_attribute`，下一轮把用户回复绑定到该结构化 attribute 后恢复。Intent override 必须废弃旧 epoch 的 pending task 和候选。

trajectory 只保存有界参数和结果摘要；不保存 API key、完整 profile、模型自由文本 rationale 或未经 catalog 支持的商品陈述。最终面向用户的推荐消息由确定性安全模板生成。

## 4. Validation & Error Matrix

| 条件 | 行为 |
| --- | --- |
| 未知 action / arguments 非对象 | 记录 planner error；达到错误上限后 deterministic fallback |
| filter/detail/recommend 使用未观察 ID | 返回 `ActionValidationError`，不得把该 ID 加入候选 |
| catalog ID 不存在 | 工具错误，不向公开响应泄漏异常 |
| ask_attribute 不在官方枚举 | 工具错误；不得暂停为非法问题 |
| profile 尚未读取 | `PlannerContext.profile_loaded=false` 且没有 `profile` 字段 |
| action step/time budget 用尽 | 保存 fallback trajectory，返回 deterministic 合法响应 |
| 模型超时、无 backend 或非法 JSON | 有界失败；默认离线路径仍工作 |
| 模型推荐重复/未知/越界 ID | 拒绝或去重、截断；最终只输出 catalog-valid Top-10 |
| 用户发送 override | 新 intent epoch，清除旧 pending/tool candidates，不恢复旧任务 |

## 5. Good / Base / Bad Cases

- Good：`search_products -> get_product_details(observed_id) -> recommend_products(observed_id)`，所有动作有 trajectory，最终 ID 来自 catalog。
- Base：未配置 API key；Agent 不构造工具循环，继续运行原 deterministic retrieval/ranking，旧 evaluator schema 和指标可比较。
- Bad：模型猜测一个 catalog-valid ID 后直接调用 detail/filter/recommend；即使 ID 真实存在也必须拒绝，因为来源不可审计。
- Bad：把 reset 时收到的完整 profile 直接放进首个 planner prompt；这破坏主动 profile 获取和最小披露。

## 6. Tests Required

- Action schema：闭集名称、mapping arguments、运行边界二次校验。
- ID provenance：对“合法但未观察”的 ID 分别断言 filter/detail/recommend 失败。
- Profile gating：首个 planner context 不含 profile；调用工具后只含允许字段。
- Pause/resume：ask-user 返回官方枚举，裸回复绑定 pending attribute，两个 session 不串 pending 状态。
- Override：旧 pending、候选和 constraint epoch 被正确废弃。
- Bounds/fallback：非法 action、工具错误、step exhaustion、模型失败均返回合法公开响应并记录 trajectory/usage。
- Regression：运行 `python3 -m unittest discover -s tests -v`；有 catalog 时运行旧 evaluator 并与改造前指标比较。

## 7. Wrong vs Correct

Wrong：只检查 ID 是否存在于 catalog。

```python
record = repository.get(model_supplied_id)
if record:
    state.tool_candidate_ids.append(model_supplied_id)
```

Correct：先检查该 ID 是否由当前 session 的搜索或 deterministic candidate pool 产生，再访问详情或推荐。

```python
if model_supplied_id not in toolbox.observed_candidate_ids:
    raise ActionValidationError("get_product_details requires observed product IDs")
record = repository.get(model_supplied_id)
```

原因：catalog-valid 只证明商品存在，不能证明模型按用户需求检索到了它；来源校验同时防止 benchmark ID 猜测和不可审计的捷径。
