# Technical Design: Flexible Natural-Language Intent Adapter

## Design Objective

在现有 deterministic shopping-agent pipeline 前加入一个窄职责的意图解释层，使自然语言只产生可验证的状态增量。模型不是状态所有者、检索器或推荐生成器；`StateReducer` 仍是唯一允许修改 session state 的组件。

## Boundaries

### Inputs

- 当前 `user_message`
- `turn`
- 当前被询问的 `ask_attribute`（若可用）
- 仅包含活动约束、类别、query evidence 摘要和最近有限轮次的安全 session snapshot

### Outputs

- 一个经过验证的 `IntentUpdate`
- 解析诊断：path、confidence、model backend、accepted/rejected fields、fallback reason

### Non-responsibilities

- 不选择或生成 `parent_asin`
- 不修改 catalog 或 evaluator
- 不直接决定 `ask_attribute`
- 不直接执行 retrieval/ranking
- 不生成用户可见推荐理由

## Proposed Components

### 1. Intent interpretation contract

为现有解析入口定义小接口：

```python
class IntentInterpreter(Protocol):
    def interpret(
        self,
        message: str,
        *,
        turn: int,
        state_snapshot: IntentStateSnapshot,
    ) -> IntentInterpretation:
        ...
```

`IntentInterpretation` 包含 validated `IntentUpdate`、解析路径、整体置信度、接受/拒绝字段和有界 failures/usage。现有 `parse_intent_update(...)` 保留为 deterministic implementation，避免破坏直接测试和工具。

### 2. Provenance-aware query evidence

当前 `SessionState.query_terms: list[str]` 无法判断某个词来自哪一轮或是否是已被覆盖的偏好。增加内部 `QueryEvidence`：

```text
text
turn
epoch
source: user | rule | model | profile
confidence
status: active | superseded | removed
attribute_hint: optional allowed attribute
```

为兼容 retrieval/ranking，提供 `active_query_terms` 投影视图；不要求下游立即理解新对象。

### 3. Scoped override semantics

将 override 分成三种内部操作：

- `attribute_replace`：新值替换同属性旧值。
- `referenced_preference_replace`：移除被当前话语明确指向的旧软偏好/query evidence，保留类别和兼容硬约束。
- `global_reset`：只有明确表达全部重来或更换完整需求时清空旧意图并开启新 epoch。

官方模板 `Actually, ignore my earlier preference. What I need is: X.` 默认解释为 `referenced_preference_replace`，而不是无条件 `global_reset`。新约束 X 的属性由规则或模型落槽；无法安全定位旧偏好时，优先 supersede 旧的低置信度/软 query evidence，不删除后续硬披露。

### 4. Deterministic parser extension

规则路径先覆盖高价值、可稳定判断的结构：

- negation / avoid
- no-longer / remove
- keep-but-change / replace
- must / require 与 prefer / ideally 强弱度
- 多值和多子句切分
- global reset 与 scoped override 区分
- 对上轮 `ask_attribute` 的回答优先绑定该属性

规则输出继续保持 conservative：不能确认属性时使用 `feature` 或 query evidence，不创建高置信度 hard filter。

### 5. Optional model parser

模型只在规则置信度低、修改/否定作用域不清、多子句冲突、指代表达或无法形成有效 mutation 时调用。

模型响应 JSON 只允许：

```text
scope: none | attribute_replace | referenced_preference_replace | global_reset
mutations[]: action, attribute, value, polarity, hardness, confidence
category_anchor: string|null
no_preference[]
global_exhausted
boundary_signal
reopen_clarification
query_terms[]
confidence
```

JSON validator 必须：

- enforce attribute/action/polarity/hardness enums
- bound list counts and string lengths
- reject any `parent_asin`, unknown key or product selection field
- clamp confidence
- default uncertain model-only constraints to soft evidence
- merge trusted model deltas with deterministic evidence rather than blindly replace

### 6. Hybrid merge policy

优先级：

1. 明确协议/规则证据（预算数字、上轮结构化回答、已知 boundary 模板）
2. 模型对复杂作用域和属性的高置信度补充
3. 原始有界 query evidence

冲突时 deterministic hard evidence 胜出。模型可补充或降低不确定项，但不能把显式 user `avoid` 改成 `prefer`。合并后只产生一个 `IntentUpdate` 给 reducer。

### 7. Integration point

当前数据流：

```text
Agent._respond_impl
  -> parse_intent_update
  -> StateReducer.apply
  -> route/retrieve/pool/rank/clarify/commit/respond
```

目标数据流：

```text
Agent._respond_impl
  -> IntentInterpreter.interpret
       -> deterministic parse
       -> trigger decision
       -> optional model parse
       -> validate + merge
  -> StateReducer.apply(validated update)
  -> unchanged route/retrieve/pool/rank/clarify/commit/respond
```

检索、排序、commit 和响应 guard 不因模型存在而绕过。

## Configuration

新增配置遵循现有环境变量模式，并保持默认关闭模型解析：intent model enabled、trigger confidence threshold、intent max tokens 和 intent timeout。意图解析与 Qwen/LLM semantic reranker 分别开关；无配置时构造 Agent 不做 I/O。

## Diagnostics

每轮增加有界字段：

- `intent_parse_path`
- `intent_parse_confidence`
- `intent_trigger_reason`
- `intent_scope`
- accepted mutation summary
- rejected model field count/reasons
- backend/failures/usage
- reducer state diff：added/superseded/removed constraint keys

不得输出 API key、Authorization header、完整无界 prompt 或 catalog 大片文本。

## Compatibility and Migration

- 保留 `parse_intent_update` 作为直接测试/工具入口。
- `SessionState.query_terms` 可先保留兼容投影；迁移测试覆盖 fingerprint、progress tracking 和 override epoch 行为。
- 现有“普通 override 必须增加 epoch 并清空状态”的测试需要按新 scoped/global 语义拆分，不能简单删除覆盖。
- API response schema、catalog whitelist、usage guard 和 deterministic fallback 保持不变。

## Rollout and Rollback

### Rollout

1. 先实现 provenance + deterministic scoped override，不启用任何模型。
2. 公共集和新 paraphrase fixture 非退化后，加入 model adapter 和 validator。
3. 仅在消融证明收益且延迟/失败可接受时启用 model trigger。

### Rollback

- 一个配置开关完全绕过 model parser。
- 如 scoped override 导致公开集退化，可回退到 deterministic interpreter 的前一行为，同时保留新诊断用于定位。
- 任何模型失败不影响 reducer、retrieval 或 response boundary。

## Key Trade-offs

- 优先用户侧意图解析，不做商品侧语义扩写；更贴合官方评测且交付风险低。
- 规则证据优先于模型；牺牲部分开放世界理解换取可复现性。
- 模型只处理难例；降低 token/延迟，但要求 trigger policy 可解释。
- 局部保留提升信息利用率，但需严格 provenance，避免错误保留真正失效的偏好。
