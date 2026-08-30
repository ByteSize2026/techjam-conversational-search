# Implementation Plan: Flexible Natural-Language Intent Adapter

## Preconditions

- 用户已审阅并明确批准本任务的 PRD、设计与实施计划。
- 运行 `task.py start` 后再修改产品代码。
- 实现前读取相关 Trellis spec；当前任务仍处于 planning，不在本步骤执行。

## Phase 1 — Freeze baselines and fixtures

- [ ] 保存当前公开集总体与四场景指标、测试数量和配置摘要。
- [ ] 建立不含 target ID 的自然语言解析 fixture，覆盖官方模板、paraphrase、否定、局部修改、删除偏好、多条件、硬软约束和安全指代降级。
- [ ] 为 scoped override 记录至少三个代表性会话：旧 query evidence、后续硬约束、override 后候选池和目标排名。
- [ ] 明确模型消融矩阵与每次运行的固定配置。

Validation:

```bash
python3 -m unittest discover -v
python3 -m evaluator.local_evaluator
```

Rollback point: 仅新增 fixture/诊断，无运行时行为变化。

## Phase 2 — Provenance and deterministic scoped updates

- [ ] 引入 provenance-aware query evidence，并提供对现有字符串 query terms 的兼容投影。
- [ ] 扩展 deterministic parser：negation、remove、replace、must/prefer、多子句、asked-attribute binding。
- [ ] 区分 `attribute_replace`、`referenced_preference_replace` 与 `global_reset`。
- [ ] 更新 reducer，使普通 override 只 supersede 被引用/冲突的软证据，保留 category 和兼容硬约束。
- [ ] 更新 fingerprint、progress tracking、state reset 和 session isolation 测试。
- [ ] 调整旧 override 测试为 scoped/global 两类明确断言。

Validation:

```bash
python3 -m unittest tests.test_agent_contract tests.test_deterministic_policy -v
python3 -m unittest discover -v
python3 -m evaluator.local_evaluator
```

Gate: 所有测试通过；总体 Hit/MRR/TechnicalScore 与 Intent Override 指标均不低于 PRD 基线。

Rollback point: 在引入模型前形成一个纯 deterministic 可提交版本。

## Phase 3 — Intent interpreter and model-safe schema

- [ ] 定义 `IntentInterpreter`、`IntentInterpretation` 和安全 state snapshot。
- [ ] 将现有 deterministic parser 包装为默认 interpreter。
- [ ] 定义严格模型 JSON schema/validator，包含字段白名单、长度/数量边界和置信度规范化。
- [ ] 实现 deterministic + model 结果合并策略。
- [ ] 确保不可信 model-only 约束只能成为软 evidence，除非有明确用户文本佐证。
- [ ] 加入 malicious/invalid/unknown/oversized 模型返回测试。

Validation:

```bash
python3 -m unittest tests.test_model_fallback tests.test_agent_contract -v
python3 -m unittest discover -v
```

Rollback point: interpreter 默认仍只使用 deterministic implementation。

## Phase 4 — Optional backend integration and trigger policy

- [ ] 复用 tiered model backend、timeout、usage 和 failure diagnostics。
- [ ] 添加 intent parser 独立配置，不与 semantic reranker 开关耦合。
- [ ] 实现规则优先 trigger policy，并记录触发原因。
- [ ] 证明高置信度模板不调用模型。
- [ ] 证明无配置、禁网、超时、异常和非法 JSON 都回退且返回合法 response。
- [ ] 保证构造 Agent 不隐式下载或加载模型。

Validation:

```bash
python3 -m unittest tests.test_model_fallback tests.test_agent_contract -v
python3 -m unittest discover -v
```

Gate: model path 的任何失败都不能比 rules-only 路径更差地破坏协议或会话状态。

## Phase 5 — Integration diagnostics and ablation

- [ ] 将 parse path、scope、accepted/rejected mutations、state diff、usage 和 fallback 原因加入有界 diagnostics。
- [ ] 运行 `rules_only`、`intent_model_only`、`reranker_only`、`both` 四组配置。
- [ ] 报告总体和四场景 Hit@10、MRR、MTTC、TechnicalScore。
- [ ] 报告 p50/p95 turn latency、模型调用率、fallback 率和 token usage。
- [ ] 检查 scoped override 代表性会话的候选池、目标是否保留和最终排名。
- [ ] 只在综合分非退化且风险可接受时推荐启用 model trigger；否则保留为默认关闭的可选增强。

## Phase 6 — Documentation and submission safety

- [ ] 更新 README：配置、模型选择、无网络 fallback、限制和复现命令。
- [ ] 披露成功模型调用的 token、延迟和估算成本。
- [ ] 在干净、无网络环境运行默认配置。
- [ ] 确认没有 API key、模型缓存路径、生成 catalog、private labels 或 evaluator 修改进入提交。
- [ ] 运行最终全量测试与 evaluator。

## Risky Files / Review Focus

- `starter/shopping_agent/state.py`：状态迁移、override、fingerprint、session isolation。
- `starter/agent.py`：解释器集成顺序、diagnostics、公共异常边界。
- `starter/shopping_agent/model.py`：后端复用、timeout、usage/failure 语义。
- `starter/shopping_agent/config.py`：默认离线行为和环境变量解析。
- `starter/shopping_agent/retrieval.py` / `ranking.py`：不得被 superseded query evidence 污染。
- `tests/test_agent_contract.py` / `tests/test_deterministic_policy.py` / `tests/test_model_fallback.py`：旧行为迁移和新安全覆盖。

## Completion Checklist

- [ ] PRD 全部 acceptance criteria 有测试或报告证据。
- [ ] 全部现有与新增测试通过。
- [ ] 公共集核心指标满足非退化门槛。
- [ ] 默认离线行为已验证。
- [ ] 代码检查确认未扩大到商品语义卡、dense index 或通用购物助手。
- [ ] 按 Trellis finish 流程更新 spec、提交并归档任务。
