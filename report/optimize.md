# 应该怎么优化

结论先说：**holdout 上我们略好于同学（0.8981 vs 0.8863），但两边公开集分数都虚高。不要新建知识库（内存目录索引已经是检索库）。下一步不要再刷公开 200。细节见 `report/optimize_kb.md`。Holdout ≠ 官方私有 800。**

## 1. 实测对照（公开 200 vs ID-holdout 200）

| 行 | Hit@10 | MRR | MTTC | Efficiency | 技术分 |
|---|---:|---:|---:|---:|---:|
| 我们 × 公开 200 | 1.000 | 0.9517 | 2.530 | 0.847 | **0.9549** |
| 我们 × holdout 200 | 0.980 | 0.8047 | 2.665 | 0.8335 | **0.8981** |
| 我们 × 随机 800（优化前，非官方私有） | 0.9725 | 0.8249 | 2.691 | 0.8309 | **0.8999** |
| 同学 × 公开 200（`result/report.md`，Mac 上 LLM+bge） | 0.995 | 0.9358 | 2.685 | 0.8315 | **0.9445** |
| 同学 × holdout 200（本机 structured，无 LLM/dense） | 0.965 | 0.8061 | 2.900 | 0.810 | **0.8863** |

- **Holdout 更好的 Agent：我们**（0.8981 > 0.8863）。
- 公开集相对 holdout：**两边都过拟合/分布偏移**。我们 0.955→0.898，同学 0.945→0.886。主因仍是 **MRR**（0.952→0.804）。Hit 现为 0.980（漏 4 条：0005/0052/0135/0183）。`0122` 已用泛约束跳过 MiniLM + 热度锁修好；`0090`（rubber sole）仍走 MiniLM，rank 10 命中。
- 同学公开分来自 LLM+bge；holdout 是 structured 回退。holdout 上的「我们略好」是和他们的无模型管道比，不是和他们的发表 run 比。
- 这套 holdout 是同一本 5 万目录上、排除公开集 `parent_asin` 的 ID 抽样（仍按评论数加权，偏热门）。**不是**组织方私有 800 条。holdout 涨分不能当成私有集保证；私有若更冷，MRR 还可能再掉。

当前 PUBLIC：`gate_size=5`、逐字 AND、`dump_slots=4` / `dump_pool_cap=80`、`min_slots_to_recommend=3`、`w_title=0`、`w_dense=0.1`、`dense_skip_generic=True`（cotton/imported/color 跳过 MiniLM，并锁热度 Top-10；`rubber sole` 这类区分项仍走 MiniLM），硬池 ≤6 时额外 `w_dense_tiny=0.12`，精确 feature/details 行 `w_field=0.35`（行键去掉句末标点，对齐模拟器 `_clean_constraint`），区分项整句出现在标题里时 `w_phrase=0.15`。精确行打平时跳过 MiniLM 已测：holdout 掉到 0.8935，**默认关**。缺 MiniLM 权重则 dense 分量为 0。

## 2. 冻住（不要为公开集再拧）

这些是评估器协议，私有 800 条也会这样；公开集上再调只会过拟合：

| 冻住 | 原因 |
|---|---|
| 始终问 `other` | 模拟器主要靠 `other` 泄出 features/details 原文；改问 `color`/`material` 会漏长句 |
| 逐字 AND，滤空则跳过 | 目标一定能匹配自己的卡槽；这是 Hit 的来源 |
| `gate_size=5` | 公开集上 8–10 的 gate 总分 ≤0.9477，MRR 跌幅大于 Efficiency |
| `w_title=0` | 标题 token 覆盖把 MRR 从 0.942 打到 0.929/0.940 |
| Override 前不出分 | `gate_before_override` 与评分规则一致 |

已经测过、**没有新的 holdout 证据就不要当 PUBLIC 默认重新打开**：

- FlashRank TinyBERT `w_rerank=0.1`（公开总分 0.9497，整体 MRR 下降）
- `strict_override_gate`（公开 0.9517，Override MRR 几乎不动、MTTC 变差）
- `overlap_margin`、`min_slots=2`、预算距离、`dump_pool_cap` 再放大

**不要只看公开 200 决定去留。** 任何新试验必须：Hit@10 不能垮（公开集不要从 1.0 掉成漏一串；holdout 不要明显低于现在的 0.975），并且用 **holdout 或公开+holdout 一起** 判，而不是公开分单独上涨。

MiniLM `w_dense=0.1` 只解释公开集大约 +0.002 分；官方评分可能没有权重。离线路径必须在没 MiniLM 时仍合法（当前会退回纯规则）。不要把 MiniLM 写成提交所必需。

## 3. 值得试（针对 holdout，不是再刷公开集）

技术分 0.50×Hit + 0.30×MRR + 0.20×Efficiency。holdout 上从 0.955 掉到 0.898，大约：

- Hit 1.000→0.980：约 −0.010 分（4/200 miss，权重 0.50）
- MRR 0.952→0.804：约 −0.044 分
- Efficiency 小幅变差

MRR 缺口更大，但 **先修 Hit 更划算、更稳**：漏一条记 MTTC=11，而且 5 条里 4 条同学也漏，像匹配/合取边界，不是「再加点 dense」。

### 3.1 先做：holdout Hit 漏（起始集合）

我们 holdout 未命中：

| sample_id | 场景 | 与同学 |
|---|---|---|
| `holdout_0005` | browsing | 两边都漏 |
| `holdout_0052` | buying | 两边都漏 |
| `holdout_0135` | intent_override | 两边都漏 |
| `holdout_0183` | intent_override | 两边都漏 |
| `holdout_0122` | buying | 仅我们漏 |

`0122`（cotton/color/imported、pop 8）已修好：泛约束跳过 MiniLM，再锁热度 Top-10。`0090`（leather + rubber sole、pop 12、MiniLM 排第 10）继续走 dense。剩下 4 条不可约：pop 16 / 54 / 252 / 23。硬抬它们会打翻公开集 popularity-first。

随机 800（优化前快照）Hit 0.9725 / 0.89989。这不是组织方私有 800。

### 3.2 然后才做：未见过 asin 上的 MRR

硬合取之后的克隆排序在新 ID 上仍差一截（我们 holdout MRR 0.805，同学 structured 0.806——几乎打平，我们靠 Hit 和 MTTC 把总分拉开）。

只允许 **硬池短名单、小权重、可回退** 的打平；用 holdout 判：

- 保持流行度为主，不要在意图卡耗尽后压低流行度（同学公开集上掉过 2 分）。
- 不要重新打开标题覆盖或 FlashRank 0.1，除非 holdout 上 Hit 不掉且总分超过现在的 0.8876。
- MiniLM 权重不要在公开集上从 0.1 往上调。

### 3.3 不要为 MTTC 再提前出表

`dump_slots=4` 已经吃掉「卡面问完还等一轮」。再提前（更宽 dump、更小 min_slots）公开集上已经伤 MRR。holdout MTTC 2.73 仍受场景地板约束（Override 约 4.1）。

## 4. 提交时怎么选

- 骨架：always-`other` + 逐字 AND + gate=5 + Override 门，**不要为公开集再加旋钮**。
- MiniLM：可选；没有权重时分数应接近无 dense 的公开 ~0.950 / holdout 需再测，但路径必须能离线跑完。
- 下一轮代码改动的验收：**holdout 技术分 > 0.8981 且 Hit ≥ 0.980，同时公开 Hit 不塌。** 只涨公开分、holdout 不动或变差，视为过拟合，不进 PUBLIC。

私有 800 条仍然不可见。holdout 只能说明「换一批同目录 ID 会掉 MRR」；不能代替官方私有集。
