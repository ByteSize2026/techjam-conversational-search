# 有界购物工具循环（已废弃，仅供历史参考）

**此架构已在任务 `08-28-agent-v2-router-value-node` 中被彻底移除。** `actions.py`、`tools.py`、`planner.py`、`orchestrator.py` 及其在 `starter/agent.py` 中的接线都已删除（不是废弃不用，是文件本身不存在了），不得重新引入。这是有意的架构决策，不是回退：自由工具循环让模型自己选择"下一步调用哪个 action"，这种控制流权力被认为架构上不可取——细节见 [Router / Value-Node 图](./router-value-node-graph.md) 的 §1。

当前架构规范见 [Router / Value-Node 图](./router-value-node-graph.md)。本文件保留仅为历史存档（原始工具闭集、action 签名等在 git 历史 `c891de8`/`b24a40d` 仍可查），**不要**依据本文件内容修改当前代码。
