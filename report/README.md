# 报告目录（ContestAgent PUBLIC）

计分实现：`starter/shopping_agent/contest_*.py`。评估器加载的 `starter.agent.Agent` 就是它。组员 Qwen 实验见分支 `legacy/qwen`。

## 当前分数

| 集合 | n | Hit@10 | MRR | MTTC | 技术分 | 说明 |
|---|---:|---:|---:|---:|---:|---|
| 公开 200 | 200 | 1.000 | 0.9467 | 2.53 | **0.9534** | `results_contest_public.json`，0 token |
| ID-holdout 200 | 200 | 0.980 | 0.7743 | 2.675 | **0.8888** | 排除公开集 asin，不是官方私有 800 |
| 随机 800 | 800 | 0.9725 | 0.8249 | 2.691 | **0.89989** | 8×100 并行；**跳过泛约束 MiniLM 之前**的快照 |

技术分 = `0.50×Hit + 0.30×MRR + 0.20×Efficiency`。下一轮进 PUBLIC：**holdout > 0.8888 且 Hit ≥ 0.980，公开 Hit 仍为 1.0**。

## 读哪篇

| 文件 | 内容 |
|---|---|
| [methods.md](methods.md) | 公开 200 消融：去留表（标题覆盖、FlashRank、gate 8–10、RRF 等已拒） |
| [holdout.md](holdout.md) | holdout 200 是什么、和同学对照 |
| [optimize.md](optimize.md) | 还怎么涨分（不要再刷公开集） |
| [optimize_kb.md](optimize_kb.md) | **不要新建知识库**；硬池 IDF/BM25 优先 |

## 复现

```bash
python eval_contest.py --only public
python eval_holdout.py --skip-generate
python -m unittest discover -s tests -v
```

协议骨架（不要为公开集再拧）：永远问 `other`、逐字 AND、`gate_size=5`、Override 前不出表、`dump_slots=4`。MiniLM 只在有区分项的硬池上；cotton/imported/color 跳过 dense 并锁热度 Top-10。缺权重则该项为 0。
