# 还怎么优化，要不要知识库

知识库结论（本赛道定义）：**不需要新建知识库；已经有了。** 这里的「知识库」= 冻结 5 万目录在内存里的检索库（`ContestIndex`），不是外挂 RAG、不是工业向量库、也不是 packing-agent 的 `knowledge/`。

当前分数（PUBLIC，`dense_skip_generic=True`，泛约束跳过 MiniLM 并锁热度 Top-10）：

| 集合 | Hit@10 | MRR | MTTC | 技术分 |
|---|---:|---:|---:|---:|
| 公开 200 | 1.000 | 0.9467 | 2.53 | **0.9534** |
| ID-holdout 200 | 0.980 | 0.7743 | 2.675 | **0.8888** |

验收下一轮改动：**holdout 技术分 > 0.8888 且 Hit ≥ 0.980，公开 Hit 仍为 1.0。** 只涨公开、holdout 不动或变差，不进 PUBLIC。

技术分 = `0.50×Hit + 0.30×MRR + 0.20×Efficiency`。holdout 相对公开掉约 0.065，其中 MRR 0.947→0.774 约占 −0.052，Hit 1.00→0.98 约占 −0.010。还能动的几乎全是 **合取之后怎么排克隆**。

---

## 1. 知识库：定义、范围、四条剩余 miss

**定义（Track 4 conversational shopping search）。** 评分只认冻结目录里的 `parent_asin`。Agent 能用的「知识」只能是目录字段：`title` / `features` / `description` / `details` / `price` / `categories` / `store` / 评分。模拟器从 **目标商品自己的** features/details 挖约束，逐字披露。没有目录外的百科、FAQ、用户评论会话可引用。

**官方范围。** `docs/competition_specification.md` L13：

> Out of scope: … and infrastructure-heavy vector databases.

同文件 L11 允许 keyword / dense / hybrid **检索**（在冻结目录上），L15–17 目录 5 万 SKU、只评 `parent_asin`。因此：

- **禁止当提交依赖：** FAISS/Milvus/Chroma/Pinecone 一类独立向量基础设施。
- **允许：** 内存里扫目录、可选本地 MiniLM（已有，缺权重则该项为 0）。
- **禁止：** 改目录、用目录外 ID、另建一份「商品知识库」文件当真相源。

**已经有的检索库。** `starter/shopping_agent/contest_index.py` 把 `data/catalog.jsonl` 一次读进内存：`blobs`（features/details 展平）、粗类目桶、`token_sets`、`popularity`（log 评论数 + 评分）。`contest_rank.conjunction_asins` / `hard_pool` 在这套索引上做逐字 AND。这就是赛道允许的知识库。MiniLM 只对硬池 2–80 做余弦，不是第二份 KB。

**四条剩余 holdout miss 不是缺事实。** 现漏 `holdout_0005`, `0052`, `0135`, `0183`。先前诊断（`constraint_matches` 全 True，目标在 hard pool）：

| id | 池子 | popularity 名次 | 含义 |
|---|---:|---:|---|
| 0005 | 24 | 16 | 宽 AND 后冷门，热度进不了 Top-10 |
| 0052 | 70 | 54 | 同上 |
| 0135 | 568 | 252 | 卡面耗尽后的大池 dump |
| 0183 | 24 | 23 | 同上 |

合取已经用上了目标自己的 features/details。再加外部 KB、评论、百科 **不会** 把 pop-rank 16/54/252/23 抬进 Top-10；那些槽位被更热的克隆占着。要救这四条等于在公开集上打倒 `w_popularity=1.0`。packing-agent 的 `knowledge/` 是另一道题，与本 Agent 无关。

**一句结论：** 不要新建知识库；不要上工业向量库。继续在现有内存索引上做 **短名单排序**。

---

## 2. 还值得试的方法（按期望 holdout 收益）

协议层冻住：始终问 `other`、逐字 AND、`gate_size=5`、Override 前不出表、`dump_slots=4`。这些是模拟器机制，不是公开 200 的偶然结构。

### 1. 硬池内小权重 IDF / BM25（优先）

合取之后每条约束大家都满足，`w_constraint` 是平的，排序 ≈ 热度 + 一点 MiniLM。同学 **structured** holdout MRR **0.806**，我们 **0.774**——他们 FTS5/BM25 融合更会排克隆，而不是多了一份 KB。

只在硬池 ≤20（或 dump 短名单）上给 **稀有 token** 小权重（`rubber sole`、`buckle closure`），热度仍为主。不要把 `w_title` 当默认打开（公开 MRR 0.943→0.929–0.940，`report/methods.md` 已拒）。

期望：holdout MRR 若 0.774→0.80，约 +0.008 分。必须 holdout 总分 > 0.8888 且 Hit ≥ 0.980。

### 2. 区分项短名单上的可选本地 listwise（非默认）

同学公开 0.9445 来自 Mac 上 LLM+bge。官方评分可能断网。若做：仅 dump 后 ≤10 条、失败退回 0-token 路径。没有 holdout 证据不当 PUBLIC 默认。MiniLM / LLM **都不是** 提交所必需。

### 3. 测量，不是旋钮：重跑随机 800

当前 800 分 0.8999 是 **跳过泛约束 MiniLM 之前** 的快照。不能当新配置的泛化证据。实现 1 或 2 之后再跑；本分析不重跑。

### 4. 不要为剩余 4 条 Hit 去倒热度

0005/0052/0135/0183 已在池内。无条件 `dense_pop_floor` 8/10 会用 `0090`（pop 12、靠 MiniLM 进第 10）换掉别的命中。RRF 把 holdout Hit 打到 0.97。这四条记为不可约。

### 5. 不要再提前出表

`min_slots=2` 公开分 0.9473。Override MTTC 地板约 4.1。Efficiency 权重 0.20，再挤 MTTC 会伤 MRR。

---

## 3. 明确不要当下一轮 PUBLIC 默认

这些在 `report/methods.md` 已拒绝或测过为负。**除非** 新的 holdout+公开证据同时过线，否则不重开：

| 旋钮 | methods.md | 为何不是下一步 |
|---|---|---|
| `w_title` 标题覆盖 | 拒绝，公开 MRR 掉到 0.929–0.940 | 过拟合公开热门标题 |
| FlashRank `w_rerank=0.1` | 拒绝，公开 0.9497 | 整体 MRR 下降 |
| `gate_size` 8–10 | 拒绝，≤0.9477 | Efficiency 不够补 MRR |
| 无条件 `dense_pop_floor` 8 或 10 | 拒绝 | 用 0090 换 0122 / 仍丢 0090 |
| `dense_rrf_k=10` | 拒绝，holdout Hit 0.97 | 并集 RRF 两条都丢 |
| `strict_override_gate` | 拒绝 | Override MRR 几乎不动、MTTC 变差 |
| `overlap_margin`、`w_price`、`min_slots=2` | 拒绝 | 公开无增益或掉分 |

下一件实事：硬池 IDF/BM25 小权重，holdout 判，公开 Hit 保 1.0。不是再加知识库。
