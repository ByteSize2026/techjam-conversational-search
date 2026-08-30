# Technical Design

## Decision Summary

采用“model-first interpretation + deterministic atomic fallback”双轨结构。模型配置关闭时保持
当前纯离线路径；配置开启时每轮尝试模型，但规则解析先完成并冻结，任何模型失败都直接返回该
冻结结果。模型权威不由全局置信度 1.0 决定，而由原文证据和安全归一化决定。

## Runtime Modes

```text
INTENT_MODEL_ENABLED=false (default)
  user message -> deterministic parser -> reducer -> retrieval/ranking/policy

INTENT_MODEL_ENABLED=true, mode=model_first
  user message -> freeze deterministic update
               -> model request + strict validation
                  ├─ success -> evidence-aware normalization/merge -> reducer
                  └─ any failure -> frozen deterministic update -> reducer

INTENT_MODEL_ENABLED=true, mode=rules_first (rollback)
  current confidence/ambiguity trigger behavior
```

建议增加 `SHOPPING_AGENT_INTENT_MODEL_MODE=model_first|rules_first`。模型开关默认仍为 false，
从而保证未配置环境不会创建网络正确性依赖；开关打开时默认 model-first，rules-first 是快速回滚。

## Evidence Model

将模型证据区分为两类，不把所有 `source=model` 一视同仁：

- `model_explicit`：字段值能在当前用户原文中对齐，动作有明确语言支持，且通过 schema 验证。
  它参与 confirmed attributes，使用普通用户偏好检索权重，但仍不能绕过 hard-filter 安全规则。
- `model_inferred`：同义推断、宽泛 category anchor 或无法精确对齐的语义补充。它保持 soft、低权重、
  不阻止澄清。

实现可以扩展 `EvidenceSource`，或在 mutation/constraint 上增加独立 `evidence_explicit` 字段；选择
以最小化 state、retrieval、ranking、structured pool 和序列化影响为准。不得靠把模型结果伪装成
`rule` 来取得权威，否则诊断和降权边界会丢失。

## Merge Pipeline

```text
validated model JSON
  -> textual support check
  -> attribute normalization
  -> action normalization using message + current state
  -> destructive-operation guard
  -> explicit/inferred provenance assignment
  -> deterministic/model conflict resolution
  -> IntentUpdate
  -> StateReducer.apply (唯一 state writer)
```

### Non-destructive normalization

- 普通“prefer/look for/prioritize X”没有替换含义时，模型 `replace` 归一为 `upsert`。
- 只有明确 `change/switch/instead/replace` 才保留 replace scope。
- 品牌提示词 `brand/label/made by` 与目录可识别文本用于 brand 归一；无法安全分类时保留 query evidence，
  不制造 hard constraint。

### Destructive normalization

`remove` 同时满足以下条件才通过：

1. 当前原文含明确撤销 marker；
2. 当前 state 有对应 active attribute/value；
3. 模型删除范围不宽于原文；
4. deterministic parser 未识别相反含义。

global reset 继续要求确定性解析器独立确认。任何不满足条件的 destructive mutation 被拒绝，但其它
安全 model mutation 可继续合并；若 completion/schema 本身非法，则整份模型结果原子回退。

## Boundary Semantics

model-first 会对每轮消息调用模型，但协议边界保持确定性 veto：

- `no additional preferences` / global exhaustion 不清空已确认约束或历史有效 query evidence；
- 模型不得从边界句重新构造新商品偏好；
- `avoid` 不得被模型翻转；
- 明确 reset 仍由 deterministic scope 控制。

这避免强制实验中“模型从历史 snapshot 把旧 query terms 重新写回来”成为生产行为。

## Policy Integration

`SessionState.confirmed_attributes` 使用 evidence provenance，而不是简单排除所有 model source。
ClarificationPolicy 不再重复询问 explicit model 已回答的 slot。CommitPolicy 继续使用 target-free 的
候选数、领先幅度、稳定性和耗尽状态；只调整“有可用高置信推荐却只问不返回”的分支，不读取隐藏
目标或 benchmark 场景。

## Failure and Fallback Matrix

| Failure | Required behavior |
| --- | --- |
| intent flag off / no key | 不调用网络，直接 rules-only |
| DNS/SSL/timeout/HTTP | 记录 failure，使用冻结 deterministic update |
| invalid JSON / unknown field / ID leakage | 整份模型 interpretation 不进入 reducer |
| unsafe remove/reset | 拒绝该 mutation，保留安全 mutation；若 schema 非法则整份回退 |
| all model tiers unavailable | rules_fallback，不影响推荐协议 |
| semantic reranker failure | 保留 feature ranking；与 intent fallback 独立 |

## Diagnostics

每轮新增或明确保留：

- mode、path、backend、trigger/fallback reason；
- normalized actions 与 normalization reason；
- accepted/rejected mutation 的 explicit/inferred provenance；
- deterministic fingerprint 与 fallback 后 fingerprint；
- intent/semantic usage 分开记录，失败不得伪造 usage。

## Compatibility and Rollback

- 不改 `Agent.reset/respond`、`IntentUpdate` reducer ownership 或 catalog whitelist。
- 默认 `INTENT_MODEL_ENABLED=false` 保持无网路径。
- `INTENT_MODEL_MODE=rules_first` 可单配置回滚 model-first trigger。
- 合并/确认/提交变化拆成小提交和独立测试门；任一阶段回归可回滚，不影响规则基线。
