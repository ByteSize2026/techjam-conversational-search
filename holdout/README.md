# 自建测试集（不是官方私有 800）

官方私有 800 不可见。这里是同一本冻结目录上、排除 `data/public_set.jsonl` asin 的抽样，用来看公开 0.95 虚高多少。

## 当前该看的文件

| 文件 | 是什么 |
|---|---|
| `holdout_200.jsonl` | ID-holdout 200：80/80/30/10，seed 2026 |
| `ours_holdout.json` | 当前 PUBLIC 在 holdout 200 上的完整 evaluate（Hit 0.980 / 0.8888） |
| `holdout_compare.json` | 我们 vs 同学 structured（同学 holdout 当时无 LLM） |
| `random_800.jsonl` | 320/320/120/40，seed 见生成脚本，与公开 asin 不交 |
| `random800_compare.json` | 8 路合并：Hit 0.9725 / 0.89989（**旧配置快照**） |
| `shards/shard_0.jsonl` … `shard_7.jsonl` | 各 100 条 |
| `shards/out_0.json` … `out_7.json` | 各片 `eval_shard.py` 输出 |

`ours_holdout.json` 与 `holdout_skip_floor.json` 是同一轮（泛约束跳过 MiniLM + 锁热度）。

## 消融残骸（不要当当前分数）

这些是试热度锁 / RRF / 只跳过 MiniLM 时的完整 payload，**没有**写进 PUBLIC：

- `holdout_popfloor.json` / `public_popfloor.json` — `dense_pop_floor=10`（用 0090 换 0122）
- `holdout_popfloor8.json` / `public_popfloor8.json` — floor=8（仍丢 0090）
- `holdout_rrf.json` / `public_rrf.json` — `dense_rrf_k=10`（holdout Hit 0.97）
- `holdout_skipgeneric.json` / `public_skipgeneric.json` — 只跳过泛约束、不锁热度（0122 仍漏）

生成：

```bash
python eval_holdout.py
python eval_shard.py --dataset holdout/shards/shard_0.jsonl --output holdout/shards/out_0.json
```
