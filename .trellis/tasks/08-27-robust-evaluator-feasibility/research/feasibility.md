# Robust Evaluator 可行性分析

## 结论摘要

结论是 **Conditional Go（建议先做可证伪 pilot，不建议按初稿直接全面实现）**。

技术上，当前 `Agent.reset/respond` 合同足以承载额外多轮 case；EComAgentBench 的 feature extraction、requirement partition、rubric schema、clarification slot、generation validation 和 by-source reporting 思路也都能通过确定性适配、离线数据增强和可选外部 API 调用迁移过来。接口与 schema 不同不是实质障碍。

但初稿如果只是“随机 target → 从 target metadata 摘几条要求 → 继续按唯一 target 算原 Technical Score”，评估意义不够，甚至会制造另一种 benchmark-specific bias。必须补上两项设计：

1. **Evidence-backed fact normalization**：从当前 title/features/details 构造带原文证据、置信度和缺失语义的规范化事实 sidecar；不改官方 catalog。
2. **Contrast-set case construction**：每个 case 必须证明初始信息对应多个候选、profile/clarification 真正缩小候选集合、完整要求仍允许一个小的正确商品集合；不能只证明 target 自己满足要求。

在这两个前提下，Robust Evaluator 能提供当前 exact-target benchmark 没有的增量信号：需求满足率、信息来源利用、澄清后的状态更新，以及“推荐了非 target 但同样满足需求的商品”是否正确。

## 1. 当前仓库：真实数据和评测语义

### 1.1 Catalog 与公开样本

当前 catalog 有 50,000 条 Clothing/Shoes/Jewelry 商品，评分 ID 是 `parent_asin`。每条记录固定含：

- `parent_asin`, `title`, `features`, `description`, `price`, `categories`, `details`
- `average_rating`, `rating_number`, `store`

仓库合同见 `docs/competition_specification.md`；实际加载见 `evaluator/local_evaluator.py:112-123`。

本地全量统计：

| 字段 | 非空覆盖 |
| --- | ---: |
| title / categories / average_rating / rating_number | 100% |
| store | 99.37% |
| details | 96.66% |
| features | 89.56% |
| description | 52.23% |
| price | 21.05%（另有 117 条字符串价格） |

`details` 非空率很高，但不等于适合构造 requirement。最常见键是 `Date First Available`、`Department`、`Item model number`、`Package Dimensions`、`Manufacturer`；真正可作为 shopping constraints 的结构化字段很稀疏：

| 结构化字段 | 条数 | 覆盖 |
| --- | ---: | ---: |
| Color | 2,439 | 4.88% |
| Brand | 2,328 | 4.66% |
| Material（含 ECom alias） | 2,125 | 4.25% |
| Style | 1,752 | 3.50% |
| Size | 925 | 1.85% |
| Special Feature | 483 | 0.97% |

公开集有 200 条样本，schema 只持久化 `ground_truth.parent_asin`、`scenario_type`、粗粒度 profile 和 bucket；没有持久化 user query、intent card 或 rubric。场景分布为 80 buying、80 browsing、30 intent_override、10 boundary，与规格的 40/40/15/5 一致。

### 1.2 当前 intent 和多轮模拟并不是结构化 requirement benchmark

`intent_card()` 在运行时从 target 商品生成：先把 features/details 展平成字符串，再把命中的 material/color 放前面，截取前 2 条作 hard constraints、后 2 条作 soft preferences（`evaluator/local_evaluator.py:52-71`）。因此当前约束常是自由文本 bullet，而非可执行 rubric；公开集 200 个 target 的 800 条生成约束中，按当前 `classify_constraint()` 有 404 条落入泛化的 `feature`，302 条 material，其他属性很少。

模拟器完全由结构化 `ask_attribute` 驱动，而不解析 agent 的自然语言问题：

- buying 首轮揭示第一条 hard constraint；browsing 从模糊 query 开始；override 在第 3/4 轮注入替代要求（`local_evaluator.py:154-185`）。
- agent 每轮同时可返回 `message`、一个粗粒度 `ask_attribute` 和 Top-K recommendation。
- profile 在 `reset(session_id, user_profile)` 时直接可见，不像 EComAgentBench 需要调用 profile tool。

这意味着 Robust Simulator 可以兼容当前接口，但它能测的是“读取 reset profile + 使用 coarse clarification slot + 跨轮更新”，不是 EComAgentBench 的自由工具规划能力。

### 1.3 当前 ranking score 的严格含义

实际评分循环见 `evaluator/local_evaluator.py:216-295`：

- 只保留前 10 个 catalog-valid、唯一 `parent_asin`。
- 第一次出现唯一 target 就结束；MRR 是首次命中那一轮的 target rank 倒数。
- miss 的 MTTC 记为 11。
- `TechnicalScore = .5 HitRate + .3 MRR + .2 Efficiency`。

因此它测的是“找到那一个购买记录对应的 target”，不是“推荐是否满足用户要求”。同样满足要求的非 target 商品仍是 miss。

当前 evaluator 的 `sessions` 只保存 sample/scenario/hit/turn/rank/reciprocal rank，不保存 messages、responses 或每轮 recommendations；Robust runner 需要新增自己的 trajectory recorder，但无需改旧 evaluator。

## 2. EComAgentBench：实际可复用的是什么

### 2.1 数据规模与 case schema

参考仓库的 released benchmark 有 662 个全部通过 validation/judge 的样本。每个样本含：

- `user_query`
- `user_persona.product_requirements`
- `clarification_script.clarification_slots`
- 完整 `target_product`
- 9–16 条 `rubrics`
- generation/validation artifacts

全量 rubric 统计：

| rubric type | 条数 |
| --- | ---: |
| attribute_match | 4,921 |
| numeric_range | 731 |
| entity_match | 662 |
| review_opinion | 190 |
| negative_attribute | 108 |
| budget_match | 33 |

info source 分布为 query 3,993、persona 1,326、clarification 1,326；660/662 个 case 正好有 2 个 clarification slots。

### 2.2 生成逻辑

ECom 的重要思想可以复用，但代码不能不加适配地搬运：

1. `ProductSampler` 先按 detail count、rating、price、review、implicit-eligible 字段过滤并做品类采样（`src/generation/planning/sampler.py:17-69`）。
2. `extract_features()` 只从白名单 details 和少数 top-level numeric fields 构造可验证 feature，并过滤 Amazon artifact（`feature_extractor.py:27-226`）。
3. `partition_features()` 按 eligible field 把 feature 分到 query/persona/clarification，并强制各 source 的最小数量（`partition.py:9-143`）。
4. `build_rubrics()` 把 feature 编译成带 `field/expected_value/info_source` 的 rubric（`rubric_builder.py:11-92`）。
5. query、persona noise、自然 clarification wording 和 generation judge 使用外部 LLM；clarification 有模板 fallback（`clarification_builder.py:11-98`）。
6. 最终 sample 同时经过程序校验和 LLM generation judge，检查 source isolation、target satisfaction 与 implicit leakage（`prompts/evaluation.py:51-114`）。

默认配置要求每个商品最少提取 8 个 feature，并至少分配 4 query、2 persona、2 clarification。这个门槛是为 3.7M、四个电子/美妆/办公为主的 catalog 设计的，不适合原样套到当前服饰 catalog。

另外有两个确定性问题应在迁移时修复：

- ECom extractor 对 price 直接 `float(product["price"])`；当前 117 条字符串价格会抛异常。
- ECom pipeline 用 `random.Random(hash(sid))` 做 per-case seed（`pipeline.py:76`），Python hash 默认跨进程不稳定。Robust 版本应使用 `sha256(global_seed + case_id + product_id)`。

这些都是低成本确定性修复，不是不可行因素。

### 2.3 ECom 的 user simulator 与当前 Agent 不同，但外部接口不阻止增加内部工具链

ECom agent 是最多 100 次 tool call 的 loop，profile 通过 `get_user_profile` 获取，clarification 通过自由文本 `ask_user(question)` 触发；最终只提交一个 `recommend_product`。`AskUserTool` 通过 keyword overlap 匹配未揭示 slot（`src/tools/ask_user.py`）。

当前公开合同只有 `reset/respond`，但这只是 facade，不限制 `respond()` 内部运行一个 tool-using orchestrator。可以在保持旧 evaluator 完全兼容的前提下新增内部 `search_products`、`filter_products`、`get_product_detail`、`get_user_profile`、`ask_user` 和 `recommend_product` actions：搜索/过滤/详情工具在一次 respond 内执行；遇到 ask_user 时暂停内部 loop，把问题映射为外部 `message/ask_attribute`，下一轮收到用户回复后恢复。

`reset()` 当前把 profile 直接交给 Agent。若要测主动 profile acquisition，facade 应只把 profile 存入 session environment，不直接放进 planner context，planner 必须调用 `get_user_profile`。这需要内部架构隔离和 trajectory 审计，但不要求改变公开方法签名。评论/review 工具则受数据限制：当前 catalog 没有 review text，需要额外数据源或暂时不纳入。

当前 Agent 是最多 10 轮、每轮一个枚举 `ask_attribute` 加 Top-10 recommendation。可控适配方式是：

- rubric field 先映射到当前 10 个 allowed attributes；
- `ask_attribute` 是 slot routing 的事实来源；
- agent `message` 只记录，不用于决定 slot，以保持与现有合同一致；
- 用模板库或离线 LLM 生成多种 user response，但冻结进 benchmark。

因此 simulator 的 slot/script、tool action 和暂停/恢复思路都可复用；ECom 的具体 DB/tool runtime 需要适配当前 catalog 和 facade，但并不存在根本接口障碍。

当前 Agent 实际上已经有一组“不可选择的内置工具等价物”：每轮固定执行 intent regex parsing、session state reduction、category/FTS retrieval、feature ranking、可选 semantic reranking、clarification policy 和 Top-K 输出。它没有让 planner 决定何时搜索、过滤、查看详情或读取 profile；这些步骤全部自动执行，所以旧 benchmark 只能测最终 target recovery，不能测 tool planning。

### 2.4 ECom rubric evaluation 实际上是 LLM-as-Judge

ECom 不是纯 deterministic rubric scorer。`RubricJudge` 把推荐商品、rubric、可选 reviews/voucher 发给 LLM（`src/evaluation/judge.py:12-81`）；prompt 要求缺失/歧义时判 false，并允许 features 中的精确语义等价（`src/prompts/evaluation.py:3-24`）。

其 overall accuracy 是 `exact_product_match OR all_rubrics_satisfied`（`src/evaluation/metrics.py:53-70`）。这解决了非 target 合理答案，但引入 judge 成本、非确定性和同模型偏差。

当前 Robust v1 只有 attribute/numeric/negative 三类，可以比参考实现做得更确定：先构造规范化 fact sidecar，再执行规则评分；仅将 LLM 用于离线生成/审查，而非每次评测。

## 3. 当前数据能否支撑 Robust Cases

### 3.1 如果原样使用 ECom 的结构化字段规则

对 50,000 条当前 catalog 逐条运行 ECom 白名单/清洗逻辑（仅增加 `parent_asin → product_id` 和字符串 price 保护）：

- 46,954 条没有任何 ECom 白名单 detail field。
- 只有 73 条原始商品具备至少 8 个允许 detail fields。
- 按 ECom 概率采样后只有 88 条能提取至少 8 个 feature。
- 按 4/2/2 source partition，只有 68 条成功。
- 当前 public set 的 200 个 target 中，只有 16 个有至少一个这种安全结构化 attribute。

所以“原样复制 ECom 的 8–20 rubrics、4/2/2 分区”会得到极小且严重偏置的 benchmark，意义有限。

### 3.2 降低 rubric 数量后，确定性 pilot 是可行的

用同一清洗规则，当前 catalog 中：

- 3,043 条（6.09%）至少有 1 个安全结构化 attribute；
- 2,865 条（5.73%）至少有 2 个；
- 2,601 条（5.20%）至少有 3 个；
- 3,004 条至少有 1 个可用于 negative_attribute 的明确字段。

若要求一个 case 至少有两个 attribute，或一个 attribute 加有效 price，共有 2,912 个潜在 target（5.82%）。足以做 50–200 case pilot，但分布明显偏向 Wallets、Jewelry、Keychains、Umbrellas 等 metadata-rich 配件，不能宣称代表整个 catalog。

更关键的是 contrast-set 验证。只使用高置信 details，排除 Brand、Age Range 和 Number of Items 这类容易泄漏/弱区分字段，并按 leaf category 计算候选集合：

- 1,777 个 target 能找到“两阶段约束”：第一条 requirement 后仍有至少 3 个候选，第二条 requirement 至少再缩小一半。
- 804 个 target 能让完整约束留下 2–10 个有效商品，而非强行唯一化 target。
- 1,757 个 target 周围存在可构造 negative constraint 的对比值。

这证明低成本 deterministic pilot 可做；也证明 exact-target ranking 与 requirement correctness 必须分开，因为许多合理 case 天然有多个正确商品。

### 3.3 使用现有文本做 data augmentation 后，覆盖可显著扩大

当前 features/title/details 文本中，使用词边界词典扫描可以找到以下潜在 evidence：

- 至少一种 material：32,567 条（65.13%）；17,161 条同时出现多个 material。
- 至少一种 color：22,514 条（45.03%）；7,456 条同时出现多个 color。

这不是说所有命中都可直接作为事实。多值可能是材质混纺、可选颜色、对比描述或配件材质。建议构建 sidecar：

```text
product_id
field
normalized_values[]
evidence[{source, json_path, raw_text, extractor}]
confidence
value_semantics: single | set | range | unknown
closed_world: true | false
extractor_version
```

优先级：

1. 结构化 details 精确字段/alias；
2. 可解析的 price/rating/count/category/store；
3. 高精度 title/feature 规则抽取，保留 evidence span；
4. 可选 LLM extraction，只允许输出原文可定位的 evidence，不允许补充外部产品常识；
5. 人工抽检和 generation judge 拒绝弱证据。

外部 API key 因此是可控增强项：用于 query/persona/clarification 表述、evidence normalization 和 generation QA。benchmark 生成后要冻结 LLM 输出、prompt/model/config/hash，运行 Robust Evaluator 不应强制联网。

不建议用外部商品事实补齐 scorer、但仍让 Agent 只看到原 catalog；这会制造 Agent 无法检索的隐藏事实。增强应从 Agent 已可访问的 catalog 内容派生，或同时提供等价检索数据。

### 3.4 Negative constraint 的限制

`negative_attribute` 不能用“文本里没出现 X”直接判满足，因为 absence of evidence 不是 evidence of absence。服饰 `parent_asin` 还可能覆盖多个 color/size variants，单个 metadata 值不一定是完整枚举。

v1 应采用三值语义：

- `satisfied`：字段存在且明确与所有 excluded values 不重叠；
- `violated`：明确匹配 excluded value；
- `unknown`：字段缺失、开放集合或证据冲突。

聚合 correctness 时 unknown 不算 satisfied；报告中必须单列 unknown rate。Negative case 只从 `closed_world=true` 或经过审核的字段构造，不能把缺失当作通过。

## 4. 五个模块的可行性

### 4.1 Robust Case Generator — Conditional High

可复用：ECom 的 feature plan、source partition、rubric schema、clarification slot schema、validation gate。

需要适配/新增：

- catalog adapter：`parent_asin/product_id`、category、price normalization、detail aliases；
- normalized facts sidecar；
- field → 当前 `ask_attribute` 的映射；
- contrast/selectivity 计算与 `acceptable_product_ids`；
- source isolation、evidence coverage、unknown、leakage 检查；
- stable SHA-256 seed、生成 manifest、case JSONL hash。

建议每个 pilot case 只用 3–5 个 hard rubrics，不追求 ECom 的 9–16 条；过多要求在当前 catalog 上只会放大 metadata-rich sampling bias。

### 4.2 Robust User Simulator — High

无需改变 Agent。新 runner 调用相同 `reset/respond`，用 persisted case 驱动 10 轮，并记录：

- turn/user_message；
- 完整 agent response；
- normalized `ask_attribute`；
- scored recommendations；
- revealed slots / active requirements / override epoch；
- usage、异常和可选 latency。

局限是 profile 在 turn 0 已可见、clarification 只有 coarse enum；报告应诚实命名为 profile utilization/state tracking，不宣称复现 ECom 的 tool-use planning。

为避免只是换一套固定话术，生成时应有多种 persisted wording families，并预留 locked holdout seed。固定 public robust JSONL 最终也会被适配，不能单独承担长期泛化证明。

### 4.3 Ranking Scorer — Formula Reuse High，语义直接复用 Low

`normalize_recommendations`、Hit/MRR/MTTC/Efficiency/TechnicalScore 公式可以直接抽成共享纯函数，旧 evaluator 保持不变。

但必须输出两套 ranking：

1. `robust_exact_target_*`：与 existing 公式和唯一 target 完全一致，仅作 diagnostic/proxy 横向观察。
2. `robust_valid_set_*`：Top-K 中第一个满足全部 requirements 的商品作为 hit，MRR 取第一个 valid product 的 rank，MTTC 取首次 valid hit turn。这才是 robust recommendation correctness。

若坚持只算 exact target，必须在生成时证明 full rubric conjunction 唯一确定 target；这会显著减少 case，并容易用高基数字段把 target 编码进需求。

即使公式相同，existing 与 robust 的 raw score 也不能解释为相同难度下的严格 apples-to-apples 比较；可以比较量纲、模型排序和 score gap，但必须同时报告 cohort/cardinality/source-mix。

### 4.4 Rubric Scorer — Numeric High，Attribute Conditional High，Negative Conditional

建议运行时 deterministic：

- `attribute_match`：对 normalized set 做 exact/alias-aware containment；缺失为 unknown。自由语义等价只在离线 fact generation 阶段处理。
- `numeric_range`：解析为 `{min,max}` 或 operator，统一单位并拒绝不可解析值。rating/count 覆盖 100%，price 仅约 21%。
- `negative_constraint`：对明确字段做集合 disjoint；缺失不通过。

输出至少包含 per-rubric status/reason/evidence、macro/micro satisfaction、all-satisfied product、Top-K any-all-satisfied 和 unknown rate。按 query/profile/clarification source 分组，才能判断 information gathering 失败在哪里。

LLM judge 可作为扩展模式，但不应是 v1 默认。若启用，要保存 judge model/prompt/version/token/cost，并用 exact target 作为 judge sanity check；ECom 自己专门统计 exact target 被 judge 判失败的 `judge_misjudged_count`。

### 4.5 Reporter — High

建议三个互不覆盖的产物：

- existing evaluator 原始 JSON（原样）；
- robust ranking summary + case/session JSONL；
- robust rubric summary + per-rubric JSONL。

另存 trajectory JSONL 和 generation manifest，支持按 requirement source/type、category、acceptable-set cardinality、unknown、slot reveal、turn 和 failure reason 切片。Reporter 可复用 ECom 的 by-source/by-type 聚合思路，但其 clarification metric 目前只统计 slot 总数，并没有真正计算 slot trigger rate，迁移时需要补齐。

## 5. 推荐的低成本、可证伪 Pilot

### Scope

- 60–100 个 frozen cases，3–5 条 rubric/case。
- 先用 details + top-level numeric 的确定性 facts；同时做一个 evidence-backed text augmentation 小样本分支，用来判断覆盖能否扩展。
- 三种 rubric：attribute_match、numeric_range、negative_attribute。
- source 至少覆盖 initial query、reset profile、clarification；另保留少量 override/no-preference case。
- 不改旧 evaluator，不把 robust score 混入 competition TechnicalScore。

### Case generation gates

- 初始可见信息后有至少 5 个候选；profile 后仍至少 3 个；clarification 后剩 1–10 个 acceptable products。
- 每条 requirement 有 catalog evidence；每个 candidate 的 scored field 有明确值或显式 unknown。
- 禁止唯一 brand/store/variant code 等一条 requirement 直接锁定 target。
- category/source/rubric type 分层采样；报告 metadata-rich sampling coverage。
- LLM 只产 wording 或 evidence-grounded normalization，输出冻结并通过 source-isolation validator。

### Validation agents / baselines

至少比较：

- 当前完整 Agent；
- 无状态或只看当前 turn 的 ablation；
- 不使用 profile 的 ablation；
- 不澄清、首轮直接推荐的 ablation；
- rubric oracle / catalog filter 上界。

如果 robust evaluator 真在测 multi-turn understanding，完整 Agent 应在 profile/clarification source satisfaction 和 valid-set MTTC 上显著优于对应 ablation。只看现有 Agent 一个分数无法验证 evaluator 自身是否有效。

### 如何证明它能区分“需求跟踪”与“话术适配”

不能只拿当前 Agent 跑一次，然后因为得分下降就说 benchmark 更 robust。应同时构造受控 case pairs 和能力删除版 Agent。

同一个 shopping intent 至少生成以下成对变体：

- `all-at-once`：所有 requirements 都在首轮给出；
- `distributed`：同一组 requirements 分散到 initial/profile/clarification；
- `paraphrased`：语义不变，只改措辞；
- `reordered`：语义不变，只改 requirement 揭示顺序；
- `counterfactual`：首轮完全相同，只把后续一个要求从 A 改成 B，使正确商品集合随之改变；
- `override`：后续明确撤销旧要求并替换成新要求。

同时运行同一套 Agent 的能力删除版本：

- 每轮清空状态，只看最新消息；
- 忽略 profile；
- 从不澄清或固定按旧 evaluator 的属性顺序提问；
- 只追加 constraint、不正确处理 override；
- 完整状态跟踪版本。

判断依据不是单一总分，而是这些差值：

- **requirement retention**：新 requirement 加入后，推荐是否仍满足此前未撤销的要求；
- **counterfactual responsiveness**：只改变后续 A→B 时，推荐是否从 A 的正确集合转向 B 的正确集合；
- **paraphrase/order stability**：语义不变时，最终 rubric satisfaction 是否基本稳定；
- **profile contribution**：含 profile requirement 的 case 中，完整版本是否显著优于 ignore-profile；
- **override correctness**：撤销后不再坚持旧要求，同时保留其他未撤销要求；
- **distributed-intent gap**：同一需求 all-at-once 与 distributed 的 satisfaction 差距。真正会跟踪需求的 Agent 差距应小，模式适配 Agent 的差距会明显变大。

Pilot 可预先规定一个简单的成功门槛：完整版本相对每个针对性 ablation，在对应 slice 上至少提高 10 个百分点；paraphrase/reorder 的 satisfaction 波动不超过 5 个百分点；counterfactual pair 的正确转向率达到 80% 以上。数值最终可按 60–100 个 pilot cases 的置信区间调整，但必须在看结果前冻结。

如果 full、stateless、ignore-profile、fixed-question 几个版本在对应 slice 上几乎同分，不能说明所有 Agent 都很强，更可能说明 case 没有真正让 hidden requirement 改变正确候选集合；此时应停下来修 case generator，而不是继续扩数据。

### Go gates

- 人工抽检 requirement/evidence/source isolation 正确率至少 95%。
- deterministic replay 对冻结输入 byte-identical；外部模型输出全部缓存。
- 至少 80% case 满足预设 staged-cardinality gate。
- unknown rate 可解释，且不会因某一品类字段缺失系统性决定胜负。
- full Agent 与至少两个能力删除 ablation 出现预期方向差异。
- exact-target 与 valid-set/rubric score 能揭示不同 failure cases，而不是完全重复当前分数。

### No-Go / 停止条件

- 数据增强后仍只能覆盖 metadata-rich 的极小配件子集，且无法形成明确的 coverage label；
- LLM-normalized facts 无法达到 95% evidence audit，或同一事实跨模型/版本不稳定；
- staged contrast-set 条件无法满足，case 大多首轮唯一或完整要求仍有数百候选；
- stateless/no-profile/no-clarification ablation 与完整 Agent 得分无实质差异，说明 benchmark 没测到声称的能力；
- 最终仍决定只用唯一 target TechnicalScore 作为 robust correctness，而不接受 valid-set/rubric 结果。

## 6. 工作量与复用判断

一个有测试的 deterministic pilot，单名熟悉 Python/evaluator 的工程师约 **7–12 工程日**：

- catalog adapter + fact sidecar + audit tooling：2–4 日；
- contrast case generator + persistence/validation：2–3 日；
- simulator/trajectory：1–2 日；
- dual ranking + rubric scoring + reporter/tests：2–3 日。

加入 LLM wording/generation judge 不会改变主架构，约增加 1–3 日集成与 QA，API key/费用由运行方提供。若要把文本 facts 扩展成覆盖大多数 catalog 的 production benchmark，预计另需 1–2 周的 extractor、人工抽检、taxonomy 和版本治理。

逻辑复用评价：

| 部分 | 复用程度 |
| --- | --- |
| rubric / info_source / clarification slot schema | 高 |
| feature cleaning、partition、validation 思路 | 高 |
| query/persona/clarification LLM prompts | 中高，需服饰域和当前 profile contract 适配 |
| ECom generation Python 代码 | 中，需 DB/schema/seed/field-map adapter |
| ECom tool-loop simulator | 低，当前 Agent 交互模型不同；slot policy 可复用 |
| 当前 ranking normalization/formulas | 高 |
| ECom LLM rubric judge | 可选；不建议作为 v1 默认 |

## 最终建议

值得做 pilot，但不要把它定位成“另一个更难的 exact-target benchmark”。它最有价值的定位是：

> 在不改变官方 score proxy 的前提下，用 evidence-backed、contrast-controlled、多答案可判定的 cases，测量需求满足、信息来源利用和多轮状态更新。

如果团队只愿意实现初稿中的 target sampling + 固定脚本 + exact-target TechnicalScore，而不加入 normalized facts、contrast-set validation 和 valid-set rubric scoring，则建议 **不做**；那套实现很可能只是把现有 bias 换成新的 bias。
