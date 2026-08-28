# Track 4.6 评审轴对照（书面，不是幻灯）

Image #1 五轴：Technical Execution 35%、Innovation & Problem Insight 20%、Impact & Relevance 20%、Feasibility & Practicality 15%、Presentation & Communication 10%。下面把 **当前 PUBLIC**（不是被拒的硬池试验）映射上去。官方私有 800 未见；holdout 200 / 随机 800 不是私有集。

## 当前可测数字（PUBLIC，`w_idf=0`，`w_exclusive=0`）

| 集合 | Hit@10 | MRR | MTTC | 技术分 | token |
|---|---:|---:|---:|---:|---:|
| 公开 200 | **1.000** | 0.9517 | 2.53 | **0.9549** | 0 |
| ID-holdout 200 | **0.980** | 0.8047 | 2.665 | **0.8981** | 0 |

技术分 = 0.50×Hit + 0.30×MRR + 0.20×clip((11−MTTC)/10,0,1)。这是 **Technical Execution** 的本地代理，不是五轴加权总分。

## 本轮试验（已拒）

硬池里若某条 **非 chrome 披露 token 的 df=1**，给独占该 token 的商品加分（`w_exclusive=0.25`）。这不是已拒的平滑 IDF `w_idf=0.15`。未打开 `w_title`、FlashRank、gate 8–10、无条件 `dense_pop_floor`、`dense_rrf_k`。

| | Hit | 技术分 | 去留 |
|---|---:|---:|---|
| 公开 + exclusive | 1.000 | 0.953414 | Hit 未塌 |
| holdout + exclusive | 0.980 | **0.888778** | 未严格大于 0.8888，**不进 PUBLIC** |

先前平滑 IDF 同样 holdout 0.888778，也未进默认。夹具 `test_exclusive_rare_token_outranks_hotter_generic_clone` 证明 shipped `rank` 在小目录上能用独占稀有词压过更热的泛克隆；50k 上热度差更大，0.25 搬不动名次。`PUBLIC.w_exclusive` 保持 0。

## 五轴

### Technical Execution 35%

本地代理即上表技术分。协议：永远 `other`、逐字 AND、gate=5、dump 第 4 槽、精确行 `w_field=0.35`、区分项整句标题 `w_phrase=0.15`。公开 Hit 满、MRR 0.952 来自热门 target + 热度/精确行排序；holdout MRR 掉到 0.805，说明执行力在未见 ID 上主要卡在 **克隆排序**，不是匹配器。剩余 miss `0005/0052/0135/0183` 已在 hard pool，pop 16/54/252/23，不能靠倒热度硬塞进 Top-10。

### Innovation & Problem Insight 20%

洞察是模拟器像老虎机：有效问题几乎只有 `other`，约束是目标自己的 features/details 原文。因此没有上 20 questions / 外挂知识库 / 工业向量库（规格 L13 排除）。相对组员 buyteSize（公开 0.9445，Mac 上 Qwen+bge），我们用 0-token 结构管道打到公开 Hit 1.0。独占稀有词打平在夹具上成立，holdout 无增益，按门禁拒绝。

### Impact & Relevance 20%

场景是 Amazon 服饰会话购物，join key 是 `parent_asin`。公开 0.95 不能当正式赛；更稳的锚是 holdout ~0.89。组内两条管道：`main` ContestAgent 计分，`legacy/qwen` 组员实验。

### Feasibility & Practicality 15%

全程内存、离线；MiniLM 可选（缺权重则该项为 0）；无 API 密钥。IDF 只扫当前硬池 token 集，不建新索引。Windows + 16GB 可在约 40s 内跑完 200 条。

### Presentation & Communication 10%

书面材料：`report/README.md`（分数入口）、`report/methods.md`（消融）、`report/holdout.md`、`report/optimize_kb.md`（不要新建 KB）。无幻灯、无演示视频。本文只服务评审轴，不代替私有 800。
