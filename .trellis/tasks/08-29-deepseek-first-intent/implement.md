# Implementation Plan

## Phase 0 — Freeze baselines and failure contract

- [ ] 记录当前 git 状态并避开用户已有修改；冻结完整 unittest 与 public evaluator rules-only 基线。
- [ ] 保存独立 7 题 benchmark 的按需 DeepSeek 与强制 1.0 消融指标，只作诊断参考。
- [ ] 新增无 key、无网络/连接错误、timeout、HTTP、非法 JSON、未知字段、空 completion 的 failure matrix tests。
- [ ] 断言每种失败的 `IntentUpdate`、state fingerprint 和推荐协议与 rules-only 等价。

Rollback point：只增加测试与基线，没有生产行为变化。

## Phase 1 — Model-first mode with atomic offline fallback

- [x] 在 config 中增加 intent mode，并保持总开关默认 false、无依赖、无下载。
- [x] `IntentInterpreter.interpret` 每轮先冻结 deterministic update；model-first 尝试模型，rules-first 保留旧触发。
- [x] 统一所有异常/非法 completion 的 `rules_fallback`，确保 reducer 只收到一个完整 update。
- [x] 增加 mode/path/fallback reason/usage 诊断与 model-first/rules-first 单测。
- [x] 运行 focused tests、完整 unittest、rules-only evaluator 非回归。

Rollback point：设置 `SHOPPING_AGENT_INTENT_MODEL_MODE=rules_first` 恢复旧触发行为。

## Phase 2 — Evidence-aware normalization and authority

- [x] 增加 explicit/inferred model provenance，不伪装为 rule。
- [x] 实现原文 span/marker 支持检查和 action normalization：普通 replace→upsert、明确替换保留 replace。
- [x] 实现 brand/category/rating 常见表达归一；无法安全结构化的内容保留 query evidence。
- [x] 实现 state-aware remove guard，仅允许明确、存在且匹配的撤销；global reset 保持 deterministic veto。
- [x] 更新 reducer、fingerprint、diagnostics，并覆盖基础 explicit brand fixture。
- [x] 验证 hostile/低置信推断不能形成 hard target-excluding filter。

Rollback point：关闭 explicit authority，仅保留 model-first + 原有保守 merge。

## Phase 3 — State, retrieval and clarification authority

- [x] 让 explicit model constraints 进入 confirmed attributes；inferred constraints 保持 model-only。
- [x] explicit model preference 使用普通显式偏好查询权重；inferred 继续使用 `MODEL_PREFERENCE_QUERY_WEIGHT`。
- [x] 修正 global exhaustion/no-preference：保留已有有效约束和 evidence，不把边界句当查询词。
  - 2026-08-30：修复复数 `preferences` 尾部被误识别为 `feature` 并使既有查询证据失效；
    parser 与 reducer 双层保证全局耗尽只记录协议级 `other`，不撤销商品属性。
- [x] 防止已回答品牌/评分后重复询问 `other` 或同 slot；普通 `other` 只问一次，boundary 事件级重开除外。
- [x] 运行 source-weight、structured-pool、clarification、boundary、intent-override 专项测试。
- [x] 增加 catalog-grounded 结构化值准入：从 50,000 商品构建按字段分桶的
  `category/brand/color/material/size/style/feature` 值索引；标题 token 单独保存在
  `title_token`，不得冒充 feature。
- [x] DeepSeek JSON mutation 的枚举值进入 reducer 前必须命中对应 catalog 字段；未知值以
  `catalog_value_not_supported` 拒绝，原文明确支持时仅保留为 lexical evidence。
- [x] canonicalizer 改为闭合字段/模板语法；未知 label 或任一 catalog 外结构化值使整轮翻译
  原子失败并回退 frozen deterministic update。`budget`、数值 rating、`use_case` 保持现有
  非枚举语义。
- [x] 修复 marker payload 使用原消息 span 二次裁剪的问题，避免 lexical evidence 尾部被误删。

Rollback point：provenance 保留但下游仍按 legacy model-only 权重处理。

## Phase 4 — Target-free commit policy adjustment

- [ ] 为“已有稳定/领先候选但策略只追问”建立确定性 fixture，锁定候选数、margin 和 stability 输入。
- [ ] 调整 commit 分支，使满足现有安全阈值时输出有界推荐，可同时提出可选问题。
- [ ] 确认无候选、宽泛 browsing、重复推荐和 global exhaustion 行为不回归。
- [ ] 不读取 target、scenario label、benchmark signature 或特定品牌名单。

Rollback point：恢复旧 commit thresholds，保留意图改进。

## Phase 5 — Validation and rollout evidence

- [x] 运行完整 unittest：`python3 -m unittest discover -s tests -v`（122/122 通过）。
- [x] 禁用所有模型变量运行 public evaluator，比较总体及 Buying/Browsing/Intent Override/Boundary 指标。
- [x] 用失败 backend stub 和禁用模型环境证明 rules fallback，无 API key 时确认模型调用数为 0。
- [ ] 配置 DeepSeek 运行 public evaluator 与独立 7 题 benchmark，报告调用率、fallback、p50/p95、usage 和分场景指标。
- [x] 检查 Agent API、catalog whitelist、session 隔离和无 target leakage。
- [x] 更新 model backend 文档和 Trellis spec，记录 model-first、offline fallback 与回滚配置。

### 2026-08-30 validation evidence

- 主 Agent：114/114 tests passed，`git diff --check` passed。
- 独立 benchmark simulator v2：33/33 tests passed，`git diff --check` passed。
- 冻结 7 题、DeepSeek on：Hit@10 1.000，Top-1 0.429，MRR 0.624，MTTC 3.00；
  重复问题由 35 次降至 0 次。
- 冻结 100 题、global exhaustion 修复前后配对：Hit@10 0.530→0.570，Top-1
  0.290→0.320，MRR 0.363→0.398，MTTC 6.72→6.38；6 个旧 miss 恢复、2 个旧
  hit 因保留了既有脏 evidence 而回归，净增 4 个命中。
- catalog-grounded validation 后：专项 intent/policy 58/58、完整 unittest 122/122 通过；
  关闭 DeepSeek/本地模型运行官方 public evaluator 200 题，Hit@10=0.970、MRR=0.759621、
  MTTC=4.74、TechnicalScore=0.838086，reported token usage 为 0。
- 自然语言冻结 100 题精度修复：纯离线最终 Hit@10=0.870、Top-1=0.580、MRR=0.686、
  MTTC=2.75；DeepSeek canonicalizer 最终在线 Hit@10=0.870、Top-1=0.630、MRR=0.716、
  MTTC=2.74。303 回合中 145 次模型结果通过，43 次因严格准入安全降级；相对离线无新增回归。

## Validation Commands

```bash
python3 -m unittest tests.test_intent_interpreter tests.test_model_fallback -v
python3 -m unittest tests.test_intent_state_reducer tests.test_source_weighted_preferences -v
python3 -m unittest tests.test_clarification_policy tests.test_commit_policy -v
python3 -m unittest discover -s tests -v

env -u SHOPPING_AGENT_DEEPSEEK_API_KEY \
    -u SHOPPING_AGENT_LOCAL_BASE_URL \
    -u SHOPPING_AGENT_LOCAL_MODEL \
    python3 -m evaluator.local_evaluator --output /tmp/deepseek-first-offline.json
```

在线验证从本机 `.env` 显式加载，输出只写 `/tmp` 或独立 benchmark 仓库；命令、测试和报告均不得
打印或保存 API key。

## Review Gates

1. Offline gate：无 key/无网络必须完整通过，且 rules-only 指标不回归。
2. Safety gate：模型不能凭空 remove/reset、生成 ID 或制造 hard exclusion。
3. Authority gate：原文明确品牌/品类/评分进入 state、confirmed 和检索。
4. Policy gate：高置信候选不再只问不推，无候选仍安全澄清。
5. Compatibility gate：接口、session、usage、catalog whitelist 和现有 benchmark hooks 不变。
