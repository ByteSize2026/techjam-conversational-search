# Implementation Plan

## Phase 0 — Baseline and task guardrails

- [ ] 固化无模型基线结果：TechnicalScore `0.743854`、HitRate `0.900000`、MRR `0.545181`、MTTC `4.485`，保留 overall、scenario、turn/rank histogram。
- [ ] 确认模型、dense 和外部网络环境变量均未启用。
- [ ] 为本任务新增独立 ablation 输出路径，避免覆盖或误提交 `results.json`。
- [ ] 运行现有全量 unittest，记录开始前状态。

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --output /tmp/techjam-baseline.json
```

Rollback point: 无代码变更。

## Phase 1 — Exhaustion and dialogue events

- [ ] 在 `SessionState` 增加 attribute/global exhaustion、boundary、ask counts、no-progress 和 previous rank/pool 状态。
- [ ] 扩展 parser/update/reducer，精确区分：
  - 单属性无偏好；
  - `no additional preference for other`；
  - Boundary `use your judgment`；
  - negative feedback；
  - visible Intent Override and applied epoch transition。
- [ ] Override 新 epoch 清理 exhaustion、ask counts、rank evidence 和旧推荐历史。
- [ ] 增加两个 session 不串状态和 reset 替换状态测试。

Tests:

```bash
python3 -m unittest tests.test_agent_contract -v
python3 -m unittest discover -s tests -p 'test*state*.py' -v
```

Rollback point: 新字段可保留，策略尚未读取，不改变推荐行为。

## Phase 2 — Other-first clarification policy

- [ ] 让 `other` 在 broad、未 exhausted、remaining turns 足够时优先。
- [ ] 允许 `other` 重复询问，直到全局耗尽或 no-progress guard 触发。
- [ ] `other` 无价值后才选择有候选证据的具体属性。
- [ ] 添加 Boundary 后继续取得约束、全局耗尽后停止提问、最后两轮不浪费问题的测试。
- [ ] 跑 `other-first only` 消融并保存 scenario metrics。

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --output /tmp/techjam-other-first.json
```

Review gate: 检查 Intent Override 和 Boundary 是否出现无效循环。

## Phase 3 — Target-preserving structured pool

- [ ] 新增 `structured_pool.py` 及 result/diagnostic 数据结构。
- [ ] 使用完整 resolved category IDs，结构化过滤前不套 category budget。
- [ ] 实现 material、color、budget、feature/detail matcher。
- [ ] 实现逐约束交集和 zero-result rollback/soften。
- [ ] 将现有 retrieval/ranker 限制在 structured eligible IDs 内；未知/模糊类目保留 lexical fallback。
- [ ] 用临时小 catalog 覆盖目标保留、空交集回退、预算不会提前截断、低 popularity 目标仍保留。
- [ ] 跑 `other-first + structured-pool` 消融。

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --output /tmp/techjam-structured-pool.json
```

Review gate: 公开 replay 中 target-in-structured-pool failure 必须单独列出并解释。

Rollback point: 通过配置关闭 structured pool 后回到现有 route union。

## Phase 4 — Ranking evidence and independent commit policy

- [ ] 从 deterministic ranking 结果计算 normalized Top-1 margin、Top-3 stability 和 pool size evidence。
- [ ] 新增 `RecommendationCommitPolicy`，与 CandidateGate 解耦。
- [ ] 支持 clarify-only、partial Top-1、partial Top-3、full small-pool、forced Top-10。
- [ ] 实现 global exhausted、late turn、no progress 和 visible override epoch transition 规则。
- [ ] Agent 输出按 decision 动态取 ranked prefix，并允许同轮 ask + recommendations。
- [ ] 覆盖六类 commit 路径和 recommendation limit 合同测试。

Initial ablation grid:

```text
commit_all_threshold = 1, 3, 5, 10
partial_threshold = 10, 25, 50
partial_limit = 1, 3
no_progress_force_commit = 1, 2
```

执行顺序：先比较无 partial 的纯 gate；固定 commit-all 后再启用 partial，避免笛卡尔积式公开集过拟合。

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --output /tmp/techjam-dynamic-commit.json
```

Review gate: 对每个阈值报告 overall + scenario，并列出低位提前命中数。

## Phase 5 — Deterministic ranking refinement

- [ ] 在最佳对话/提交策略固定后，单独评估 ranking signals。
- [ ] 确保 hard match 控制资格，避免重复用超大权重模拟过滤。
- [ ] 依次消融 popularity、rating、BM25/title coverage、profile prior。
- [ ] 记录 target-in-pool 时的 target rank histogram 和 Top-1 count。
- [ ] 不启用 LLM、dense 或网络后端。

Validation matrix:

```text
structured + popularity
+ rating
+ BM25/title coverage
+ low-weight profile prior
```

Review gate: 选择在 overall 与四场景上稳定的最小信号集合，不按单一总体最高点堆权重。

## Phase 6 — Integration, quality check, and documentation

Correctness follow-up (2026-08-27):

- [x] 将“否定推荐 + 明确要求继续提问”解析为 `reopen_clarification` 事件。
- [x] 使用 one-turn clarification bypass，保持 `global_exhausted` 与 forced recommendation 提交语义。
- [x] 临时具体问题排除 `other`、已耗尽/已问和已有 active constraint 的属性。
- [x] `py_compile`、focused 24/24、full unittest 75/75、独立 reviewer 通过。
- [x] 单次最终评测：TechnicalScore `0.884923`、HitRate@10 `0.990000`、MRR `0.775411`、MTTC `3.135`、usage `0`；miss 为 `public_0013`、`public_0071`。

- [ ] 运行完整 unittest。
- [ ] 运行最终 200-session evaluator，并复跑一次确认确定性。
- [ ] 比较 baseline、每阶段 ablation、最终结果。
- [ ] 检查无网络、无模型环境，确认 usage 不伪报。
- [ ] 更新 README/architecture/report 中的确定性路径、配置、性能与限制。
- [ ] 检查 submission contract、catalog-valid IDs、session isolation 和异常 fallback。
- [ ] 使用 `trellis-check`/等价独立检查完成代码审阅。
- [ ] 评估是否有可沉淀进 `.trellis/spec/` 的新规则，再进入 finish-work。

Final validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --output /tmp/techjam-final-a.json
python3 -m evaluator.local_evaluator --output /tmp/techjam-final-b.json
```

Final acceptance:

- 所有 PRD functional criteria 通过；
- 两次最终结果完全一致；
- TechnicalScore/HitRate/MRR 不低于 baseline；
- 达到第一阶段目标或提交失败分类与下一步建议；
- 无 LLM/dense/network 依赖。

## Files expected to change

- `starter/agent.py`
- `starter/shopping_agent/state.py`
- `starter/shopping_agent/policy.py`
- `starter/shopping_agent/structured_pool.py`（新增）
- `starter/shopping_agent/config.py`（仅策略开关/阈值）
- `tests/` 下对应 state、policy、structured pool、integration 测试
- 可选独立 benchmark/diagnostic 脚本与文档

`evaluator/local_evaluator.py` 不在普通实现修改范围内。
