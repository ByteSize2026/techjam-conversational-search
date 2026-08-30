# DeepSeek-first 意图理解与强制离线降级

## Goal

在不改变 `Agent.reset/respond` 合同、不泄漏目标商品、不把网络变成正确性前提的条件下，
提高 DeepSeek 对自然语言品类、品牌、评分、否定和意图覆盖的实际决策权。配置模型时采用
model-first 意图解释；未配置、无网络、超时、失败或输出非法时，必须原子回退到当前确定性
规则、召回、排序和提交路径。

## Background

独立自然语言 benchmark 的 7 题诊断显示：按需 DeepSeek 版本 32 轮中有 18 轮调用意图
模型，但没有一轮模型意图被接受；品牌经常被识别出来，却因置信度为 0、`replace` 动作或
保守来源策略被过滤。强制每轮 DeepSeek 且把置信度覆盖为 1.0 后，Hit@10 从 0 提升到
0.429，但 `Skechers`、`Solocute`、`SOCIETY NEW YORK` 仍因动作/属性归一化失败被拒绝，
`Luoika` 虽被接受，旧预算移除仍被拒绝。另有多轮目标已位于中间排序 Top-1，但策略继续
追问 `other` 而未提交。

## Requirements

### R1. Model-first 运行模式

- 当 `SHOPPING_AGENT_INTENT_MODEL_ENABLED=true` 且存在可用模型 backend 时，每轮用户消息
  都进入模型意图解释，包括补充条件、指代、无偏好和 intent override。
- 确定性解析器始终先生成完整 fallback update；模型不能绕过 schema、catalog ID 禁令或
  reducer，不能直接修改 state。
- 默认无配置仍为 rules-only；提供显式 rules-first 回滚配置，不要求修改调用方接口。

### R2. 原文证据驱动的模型权威

- 对用户原文明确包含的品类、品牌、评分、预算、否定或替换值，将通过验证的模型 mutation
  标记为 explicit model evidence，并允许进入结构化 state。
- explicit model evidence 与规则识别的用户事实具有同等级检索权重，并可把对应属性标记为
  已回答；纯推断 model evidence 继续保持 soft、低权重、不可单独形成 target-excluding filter。
- 生产逻辑不得全局硬编码置信度 1.0；权威提升必须来自原文支持、合法字段、合法动作和有界
  置信度的组合。

### R3. 安全动作和字段归一化

- 普通偏好中的 `replace brand=Skechers` 在没有明确 change/replace 语义时安全归一为
  `upsert brand=Skechers`，而不是整条拒绝。
- `label`、`made by` 等明确品牌表达可归一为 `brand`；`rated X stars` 归一为评分/查询证据
  的现有合法表示；明确类目表达产生可解析 category anchor/constraint。
- `remove` 仅在原文含明确撤销语言、待删除字段确实存在且字段/值匹配当前 state 时允许；模型
  不得凭空清空约束或触发 global reset。
- 确定性显式否定、边界和 global exhaustion 具有安全 veto，模型不能把 avoid 翻成 prefer，
  也不能把“没有更多偏好”变成新的商品偏好。

### R4. 对话与提交策略使用已接受模型事实

- explicit model evidence 对应的属性视为 confirmed，避免用户回答品牌/评分后继续重复询问
  `other` 或同一属性。
- 当候选集中度、排名领先幅度或稳定性满足现有有界阈值时，允许输出推荐并附带可选问题；
  不得在高置信候选已存在时只追问、不推荐。
- 不针对 benchmark target、场景标签或特定品牌写捷径。

### R5. 强制离线降级红线

- 以下情况必须在同一轮自动返回确定性结果：无 API key、无网络、DNS/SSL 失败、超时、HTTP
  错误、非法/未知 JSON 字段、越界内容、空 completion 或 backend 全部失败。
- fallback 必须使用模型调用前冻结的 deterministic update；不得应用半份模型 mutation，
  不得污染 session state，不得跨轮保留失败 completion。
- 离线路径不得下载模型、打开网络、要求第三方依赖或伪造 usage；完整 unittest 与公共 evaluator
  必须可在禁网环境运行。

### R6. 可观察性和兼容性

- 保留 `intent_path`、backend、accepted/rejected、failure、usage、state diff 诊断，并增加
  explicit/inferred provenance、action normalization 和 fallback reason。
- 保留 `Agent` 接口、catalog ID whitelist、最多 10 轮/Top-10 评分语义和 session 隔离。

## Acceptance Criteria

- [ ] 默认环境无 key、无网络时，模型 client 调用次数为 0，完整 Agent 可正常推荐。
- [ ] model-first 模式下，每轮消息尝试模型；成功输出经过严格验证并进入 reducer，失败则返回
  与同消息 rules-only 完全等价的 `IntentUpdate`。
- [ ] DNS/SSL、timeout、HTTP、非法 JSON、未知字段和空 completion 均有独立 fallback 测试，
  且 state fingerprint 与纯规则路径一致。
- [ ] `Skechers`、`BTFBM`、`Solocute`、`SOCIETY NEW YORK` 类表达能够成为 evidence-backed
  brand，而不会仅因模型使用 `replace` 被拒绝。
- [ ] 明确 `ignore earlier $50` 可只撤销已有 budget；含糊删除、模型凭空删除和 global reset
  继续被拒绝。
- [ ] 模型显式事实算 confirmed；纯推断事实仍为 model-only，低检索权重且可继续澄清。
- [ ] “没有更多偏好”保留既有有效约束和查询证据，不产生新偏好，不重复追问。
- [ ] 高置信候选存在时提交策略至少输出有界推荐，不再只问 `other`；无候选时仍可安全追问。
- [ ] `python3 -m unittest discover -s tests -v` 全部通过，无新增第三方依赖。
- [ ] rules-only 公共 evaluator 的 Hit@10、MRR、MTTC 和 TechnicalScore 不低于实施前冻结基线；
  Intent Override 和 Boundary 分场景不得回归。
- [ ] 独立 7 题 benchmark 分别记录 rules-only、按需模型和 model-first 指标；在线评测只作为
  增强证据，离线非回归是发布硬门。

## Out of Scope

- 模型训练/微调、embedding 或 dense retrieval。
- 修改 evaluator、题库、ground truth 或 catalog 数据来迎合 Agent。
- 将强制置信度 1.0 作为生产策略。
- 把 API key、端点或 benchmark 结果写入提交源码。
