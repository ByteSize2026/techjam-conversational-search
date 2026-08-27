# Implementation Plan

## Preconditions

- 用户审阅并明确批准本规划。
- 批准后运行 `task.py start`，再修改产品代码。

## Phase 0 — Freeze seed-2026 baseline

- [ ] 运行当前全量 unittest。
- [ ] 核验 `holdout/holdout_200.jsonl` 为 200 条、80/80/30/10、与 public targets 不交。
- [ ] 显式实例化当前 `LegacyAgent` 复跑该 200，确认总体 `0.848503` 和 Override `0.592512` 场景基线；不得使用 ContestAgent 或旧结果替代。

## Phase 1 — Provenance schema without behavior change

- [ ] 增加 query evidence、active projection 和 constraint disclosure kind。
- [ ] 更新 fingerprint、reset、serialization 和 session isolation。
- [ ] 证明未提供 provenance 的现有调用保持兼容。

Validation:

```bash
python3 -m unittest tests.test_deterministic_policy tests.test_agent_contract -v
python3 -m unittest discover -s tests -v
```

## Phase 2 — Scoped parser and reducer

- [ ] 增加 override scope 与官方模板规则。
- [ ] 增加 attribute replacement、explicit global reset 和 conservative fallback。
- [ ] 将 epoch transition 与 full wipe 解耦。
- [ ] supersede provisional/conflicting evidence，carry forward confirmed compatible evidence。
- [ ] 清理新 epoch 的 recommendation/rank/progress/ask/exhaustion 状态。
- [ ] 增加 scope/state-diff diagnostics。

Focused validation:

```bash
python3 -m unittest tests.test_deterministic_policy tests.test_agent_contract -v
python3 -m unittest discover -s tests -v
```

Review gate: superseded evidence 不得进入 query、pool、ranking 或 fingerprint；retained evidence 不得携带旧 seen penalty。

## Phase 3 — Seed-2026 200 ablation

- [ ] 用完全相同的数据和默认离线配置跑 before/after。
- [ ] 报告总体与四场景 Hit@10、MRR、MTTC、TechnicalScore。
- [ ] 列出 Override miss，并在 benchmark 层分析 target-in-pool、rank 和 commit failure；Agent 不读取 target。
- [ ] 检查 Buying/Browsing/Boundary 是否因 provenance 投影回归。
- [ ] 执行 PRD R5 采用门槛。

## Phase 4 — Decision

- [ ] 门槛通过：保留 scoped behavior，向用户汇报并申请下一阶段 public/800 确认。
- [ ] 门槛失败：恢复旧默认或保持 scoped 模式关闭，记录失败分类。
- [ ] 运行最终全量测试和 `git diff --check`。

## Risky Files

- `starter/shopping_agent/state.py`：scope、provenance、epoch、fingerprint、reducer。
- `starter/agent.py`：状态更新顺序与 diagnostics。
- `starter/shopping_agent/retrieval.py` / `ranking.py`：active query projection。
- `starter/shopping_agent/structured_pool.py`：active constraints only。
- `tests/test_deterministic_policy.py` / `tests/test_agent_contract.py`。

## Completion Gate

- [ ] 全部 PRD acceptance criteria 有测试或报告证据。
- [ ] seed-2026 200 before/after 使用相同数据、配置和 LegacyAgent 入口。
- [ ] 默认行为与回滚状态明确。
- [ ] 独立检查、spec 评估、提交和 Trellis 归档完成。
