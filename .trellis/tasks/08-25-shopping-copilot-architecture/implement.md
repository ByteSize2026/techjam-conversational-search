# Shopping Copilot Implementation Plan

## Gate

本计划须先由用户评审 `prd.md` 与 `design.md`。用户确认后才能运行 `task.py start` 并修改产品代码。

Gate status: 2026-08-26 用户已确认开始实现，首个 vertical slice 为“DeepSeek API -> 3B–8B local endpoint -> deterministic fallback”。

## Phase 0 — Reproducible Baseline and Experiment Harness

- [ ] 运行现有 evaluator，复现 `docs/baseline_results.json`。
- [ ] 记录 catalog row count、checksum、索引构建时间、进程内存和 baseline respond latency。
- [ ] 固定 scenario + difficulty 分层的 dev/validation/locked-holdout manifest；manifest 只存 sample IDs，不存进 Agent。
- [ ] 建立逐会话结果、配置 hash、时延和 candidate diagnostics 的实验脚本。
- [ ] 增加 baseline 复现说明，不修改官方 evaluator 语义。

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output /tmp/techjam-baseline.json
```

Gate: published baseline 可复现或差异原因被查明；locked holdout 在后续调参中保持封存。

Planning evidence: 2026-08-25 已运行现有 3 个 unittest 并精确复现公开 baseline；实现开始后仍需再次执行，作为变更前锚点。

## Phase 1 — Contracts, Types, Catalog and Session State

- [ ] 将 `starter/agent.py` 收缩为 facade，建立 `starter/shopping_agent/` 包。
- [ ] 定义 `Attribute`、`Constraint`、`IntentUpdate`、`SessionState`、`Candidate` 和 `ConstraintEvidence`。
- [ ] 实现 `CatalogRepository` 与合法 ID/字段规范化。
- [ ] 实现 `SessionStore.reset/get` 和 `StateReducer`。
- [ ] 实现 intent epoch、active/inactive constraint、no-preference、asked attributes 和 recommendation history。
- [ ] 保持 baseline lexical retrieval 作为可运行路径。

Tests:

- 两个 session 不串状态；同 ID reset 会覆盖旧状态；
- upsert/replace/remove/no-preference；
- global override 保留 category anchor、停用旧偏好并递增 epoch；
- override 前后推荐历史隔离；
- response contract 与合法唯一 ID。

Gate: 所有场景状态测试通过，官方 baseline 不因重构出现无法解释的退化。

### Phase 1A — Tiered Model Backend Vertical Slice

- [x] 实现无第三方 SDK 的 OpenAI-compatible client，密钥与 endpoint 只来自环境变量。
- [x] 实现 DeepSeek API、本地 3B–8B endpoint 和确定性排序三级 failover。
- [x] semantic rank 请求只包含 distilled intent 与 catalog-valid 候选；严格修复未知、重复与遗漏 ID。
- [x] 未配置模型时不联网；超时、HTTP、JSON/schema 异常均返回 Feature Ranker 原顺序。
- [x] 只有真实成功响应才返回 token usage，并为所有 failover 分支补 unittest。

Gate: 默认离线路径、API 成功、本地降级和全失败四种测试均通过；公开接口不变。

## Phase 2 — Intent Parsing and Context Distillation

- [ ] 实现高精度 deterministic parser：override、否定、无偏好、budget、material、color、size、brand/category 常见表达。
- [ ] 建立严格的 `ModelAdapter` 与结构化输出 schema；解析模型不得返回商品 ID。
- [ ] 实现规则优先、模型补充、低置信拒绝和 timeout fallback。
- [ ] 实现 `ContextDistiller`，把 profile、活动约束、拒绝值、未问属性、intent epoch 和剩余轮次压缩为 `RuntimeContext`。
- [ ] 建立解析/context fixture，覆盖 add/replace/remove/override/boundary 与跨轮 context size 上限。
- [ ] 只有真实模型调用时才返回 usage。

Gate: parser/context fixture 全部通过；模型超时和非法 JSON 时 Agent 均返回合法响应；RuntimeContext 不随完整消息历史无界增长。

## Phase 3 — IntentRouter and Buying Route

- [ ] 实现 `IntentRouter`，每轮输出 Buying/Browsing/Mixed、route weights、retrieval budget 和 reason code。
- [ ] 实现 canonical product document 与字段规范化版本。
- [ ] 实现 Buying QueryPlanner：latest/category/hard/profile/cheap-probe/recovery 查询。
- [ ] 实现 category narrowing、SQLite FTS5/BM25 多查询及来源追踪。
- [ ] 实现 budget/material/color/size/brand/category 的 match/contradict/unknown evidence。
- [ ] 实现 fail-open 硬约束过滤，并覆盖空查询、特殊字符和候选不足。

Gate: Buying validation 的 candidate recall@50/100、HitRate 与 MTTC 相比 Phase 1 有稳定收益；Router reason code 可解释且不永久锁死首轮路线。

## Phase 4 — Browsing Route and Multi-Route Fusion

- [ ] 选择 2–3 个轻量 embedding 候选，冻结 checkpoint、license 和 canonical document schema。
- [ ] 构建预计算商品向量与 manifest；catalog checksum 不匹配时安全回退。
- [ ] 实现 Browsing query embedding、50k cosine Top-N 和 category/result diversity。
- [ ] 实现 Buying/Browsing candidate union 与 RRF，保留 route/source/rank diagnostics。
- [ ] 比较 lexical-only、dense-only、union、RRF 的 candidate recall@50/100 与场景指标。
- [ ] 量化资产大小、cold start、RSS、CPU p95，不依赖在线下载或官方 GPU。

Gate: Browsing dense route 必须提供稳定互补 recall/score 增益并满足 CPU/资产预算；否则启用 broad lexical + diversity fallback，并在最终报告中明确与目标完整路线的差异。

## Phase 5 — CandidateGate and Proactive Clarification

- [ ] 实现 cheap candidate probe 与 `CandidateStats`：estimated count、route overlap、category/attribute entropy、hard-constraint count。
- [ ] 实现 `CandidateGate` 的 focused/over-general 决策、reason code 和 retrieval/model budget。
- [ ] over-general 时跳过昂贵 LLM Semantic Ranker，返回廉价多样 Top-10，并立即提出一个高信息量属性。
- [ ] 实现 `protocol_aware` 与 `catalog_entropy` clarification policy。
- [ ] 在 Buying、Browsing、Intent Override、Boundary 比较无 gate、count-only gate、count+entropy gate。
- [ ] 记录 cutoff 率、有效披露率、重复问题率和 MTTC 变化。

Gate: Boundary 不重复问无偏好属性；CandidateGate 不因过早 cutoff 显著损害 HitRate/MRR，并能降低无效模型调用或 MTTC。

## Phase 6 — Deterministic Ranking and Turn Coverage

- [ ] 实现 Feature Ranker：RRF、硬/软匹配、profile、质量先验和矛盾惩罚。
- [ ] 将 focused-path union 压缩到 20–30 个 catalog-valid 候选。
- [ ] 实现 same-intent-epoch seen penalty/排除，提高跨轮 unique coverage。
- [ ] override 后清除 seen penalty，并验证 pre-override ID 可以再次出现。
- [ ] 对权重只做预先定义的小网格，不对 200 条逐例调参。
- [ ] 输出 feature breakdown 到 diagnostics，不暴露给顾客 schema。

Gate: dev/validation 的 HitRate、MRR、MTTC 变化可解释；至少一个指标改善且 locked holdout 无明显回退。

## Phase 7 — LLM Semantic Ranking

- [ ] 定义 `SemanticRankingRequest/Result`：distilled intent、硬约束、20–30 个压缩商品和候选 ID 白名单。
- [ ] 实现每轮最多一次的 listwise LLM ranking，固定 temperature、prompt/schema version 与 timeout。
- [ ] 验证模型只能重排输入 ID；未知、重复、遗漏 ID 由 Feature Ranker 顺序修复。
- [ ] 对候选本地 CPU 模型/runtime 做 cold/warm latency、RSS、token 和质量 benchmark。
- [ ] 比较 feature-only、CrossEncoder、LLM listwise 以及 LLM-failure fallback。
- [ ] 在 over-general path 验证 LLM Ranker 确实被跳过。

Gate: LLM Semantic Ranker 成为 focused-path 目标 MVP 默认，且相对 feature-only 在 validation/locked holdout 的 MRR 或 TechnicalScore 有稳定收益；若官方资源无法支持，必须保留合法 fallback 并明确披露，不伪称完整实现。

## Phase 8 — Hardening, Packaging and Final Freeze

- [ ] ContractGuard 覆盖非法 message、attribute、重复/无效 ID、NaN score 和 usage。
- [ ] 添加四场景端到端小 catalog fixtures 与完整 200-session smoke test。
- [ ] 测试 Router route transitions、CandidateGate 两条路径、禁网、模型超时、dense asset 缺失、semantic rank 失败和 catalog schema 异常。
- [ ] 冻结默认配置、依赖版本、asset manifest 和一条运行命令。
- [ ] 编写 setup、模型/成本/token/延迟/网络/fallback/限制报告。
- [ ] 运行最终 ablation，确认每个保留组件都有可测收益。

Validation:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output /tmp/techjam-final.json
```

Gate: 输出合同、四场景、离线运行和复现说明全部通过；最终配置不读取 public ground truth 或 evaluator hidden fields。

## Review Gates

1. **Architecture review**：用户确认本任务的 PRD/design/implementation plan。
2. **Core review**：Phase 1–6 完成后先评估 routing/retrieval/gating 主线，再冻结 LLM Ranker 的候选输入合同。
3. **Model asset review**：Dense/LLM/reranker 型号、license、大小和官方环境假设由用户确认。
4. **Final freeze review**：locked holdout 只在候选配置已收敛时运行，冻结后不继续按个例调权。

## Rollback Points

- Phase 2 解析模型问题：保留 deterministic parser 与 bounded RuntimeContext。
- Phase 4 dense 无收益/过重：Browsing 回到 broad lexical + category diversity。
- Phase 5 gate 误伤：回到记录-only gate，不执行 cutoff。
- Phase 7 LLM semantic rank 失败/过慢：回到 Feature Ranker 顺序并记录 fallback。
- Clarification 过拟合：回到固定的 protocol-aware 安全策略。
- 任意增强破坏合同：回到上一阶段 tag/config，并保留 `Agent` facade 不变。
