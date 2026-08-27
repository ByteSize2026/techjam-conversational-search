# Agent 合同与会话状态

## 接口是不变边界

提交入口必须导出 `Agent`，并实现：

```python
reset(self, session_id: str, user_profile: dict) -> None
respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict
```

该签名来自 `docs/agent_api_contract.json` 和 `docs/submission_rules.md`。评估器在 `evaluator/local_evaluator.py:evaluate` 为每个样本生成新的 `session_id`，调用 `agent.reset(...)` 后才调用 `agent.respond(...)`。`starter/agent.py:Agent.respond` 把未 reset 的会话视为错误；新实现可以选择不同的防御方式，但不得把一个会话的约束或推荐结果泄漏到另一个会话。

`user_profile` 是匿名聚合信息。合同中的字段为 `purchase_frequency`、`average_prior_rating`、`rating_style`、`preference_tags`、`summary`；只能将其用于允许的个性化，不能假定存在直接身份、购买时间戳、原始购买历史或隐藏目标。来源：`docs/agent_api_contract.json:reset_request`、`docs/competition_specification.md`。

## 实现模块边界

`starter/agent.py` 是提交入口和稳定 facade：它负责依赖组装、会话生命周期、
整轮编排与最终诊断。可复用的实现职责位于 `starter/shopping_agent/`：

- `retrieval.py` 负责路由召回、类目配额、候选合并、多样化和召回诊断。
- `ranking.py` 负责候选统计、词法证据、确定性特征融合与排序置信证据。
- `response.py` 负责语义排序适配、usage/failure 提取、fallback 和响应合同校验。

不要把这些实现重新堆回提交 facade。仓库内测试和 benchmark 会直接调用或替换
`Agent._retrieve`、`_feature_rank`、`_rank_evidence`、`_valid_ids` 等兼容入口；
重构内部组件时应保留这些薄委托，并让整轮编排继续通过实例方法调用，使运行时
替换仍然生效。

## 每轮响应合同

`respond` 的请求中 `turn` 为 1 至 10，`top_k` 固定为 10。响应至少包含：

- `message`：面向顾客的字符串。
- `ask_attribute`：`None` 或下列枚举之一：`category`、`material`、`color`、`size`、`style`、`brand`、`budget`、`feature`、`use_case`、`other`。
- `recommendations`：按最佳到最差排序的列表；每项含非空字符串 `parent_asin`，可额外含数值 `score`。

这是 `docs/agent_api_contract.json:turn_response` 的合同。该 schema 容许最多 100 项 recommendations，但这不是评分配额：`evaluator/local_evaluator.py:normalize_recommendations` 只保留**最先出现的 10 个**有效、唯一且在 catalog 内的 `parent_asin`。因此应输出去重后的高置信候选，不要依赖重复、无效 ID 或第 11 项以后的候选得分。

无模型时 `usage` 可省略；若提供，`prompt_tokens` 与 `completion_tokens` 必须是非负整数。`evaluator/local_evaluator.py:evaluate` 仅累计合法的整数 usage。异常、非字典响应或非字符串 `message` 会被评估器降级为空响应，通常导致 miss。

## 状态和意图更新

将每个 `session_id` 的已知约束、问询历史、候选集和轮数分别保存。`reset` 必须清空或替换该会话先前状态，不能仅依赖全局“最近一轮”变量。当前基线用 `starter/agent.py:Agent._sessions` 明确记录已初始化会话；更复杂的策略应沿用同样的隔离原则。

不要把首轮意图永久锁死。`evaluator/local_evaluator.py:behavior_for` 与 `evaluate` 定义 `intent_override`：评估器会在第 3 或第 4 轮发送“忽略此前偏好”的新要求，且在新意图送达前命中不计分。收到覆盖消息后，废弃或降权被覆盖的偏好，基于新硬约束重新检索和排序。

`boundary` 场景中，`evaluator/local_evaluator.py:customer_reply` 可能对已请求属性回答“没有偏好”。这是未知值，不是任意值：保留已知约束，避免反复追问同一属性，并用其他可判别属性、类目或检索证据继续推荐。

全局信息耗尽通常停止继续提问，但存在一个事件级例外：用户同时明确否定当前推荐并要求继续询问具体属性时，只允许当前轮临时绕过提问禁令。不得清除持久的 `global_exhausted`，提交策略仍应按耗尽状态返回可用推荐；临时问题必须排除 `other`、已耗尽/已问属性和当前意图已有约束的属性。下一轮若没有新的明确请求，应恢复全局耗尽行为。

## 实现反模式

- 不修改 `evaluator/local_evaluator.py` 来适配 Agent；评估器是外部协议的执行者。
- 不从公开样本字段推断私有 `intent_card`、`behavior` 或 ground truth 以构造捷径；私有评估不会提供这些内部状态。
- 不用自然语言提问代替 `ask_attribute`。模拟器读取的是结构化字段，见 `customer_reply`。
- 不把外部 API 密钥写入源码、catalog 或提交包；边界见 [数据与提交规则](../data-contracts/jsonl-privacy-submission.md)。

相关评估细节见 [评估协议与评分](../evaluator/protocol-and-scoring.md)，回归方式见 [unittest fixture](../testing/unittest-fixtures.md)。
