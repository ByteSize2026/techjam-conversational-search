# Technical Design

## Objective and Boundary

为 `LegacyAgent` 增加 provenance-aware scoped override。模型、排序权重、commit 阈值、ContestAgent、evaluator 和 catalog 均不改变。

## State Contract

为 `IntentUpdate` 增加内部 scope：

```text
none | attribute_replace | referenced_preference_replace | global_reset
```

用有界 query evidence 对象作为事实来源：

```text
text, turn, epoch,
kind: initial | direct | clarification | override,
attribute_hint, confidence,
status: active | superseded | removed
```

现有下游通过 `active_query_terms` 兼容投影继续读取字符串。`Constraint`/`ConstraintMutation` 增加兼容默认的 disclosure kind，现有调用方无需立即修改。

## Parsing

- `Actually, ignore my earlier preference. What I need is: X.` → `referenced_preference_replace`，X 为 override hard evidence。
- 明确 `change A to B / B instead of A` → `attribute_replace`。
- 仅明确 `forget everything / start over / completely different request` → `global_reset`。
- 普通首轮话语不预测未来场景。
- 歧义时采用 conservative fallback，不做破坏性 global wipe。

## Reducer Transition

所有明确 override 都开启新推荐/进展 epoch，但“切换 epoch”和“清空 constraints”解耦：

1. supersede 同属性冲突值；
2. supersede 被指代的 provisional initial evidence；
3. 保留 clarification-confirmed compatible constraints/evidence；
4. 将 retained evidence carry forward 到新 epoch；
5. 添加 override hard constraint；
6. 清除旧 recommendation history、rank/progress、ask/exhaustion、softened keys 和 last candidates。

`global_reset` 才执行完整 wipe。保留 category anchor，除非明确更换类别。

## Downstream Integration

```text
parse -> scoped IntentUpdate -> StateReducer
      -> active constraints/query projection
      -> unchanged retrieve/pool/rank/clarify/commit/respond
```

- structured pool 只读 active constraints；
- retrieval/ranking 只读 active query terms；
- fingerprint 不包含 superseded evidence；
- 新 epoch seen recommendations 为空；
- 不修改 commit policy，先观察正确状态是否自然缩小候选池并提前命中。

## Diagnostics

增加有界字段：`intent_scope`、`epoch_changed`、`added_constraint_keys`、`retained_constraint_keys`、`superseded_constraint_keys`、evidence counts 和 carry-forward count。

## Compatibility and Rollback

- 保留 `parse_intent_update(...)` 入口与旧 Constraint 构造默认值。
- 现有“普通 override 必然全清”测试拆成 scoped/global 两类，不直接删除覆盖。
- 第一阶段先加入 schema/测试，再切换 reducer 行为。
- seed-2026 200 门槛失败则恢复旧 full-reset 默认；不修改 ranking/commit 掩盖失败。

## Rejected Alternatives

- 首轮预测 Override：真实场景不可成立。
- 保留全部旧约束硬 AND：会利用模拟器同目标生成新旧偏好的特性。
- 立即接模型：无法区分收益来自正确状态语义还是模型适配。

