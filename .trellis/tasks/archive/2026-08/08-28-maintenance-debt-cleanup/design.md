# Technical Design

## Boundaries

保持运行链路不变：

```text
starter.agent.Agent
  -> intent.parse_intent_update
  -> state.StateReducer / state.SessionState
  -> retrieval / structured_pool / ranking / optional reranker
  -> response guard
```

新增 `starter/shopping_agent/intent.py`，承载消息意图解析所需的正则常量、属性推断、
override scope 判断、约束提取和 `parse_intent_update`。该模块从 `state.py` 导入
`ConstraintMutation`、`IntentUpdate`、`QueryEvidence` 等数据类型。

`state.py` 在定义完数据类型和 reducer 后，从 `intent.py` 重导出
`parse_intent_update`。若实现时发现 Python 导入环不稳定，则改为 `state.py` 内的薄
懒加载包装函数；公共签名保持不变。

## Compatibility

- `starter.agent.Agent` 可继续从 `.shopping_agent.state` 导入解析函数。
- 现有测试和外部调用者无需改变导入路径。
- `starter/shopping_agent/__init__.py` 的 `__all__` 保持现有名称。
- 这是纯文件边界重构；正则、置信度、hardness、scope 和 mutation 生成顺序逐字迁移。

## Documentation Changes

- README 删除不存在的四个链接，不替换成无证据的新流程。
- Agent spec 使用职责表描述实际模块，并记录兼容重导出和依赖方向，避免未来再次把
  orchestration 或解析逻辑堆回 facade。

## Risks and Rollback

- 最大风险是循环导入或机械迁移遗漏。以旧导入路径测试、模块 import smoke 和完整
  unittest 锁定。
- 行为漂移通过移动代码而非重写代码控制；迁移后用 diff 和针对性 Intent Override
  测试复核。
- 若兼容重导出导致循环导入，回退为薄包装而不是撤销模块边界。
