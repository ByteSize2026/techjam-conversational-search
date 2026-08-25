# Research: Shopping Copilot 算法与工程难度、路线选项

- **Query**: 以 trellis-research 身份独立分析该 Shopping Copilot 赛题的算法与工程难度。基于当前仓库和官方资料，重点研究 50k 商品/200 公共会话条件下：BM25、dense、hybrid、cross-encoder/LLM rerank、状态机、槽位衰减、澄清策略、MTTC 与 MRR/HitRate 的冲突、公开集过拟合风险、CPU/GPU/外部 API 方案。提出保守/平衡/进取三档路线和可量化实验顺序。不要实现产品代码。
- **Scope**: mixed
- **Date**: 2026-08-25

## 结论摘要

**总体难度：中高。** 50,000 商品本身不是大规模检索难题；真正的难点是：在仅 200 个公开、有标签的会话上做选择而不把策略调成公开集专用，同时在 10 轮内兼顾早期命中、Top-10 排序与对话信息获取。基于可见评测器，比赛更像一个**确定性模拟器上的多回合、精确 ID 检索**任务，而不是开放式聊天任务。

**建议首版采用平衡路线：规则化状态机 + 结构化槽位 + SQLite FTS5 BM25 多查询候选召回 + 预计算 dense 候选召回 + 轻量融合 + 本地小型 cross-encoder 对 30–80 个候选重排；每轮同时给 Top-10 并提出一个信息增益最高的问题。** 该路线保留离线可运行的提交形态，并覆盖官方明确鼓励的检索、状态与澄清方向。不要将在线 LLM/API 置于主路径；其网络和凭证依赖在最终评分时存在明确合规/可用性风险。

---

## 证据等级

- **[官方/仓库证据]**：当前 participant kit 中的 README、规则、接口、评测器和 baseline 的直接事实。
- **[外部一手资料]**：SQLite 与 Sentence Transformers 的官方文档，说明实现机制的一般能力与代价，不证明本赛题必然得分更高。
- **[推断]**：从上述事实导出的设计判断；必须经本地、分层、留出式实验验证。
- **[待官方确认]**：当前公开材料没有给出答案，不能当作既定事实。

## Findings

### Files Found

| File Path | Description |
|---|---|
| `README.md:4-11, 13-21, 45-85` | 官方任务摘要：50k 冻结目录、200 公开/800 私有会话、最多 10 轮、baseline 与综合评分。 |
| `docs/competition_specification.md:4-39, 66-90` | 官方范围、字段、场景比例、会话协议、指标与模型政策。 |
| `docs/agent_api_contract.json:24-67` | Agent 输入/输出 schema；`ask_attribute` 只能为固定 10 类或 `null`。 |
| `evaluator/local_evaluator.py:154-184, 221-294` | 可见模拟器：首消息构造、顾客对 `ask_attribute` 的确定性回应、override 门控、命中与指标实现。 |
| `starter/agent.py:34-101` | 可编辑弱 baseline：SQLite FTS5、当前轮孤立 OR 词查询、BM25 字段权重、无澄清与无状态。 |
| `docs/baseline_results.json` | 弱 BM25 在公开集：Hit@10 0.125、MRR 0.068034、MTTC 9.81、综合分 0.10671。 |
| `data/README.md:2-12` | 公开集固定为 200：80 Buying、80 Browsing、30 Intent Override、10 Boundary；目录期望 50k 行。 |
| `docs/submission_rules.md:33-63, 75-102` | 允许轻量本地资产，但最终官方可能禁用网络，且可能施加 CPU、内存、超时限制。 |
| `data/public_set.jsonl` | 本地检查确认 200 条、200 个不同 target；80 easy Buying、80 medium Browsing、30 hard Intent Override、10 medium Boundary。 |

### 赛题机制与直接影响

#### 1. 这是精确 `parent_asin` 检索，不是“语义上相似即可”

- **[官方/仓库证据]** 只有 exact `parent_asin` equality 产生命中；评测只保留前 10 个有效、去重、且在 catalog 中存在的 ID（`README.md:81`; `docs/competition_specification.md:57-64`; `evaluator/local_evaluator.py:95-108`）。
- **[推断]** 召回（target 是否能进入候选）优先于自然语言推荐解释。标题、特征、详情等元数据噪声会直接转换为“同类但错误 ID”的失败；最终排序必须对具体商品而非只对品类优化。

#### 2. 每轮推荐和澄清可同时输出，澄清没有必须“空等一轮”的协议成本

- **[官方/仓库证据]** 单个 response 同时含 `message`、`ask_attribute` 与 recommendations（`docs/agent_api_contract.json:34-66`）。评测器是在同一轮先评分 recommendations，未命中才根据 `ask_attribute` 生成下一句顾客回复（`evaluator/local_evaluator.py:238-267`）。
- **[推断]** 默认策略应是“**每轮都推荐 + 每轮最多问一个有价值槽位**”，而非先连问数轮再推荐。这样可保留第 1 轮的早期命中机会，又能为下一轮补信息。

#### 3. 对话并非自由文本理解竞赛，关键控制面是结构化 `ask_attribute`

- **[官方/仓库证据]** 模拟器以 `ask_attribute` 决定揭示什么，不靠 message 文本猜测；未提出合法属性会收到“Ask me about one specific attribute”（`docs/competition_specification.md:37-39`; `evaluator/local_evaluator.py:166-184`）。可问属性限定为 category/material/color/size/style/brand/budget/feature/use_case/other（`docs/agent_api_contract.json:39-42`）。
- **[推断]** 自然语言 question 可以模板化且简短；工程主力应放在“问哪个字段”和“把回复解析入何槽位”。LLM 对提问文案的提升预期低于对 query rewrite / difficult rerank 的可验证收益。

#### 4. Intent Override 不是普通的反悔：新意图之前命中不计分

- **[官方/仓库证据]** Intent Override 占 15%，新意图在第 3 或第 4 轮送达；评测器只有 `override_applied` 后 target 位于候选才记录 hit（`docs/competition_specification.md:22-28, 35-37`; `evaluator/local_evaluator.py:234-264`）。
- **[推断]** 对该场景，第 1–2 轮“旧偏好检索得好”不会直接得分，却仍决定你是否获得更明确的下一句；收到 “Actually, ignore my earlier preference...” 后必须以**硬清除/重置**旧冲突槽位、候选缓存和 query，而不是一般的时间衰减。

#### 5. Boundary 对“无偏好”提供负面信号，而非继续臆测约束

- **[官方/仓库证据]** Boundary 占 5%；首次询问任意非空 attribute 时，模拟顾客回答没有该属性偏好（`docs/competition_specification.md:27-28`; `evaluator/local_evaluator.py:167-169`）。
- **[推断]** 把“no preference”显式写为 `unknown`/`do_not_ask_again`，降低该槽位的再次提问价值；**不能**把它解释成“任意值都满足”后继续向该属性加正向 query token。该场景样本少，公开集上不能据 10 例大幅调策略。

### 检索与重排方案的难度判断

#### A. BM25 / FTS5

- **[官方/仓库证据]** starter 在内存 SQLite FTS5 建 title/categories/features/details/store/description 索引；对**当前单条** user message 分词、去停词、取至多 40 个 unique terms，以 OR 查询；BM25 字段权重为 parent_asin 0、title 6、categories 4、features/details 2.5、store 1.5、description 1（`starter/agent.py:43-70, 85-95`）。它不保存 profile 或历史对话，也不提问（`starter/agent.py:72-100`）。
- **[外部一手资料]** SQLite FTS5 是进程内全文检索虚表；`bm25()` 可用列权重，且“数值越小越相关”，因此 starter 的 `ORDER BY bm25(products, ...)` 方向正确。SQLite 官方文档：`https://www.sqlite.org/fts5.html`。
- **[推断]** 50k 条目对 FTS5 极其可控，且无额外服务。高收益的 BM25 升级并非换搜索引擎，而是：
  1. 将已确认的槽位、当前句、低权 profile tags 组合成字段化查询；
  2. 对 title/category/brand 采取 AND/phrase/field-restricted 严格查询，对 features/description 采用 recall-oriented OR 查询；
  3. 为价格建立数值过滤/soft score，而不是把“$xx”只当 token；
  4. 合并多查询 top-N 去重并保留子查询来源与 rank；
  5. 否定或被 override 的值从 query 和候选缓存删除。
- **工程难度：低到中。** 在现有 stdlib/SQLite 基线之上可渐进实现，离线、可复现、CPU 友好。
- **主要失败模式 [推断]**：词法不匹配（同义词、描述性表达）；宽 OR 导致噪声；商品元数据中价格/尺寸表达不规范；将用户偏好标签当硬约束而误滤 target。

#### B. Dense / bi-encoder 召回

- **[官方/仓库证据]** 官方明确把 dense retrieval 列为 in-scope；同时禁止“infrastructure-heavy vector databases”，但未禁止预计算向量或本地轻量索引（`docs/competition_specification.md:9-13`）。
- **[外部一手资料]** Sentence Transformers 官方语义搜索接口允许用 query/corpus embedding 做 top-k，并默认支持 corpus 分块；文档的默认 `corpus_chunk_size` 为 500,000，说明 50k 级语料可以直接本地比较，不必部署远端向量 DB。`https://www.sbert.net/examples/sentence_transformer/applications/semantic-search/README.html`
- **[推断]** 用 float32 保存 50,000 × 384 维向量约 **73 MiB**；768 维约 **146 MiB**（仅原始矩阵，不含模型/索引）。因此可选：CPU 上 NumPy/torch 矩阵乘法精确 top-k，或提交 bundle 允许时用轻量 ANN。由于每会话最多 10 轮、只有 50k 文档，**不需要为了 ANN 再引入重型向量数据库**。
- **工程难度：中。** 难点在可复现的依赖/模型资产、首次加载时间、文本拼接模板、归一化与运行限制，而不是向量检索算法本身。
- **主要失败模式 [推断]**：预训练 embedding 对 Amazon 服饰细粒度款式/品牌/SKU 区分不足；语义相近商品互相挤占 top-10；CPU-only rerun 延迟与模型下载不确定；把完整长 description 编入向量造成截断或噪声。

#### C. Hybrid（建议）

- **[官方/仓库证据]** hybrid retrieval 是正式 in-scope（`docs/competition_specification.md:9-11`）。
- **[外部一手资料]** Sentence Transformers 将 retrieve-and-rerank 作为标准流程；bi-encoder 提供候选，cross-encoder 做精排。`https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html`
- **[推断]** 最稳妥的 hybrid 是：BM25 top 50–100 ∪ dense top 50–100，采用 reciprocal-rank fusion (RRF) 或先按候选来源归一化后线性融合，再进入 rerank。BM25 提供精确品牌/材质/颜色 token 命中，dense 弥补自然语言表述与元数据措辞差异。固定“每个通道至少保留若干候选”比直接拼原始分数更稳健，因为二者分数尺度不同。
- **工程难度：中。** 比单系统增加 embedding 生命周期和融合调参，但对 50k 规模仍很轻。
- **主要失败模式 [推断]**：在 200 条公开集上把融合权重调得过细；dense 和 BM25 高度重合而复杂度没有回报；候选 union 太小导致 cross-encoder 无法挽救召回。

#### D. Cross-encoder rerank

- **[外部一手资料]** Sentence Transformers 官方说明 cross-encoder 对 query-document pair 直接计算相关分，通常优于 bi-encoder，但因为要为每对分别计算而更慢，通常只用于重排 bi-encoder top-k。`https://github.com/huggingface/sentence-transformers/blob/main/docs/cross_encoder/usage/usage.rst`
- **[推断]** 对本赛题应把它限定在 hybrid union 的 **30–80** 个候选，并对每个商品使用压缩 document（title + categories + store + 前若干 feature/detail）而非完整 descriptions。若 CPU 时延可接受，小型 cross-encoder 能提升同类商品之间的精确排序，直接作用于 MRR；但它无法找回未进入候选池的 target。
- **工程难度：中到高。** 需要本地模型兼容性、batching、冷启动测量、内存和超时实测；不建议在未证明 candidate recall 足够前投入。
- **主要失败模式 [推断]**：CPU 每轮批量重排超时；通用 MS MARCO 类模型与商品匹配域偏移；仅凭 200 标签微调/挑模型会过拟合；token 截断丢失商品关键属性。

#### E. LLM rerank / parser

- **[官方/仓库证据]** 官方允许合法 API 或本地模型，token 和 latency 是 feasibility 而非核心 technical score；但官方最终环境**可能禁网**，且不允许依赖未声明的外部服务（`README.md:83-85`; `docs/competition_specification.md:88-90`; `docs/submission_rules.md:51-63, 99-102`）。
- **[推断]** 在线 LLM 可用于开发时辅助检查：从用户句提取槽位、生成 query rewrite、离线分析错误；不宜作为唯一正式检索/重排路径。对每轮 50–100 候选调用 LLM 做列表排序会带来延迟、成本、格式失败和不可复现性，还存在 final 无网络即整体失效的风险。若使用，应严格限制为“top 10–20 的可选并行 rerank/解析”，设置 deterministic local fallback，并在报告中披露。
- **工程难度：高（作为 production path）。** 外部 API 不减少比赛工程风险，只将其转为凭证、网络、速率、成本和部署风险。

### 对话状态机、槽位衰减与澄清策略

#### 建议状态模型

**[推断]** 以每 session 独立 state 保存以下结构，而不是将历史文本直接串接：

```text
SessionState
  turn
  route: buying | browsing | override-suspected | unknown
  slots[attr]: {values, polarity, confidence, source_turn, status}
    status ∈ {hard, soft, unknown/no_preference, superseded}
  asked_attributes: set
  current_query_text
  candidate_cache / retrieval provenance
  override_seen: bool
```

- `hard`：首轮明确的 Buying requirement、或回复中“what matters is”提供的约束；对检索用强 filter/高权重。
- `soft`：profile tag、含糊描述、探索性倾向；用于 rerank 或弱 query expansion，不能过早过滤。
- `unknown/no_preference`：Boundary 的回答；抑制重复提问，不生成正向属性约束。
- `superseded`：检测到 “actually / ignore earlier / instead” 或至少官方模拟器一致的 override 模式时，把冲突旧值置为不可用并失效旧 cache。

#### 衰减规则

- **[官方/仓库证据]** Override 文字固定包含 “Actually, ignore my earlier preference. What I need is: ...”（`evaluator/local_evaluator.py:76-85, 258-264`）。
- **[推断]** 不应对所有槽位做统一指数衰减：它会无缘无故削弱仍有效的 material/category。采用**事件驱动的衰减/清除**更合理：
  - 新句明确冲突：相关 attribute 的旧值从 `hard/soft` 立即转 `superseded`；
  - 每轮未被再次证实时，profile-derived soft prior 的 rerank 权重可按 0.7–0.9 乘衰减；
  - 用户显式说无偏好：attribute 转 `unknown` 至少 3 轮或会话结束；
  - category 通常保持，除非新句也明确换品类。
- **注意 [推断]**：公开模拟器把 override 的 `new_value` 放进 `disclosed`；真实 final 是否保留完全相同措辞不可假设。因此状态更新要基于语义/规则检测，也要以新文本内容抽取为准。

#### 澄清选择：信息增益而非“轮流问字段”

- **[官方/仓库证据]** 顾客只会就所问的一个结构化属性揭示至多两条尚未披露的、属性匹配的 intent card constraints；不匹配则明确没有额外偏好（`evaluator/local_evaluator.py:166-184`）。
- **[推断]** 评估每个可问 attribute 的 utility：

```text
utility(a) = P(answer useful | current state, a)
             × expected candidate-set reduction(a)
             × confidence_that_attribute_is_retrievable(a)
             − repeat/no-preference penalty(a)
```

无需一开始拟合概率模型；可用可解释启发式：
1. 已明确 category 但 material/size/color/feature 尚缺，优先问当前候选中最能分裂的属性；
2. 若 profile 明显偏好 fit/comfort 且候选有 size/fit metadata，优先 `size` 或 `style`，但仅作为问法线索；
3. 若首句含价格数字，优先抽取为 budget，不再询问；
4. `other` 可用于捕捉难归类 feature，但应低优先级，因为不确定模拟器是否有可匹配 constraint；
5. 某槽位返回 no preference 或已问过时，显著降权；
6. 最后 1–2 轮停止为信息很弱的属性提问，专注以完整 state 重排。

### MTTC、MRR、HitRate 的目标关系

#### 精确公式与量级

- **[官方/仓库证据]** `TechnicalScore = .50 HitRate@10 + .30 MRR + .20 Efficiency`，而 `Efficiency = (11 - MTTC)/10`（截断范围内）（`docs/competition_specification.md:66-76`; `evaluator/local_evaluator.py:277-280`）。
- **[推断]** 在本题 MTTC 总在 1–11 的有效范围内时，可写为：

```text
TechnicalScore = 0.50 × HitRate + 0.30 × MRR + 0.02 × (11 − MTTC)
```

对 200 会话的单一成功样本：
- 同一 target 从 rank 10 升到 rank 1，分数增加 `0.30 × (1 − .1) / 200 = 0.00135`；
- 同一 target 早 1 轮命中，分数增加 `.02 / 200 = 0.00010`；
- 将一个 miss 转为第 10 轮 rank-10 hit，增加 `(.50 + .30×.1 + .02×1)/200 = 0.00275`；
- 将一个 miss 转为第 1 轮 rank-1 hit，增加 `(.50 + .30 + .02×10)/200 = 0.00500`。

#### 设计含义

- **[推断]** 首先追求 **Hit@10/候选召回**，因为由 miss 变 hit 的边际价值最大；随后优化 rank，MRR 的影响大于“同一已命中会话提前一轮”的纯 MTTC 改进。
- **[推断]** 但“先问几轮再推荐”同时损害 hit 的早期机会和 MTTC；由于协议允许同回合问+推，通常不存在这种取舍的必要。
- **[推断]** Intent Override 在新信息前命中被评测器禁止，因此这些会话的 MTTC 下界由 override turn 决定。不要把它们错误地与 Buying 的第 1 轮潜在命中混在一起评价澄清策略；必须按 scenario 分层报告。
- **[推断]** 在 public 样本上比较两个系统时，除了 aggregate score，必须记录：candidate recall@N、Hit@10、MRR、MTTC、first-hit turn 分布、按四场景的以上指标。否则“问得更多带来最终 hit”与“早期推荐变差”会被总分掩盖。

### 公开集过拟合风险

- **[官方/仓库证据]** 公开集只有 200 条，私有集有额外 800 条；两者固定相同 scenario mix，且 private intent/target/simulator state 不发给 Agent（`README.md:4-11`; `docs/competition_specification.md:16-20, 22-28`）。本地 public target 均不同；检查显示 `difficulty_bucket` 与场景完全共线：Buying=easy 80、Browsing=medium 80、Override=hard 30、Boundary=medium 10。
- **[推断]** 200 条总体指标的单样本波动很大：例如 HitRate 单例变动为 0.005；而 Boundary 只有 10 条，单例变动 0.10。依公共 aggregate 挑许多融合系数、模型、问题顺序，容易把随机会话/元数据特例当规律。
- **[推断]** 最严重的泄漏不是训练模型，而是把 `public_set.jsonl` 的 target ID、sample ID 或 profile-summary 与 ID 的对应关系写入提交逻辑。私有集目标不同用户与商品，且 official rules 禁止 private-label reconstruction；这样的 lookup 不能泛化，也破坏比赛精神。
- **[推断]** `difficulty_bucket`、`category_bucket`、`ground_truth` 均是 public data 的标签性字段，运行中的 `reset` contract 并不提供它们。任何只靠 evaluator 内部 sample 字段做 route 的实现都不符合正式接口泛化条件。

#### 防过拟合实验纪律（建议）

1. **固定一个最终 public holdout**：首次按 scenario 分层随机划分 60% dev / 20% validation / 20% locked test（并记录 seed）；所有候选架构仅在 dev/validation 选择，locked test 只在路线冻结时跑少数次数。
2. **优先 leave-one-out 或 5-fold stratified CV 汇总**：尤其对 30 个 Override 与 10 个 Boundary。报告均值、折间范围，而不是单点最好分。
3. **冻结试验矩阵**：每次只更改一个模块（state、BM25 query、dense、fusion、rerank、question policy），保存逐 session JSON；不要凭肉眼个例连续微调。
4. **不使用可见 `ground_truth` 做运行时特征，不以 sample 顺序调参**；公共 labels 只用作离线评价。
5. **用机制不变量检查泛化**：例如 override 必须清旧 state、Boundary 不重复问、每轮候选都为 10 个有效唯一 catalog ID；这比 public score 更可靠。

### 50k / 200 约束下的部署选择

| 方案 | 最终运行依赖 | 适合模块 | 优点 | 主要风险/不确定性 | 建议定位 |
|---|---|---|---|---|---|
| 纯 CPU / stdlib + SQLite FTS5 | Python stdlib、SQLite | BM25、多查询、规则状态机 | 最可复现；当前 starter 已验证可运行；无网络/GPU | 语义召回与细粒度排序有限 | 必须具备的离线基线/回退 |
| CPU + 本地 embedding/cross-encoder | PyTorch/ONNX 等、预下载模型与向量资产 | dense、hybrid、top-N rerank | 离线且质量上限更高 | 模型资产大小、冷启动、CPU latency、官方环境兼容性 | 推荐主路线，须先基准测试 |
| GPU + 本地 embedding/cross-encoder | GPU 与对应 runtime | 开发、批量预计算、快速实验 | 快速计算 embedding/rerank | 官方并未承诺 GPU；不可把 GPU 当假设 | 开发加速，不是最终唯一依赖 |
| 外部 embedding/rerank/LLM API | 网络、密钥、供应商 SLA | 研究原型、可选增强 | 开发速度快，可提升 parser/rewrite | final 可能禁网；成本/速率/延迟；凭证和可复现性 | 仅可选增强，必须有本地 fallback |

- **[官方/仓库证据]** 最终可能在 CPU、内存、timeout、网络限制下运行；若无法无 live credentials 运行必须明确说明（`docs/submission_rules.md:51-63, 75-102`）。
- **[推断]** 预计算商品 embedding 是最合理的计算转移：catalog 是 frozen，只有 query/state 每轮变化。提交前把模型和资产下载固定版本并测试 cold start；若轻量依赖不允许，则退回 BM25 + structured rerank，不应临场依赖 API。

## 三档路线

### 路线 1：保守 — Offline lexical/state baseline+

**架构**

```text
reset -> SessionState(profile soft priors)
turn -> rule slot parser -> override/no-preference state handling
     -> multi-query SQLite FTS5 (strict + recall query)
     -> deterministic metadata scoring (price/category/brand/slot matches)
     -> Top-10 + template question
```

- **依赖**：Python stdlib + SQLite；可从当前 starter 演进。
- **核心工作**：累积对话 query；字段化 BM25；价格/颜色/材质/品牌等 deterministic matcher；状态机；合法 ID、去重、cache invalidation；分层 evaluator 输出。
- **预期收益 [推断]**：相对于 stateless single-message BM25，预计最先改善 Buying 与 Browsing 的 Hit/MTTC，并让 Override 不再受过期 query 污染。收益数值不得预承诺，必须由 CV 验证。
- **工程成本/风险**：低；最大风险是词法检索 ceiling 和过度 hard-filter。
- **适用条件**：时间非常紧、无人能稳定打包本地 ML 模型、最终环境未知或偏保守。
- **退出标准**：若 locked/CV 中 candidate recall@100 或 Hit@10 因语义措辞明显受限，再进入路线 2；不要先做 LLM。

### 路线 2：平衡 — Offline hybrid + small local reranker（推荐）

**架构**

```text
frozen catalog
  -> FTS5 index + canonical product text
  -> offline-precomputed normalized embeddings

per turn:
  state machine / slot parser
  -> query bundle (current + accumulated hard + weak profile prior)
  -> BM25 top-100 + dense top-100
  -> union + RRF / calibrated fusion
  -> hard-constraint penalty/filter + cross-encoder rerank top 30-80
  -> deterministic tie-break (exact title/brand/category/price)
  -> Top-10 and next-best-attribute question
```

- **依赖**：保守路线 + 固定版本的 embedding 模型、轻量 local inference runtime；cross-encoder 可 feature flag，CPU 基准不通过就停用。
- **核心工作**：canonical product document；离线 embedding build；cosine top-k；union/fusion；candidate recall@N instrument；batch rerank；产物打包和 CPU cold/warm benchmark。
- **预期收益 [推断]**：dense 可覆盖 BM25 未命中的意图表达，cross-encoder 对相似商品间 rank 更有帮助；hybrid 的收益应主要体现在 candidate recall 与 MRR，而非仅对话文案。
- **工程成本/风险**：中等；模型/依赖约束和 CPU latency 是最早必须验证的硬风险。
- **适用条件**：至少一位成员能负责 local ML 推理与依赖打包，且有时间做严格 CV。
- **明确不做**：不训练/fine-tune retriever/reranker（官方也将 full-model training 列为 out of scope）；不使用重型远端 vector DB。

### 路线 3：进取 — Hybrid + learned/calibrated policy + optional LLM enhancement

**架构**

在路线 2 之上加入：
- 从 public dev folds 学习/校准 BM25-dense-rerank-structured feature 融合器；
- 基于候选熵/属性分布的 question value estimation；
- 可选 LLM 只处理难例的 slot normalization、query rewrite 或 top-20 listwise rerank；
- 完全离线路线 2 fallback，并记录每次 fallback。

- **预期收益 [推断]**：若有效，主要改善 vague Browsing 的问法选择和同类商品排序；但 200 labels 很少，学习策略高度不稳。
- **工程成本/风险**：高。可能因 fold 泄漏、策略过拟合、API 不可用、模型结果不确定/格式错误、时延而损害 final 可复现性。
- **适用条件**：路线 2 已在 CV 中稳定超过路线 1，团队有 GPU/ML 经验，并能够把外部 API 完全从正式必需路径移除。
- **Go/no-go 门槛 [推断]**：只有当 5-fold 平均 technical score、Hit@10、MRR 都提高，且最差 fold 未明显退化，并且 CPU/offline fallback 仍完整可交付，才保留该附加层。

## 可量化实验顺序（按价值与依赖排序）

### 0. 先建立实验卫生与运行预算

- 交付：固定 split/CV seed、每 session trace（turn、输入、state、asked attr、候选来源、rank、耗时）、aggregate + scenario metrics。
- 指标：每次记录 Hit@10、MRR、MTTC、technical score、candidate recall@10/50/100、首命中轮次、p50/p95 respond latency、初始化时间、RAM、模型/资产大小。
- 验收：复跑同一配置得到完全相同结果；每轮输出 10 个有效唯一 ID（可能不足 10 时明确记录原因）。

### 1. 复现 baseline，建立不可退化锚点

- 操作：用真实 `catalog.jsonl` 下载后跑 starter；在同一 catalog hash/环境记录 index build time 和 metrics。
- 已知锚点：公开结果应接近 Hit 0.125、MRR 0.068034、MTTC 9.81、score 0.10671（`docs/baseline_results.json`）。
- 验收：若偏离，先检查 catalog 版本、路径、SQLite FTS5、数据完整性，暂不比较新算法。

### 2. 状态机 + “每轮推荐且问一项”

- 对照：stateless current-message BM25 vs accumulated slots/state query。
- 消融：关闭 profile priors；关闭 override reset；关闭 no-preference suppression；固定问题顺序 vs utility heuristic。
- 预期验证：Buying MTTC 下降；Override 在新意图后检索质量恢复；Boundary 不出现重复问同 attribute。
- 决策：若 state 本身不提高稳定 CV 指标，检查 parser/slot 质量，勿急于加模型。

### 3. 强化 lexical retrieval 与 metadata feature rerank

- 对照：baseline OR query vs query bundle / field constraints / RRF of lexical subqueries。
- 指标重点：candidate recall@50/100 和 Top-10 Hit；确认 hard filter 不会大幅损害 recall。
- 决策：选少量（例如 3–6 个）可解释参数，避免针对单个 public session 调权。

### 4. Dense-only 和 hybrid candidate recall 试验

- 对照：BM25、dense、BM25∪dense、RRF；先不启用 cross-encoder。
- 指标重点：每 scenario candidate recall@50/100；判断 dense 是否给 BM25 引入互补候选，而不仅是改写相同排名。
- Go/no-go：若 union 的 recall 无稳定增益，移除 dense 以保留保守交付；若稳定增益，保留 hybrid。

### 5. Cross-encoder 的候选数与硬件基准

- 网格：rerank K ∈ {20, 40, 80}；短 product text vs 较长 text；CPU batch size。
- 指标重点：MRR/Hit 提升相对第 4 步、p95 单 turn latency、cold/warm load、内存。
- Go/no-go：只有在有稳定排序收益且可在保守的 CPU/timeout 假设下运行，才保留。否则使用 structured metadata rerank。

### 6. 澄清策略实验（最后再做）

- 对照：固定 `material -> size -> color ...`、最高候选分裂度、profile-aware utility。
- 分层：Browsing 与 Boundary 为主；Override 单独分析，因为命中门控不同。
- 约束：question policy 不得读取 scenario/difficulty/ground_truth；只使用 Agent contract 输入与自己的 state。

### 7. 进取增强及最终冻结

- 可选：LLM parser/rewrite/rerank 仅对路线 2 的 hard cases 做离线 A/B；在 API 失败、禁网时自动走路线 2。
- 验收：locked test 仅在配置冻结后运行；提交前在无网络、干净环境、官方命令下从零测试。

## 难度分解

| 维度 | 难度 | 证据/理由 |
|---|---|---|
| Catalog scale / 索引 | 低 | 仅 50k 冻结文本商品；SQLite baseline 已用内存 FTS5 成功建索引（`starter/agent.py:43-70`）。 |
| Exact-product retrieval | 中高 | 只认 exact parent_asin；服饰相似品多，Top-10 而非品类相关性决定成功。 |
| Dense/hybrid engineering | 中 | 规模允许预计算和本地 brute-force，但依赖/资产/CPU benchmark 是交付问题。 |
| Cross-encoder | 中高 | 有标准两阶段收益可能，但逐对成本、domain shift 和 CPU timeout 需要实测。 |
| Dialogue/state | 中高 | 四类场景及 Override 门控要求 robust state reset；结构化 ask 是主要交互控制面。 |
| Metric optimization | 中 | 公式透明但 Hit、rank、early turn 的边际价值不同，必须分层诊断。 |
| Statistical validity | 高 | 200 public 对 800 private，且 Boundary=10；极易按公开数据选噪声策略。 |
| Submission/reproducibility | 中高 | 网络/GPU都未承诺，必须交付可说明、可复现、可运行的依赖与 fallback。 |

## Related Specs

- 无 `.trellis/spec/` 目录或比赛实现层 spec 可供引用；本调研以仓库随附的 participant-kit 文档与可见 evaluator 为准。

## Caveats / Not Found

1. **未找到 catalog 文件本体**：当前 `data/` 只有 `public_set.jsonl` 与 README；`catalog.jsonl` 需按 `README.md:23-32` 从 release 下载并校验。因此尚未能实测索引构建时长、BM25 latency、真实 catalog 文本长度、向量内存与 reranker CPU 耗时。
2. **未能从公开网络检索到独立的 TechJam 官方 GitHub/release 页面**：当前“官方资料”依据仓库随附、明确标为 competition specification/contract/rules 的 participant kit 文件；外部资料只用于一般技术能力判断。
3. **公开资料未承诺正式硬件、内存、超时、是否有 GPU 或网络**；规则仅说明组织方可能施加限制。因此任何 GPU/API-only 路线都是待确认假设。
4. **公开 evaluator 是本地可见模拟器，不保证 private harness 的自然语言表述完全一致**。`docs/competition_specification.md:39` 说明组织方可加入自然语言 paraphrasing，但不影响 correctness；不要只对当前固定回复模板写字符串匹配。
5. **官方 webinar 待确认问题**：
   - 最终评分的 Python 版本、CPU/内存/timeout、是否有 GPU、是否默认禁网？
   - 是否允许/建议在 submission 中携带预计算 embedding 与开源模型权重，资产大小上限是多少？
   - private 数据是否采用同一可见 evaluator 逻辑与同样的字段/回复模板，paraphrasing 的范围是什么？
   - 是否允许第三方本地 inference runtime（ONNX Runtime、FAISS、PyTorch），以及依赖安装是否联网？
   - final 是否只评 TechnicalScore，还是 latency/token disclosure 有额外门槛或人工审查？
   - “full-model training” out-of-scope 是否排除仅在公开集上学习融合权重/轻量校准？保守做法是假设不可依赖此路径。
