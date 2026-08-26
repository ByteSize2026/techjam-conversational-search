# Technical Design: Deterministic Dialogue, Ranking, and Commit Policy

## 1. Design intent

本设计不以替换当前 Agent 为目标，而是在现有 category resolver、SessionStore、StateReducer、output guard 和诊断能力上，增加三个缺失边界：目标保留型 structured pool、显式 exhaustion 状态、独立 dynamic commit policy。排序保留为独立阶段，并向 commit policy 暴露相对置信证据。

## 2. Current problem

当前主链在 category route 上先分配有限预算，再把用户约束主要作为 feature score；CandidateGate 只控制昂贵排序是否执行，最终仍固定输出最多 10 个推荐。ClarificationPolicy 又把 `other` 放在最后且不重复已问属性。

这产生四种耦合失败：

1. 正确约束到来时，目标可能已经不在被截断 shortlist 中。
2. 目标低位进入 Top 10 会提前结束会话，无法在下一轮提升至 Rank 1。
3. 对 feature/detail 型意图要依次问完 material/color/size 等低价值问题。
4. 排序失败、召回失败和提交过早在指标中混为一体。

## 3. Proposed architecture

```text
user message
  -> protocol-aware parse + generic fallback
  -> StateReducer
       - constraints / intent epoch
       - attribute_exhausted / global_exhausted
       - boundary_seen / no_progress_streak
  -> StructuredCandidatePool
       - full resolved category IDs
       - high-confidence constraint intersection
       - zero-result rollback
  -> DeterministicRanker
       - constraint fit / BM25 / title / popularity / rating / profile
       - RankEvidence(top1 margin, stability, ranked prefix)
  -> ClarificationPolicy
       - other-first, repeatable while valuable
  -> RecommendationCommitPolicy
       - clarify-only / partial / full / forced
  -> response guard
```

提问决策与推荐提交决策独立运行，因此同一轮可以出现：

```text
ask = other, recommendations = []
ask = other, recommendations = [top1]
ask = other, recommendations = [top1, top2, top3]
ask = None,  recommendations = top10
```

## 4. Module boundaries

### 4.1 `starter/shopping_agent/state.py`

扩展 `SessionState`：

```python
attribute_exhausted: set[str]
global_exhausted: bool
boundary_seen: bool
exhaustion_reason: str | None
ask_counts: dict[str, int]
previous_structured_pool_size: int | None
no_progress_streak: int
previous_ranked_ids: list[str]
```

扩展 `IntentUpdate` 或增加对应事件，使解析与 reducer 之间传递：

```python
attribute_exhausted
global_exhausted
boundary_signal
negative_feedback
```

状态规则：

- `I don't have a preference for X; use your judgment`：标记 `X` exhausted 和 `boundary_seen`，不标记 global exhausted。
- `I don't have an additional preference for other`：标记 global exhausted。
- Intent Override：新 epoch 清空旧 constraints、ask counts、previous rank evidence、seen recommendations 和 exhaustion 状态。
- 每轮 structured pool size 或 fingerprint 改善时 no-progress 归零，否则加一。

### 4.2 `starter/shopping_agent/structured_pool.py`（新增）

职责仅为候选资格，不负责最终排序。

建议数据结构：

```python
StructuredPoolResult(
    ids: tuple[str, ...],
    category_resolution: CategoryResolution,
    applied_constraints: tuple[ConstraintApplication, ...],
    softened_constraints: tuple[ConstraintApplication, ...],
    previous_size: int,
    final_size: int,
)
```

流程：

1. 从 `CategoryResolution.product_ids` 取得完整类目集合；未解析类目才退回 bounded lexical recall。
2. 对 filterable constraint 逐条匹配完整 product canonical text/price。
3. 交集非空则接受；交集为空则保留上一池并将该约束记为 soft。
4. 输出完整 eligible IDs，不在此模块做 Top-N 截断。

初版 matcher：

- color/material：词边界；
- budget：解析数值并用文档化容差；
- feature/detail：规范化空白和标点后做原文/分段匹配；
- 其他属性：先保留为 soft，除非有高置信 parser 证据和对应测试。

性能策略：复用 repository 已缓存的 `ProductRecord.canonical_text`、category product IDs 和 ID map。先实现可验证的类目内扫描；只有 profiling 证明需要时才增加 inverted sets，避免提前复杂化。

### 4.3 `starter/shopping_agent/policy.py`

保留现有 `CandidateGate` 为计算成本 gate，新增：

```python
RankEvidence(
    pool_size: int,
    top1_margin: float,
    top3_stability: float,
    hard_constraint_count: int,
    no_progress_streak: int,
)

CommitDecision(
    mode: Literal["clarify_only", "partial", "full", "forced"],
    recommendation_limit: int,
    reason_code: str,
)
```

`CommitDecision` 刻意不包含 `ask_attribute`；问什么由 ClarificationPolicy 决定，提交多少由 RecommendationCommitPolicy 决定，最后只在 Agent 编排层组合两者。

`ClarificationPolicy`：

- `other` 不受普通“问过一次就禁用”规则限制；由 ask count、global exhausted、no progress 和 remaining turns 共同控制。
- broad pool 默认 `other`。
- `other` 无效后才从具体属性中选择有候选证据且未 exhausted 的属性。

`RecommendationCommitPolicy` 初版规则：

```text
global exhausted                -> forced top10
turn >= 9                       -> forced top10
no_progress_streak >= 2         -> forced top10
pool_size <= commit_all         -> full pool (<= top_k)
pool_size <= partial_threshold
  and top1 stable/high margin   -> partial top1
pool_size <= partial_threshold
  and top3 stable               -> partial top3
otherwise                       -> clarify_only
```

阈值必须来自配置并进入诊断。`5` 和 `25` 只是待测试默认值，不是合同。

### 4.4 `starter/agent.py`

编排顺序改为：

1. parse/reduce；
2. build structured pool；
3. 在 eligible pool 内生成 deterministic ranking；
4. 计算 RankEvidence；
5. 分别选择 ask attribute 和 commit decision；
6. 按 decision 的 dynamic limit 构造 recommendations；
7. 更新 ask/rank/pool/no-progress state；
8. guard 输出并写 diagnostics。

语义 ranker路径保持兼容但本任务不启用、不作为验收依赖。

### 4.5 Evaluation diagnostics

Agent 不能访问 target，因此 target-aware 分类只能放在本地 benchmark/reporting 层，而不是 Agent 决策层。评估 replay 可在每轮读取 Agent diagnostics 并与公开 target 对照，输出：

```text
target_in_structured_pool
target_rank_before_commit
returned_list_size
hit_rank / hit_turn
failure_class
```

不得修改 official evaluator 的评分公式；需要详细 trace 时使用独立脚本或测试 harness。

## 5. Ranking design

排序只在 structured eligible pool 内进行。第一阶段保留当前 feature ranker 的可用部分，但重新明确优先级：

1. filterable hard constraints 决定成员资格，不再重复用超大加分模拟硬过滤；
2. BM25/title coverage 表达当前意图相关性；
3. log popularity 和 rating 区分约束相同的商品；
4. profile prior 只作低权重 tie-break；
5. seen recommendation penalty 仅在当前 intent epoch 生效。

Top-1 margin 使用相对归一化而非原始 route score。建议初版：

```text
normalized_margin = (score1 - score2) / max(score1 - score_last, epsilon)
```

Top-K stability 使用本轮与上一轮 ranked prefix 的 overlap，例如 Top-3 Jaccard 或位置加权 overlap。阈值通过消融选择。

## 6. Key invariants

- Agent 决策永远不读取 target 或 evaluator hidden fields。
- structured pool 中接受一条新硬约束后不得无诊断地变成空集。
- global exhausted 后默认不再询问；若用户同时否定当前推荐并明确要求继续提问，只在当前轮允许一个尚未询问、尚未耗尽且没有 active constraint 的具体属性，同时保持 forced recommendations 和持久 exhausted 状态。
- Override 消息到达后立即切换新 epoch；不沿用旧 epoch 的 seen penalty。
- recommendation limit 永远处于 `0..min(top_k, 10)`。
- partial commit 只输出 ranked list 的高置信前缀，不能从尾部抽样。
- 每轮最多一个 `ask_attribute`。

## 7. Alternatives considered

### Always return Top 10 while asking

拒绝作为默认策略。它提高早期 HitRate 机会，但会让低位目标提前结束会话，损害 MRR；公开消融表明提交门控的主要收益正来自避免这种低位提前命中。

### Always ask `other` and return nothing until pool <= 5

适合作为 benchmark baseline，但过于刚性，放弃了高置信 Top-1 的早期命中机会。保留为消融对照，不作为最终设计。

### LLM or dense reranking

本任务排除。竞争实现关闭两者后分数仅下降约 `0.001`，不能解释主要架构差距。

### Replace current state model with competitor state machine

拒绝。当前 epoch、constraint mutation 和 session isolation 更通用；只吸收 exhaustion、other policy 和 commit gate 概念。

## 8. Rollout and rollback

- 所有新策略通过配置开关逐层启用：structured pool、other-first、dynamic commit、ranking confidence。
- 每层有独立 ablation；若场景指标退化，可回滚单层而不回滚状态 schema。
- 不删除现有 lexical fallback、output guard 或 model integration hook。
- 最终默认值只有在完整 unittest 和 200-session evaluator 通过后切换。

## 9. Planning-review decisions

建议 planning review 采用以下默认实验集合，而非现在锁死单点：

- `commit_all_threshold`: `1, 3, 5, 10`
- `partial_threshold`: `10, 25, 50`
- `partial_limit`: `1, 3`
- `no_progress_force_commit`: `1, 2`

先固定排序权重，只调策略阈值；再固定最佳策略调排序，避免参数相互污染。
