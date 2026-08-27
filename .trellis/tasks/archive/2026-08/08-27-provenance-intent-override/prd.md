# Provenance-aware Intent Override

## Goal

在当前 `LegacyAgent` 中，将明确的 Intent Override 从“无条件清空全部意图”改为可解释的 scoped update：删除被指代或与新要求冲突的旧偏好，同时保留类别和追问中已经确认、且与新要求兼容的约束。第一轮只实现完全离线、确定性的基线，并优先用 seed 2026 的固定 200 条 holdout 判断是否有效。

## Background

- 当前 `parse_intent_update(...)` 把 `Actually, ignore my earlier preference. What I need is: X.` 视为 `global_override`；`StateReducer` 随后清空全部 active constraints、query terms 和策略状态。
- evaluator 可能在 override 前通过 `other` 已经披露其他约束；清空后这些约束通常不会再次出现，因此候选池重新变宽。
- fixed seed-2026 holdout 200 的当前 `LegacyAgent` 基线为：Hit@10 `0.970000`、MRR `0.707343`、MTTC `3.435000`、TechnicalScore `0.848503`、usage `0`。
- 其中 Intent Override 30 条基线为：Hit@10 `0.800000`、MRR `0.348373`、MTTC `6.600000`、场景 TechnicalScore `0.592512`；6 个 miss 全部来自 Override。
- `Constraint` 已有 `turn`、`epoch`、`source`、`hardness` 和 `status`，但 `query_terms` 仍是无来源字符串，无法区分初始 provisional 偏好、追问披露和 override 新证据。
- 仓库已有更大的 flexible intent-adapter 规划；本任务是其规则优先、无模型的窄前置实验。

## Requirements

### R1. 保持协议与真实输入边界

- 保持 `Agent.reset/respond`、响应 schema、catalog ID 白名单和 session 隔离不变。
- 不修改 evaluator、catalog、评分语义或数据标签。
- 不从首轮句式预测未来 Override；只有收到明确覆盖语言后才能更新状态。
- 运行时不得读取 target、`scenario_type`、hidden intent card 或 sample ID。
- 只修改 `LegacyAgent` 路径，不改变 ContestAgent PUBLIC 入口。

### R2. 增加最小 provenance

- query evidence 至少可追溯到 `turn`、`epoch` 和 kind。
- MVP kind 区分初始/直接偏好、追问披露和 override 新要求。
- 为现有 retrieval/ranking 提供仅包含 active evidence 的字符串投影；superseded evidence 不得进入 BM25、ranking query、structured pool 或 fingerprint。
- 现有不带 provenance 的 `Constraint` 构造保持兼容。

### R3. Scoped override

- 官方模板默认解释为 scoped override，而不是完整 reset。
- 新要求 X 成为高置信 hard constraint。
- 与 X 同属性且值冲突的旧约束必须 supersede。
- 被“earlier preference”指代的初始 provisional 偏好/query evidence 必须 supersede。
- 覆盖前由追问明确披露、且与 X 不冲突的约束必须保留。
- 明确 override 仍开启新的推荐/进展 epoch，清除旧推荐惩罚、rank stability、no-progress、ask/exhaustion 等策略状态；保留的约束显式 carry forward 到新 epoch。

### R4. Global reset 与歧义处理

- 只有明确的“forget everything / start over / completely different request”等语义才完整清空旧意图。
- 明确单属性替换只删除同属性冲突值。
- 无法安全定位作用域时，不做无依据的全清；新明确要求 active，其他不确定旧证据最多降级为非硬过滤证据，并允许后续澄清。

### R5. 测试、诊断与第一轮采用门槛

- 单元测试覆盖 scoped override、同属性替换、global reset、query evidence 失效、carry-forward 和 session isolation。
- diagnostics 记录 scope、epoch change、added/retained/superseded keys。
- 第一轮只用固定 `holdout/holdout_200.jsonl` 做 before/after；不重新抽样，不调 ranking/commit 参数。
- 只有同时满足以下条件，才认为 seed-2026 200 初测通过：
  - 全量测试通过；
  - 总体 TechnicalScore `>= 0.848503` 且 Hit@10 `>= 0.970000`；
  - Override Hit@10 `>= 0.800000`、MRR `>= 0.348373`、MTTC `<= 6.600000`；
  - Override 三项指标至少一项严格改善。
- 初测通过后，再向用户汇报并决定是否进入 public 200 和 fixed 800 的确认阶段；它们不属于第一轮阻塞门槛。
- 初测失败时保留测试和诊断，但恢复旧默认行为，不做 sample-specific 特判。

## Acceptance Criteria

- [x] 官方 override 模板不再触发完整 constraints/query evidence wipe。
- [x] 被指代的初始 provisional 偏好失效，后续明确披露的兼容约束保留，新要求成为 hard constraint。
- [x] 同属性冲突值被替换，无关属性不受影响。
- [x] 明确 global reset 仍清空完整意图并开启新 epoch。
- [x] scoped override 后旧推荐、rank stability、no-progress 和 exhaustion 不污染新 epoch。
- [x] superseded evidence 不进入任何 active 下游投影。
- [x] 两个 session 的 provenance、epoch 和约束不串用。
- [x] `python3 -m unittest discover -s tests -v` 全部通过。
- [x] seed-2026 holdout 200 before/after 结果按总体和四场景报告，并执行 R5 采用门槛。

## Out of Scope

- LLM/小模型 Intent Interpreter、Qwen、dense、embedding 或排序权重调参。
- 从首轮模板预测隐藏场景，或保留所有旧约束继续硬 AND。
- 修改 ContestAgent、PUBLIC 配置、evaluator、catalog 或评分公式。
- 把 fixed holdout 描述为官方私有集。
- 在第一轮实现中跑新的随机种子、public 200 或 800 条确认集。

## Risks and Deferred Items

- 真实语言中“earlier preference”的作用域可能含糊；MVP 只对明确规则证据做破坏性更新，模型解释留给后续任务。
- provenance 会影响 fingerprint 和 retrieval query，必须检查非 Override 场景不因投影迁移而改变。
- seed-2026 只有 30 条 Override，初测只用于快速判断方向；通过后仍需更大样本确认。
