# 提交说明（ContestAgent PUBLIC）

官方要：可复现的 `Agent`、短报告、延迟 / token / 成本、一场多轮演示。计分入口是 `starter.agent.Agent`（ContestAgent + `PUBLIC`）。

## 怎么跑

Python 3.10+。计分路径只依赖标准库。

```bash
gzip -dc catalog.jsonl.gz > data/catalog.jsonl   # 发布包，见 SHA256SUMS
python -m unittest discover -s tests -v
python -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output results.json
python demo/run_demo.py --session public_0002
```

可选 MiniLM：先读本机缓存 `sentence-transformers/all-MiniLM-L6-v2`，没有再从 Hub 下一次。硬池余弦 `w_dense=0.1`（区分项且池 ≤6 时再加 `w_dense_tiny=0.12`）。加载失败则该项为 0。强制只用缓存：`TECHJAM_DENSE_OFFLINE=1`。不需要 API key。

## 模型、token、成本、延迟

| 组件 | 默认 | 成本 | 断网 |
|---|---|---|---|
| 类目锁 + 逐字 AND + 热度 | 标准库内存索引 | $0 | 是 |
| MiniLM（可选） | 缓存优先，缺则 Hub | $0 | 有缓存即可；`TECHJAM_DENSE_OFFLINE=1` 禁止下载 |
| DeepSeek / Qwen | 不计分默认关 | — | 不依赖 |

公开 200 次评估：`usage` **0 token**。本机 Windows、catalog 5 万条：索引约 8s，200 会话约 35s（约 0.2s/会话，含可选 MiniLM）。无 MiniLM 时会话循环更快，分数接近无 dense 的公开 ~0.950。

## 演示会话

`demo/run_demo.py` 用官方模拟器策略重放公开集。稳定样本：

| 场景 | 命令 | 记录 |
|---|---|---|
| Buying | `--session public_0001` | `report/demo_buying.txt` |
| Browsing | `--session public_0006` | `report/demo_browsing.txt` |
| Intent Override | `--session public_0002` | `report/demo_override.txt` |
| Boundary | `--session public_0035` | `report/demo_boundary.txt` |

## 限制

- 公开 0.95 不能当私有 800 的预测；holdout 上 MRR 会掉（克隆共享模板句）。
- 热度名次深于 Top-10 的合取命中（holdout `0005/0052/0135/0183`）不倒热度硬抬。
- 模拟器只看 `ask_attribute`；字段必须继续问 `other`，口头问题可以复述已记意图。

## 贡献

- ContestAgent PUBLIC（`contest/public` / 本工作树）：模拟器协议、合取检索、holdout 门禁、演示与报告。
- `legacy/qwen`：组员 FTS / 结构化池 / 可选 Qwen，评估器不加载。
