# ID-holdout 200：我们 vs 同学 vs 公开集

这不是官方私有 800 条。会话由 `eval_holdout.py` 从冻结 `data/catalog.jsonl` 抽样，**排除** `data/public_set.jsonl` 的全部 `parent_asin`，比例仍是 80 Buying / 80 Browsing / 30 Override / 10 Boundary。评估器现算 `intent_card`。抽样按 `log1p(rating_number)` 加权，仍偏热门，但是 **ID holdout**，不是冷启动压力测试。

同学 holdout 走 structured 回退（本机 `TECHJAM_NO_LLM=1`、`TECHJAM_NO_EMBED=1`，gate=5）。其公开集 0.9445 来自 Mac 上 LLM+bge 的 `result/report.md`，和 holdout 不是同一变体。

## 四行对比

| 行 | Hit@10 | MRR | MTTC | Efficiency | 技术分 |
|---|---:|---:|---:|---:|---:|
| 我们 × 公开 200 | 1.000 | 0.9467 | 2.530 | 0.847 | **0.9534** |
| 我们 × holdout 200 | 0.980 | 0.7743 | 2.675 | 0.8325 | **0.8888** |
| 我们 × 随机 800（优化前快照，非官方私有） | 0.9725 | 0.8249 | 2.691 | 0.8309 | **0.8999** |
| 同学 × 公开 200（发表，LLM+bge） | 0.995 | 0.9358 | 2.685 | 0.8315 | **0.9445** |
| 同学 × holdout 200（structured） | 0.965 | 0.8061 | 2.900 | 0.810 | **0.8863** |

- **Holdout 上更好的 Agent：我们**（0.8888 > 0.8863）。
- **两边的公开集分数都偏高**：我们 0.953 → 0.889，同学 0.945 → 0.886。掉分主要是 MRR。
- 我们 holdout 现漏 4 条：`holdout_0005, 0052, 0135, 0183`（pop 16/54/252/23）。`0122` 已修好；`0090` 靠 MiniLM 保命中。同学漏 7 条。
- 随机 800（优化前快照）：Hit 0.9725，技术分 0.89989。**不是**组织方私有 800。

复现：

```text
python eval_holdout.py
python eval_shard.py --dataset holdout/shards/shard_0.jsonl --output holdout/shards/out_0.json
```

工件：`holdout/holdout_200.jsonl`、`holdout/holdout_compare.json`、`holdout/random_800.jsonl`、`holdout/random800_compare.json`。
