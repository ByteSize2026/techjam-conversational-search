# Agent 全部 Prompt 审查清单（供人工检查）

生成时间：2026-08-30。范围：`starter/shopping_agent/` 下真实参与线上对话的 LLM 调用（不含
`scripts/probe_llm_node_quality.py` 等一次性诊断脚本里的探针 prompt）。全代码库搜索确认：
只有两处构造 `"role": "system"` 消息——`llm_nodes.call_llm_value_node`（7 个节点复用同一个
壳）和 `semantic_ranking.py` 的 `LLMSemanticRanker.rank`（1 处独立内联 prompt）。共 8 个 prompt。

---

## ✅ 定稿说明（2026-08-30）

`EXTRACT_CONSTRAINTS_PROMPT` 已重写并定稿，用户已知悉并同意收尾，不再继续迭代措辞。变更摘要：

1. 原来两处测试集原文（`public_0023` 的"Department: Womens"、`public_0019` 的"Outdoor & Work
   Rain"）已删除，换成通用合成例子（"winter running shoes"、"像是从商品页抄来的文字"这类抽象
   描述），不再引用任何评测集字面文本。
2. 整段 prompt 按 `ExtractConstraintsOutput` 的 5 个输出字段重新组织——每个字段一段、只讲一次
   规则，不再是"哪里报错往哪贴一块"的补丁堆叠。`query_terms` 字段第一次有了明确的填写说明（之前
   完全没提过它该装什么）。
3. 重写过程中一度把"遇到怪异措辞也别沉默"这条要求写成了"逐字整句抄下来"，用标准 10 条无偏小样本
   测试（`--limit 10`，不挑样本）验证时抓到 `public_0002` 从改动前的命中变成了改动后的 miss——是
   这次改动实打实引入的回归，已收窄措辞为"保留原词、但只取真正表达值的短语，不要把整句话焊死进
   一个字段"。
4. 收窄措辞后 `public_0002` 依然 miss，但追查确认根因已经不在 prompt 层——是排序层对"约束值被
   替换"缺乏鲁棒性的问题（跟 2b 修过的打分量级问题同属一个家族，触发条件不同），详见
   `ARCHITECTURE_IMPROVEMENT_PLAN.md` 里"`EXTRACT_CONSTRAINTS_PROMPT` 重写"一节。这个问题**尚未
   立项、未修复**，按用户指示先定稿现状，不在此基础上继续打补丁。
5. `--limit 10` 标准无偏小样本最终 9/10 命中（`public_0003` 是早就记录在案的难例，跟本次改动无
   关）；全量单元测试 79/79 通过。

---

## 1. `EXTRACT_CONSTRAINTS_PROMPT`

**来源**：`starter/shopping_agent/llm_nodes.py` 第 287-336 行，常量 `EXTRACT_CONSTRAINTS_PROMPT`。
调用方：`graph.py` 第 802 行 `_extract_constraints_node`（`ExtractConstraints` 节点），经
`call_llm_value_node`（`llm_nodes.py:165`）发起调用。

**作用**：每轮对话里，把顾客这句话解析成结构化的"意图更新"——有没有换/加/删某个属性的偏好
（材质、颜色、尺码、风格、品牌、预算、功能、用途、类目锚点），有没有整体推翻之前的搜索
（`global_override`），有没有明确表示"这个属性无所谓"（`no_preference`），以及要不要往搜索词里
追加自由文本（`query_terms`）。是整个对话状态机里最核心的一个理解节点——后面所有排序、追问、召回
都依赖它的输出是否准确。

**预期输入格式**（`_extract_constraints_input`，`graph.py:780`，JSON 对象）：
```json
{
  "message": "顾客这一轮说的话（原文字符串）",
  "recent_turns": ["最近至多 3 轮顾客说过的话"],
  "known_constraints": [
    {"attribute": "material", "value": "leather", "hardness": "hard"}
    // 至多 10 条，当前已生效的约束
  ],
  "answering_attribute": "size"  // 仅当上一轮机器人正在等待某个属性的回答时才会出现
}
```

**预期输出格式**（pydantic 模型 `ExtractConstraintsOutput`，`llm_nodes.py:80-93`，模型必须返回
校验通过的 JSON）：
```json
{
  "global_override": false,
  "mutations": [
    {
      "action": "upsert",       // 或 "remove"
      "attribute": "material",  // 只能是下面 9 个之一
      "value": "leather",
      "polarity": "prefer",     // prefer / avoid / require
      "hardness": "soft",       // soft / hard
      "confidence": 0.85
    }
  ],
  "category_anchor": "winter running shoes",  // 或 null
  "no_preference": ["color"],
  "query_terms": ["waterproof"],
  "confidence": 0.8
}
```
允许的 `attribute` 枚举：`category, material, color, size, style, brand, budget, feature,
use_case, other`。

**实际发给模型的完整 system prompt**（`task_prompt` + `_schema_instructions`，见
`llm_nodes.py:184` / `156-162`）= 下面这段英文原文，**外加**一段自动生成的、把
`ExtractConstraintsOutput` 的 JSON Schema 原样贴上去的文字（"Respond with a single JSON object
only ... that validates against this JSON Schema: {...}"），这部分是代码自动生成的，不是手写的
自然语言指令，此处不重复贴出。

**英文原文（定稿版本）**：
> You extract shopping constraints from one customer message in a multi-turn conversation,
> into the given output fields. Only report what the message actually supports; never invent
> a value that isn't there. Fill each field using its own rule below -- do not skip a field
> just because the wording is unusual; an unusual-looking value copied verbatim is not the
> same as an invented one.
>
> - category_anchor: the product category or type the customer names, copied verbatim. It may
> be several words (e.g. 'winter running shoes', not shortened to 'shoes'); do not move it
> into query_terms instead. Null if no category or product type is named at all.
>
> - mutations: one entry per attribute value the customer states. Each entry's `attribute`
> must be one of: category, material, color, size, style, brand, budget, feature, use_case,
> other. When `answering_attribute` is given, the message is a direct reply to a question
> about that attribute:
>   * If the reply declines (e.g. 'no preference', 'any is fine'), do not report a mutation
>   for it -- see no_preference below instead.
>   * Otherwise report a mutation for `answering_attribute`. Use the customer's own words for
>   the value rather than paraphrasing them into different wording, even if the value itself
>   looks unusual (e.g. text that reads like it was copied from a product listing) -- an
>   unusual value is still a real value, not something to discard. This means the shortest
>   phrase that actually names the value, not the whole sentence: drop lead-in words like
>   'what matters is', and if the reply lists more than one related detail (e.g. separated by
>   a semicolon), take the core value as the mutation and put any leftover detail in
>   query_terms instead of concatenating everything into one value. Reporting nothing is wrong
>   whenever the customer stated something.
>   * Exception: if the reply plainly names a different, unrelated kind of requirement instead
>   of answering what was asked (e.g. an override marker like 'actually, what I need is: X'
>   introducing a requirement the question never asked about, such as a care instruction when
>   a size was asked), classify it by the reply's own content instead of `answering_attribute`.
>
> - global_override: true only if the customer is abandoning the product category itself for
> something unrelated (e.g. switching from jackets to running shoes). Swapping out one or more
> attribute values within the same search stays false, even with phrases like 'scratch that'
> or 'never mind' attached (e.g. 'scratch that, let's go with leather' while still shopping
> for the same jacket) -- report that as an `upsert`/`remove` mutation on just that attribute
> instead.
>
> - no_preference: attributes the customer explicitly declined to state a preference for.
>
> - query_terms: any other free-text descriptive words (features, use cases) the customer
> used that are not already captured by a mutation above -- short keywords, not full
> sentences.

**中文翻译**：
> 你要从多轮对话中顾客的这一句话里，提取购物约束，填进给定的输出字段。只报告这句话真正能支持的
> 内容；绝不编造一个不存在的值。按下面每个字段各自的规则填写——不要因为措辞不寻常就跳过某个字段；
> 一个逐字抄下来的、看着不寻常的值，跟编造出来的值不是一回事。
>
> - **category_anchor**：顾客说出的商品类目或品类词，逐字抄录。可以是好几个词（比如"winter
> running shoes"，不要缩短成"shoes"）；不要把它挪去 query_terms。完全没提到任何类目或品类时才
> 留空。
> - **mutations**：顾客陈述的每一个属性值各占一条。每条的 `attribute` 只能是这 9 个之一：
> category、material、color、size、style、brand、budget、feature、use_case、other。当
> `answering_attribute` 给出时，这句话是在直接回答关于那个属性的提问：
>   - 如果是拒答（比如"无所谓"、"随便都行"），不要为它报一条 mutation——见下面的 no_preference。
>   - 否则要为 `answering_attribute` 报一条 mutation。用顾客自己的原词作为值，不要改写成别的
>   说法，哪怕这个值本身看起来不寻常（比如读起来像是从商品页面抄来的文字）——一个不寻常的值仍然
>   是一个真实的值，不是该被丢弃的东西。这意味着要取真正表达这个值的最短短语，而不是整句话：去掉
>   "what matters is"这类引导词；如果这句回答列出了不止一个相关细节（比如用分号隔开），把核心值
>   作为 mutation，剩下的细节放进 query_terms，而不是把所有内容焊死进一个 value 里。只要顾客确实
>   说了什么，"什么都不报告"就是错的。
>   - 例外：如果这句回答明显是在讲一个完全不同、不相关类型的需求，而不是在回答刚才问的问题（比如
>   带着"actually, what I need is: X"这种改口标记、引出的是问题压根没问过的需求，例如问的是尺码、
>   答的却是护理说明），这种情况要按这句话本身的内容分类，而不是按 `answering_attribute`。
> - **global_override**：只有当顾客彻底放弃当前商品品类、转向完全不相关的东西时才为 true（比如
> 从夹克换成跑鞋）。在同一次搜索里换掉某一个或几个属性的值，依然保持 false，哪怕带着"scratch
> that"/"never mind"这种词（比如"scratch that, let's go with leather"但仍在买同一件夹克）——把
> 这次替换报告成针对那一个属性的 `upsert`/`remove` mutation。
> - **no_preference**：顾客明确表示不需要偏好的那些属性。
> - **query_terms**：顾客用到的、还没被上面某条 mutation 覆盖的其他自由描述词（特性、用途）——
> 是简短关键词，不是完整句子。

---

## 2. `CLASSIFY_INTENT_PROMPT`

**来源**：`llm_nodes.py` 第 321-330 行。调用方：`graph.py:751` `_classify_intent_node`
（`ClassifyIntent` 节点）。

**作用**：在已经给顾客展示过推荐商品之后，判断顾客这一轮话是四种意图里的哪一种——要看某几件
已展示商品的细节、在同一个搜索里继续微调、开始一个全新的不相关搜索、还是已经选定要买了。这个
判断结果只是给路由用的临时数据（不写入持久状态），决定下一步该走"对比详情"还是"继续问/搜"还是
"结束"这几条分支中的哪一条。

**预期输入格式**（`graph.py:759-763`）：
```json
{
  "message": "顾客这一轮说的话",
  "last_candidate_ids": ["B0XXXXXXXX", "..."]  // 上一轮展示过的商品 ID，至多 20 个
}
```

**预期输出格式**（`ClassifyIntentOutput`，`llm_nodes.py:96-102`）：
```json
{
  "intent": "refine_search",  // 只能是 compare_details / refine_search / new_search / confirm_choice 之一
  "target_ids": ["B0XXXXXXXX"]  // 仅 compare_details 时有意义，且必须是 last_candidate_ids 的子集（由调用方二次过滤，不信任模型自称的 ID）
}
```

**英文原文**：
> You classify a customer's follow-up message after recommendations were already shown. Pick
> exactly one intent: compare_details (the customer wants more detail on specific
> already-shown items -- list their parent_asin values in target_ids, using only IDs present
> in last_candidate_ids), refine_search (the customer is narrowing or adjusting the same
> search), new_search (the customer is starting an unrelated search), or confirm_choice (the
> customer is done and has picked/accepted an item).

**中文翻译**：
> 在已经展示过推荐商品之后，对顾客这句后续消息做意图分类。只能选一个意图：compare_details
> （顾客想了解某几件已展示商品的更多细节——把它们的 parent_asin 列进 target_ids，只能用
> last_candidate_ids 里出现过的 ID）、refine_search（顾客在同一个搜索范围内做细化或调整）、
> new_search（顾客在开始一个不相关的全新搜索）、或 confirm_choice（顾客已经确定、选好了某件
> 商品）。

---

## 3. `ASK_ATTRIBUTE_FILL_MISSING_PROMPT`

**来源**：`llm_nodes.py` 第 332-336 行。调用方：`graph.py:975` `_ask_attribute_node`（当
`mode == "fill_missing"` 时），也就是"顾客还没提过这个属性，问一下"的场景。要问哪个属性、用
哪种模式，都是调用它的 Router 已经决定好的，这个节点唯一的自由度是怎么把问题写成一句自然的话。

**预期输入格式**（`graph.py:986-995`）：
```json
{
  "attribute": "material",
  "mode": "fill_missing",
  "known_constraints": [
    {"attribute": "color", "value": "black"}  // 至多 6 条，已知约束，仅供措辞时参考语气/上下文
  ]
}
```

**预期输出格式**（`AskAttributeOutput`，`llm_nodes.py:105-113`）：
```json
{ "question_text": "Do you have a material preference?" }
```

**英文原文**：
> Write one short, friendly clarification question asking the customer for their preference
> on the given attribute. Do not mention any other attribute or invent product facts.

**中文翻译**：
> 写一句简短、友好的澄清提问，问顾客在给定这个属性上有什么偏好。不要提到其他任何属性，也不要
> 编造商品事实。

---

## 4. `ASK_ATTRIBUTE_RELAX_CONFLICT_PROMPT`

**来源**：`llm_nodes.py` 第 338-343 行。调用方：同上 `_ask_attribute_node`，但在
`mode == "relax_conflict"` 时使用——也就是"顾客当前这个属性的要求把商品全过滤没了，问问要不要
放宽"的场景。

**预期输入格式**：跟第 3 条完全一样的结构（同一个 payload 构造代码），只是这时候
`known_constraints` 里通常包含那个卡死了搜索结果的具体约束。

**预期输出格式**：同 `AskAttributeOutput`，只有 `question_text` 一个字段。

**英文原文**：
> The customer's current requirement on the given attribute matched no products. Write one
> short, friendly question asking whether they would like to relax or change that specific
> requirement so more options can be shown. Do not mention any other attribute or invent
> product facts.

**中文翻译**：
> 顾客当前在给定这个属性上的要求，没有匹配到任何商品。写一句简短、友好的提问，问顾客是否愿意
> 放宽或改变这个具体要求，以便能展示更多选项。不要提到其他任何属性，也不要编造商品事实。

---

## 5. `DISTILL_PROFILE_PROMPT`

**来源**：`llm_nodes.py` 第 345-351 行。调用方：`graph.py:916` `_distill_profile_node`
（`DistillProfile` 节点），只有在这一轮确实产生了新证据（`DistillTriggerRouter` 判定为"有变化"）
时才会被调用。

**作用**：从这一轮约束的变化里，提炼出两个很"软"的长期画像信号——对价格敏不敏感、有没有风格
偏好——写进跨会话保留的 `session_profile`。刻意设计得很浅（只有两个字段），不是拿来存放约束本身
的地方。

**预期输入格式**（`_distill_profile_input`，`graph.py:880-913`）：
```json
{
  "previous_profile": {"price_sensitivity": null, "style_signal": "casual"},
  "diff": {
    "mutations": [{"attribute": "material", "value": "leather", "action": "upsert"}],
    "no_preference": ["color"],
    "category_anchor": "Outdoor & Work Rain",
    "global_override": false
  },
  "rejection_signal": false  // ClassifyIntent 判定为 new_search 时为 true，代表"顾客否定了刚展示的东西"
}
```

**预期输出格式**（`DistillProfileOutput`，`llm_nodes.py:116-125`）：
```json
{ "price_sensitivity": "budget-conscious", "style_signal": null }
```
两个字段都可以是 `null`（模型不确定时应该留空，而不是硬编一个）。

**英文原文**：
> You maintain a tiny long-term shopping profile from this turn's constraint changes and any
> rejection signal. Only fill a field when this turn's diff actually supports it; leave a
> field null if unsure. Do not restate the constraints themselves -- only soft signals about
> price sensitivity or style preference.

**中文翻译**：
> 你在维护一个很小的长期购物画像，依据是这一轮约束的变化和任何"被否定"的信号。只有当这一轮的
> 变化确实能支持某个字段时才填它；不确定就留空。不要把约束本身复述一遍——只记录关于价格敏感度
> 或风格偏好的软性信号。

---

## 6. `EXPLAIN_PROMPT`

**来源**：`llm_nodes.py` 第 353-360 行左右（`EXPLAIN_PROMPT`）。调用方：`graph.py:1396`
`_explain_node`（`Explain` 节点）。

**作用**：给这一轮展示的推荐商品写一句引入语，纯粹是措辞/生成，不允许陈述任何没被喂给它的
事实（价格、评分等）——防止模型编造商品信息。

**预期输入格式**（`graph.py:1406-1414`）：
```json
{
  "message": "顾客这一轮说的话",
  "results": [
    {"parent_asin": "B0XXXXXXXX", "title": "商品标题"}
    // 至多 5 条，只有 ID 和标题，没有价格/评分等其他字段
  ]
}
```

**预期输出格式**（`ExplainOutput`，`llm_nodes.py:128-136`）：
```json
{ "message": "Here are some rain boots we think you'll love!" }
```

**英文原文**：
> Write one short, friendly sentence introducing the shown recommendations to the customer,
> in light of their message. Reference only the product titles supplied; do not state a
> price, rating, or any other fact not given to you.

**中文翻译**：
> 结合顾客这句话，写一句简短、友好的话，向顾客引出这次展示的推荐商品。只能引用给你的商品标题；
> 不要陈述价格、评分，或任何没有给你的事实。

---

## 7. `COMPARE_PROMPT`

**来源**：`llm_nodes.py` 第 342-346 行。调用方：`graph.py:1484` `_compare_node`（`Compare`
节点），仅当 `ClassifyIntent` 判定为 `compare_details` 时才会走到这里。

**作用**：对顾客点名要对比的几件商品，写一段基于给定字段的对比文字，同样不允许编造字段外的
事实。

**预期输入格式**（`graph.py:1494-1506`）：
```json
{
  "message": "顾客这一轮说的话",
  "products": [
    {
      "parent_asin": "B0XXXXXXXX",
      "title": "商品标题",
      "price": 39.99,
      "rating": 4.5,
      "features": ["特性1", "特性2"]  // 至多 5 条
    }
  ]
}
```

**预期输出格式**（`CompareOutput`，`llm_nodes.py:139-144`）：
```json
{ "message": "The first option is cheaper, while the second has a higher rating..." }
```

**英文原文**：
> Write a short comparison of the given products for the customer, based only on the supplied
> fields. Do not state a fact that is not present in the input.

**中文翻译**：
> 只依据给定的字段，为顾客写一段简短的商品对比。不要陈述输入里没有的事实。

---

## 8. `LLMSemanticRanker` 的内联 system prompt（唯一不走 `llm_nodes.py` 壳的节点）

**来源**：`starter/shopping_agent/semantic_ranking.py` 第 246-333 行，`LLMSemanticRanker.rank`
方法内联构造，没有独立命名常量。调用方：`graph.py:1265` `_semantic_rank_node`（`SemanticRank`
节点）——设计上刻意不走 `call_llm_value_node` 那套重试/兜底壳，因为这个类自己就实现了等价的
"任何失败都返回原始顺序、绝不凭空造 ID"的保证（细节见 `graph.py:1266-1272` 的节点文档字符串）。

**作用**：对 `Rank`（确定性打分）已经排好序的候选商品，最多取前 30 个（由
`_semantic_rank_node` 里的 `semantic_limit` 卡死，不受配置项影响），做一次"听懂顾客真实意图"的
列表级重排。失败或未配置时，直接维持 `Rank` 给出的原始顺序。

**预期输入格式**（`user` 消息，`json.dumps` 后的内容）：
```json
{
  "intent": "{\"message\": \"顾客这一轮说的话\", \"constraints\": [{\"attribute\": \"material\", \"value\": \"leather\"}]}",
  "candidates": [
    {"parent_asin": "B0XXXXXXXX", "product": "商品摘要文本（截断到有界长度）"}
    // 至多 semantic_limit 条（≤30）
  ]
}
```
注：`intent` 字段本身是一段被 `json.dumps` 过、再当字符串塞进去的 JSON，双重编码，这是现有实现
的写法，不是我这次改的。

**预期输出格式**（`_validate_ranking_json` 校验，非 pydantic 模型，手写校验）：
```json
{
  "ranked_parent_asins": ["B0XXXXXXXX", "..."],  // 只能用输入里出现过的 ID，允许只给部分（调用方会把没提到的按 Rank 原序补在后面）
  "scores": {"B0XXXXXXXX": 0.92}
}
```

**英文原文（system 消息原文）**：
> You are a constrained product relevance ranker. Return JSON only, with schema
> {"ranked_parent_asins":[string],"scores":{string:number}}. Use only the supplied
> parent_asin values. Do not invent products. The list may be partial; the caller will repair
> omissions.

**中文翻译**：
> 你是一个受限的商品相关性排序器。只返回 JSON，schema 是
> {"ranked_parent_asins":[字符串数组],"scores":{字符串: 数字}}。只能使用给定的 parent_asin
> 值。不要凭空生成商品。列表可以是不完整的；调用方会补全遗漏的部分。

---

## 小结：哪些 prompt 是今天这个会话改动过的（已定稿）

- `EXTRACT_CONSTRAINTS_PROMPT`（1 号）：今天做了两轮改动。第一轮按输出字段零散加了三条说明（含
  被你指出的测试集原文泄漏），第二轮在你的批评下**按 `ExtractConstraintsOutput` 的 5 个字段整体
  重写**——不是继续打补丁，而是每个字段一段、只讲一次规则，测试集原文全部换成合成例子。定稿版本
  已贴在上面第 1 条。重写过程中发现的一个真实回归（`public_0002`）已收窄措辞修复；剩下追出来的
  排序层问题（约束值替换导致排名不稳）不在 prompt 范围内，未修复，记录在
  `ARCHITECTURE_IMPROVEMENT_PLAN.md`。
- 其余 7 个 prompt：今天没有改动，原样列出供你核对当前状态。
- 其余 7 个 prompt：今天没有改动，原样列出供你核对当前状态。
