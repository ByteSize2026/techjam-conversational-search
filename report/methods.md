# TechJam 2026 Track 4 — 参赛方法报告（公开 200 集）

本文记录：网上查到的会话式商品搜索方法、它们和本赛模拟器的匹配关系、以及在冻结 `data/public_set.jsonl`（200 条）+ `data/catalog.jsonl`（50k）上的实测去留。官方评估器、`docs/`、公开集和目录均未改。计分路径全程内存、离线、0 token。

**当前公开集地板（改 PUBLIC 默认之前）**

| 配置 | Hit@10 | MRR | MTTC | Efficiency | TechnicalScore |
| --- | ---: | ---: | ---: | ---: | ---: |
| PUBLIC 基线（always-`other` + 逐字 AND + gate=5 + 满 3 槽且池≤20 则推荐） | 1.000 | 0.936845 | 2.555 | 0.8445 | **0.949954** |

剩余误差：Hit 已满；约 21 条会话进了 Top-10 但不是第 1（同类商品共享 `cotton` / `Imported` / `Machine Wash` 等模板句）；Buying MTTC 约 2.1，仍有「池子已经不大却还在等 `other` 说没有更多偏好」的回合。标题 token 覆盖已测过，MRR 从 0.942 掉到 0.929/0.940，**禁止作为 PUBLIC 默认**。

---

## 1. 模拟器约束（方法必须过这道门）

评分循环见 `evaluator/local_evaluator.py`：

- 只认精确 `parent_asin`。
- `ask_attribute` 为枚举；`null` 不泄槽。
- 问 `other` 时，模拟器从意图卡最多再吐 **2** 条未披露约束（features/details 原文、`color: x`、`budget around $x`）。
- 问 `color`/`material` 等具体属性时，**只有** `classify_constraint` 对得上的卡槽才会出现；意图卡里大量是长 feature 句，标成 `feature`/`other`，问 `color` 往往一无所获。
- Intent Override 在第 3/4 轮前命中不计分。
- 官方评分可能断网。

因此：论文里「按属性做 20 questions / GBS 选槽」不能直接搬；「等到候选集足够小再推荐」和「用区分度高的词在小池里排序」可以离线落地。

---

## 2. 网上查到的方法 → 本赛适配

### 2.1 D2D：属性感知追问 + 重叠感知 / 信息增益耗尽后的推荐时机

- **来源：** Harne, Modani, Mahapatra, Agarwal. *Dialogue to Discovery: Attribute-Aware Preference Elicitation for Conversational Product Search Assistants*. arXiv:2606.24194, 2026. https://arxiv.org/abs/2606.24194
- **要点：** 在检索子集上维护属性–取值偏好与不确定性；用 **TOI（top overlapping item set）** 决定何时推荐：若与当前最高分商品置信区间重叠过大的商品数 > 展示槽，则先不推荐、继续问最有信息量的属性（ACE 熵 × APU 不确定性）。论文在 Amazon Reviews 衍生目录上强调：过早推荐会锁死差的 NDCG，过晚则放弃会话。
- **适配：** 不能上 LLM 属性问题（见上）。TOI 可离线翻译成：在「已有 ≥3 条合取、池子 6–20」的提前推荐上，若池内 **流行度第一与第二的间隔过小**，视为重叠过大，继续 withhold，等下一条 `other` 披露；间隔足够大则视为候选已被流行度分开，立即推荐。这针对的是基线用 `min_slots_to_recommend=3` 换 MTTC、却把接近的 clone 赛锁在较差 MRR 的那批会话。
- **决策：** TOI（流行度间隔）在公开 200 上 MRR 回升、MTTC 回退，总分 ≤ 0.949729，**拒绝进 PUBLIC**。同一篇论文里「没有更多有信息量的问题就推荐」落到第 3 节的 `dump_slots=4`。

### 2.2 GBS / Learning to Ask：选最能二分候选集的问题

- **来源：** Zou, Huang, Ren, Kanoulas. *Learning to Ask: Conversational Product Search via Representation Learning*. ACM TOIS, 2022. arXiv:2411.14466. https://arxiv.org/abs/2411.14466 ；更早的问实体版 Zou & Kanoulas, CIKM 2019, arXiv:1908.11733. https://arxiv.org/abs/1908.11733
- **要点：** Generalized Binary Search 选使候选集最接近两半的 slot；目标是用尽量少的问题打到目标。
- **适配：** 本模拟器的有效问题几乎只有 `other`（一次最多两条原文约束）。把 `ask_attribute` 改成 `color`/`material` 会漏掉长 feature 句，公开集上会拉高 MTTC 甚至掉 Hit。GBS 的「信息增益选问什么」**不拟合**，报告为拒绝。
- **决策：** 拒绝改变追问属性。不改 PUBLIC。

### 2.3 Google Conversational Product Filtering：问到候选足够少再停

- **来源：** Google Cloud, *Conversational product filtering overview*（AI Commerce Search）. https://docs.cloud.google.com/retail/docs/conversational-filtering （2026-08-11）
- **要点：** 宽查询先出大结果，再按属性追问并立刻过滤；对话持续到 **预配置的最小商品数**、用户点商品、或问题问完。文档写明「只剩两件时再问没有意义」。
- **适配：** 已实现为 `gate_size=5`（同学结构）+ 满 3 槽且池≤20 可提前出表。再把 gate 放到 8/10 会让 MRR 下降超过 Efficiency 收益（内部 gate 扫描：gate=10 时分 0.946189 < 0.949954）。
- **决策：** 拒绝再放宽 gate。最小件数思想已在基线里。

### 2.4 区分性词项 / BM25 IDF：压低 corpus 里太常见的词

- **来源：** Robertson & Zaragoza, *The Probabilistic Relevance Framework: BM25 and Beyond*, FnTIR 2009（BM25 IDF）；SIRA（*Superintelligent Retrieval Agent*）把检索写成「用能把目标从混淆项里分开的词」，arXiv:2605.06647. https://arxiv.org/html/2605.06647v1
- **要点：** IDF 降低 `cotton` 这类高 DF 词，抬高稀有术语。SIRA 明确：相关不够，必须在混淆项之上拉开间隔。
- **适配：** 硬 AND 之后，披露约束的全文已在池内每条 blob 里，约束 token 的池内 DF≈N，IDF≈0，**排序不变**。标题 token 覆盖（同类想法的朴素版）已在公开集上伤害 MRR：目标的区分句在 features/details，热门 clone 的标题更常写 Cotton。对公开 200 条用「长约束只打 features+details」收紧合取：目标 0 丢失，平均池 2.51→2.48，流行度第 1 仍 185/200，几乎无增益。
- **决策：** 词面 IDF / 标题覆盖拒绝。**池内 MiniLM 余弦**（all-MiniLM-L6-v2，仅 2–80 的硬池，`w_dense=0.1`，缺权重则分数不变）在公开 200 上抬了 MRR，见第 4 节。

### 2.5 RelQuest / PSCon：从候选描述里生成能消歧的问题；以及「何时推荐」子任务

- **来源：** RelQuest（UMass，从 top 商品描述聚类再提问以消歧），https://scholarworks.umass.edu/bitstreams/d6f77e39-6ffc-45b0-a180-af1778325624/download ；Zou et al., *PSCon: Product Search Through Conversations*, arXiv:2502.13881. https://arxiv.org/html/2502.13881v3 （T3 系统动作：问 vs 推荐，T4 选题，T5 排序）
- **适配：** 问句生成必须走 `ask_attribute=other`，不能把聚类词变成 `color`。PSCon 的「何时推荐」与 D2D TOI 同类，已由 overlap-margin 覆盖。
- **决策：** 不单独实现 LLM/聚类提问。

### 2.6 其它扫过、明确不搬进计分路径

- 稠密检索 / MiniLM / FlashRank / 本地 LLM listwise：同学仓库与我方 ablation 均为净负或不可离线依赖。
- 标题覆盖作默认排序：见上，已拒。

---

## 3. 本次落地的一条杠杆（信息增益耗尽 → `dump_slots=4`）

意图卡固定为最多 2 条 hard + 2 条 soft。always-`other` 在第 3 次披露之后，下一句几乎一定是「I don't have an additional preference」。再 withhold 一轮，池子往往不变，只烧 MTTC。

这对应：

- D2D：没有更多能分开候选的问题时再推荐（TOI 间隔实测失败后，改用「槽位已经等于卡面长度」作停止规则）。
- Google conversational filtering：问题问完就停。
- GBS/PSCon 的「何时推荐」：下一问的信息增益≈0 时出表。

**配置（KHANNA/CLASSMATE 默认 0=关闭）：**

- `dump_slots=4`：已有 ≥4 条合取槽。
- `dump_pool_cap=80`：工作池不超过 80 则推荐（公开集上 cap=80 与 cap=200 同分，说明剩余大池都不超过 80）。
- 仍问 `other`；不改属性枚举。池子 ≤5 的原 gate 与满 3 槽且池≤20 的提前出表都保留。

**预期：** Hit 仍为 1.0（这些会话基线已在 Top-10），MTTC 下降约 0.025，MRR 不掉。

---

## 4. 官方 200 实测表

`python eval_contest.py --only public` 调用 `evaluator.local_evaluator.evaluate`。下列行在试验后填写；**只有分数严格大于 0.949954 且 Hit=1.0 的配置才会写进 PUBLIC 默认。**

| 试验 | Hit@10 | MRR | MTTC | score | 相对 0.949954 | 去留 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| PUBLIC 基线（3 槽且池≤20） | 1.000 | 0.936845 | 2.555 | 0.949954 | 0 | 地板 |
| 标题 token 覆盖（历史） | 1.000 | 0.929–0.940 | 2.725 | 0.944–0.947 | 负 | 拒绝 |
| 放宽 gate 到 8–10（历史模拟） | 1.000 | ↓ | ↓ MTTC | ≤0.9477 | 负 | 拒绝 |
| 长约束只打 features+details（池探针） | 目标仍在 | 流行度第1仍 185 | — | 池 2.51→2.48 | 负 | 拒绝 |
| GBS 改问具体属性 | — | — | — | 未上线 | 模拟器不拟合 | 拒绝 |
| `overlap_margin=0.04`（D2D TOI） | 1.000 | 0.939762 | 2.610 | 0.949729 | −0.000225 | 拒绝 |
| `overlap_margin=0.08` | 1.000 | 0.939881 | 2.670 | 0.948564 | −0.00139 | 拒绝 |
| `w_price=0.85 / 1.2`（预算距离） | 1.000 | 0.936845 | 2.555 | 0.949954 | 0 | 拒绝（无差） |
| `min_slots=2, cap=12` | 1.000 | 0.928798 | 2.565 | 0.947339 | −0.002615 | 拒绝 |
| `dump_slots=4, cap=10/20` | 1.000 | 0.936845 | 2.555 | 0.949954 | 0 | 与基线同 |
| `dump_slots=4, cap=30` | 1.000 | 0.936845 | 2.550 | 0.950054 | +0.0001 | 次优 |
| `dump_slots=4, cap=40` | 1.000 | 0.936429 | 2.540 | 0.950129 | +0.000175 | MRR 微降 |
| `dump_slots=4, cap=80` | 1.000 | 0.936845 | 2.530 | 0.950454 | +0.0005 | 保留协议 |
| `dump_slots=4, cap=200` | 1.000 | 0.936845 | 2.530 | 0.950454 | +0.0005 | 与 80 同分 |
| `dump_slots=4, cap=80` + MiniLM `w_dense=0.1` | 1.000 | 0.942964 | 2.530 | 0.952289 | +0.002335 | 次优 |
| MiniLM 热度保护 `dense_pop_floor=10` | 1.000 | 0.942964 | 2.530 | 0.952289 | +0.002335 | 拒绝（holdout 用 0090 换 0122，MRR 掉） |
| MiniLM 热度保护 `dense_pop_floor=8` | 1.000 | 0.942964 | 2.530 | 0.952289 | +0.002335 | 拒绝（仍丢掉 0090） |
| pop∪dense RRF `dense_rrf_k=10` | 1.000 | 0.941625 | 2.530 | 0.951887 | +0.001933 | 拒绝（holdout Hit 0.97） |
| 仅 `dense_skip_generic` | 1.000 | 0.946714 | 2.530 | 0.953414 | +0.00346 | 拒绝（holdout 0122 仍漏，MRR 掉） |
| 泛约束跳过 MiniLM + 跳过时锁热度 Top-10 | 1.000 | 0.946714 | 2.530 | 0.953414 | +0.00346 | 前 PUBLIC（holdout 0.8888） |
| 硬池 IDF `w_idf=0.15` | 1.000 | 0.946714 | 2.530 | 0.953414 | +0.00346 | 拒绝（holdout 0.888778 未 > 0.8888） |
| 硬池独占稀有词 `w_exclusive=0.25` | 1.000 | 0.946714 | 2.530 | 0.953414 | +0.00346 | 拒绝（holdout 0.888778 未 > 0.8888） |
| 区分项早出表 `distinctive_early_cap=10` | 1.000 | 0.946714 | 2.505 | 0.953914 | +0.00396 | 拒绝（holdout 0.88453 / MRR 0.758） |
| 小池标题 `w_title=0.12` `title_pool_limit=6` | 1.000 | 0.946714 | 2.530 | 0.953414 | +0.00346 | 拒绝（公开/holdout 与基线同） |
| 泛约束小池 MiniLM `dense_generic_cap=6` `w_dense_tiny=0.25` | 1.000 | 0.915464 | 2.530 | 0.944039 | −0.0059 | 拒绝（holdout 0.8920，公开 MRR 掉到 0.915） |
| 小池加强 MiniLM `w_dense_tiny=0.12` cap=6 | 1.000 | 0.941714 | 2.530 | 0.951914 | +0.00196 | 前 PUBLIC（holdout 0.890778） |
| 硬池 BM25 `w_bm25=0.12`（类目 IDF + 标题 tf×2） | 1.000 | 0.936964 | 2.530 | 0.950489 | +0.0005 | 拒绝（holdout Hit 0.97 / 0.88624，多漏 0067/0106） |
| 硬池 BM25 `w_bm25=0.20` | 0.995 | 0.933631 | 2.565 | 0.946289 | −0.0036 | 拒绝（公开漏 0144；holdout 掉 0090） |
| 硬池标题独有词 `w_uniq=0.15` | 1.000 | 0.924214 | 2.530 | 0.946664 | −0.00525 | 拒绝（holdout Hit 0.97 / 0.886084，掉 0067/0090） |
| 热度近并列才加强 MiniLM `dense_tie_margin=0.04` cap=20 | 1.000 | 0.940923 | 2.530 | 0.951677 | +0.00172 | 拒绝（holdout 0.890599 未 > 0.8908） |
| 精确 feature/details 行 `w_field=0.15` | 1.000 | 0.955417 | 2.530 | 0.956025 | +0.00607 | 前 PUBLIC（holdout 0.894705） |
| `field_key` 去句末标点（对齐模拟器） | 1.000 | 0.955417 | 2.530 | 0.956025 | 0 | 保留正确性（分数不变） |
| 精确行 `w_field=0.25` | 1.000 | 0.956250 | 2.530 | 0.956275 | +0.00632 | 前 PUBLIC（holdout 0.895843） |
| 精确行打平时跳过 MiniLM `dense_skip_field_flat` | 1.000 | 0.954881 | 2.530 | 0.955864 | +0.00591 | 拒绝（holdout 0.893543，rank1 142→139） |
| 精确行 `w_field=0.35` | 1.000 | 0.951667 | 2.530 | 0.954900 | +0.00495 | 前 PUBLIC（holdout 0.897918） |
| **区分项整句标题 `w_phrase=0.15`（PUBLIC）** | **1.000** | **0.951667** | **2.530** | **0.954900** | **+0.00495** | **保留**（holdout Hit 0.980 / 0.898118；公开 Hit 1.0 分数不变） |
| Override 关闭 dump（`strict_override_gate`） | 1.000 | 0.943083 | 2.560 | 0.951725 | −0.000564 | 拒绝（Override MRR 几乎不动） |
| FlashRank TinyBERT `w_rerank=0.1` | 1.000 | 0.934333 | 2.530 | 0.9497 | −0.002589 | 拒绝（整体 MRR 下降） |
| 上两者同时开 | 1.000 | 0.935167 | 2.560 | 0.94935 | −0.002939 | 拒绝（Override MRR 到 0.956，总分仍掉） |

---

## 5. 复现

```text
python eval_contest.py --only public
python -m unittest discover -s tests -v
```

`evaluator.local_evaluator` 加载的 `starter.agent.Agent` 就是 `ContestAgent`（`PUBLIC`）。合同测试走 `LegacyAgent`。计分不调用 LLM、`usage` 为 0 token。MiniLM 只读本机 Hugging Face 缓存（`sentence-transformers/all-MiniLM-L6-v2`）；缺失时 `w_dense` 不生效，退回流行度排序。
