# 跨层变更影响

## 从符号而不是猜测开始

先搜索拟改的真实符号和字段，再判断影响面。此项目的主链是：

```text
catalog/public_set JSONL
  -> Agent.reset / Agent.respond
  -> evaluator.evaluate
  -> normalize_recommendations
  -> Hit@10 / MRR / MTTC / scenario_metrics
```

来源分别是 `starter/agent.py`、`evaluator/local_evaluator.py`、`docs/agent_api_contract.json` 和 `docs/evaluation_config.json`。不要将策略改变误判为可单独修改的局部函数：会话状态、响应结构与评分过滤相互约束。

## 影响矩阵

| 修改目标 | 必查真实位置 | 最小验证 |
| --- | --- | --- |
| `Agent.reset` 或会话状态 | `starter/agent.py:Agent.reset`、`Agent.respond`、`evaluator.local_evaluator:evaluate` | 两个 session 不串状态；reset 后按首轮处理 |
| `respond` 输出字段 | `docs/agent_api_contract.json:turn_response`、`customer_reply`、`normalize_recommendations` | 枚举、字符串 message、有效唯一 ID 顺序 |
| 排序或候选量 | `normalize_recommendations`、`TOP_K` | 目标在前 10 个有效唯一 ID 内才可命中 |
| 查询/约束策略 | `initial_message`、`customer_reply`、`behavior_for` | Buying、Browsing、Intent Override、Boundary 都不退化 |
| 指标或评分 | `metric_summary`、`evaluate`、`docs/evaluation_config.json`、`tests/test_evaluator.py` | miss=11、MRR rank、0.50/0.30/0.20 权重 |
| JSONL 或 catalog 读取 | `load_jsonl`、`catalog_index`、`data/README.md` | 小型 `TemporaryDirectory` fixture 可读且不写冻结数据 |
| 网络/模型依赖 | `docs/submission_rules.md`、`docs/competition_specification.md` | 明确环境变量、离线 fallback 或无法离线运行的声明 |

## 必查场景

修改用户消息解析、约束状态或追问策略时，不能只看 Buying：

- `intent_override` 的新要求在第 3 或第 4 轮出现，旧偏好必须能被撤销或降权，且覆盖前命中不计分。
- `boundary` 对已询问属性可返回无偏好；应继续使用其他证据，不循环追问。
- `browsing` 的初始信息稀少，过早空推荐或不合规提问会浪费轮次。

每项约束都来自 `evaluator/local_evaluator.py:behavior_for`、`initial_message`、`customer_reply`、`evaluate`。场景指标分开报告，不能只以总分判断回归。

## 评审顺序

1. 读取对应领域 index 和专题规范。
2. 搜索修改符号的所有使用点。
3. 为受影响的过滤、场景或评分补充最小确定性 unittest。
4. 运行 `python3 -m unittest discover -s tests -v`。
5. 若本地有 `data/catalog.jsonl`，再运行 evaluator；没有时保持测试 fixture 路径并说明缺失数据，不伪造全量评估。

离线前提和提交限制见 [离线复现](./offline-reproducibility.md)。
