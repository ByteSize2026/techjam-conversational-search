# Router / Value-Node 图

## 1. Scope / Trigger

修改 `starter/shopping_agent/graph.py`、`starter/shopping_agent/llm_nodes.py`，或把新节点接入 `starter/agent.py` 的 `respond()` 时，必须遵守本合同。这是 08-28-agent-v2-router-value-node 任务的产物，取代了旧的自由工具循环（`actions.py`/`planner.py`/`orchestrator.py`/`tools.py`，已彻底删除，**不得重新引入**，参见 `tool-loop.md` 的历史记录 / git 提交 `c891de8`）。

核心原则不可协商：系统的每一步，要么是 **Router**（纯代码，只决定"下一个节点是谁"，绝不调用模型，绝不产生新事实），要么是 **Value Node**（产生恰好一个事实——确定性算法或一次有界 LLM 调用——绝不决定下一步是谁）。模型的输出可以被 Router 读取作为分支条件，但模型本身永远不能选择下一个节点、不能重写 `ROUTERS`/`NODES`。这不是性能优化,是本项目对"智能架构"的核心叙事:可审计、可终止性证明、不给模型任何隐式控制权。任何"看起来更方便"的走捷径(例如"让模型判断是否需要追问")都必须改写成"Router 读取模型产生的某个事实来判断"。

## 2. Signatures

```python
run_graph(
    session: SessionState,
    *,
    turn: int,
    top_k: int,
    message: str,
    services: GraphServices,
) -> GraphState

ROUTERS: dict[str, Callable[[GraphState], str]]   # 18 keys: 5 real branching routers + 9 trivial pass-throughs + 4 aliases
NODES: dict[str, Callable[[GraphState, ...], GraphState]]  # 14 keys: 12 Value Nodes + Render (terminal) + Done (no-op)

MAX_INTERNAL_STEPS = len(unique_node_names) * 3  # == 42；防御性保险丝，非预算，见 §4
```

主循环形状（`starter/shopping_agent/graph.py:run_graph`）：

```python
node = "Entry"
while node != "Done":
    steps += 1
    assert steps <= MAX_INTERNAL_STEPS
    node = ROUTERS[node](state)          # 纯代码决策
    state = NODES[node](state, args)     # 单一写入口：StateReducer.apply
```

## 3. Contracts

**真正带分支逻辑的 5 个 Router**：`Entry`、`IntentRouter2`（keyed as `ROUTERS["ClassifyIntent"]`）、`DistillTriggerRouter`（keyed as `ROUTERS["ExtractConstraints"]`）、`SlotCheckRouter`（keyed as `ROUTERS["DistillProfile"]`）、`CandidatePoolRouter`（keyed as `ROUTERS["Search"]`）、`RankRouter`（keyed as `ROUTERS["Rank"]`，Phase 7 新增，见下）。其余 `ROUTERS` 条目是固定单一出口的直通——这是主循环伪代码本身要求的（`ROUTERS[node]` 对每个刚执行完的节点都要有一条"接下来去哪"的记录），不是设计意图上的"8 Router"膨胀。

**`ROUTERS["Rank"]` / `RankRouter`**：Phase 7 集成时发现 `CandidatePoolRouter`/`SlotCheckRouter` 若把"需要追问"和"需要排序"当作互斥分支，会导致追问那一轮 `recommendations` 完全为空（旧 v1 流程是先排序再决定是否追问）。修复方式：非空候选池**总是**先进入 `Rank`；"是否追问"的判断（来自 `SlotCheckRouter` 的预检索证据或 `CandidatePoolRouter` 的过泛化信号）存进 `gs.scratch["rank_ask_attribute"]`，由 `RankRouter` 读取后决定跳到 `AskAttribute`（省掉 `SemanticRank`/`Explain`）还是继续 `SemanticRank`。`rank_ask_attribute` 只能由 Router 自己的纯代码逻辑（`ClarificationPolicy.choose_attribute`、`CandidateGate.evaluate`）写入——绝不能被任何 LLM Value Node 的输出直接或间接写入,这是本文件 §1 核心原则在这一具体分支上的落地,任何后续修改都要保这一条。

**唯一允许的环**：`CandidatePoolRouter → LoosenConstraints → Search`，由 `state.search_retry_count` 计数守卫，硬上限 1，每个官方轮次开始时重置为 0。环本身不是被禁止的——被禁止的是"模型决定环是否重复"；只要有代码检查的、有界的计数器守卫，环就是安全的。未来如果需要新增环，必须遵循同样的形状（state 里的计数器、Router 检查、有上限、每轮重置），不能引入第二种环模式。

**`StateReducer`（`starter/shopping_agent/state.py`）单一写入口**受保护的字段集合（`tests/test_state_reducer.py` 用 AST 扫描强制检查，任何模块直接赋值这些字段都会让测试失败）：

```text
constraints, session_profile, candidates, ranked, details_cache,
pending_question, node_trace, search_retry_count
```

`asked_attributes`、`last_candidate_ids`、`recommendations_by_epoch` **不在**这个保护集合内——它们由 `_ask_attribute_node`/`_render_node` 直接写（`state.record_asked(...)`、`session.record_recommendations(...)`），这是延续旧 orchestrator.py 的写法（`git show c891de8` 可查），不是疏漏。新增字段前先判断它是否该受 `StateReducer` 保护：如果多个节点都可能写它、或它影响后续 Router 分支，纳入保护集合；如果只是单一节点的记账字段，可以留在外面，但要在这里补一行说明，避免下次被误判为"漏写"。

**LLM Value Node 共享合同**（`starter/shopping_agent/llm_nodes.py`）：7 个 LLM 节点（`ClassifyIntent`、`ExtractConstraints`、`DistillProfile`、`AskAttribute`、`Explain`、`Compare`，以及走独立路径的 `SemanticRank`）中的前 6 个都通过同一个 `call_llm_value_node(client, task_prompt, user_payload, output_model)` 助手：pydantic 结构化输出校验失败 → 重试一次 → 仍失败或无 client 配置 → 返回 `None`，调用方（Router 或 Value Node 自己）走各自的确定性 fallback 分支。这一层retry 在 `TieredModelClient` 自身的后端多路重试之上，两层不应复合成无界重试：单次调用最坏情况是 `(配置的后端数) × 2`。`SemanticRank` 不走这个助手，直接复用 `LLMSemanticRanker`（它自己已经是排列安全、不会捏造 ID 的等价 fallback 合同，重复包一层没有意义）。

新增/修改任何 LLM Value Node 时：输入模型只放该节点真正需要的字段（不传整个 `SessionState`，不传超出设计所需的对话历史）；输出必须是 pydantic 模型，且 schema 里不能出现"下一步做什么"这类控制字段——出现了就是违反 §1 核心原则。

**`node_trace`**（`state.node_trace`，`NodeTraceEntry` TypedDict：`step`/`node`/`kind`/`input_summary`/`output_summary`/`elapsed_ms`）：每个节点执行都会写一条，有界（`NODE_TRACE_LIMIT = 64`），每个官方轮次开始时清空，不跨轮累积。`input_summary`/`output_summary` 复用 `evaluator/trace_runner.py:sanitize()` 的脱敏逻辑（延迟/函数内 import，避免与 `evaluator.trace_runner` 已经反向 import `starter.agent` 形成加载期循环）。`evaluator/local_evaluator.py` 不读这个字段——它纯粹用于调试、demo storytelling 和 `artifacts/scenario_showcase/` 里的场景展示材料。

## 4. Validation & Error Matrix

| 条件 | 行为 |
| --- | --- |
| `search_retry_count >= 1` 且再次空结果 | 不再进 `LoosenConstraints`；有剩余约束可放宽且轮数够 → `AskAttribute(mode="relax_conflict")`；否则 → `NoMatch` |
| LLM Value Node 结构化输出解析失败两次（1 次原始 + 1 次重试） | 返回 `None`，调用方走确定性 fallback（例如 `ExtractConstraints` 失败 → 关键词解析；`AskAttribute` 失败 → 固定模板，因为它在关键响应路径上，必须总能成功） |
| `ClassifyIntent` 模型不可用/失败 | fallback 到 `"refine_search"` 而不是 `"new_search"`——后者会带 `global_override=True` 清空已收集约束，是错误的默认值 |
| `run_graph` 内部步数超过 `MAX_INTERNAL_STEPS` | 断言失败——这是保险丝，说明某处引入了未受计数器保护的环，属于必须修复的 bug，不是正常路径 |
| `IntentRouter2` 收到 `ClassifyIntent` 给出的 `target_ids` | 不存在于 `last_candidate_ids`/`details_cache` 的 ID 在进 `FetchDetails` 前被丢弃——绝不信任模型给出的 ID |
| `rank_ask_attribute` 被设置 | 只能来自 `SlotCheckRouter`/`CandidatePoolRouter` 的纯代码判断；若发现任何 LLM 输出路径能设置这个 key，视为架构违规，立即修复 |

## 5. Good / Base / Bad Cases

- Good：`Entry → ExtractConstraints → DistillTriggerRouter → SlotCheckRouter → Search → CandidatePoolRouter → Rank → RankRouter → SemanticRank → Explain → Render`，每步都在 `node_trace` 里可见，`kind` 字段正确区分 router/value_node_deterministic/value_node_llm。
- Base：无 API key 时，7 个 LLM 节点全部落到各自确定性 fallback，`respond()` 仍返回合法响应，`python3 -m unittest discover -s tests -v` 与 `evaluator/local_evaluator.py` 全部离线可跑。
- Bad：新增一个 Router，让它的分支条件直接读某个 LLM 节点的原始输出文本（而不是该 LLM 节点产生的、已经过 pydantic 校验的结构化事实）——即使"看起来只是读了一下"，这也是模型在间接控制流程。
- Bad：给某个 Value Node 加一个 `next_node` 返回值，"为了方便"——Value Node 的唯一职责是产出一个事实，绝不能返回下一步去哪。

## 6. Tests Required

- 静态图测试（`tests/test_router_graph.py`）：从 `Entry` 遍历 `ROUTERS`，断言除声明的 `LoosenConstraints → Search` 回边外没有节点被重复访问，且该回边受 `search_retry_count` 计数守卫（上限 1）。这是终止性的真正证明,不是注释里写写。
- 单一写入口测试（`tests/test_state_reducer.py`）：AST 扫描 `starter/`、`evaluator/`、`tests/`，确认受保护字段只能在 `StateReducer` 自己的类体内被写。
- LLM 共享 helper 测试（`tests/test_llm_value_nodes.py`）：一次测试覆盖 parse success / parse failure → retry → fallback，而不是每个节点各写一遍——所有 7 个节点走同一个 helper,测一遍即可。
- `DistillTriggerRouter` 门控测试：no-op 轮次产生零次模型调用。
- `CandidatePoolRouter` 无模型测试：任何 `CandidateStats` 输入都不触碰 model client。
- 回归：`python3 -m unittest discover -s tests -v` + `python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl`，作为安全网而非优化目标（见测试哲学，§7）。

## 7. Wrong vs Correct

Wrong（让 Router 读取未经校验的模型原始输出来分支）：

```python
def _some_router(gs: GraphState) -> str:
    raw = gs.scratch.get("classify_intent_raw_text")  # 模型自由文本
    if "compare" in raw.lower():
        return "FetchDetails"
    return "ExtractConstraints"
```

Correct（模型输出先经 pydantic 校验成封闭枚举事实，Router 只读这个事实）：

```python
def _intent_router2(gs: GraphState) -> str:
    output: ClassifyIntentOutput | None = gs.scratch.get("classify_intent")
    intent = output.intent if output is not None else "refine_search"  # 确定性 fallback
    return {"compare_details": "FetchDetails", "confirm_choice": "Render"}.get(
        intent, "ExtractConstraints"
    )
```

原因：模型可以产生"事实"（一个封闭枚举、一个校验过的结构化值），但绝不能产生"决策"。Router 读取事实、自己做决策，这条界线一旦模糊，整个"可审计、可终止性证明"的架构叙事就站不住了——这是本项目从上一次失败尝试（自由工具循环）里吸取的核心教训。

## 8. 测试哲学（补充说明，非独立小节但值得单列）

本任务刻意精简了单测数量：结构性测试（上面 §6 列的那几个）证明"图会终止""状态只有一个写入口""LLM fallback 机制本身可靠"这几条架构性主张；但"系统是否真的表现得聪明"（该追问时追问、该收敛时收敛、死路时能恢复）不是靠断言数量堆出来的证据。这类证据来自 `artifacts/scenario_showcase/`——6 段完整的、可读的多轮对话 trace，覆盖模糊查询澄清收敛、过泛化追问、空搜索自动放宽重试、真死角、意图打断、语义重排。新增行为时，优先补一个场景展示而不是一堆逐分支单测；只有当行为本身是"架构性质"（终止性、单写入口、fallback 边界）而非"行为质量"时，才补结构性单测。

## 交叉引用

`Agent.reset`/`Agent.respond` 的外部合同不变，见 [接口、状态与输出规则](./contract-and-state.md)。评估器容错与评分见 [Evaluator](../evaluator/index.md)。旧的自由工具循环架构（已删除，历史参考）曾记录于本目录的 `tool-loop.md`，现已被本文件取代。
