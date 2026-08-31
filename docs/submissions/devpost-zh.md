# TechJam 多轮对话电商搜索：从官方高分到自然语言增强

## 总览

我们构建了一个多轮购物搜索 Agent：它在冻结的商品目录中持续维护用户意图，处理新增条件、否定、局部修改和“无偏好”回答，并在最多 10 轮内返回目标商品的 Top-10 `parent_asin`。

在开发过程中，我们发现**官方评分协议可以由一个完全离线的确定性架构稳定解决；但高分不等于自然语言交互能力。** 因此我们又构建了独立的自然语言 evaluator，用量化结果定位差距，再把 DeepSeek 放在一个受控的“语言翻译层”，而不是让模型直接接管商品推荐。

## 我们解决了什么问题

普通关键词搜索只看当前一句话，无法可靠处理多轮购物对话。用户可能先说品类，再补充预算，随后否定颜色、修改品牌，或表示“颜色你决定”。如果系统把每轮文本简单拼接，容易丢失有效约束、误删旧意图，或过早把目标商品从候选池中截掉。

我们的 Agent 显式维护：当前类别、硬约束、软偏好、否定条件、已问属性、Boundary 状态、意图 epoch、候选池、推荐历史及证据来源。所有状态变化都经过 `StateReducer`；召回、过滤、排序、澄清和最终响应彼此分离。

## 解决方案

### 1. 先用确定性系统完成官方任务

官方 evaluator 的输入较结构化，适合可审计、可复现的离线路径。我们从弱 starter 的 SQLite FTS/BM25 搜索器出发，逐步加入：

- Buying/Browsing 动态路由；
- 自适应类目召回和结构化候选池；
- 逐约束过滤、零结果回退和 lexical tail 保留；
- 动态澄清与推荐提交策略；
- provenance-aware 的局部 Intent Override；
- Boundary 的“无偏好”处理；
- 输出 schema 与合法商品 ID 守卫。

在公开 200 题上，最终离线 Agent 达到：

| 指标 | 弱 starter | 最终离线 Agent |
| --- | ---: | ---: |
| Hit@10 | 0.125 | **1.000** |
| MRR | 0.068034 | **0.805024** |
| MTTC | 9.81 | **3.005** |
| Technical Score | 0.106710 | **0.901407** |

Buying、Browsing、Intent Override、Boundary 四类官方场景的 Hit@10 均为 1.0；该路径零 token、无需网络、无需模型 API。

这说明：**如果目标只是完成官方跑分，结构化状态、召回、排序和提交策略已经足够高效，不需要把 LLM 放进主路径。**

### 2. 再测量真实自然语言交互的差距

官方协议不能充分覆盖同义表达、否定、多条件、指代和隐藏 profile 等真实交互。为此我们建立了独立的智能自然语言 evaluator v2：

- 100 个冻结样本；
- 50,000 个商品目录；
- 最多 10 轮；
- 精确 `parent_asin` Top-10 评分；
- 7 类场景：Budget Rating、Clarification、Direct Search、Intent Override、Multi Constraint、Negative Constraint、Profile Hidden。

### 这个 evaluator 如何工作

它不是让一个大模型随意扮演用户，而是一个**可冻结、可验证、目标不泄露**的多轮测试器：

```text
冻结 catalog
   ↓
确定性生成器选定目标商品，并生成唯一事实签名
   ↓
事实拆分为：初始查询、匿名 profile、隐藏 clarification slots
   ↓
每轮调用 Agent → 解释 ask_attribute / 自然语言问题
   ↓
只披露目标商品支持的下一条事实，更新候选状态
   ↓
记录逐轮 trace，最后做 exact parent_asin 评分
```

具体做法有四点：

1. **先选目标，再生成对话。** 生成器从本地 catalog 选定一个目标，并要求完整事实合取在 catalog 中唯一命中它；初始信息则故意保留多个候选，避免第一轮就泄露答案。
2. **把事实分成已知和隐藏。** 一部分事实放入初始 query 或匿名 profile，其余放入 clarification slots。Agent 只有通过合适的追问，或用户自然地表达出条件，才会获得下一条隐藏事实。
3. **同时理解结构化追问和自然语言。** evaluator 既读取 Agent 的 `ask_attribute`，也解析 `message` 中的自然语言问题；两者冲突时标记为 `ambiguous`，不会猜测。它还识别同义表达、重复追问、宽泛问题、无偏好、无关问题和信息耗尽。
4. **评测器与 Agent 隔离。** `target_parent_asin`、事实签名和候选数量只存在于父进程和评分器中，不会传入 Agent。每轮旁路记录意图路径、状态、召回/排序阶段和问题解释，便于区分“没理解”“被安全校验拒绝”“召回遗漏”和“提交策略丢失”。

我们用固定 seed 生成并冻结 100 题，再由 validator 重新检查每条 query、profile、override 和隐藏回复确实包含对应事实。这样每次比较的输入完全相同，DeepSeek 的变化可以归因于语言理解层，而不是题目随机变化。

实现上，`generator.py` 负责从 catalog 生成目标与事实签名，`simulator.py` 负责推进多轮用户状态，`question_interpreter.py` 负责解释 Agent 的追问，`validator.py` 负责防止题目泄露或事实不一致，`evaluator.py` 与 `metrics.py` 负责隔离运行 Agent 并计算指标。评测过程中使用一次性的 worker 子进程，父进程不会把目标字段导入 Agent 的调用栈。

冻结集的验证与评测命令如下（从独立 benchmark 仓库运行）：

```bash
cd /path/to/techjam-natural-language-benchmark
python3 -m nl_benchmark validate \
  --catalog /path/to/techjam-conversational-search-main/data/catalog.jsonl \
  --dataset outputs/frozen-100-seed-20260830.jsonl

python3 -m nl_benchmark evaluate \
  --protocol-profile natural_language \
  --agent-repo /path/to/techjam-conversational-search-main \
  --catalog /path/to/techjam-conversational-search-main/data/catalog.jsonl \
  --dataset outputs/frozen-100-seed-20260830.jsonl \
  --output /tmp/techjam-natural-language-results.json
```

评分仍然只看最终任务：目标商品是否进入 Top-10、首次命中排名和首次命中轮次；不根据模型自评或文字相似度给分。`intent_override` 还专门构造“旧的错误偏好 → 新的目标事实”转移，override 之前的推荐不会计入新意图的 exact 指标。

将第一版确定性 Agent 直接放入该 evaluator 后，Hit@10 只有 0.580，平均 MTTC 为 7.08。这不是官方 Agent “失效”，而是说明模板化高分路径对自然表达覆盖有限。

### 3. 用 DeepSeek 做受控语言翻译

我们加入 DeepSeek canonicalizer，把口语化需求改写成系统能够稳定读取的“购物事实”。模型返回的不是商品推荐，而是一个只含 `canonical_text` 的 JSON；其中每行只表达一个条件。

例如，用户说：

> I'm after something comfy for rainy commutes. Keep it under $80, and please don't show me black. Actually, make it Skechers instead of the brand I mentioned earlier.

DeepSeek 会把它整理成：

```
{
  "canonical_text": "A key requirement is: use_case: rainy commutes.\nA key requirement is: feature: comfortable.\nA key requirement is: budget: under $80.\nI do not want color: black.\nActually, change the brand to Skechers."
}
```

随后，原有确定性 parser 会把这些行转换成 Agent 内部能够执行的条件：

- 使用场景：雨天通勤；
- 希望具备：舒适；
- 预算：不超过 80 美元；
- 排除：黑色；
- 品牌：把之前的品牌改为 Skechers，其他条件继续保留。

翻译层只允许描述购物中真正有用的字段，包括品类、品牌、预算、最低评分/评论数、颜色、材质、尺寸、风格、功能和使用场景；同时支持“不要黑色”“预算不再重要”“把品牌改成 Skechers”这类否定、撤销和局部修改。

```
自然语言消息
  → DeepSeek 整理成一行一个购物事实
  → 检查这些事实是否符合用户原意、商品库里是否存在
  → 只更新用户明确新增或修改的偏好
  → 交给原有召回、排序、澄清和推荐流程
```

为了避免“模型理解得很流畅，但理解错了”，系统会逐轮做几项检查：品牌、颜色、材质等值必须能在本地商品库中找到；翻译中不能出现商品 ID 或系统不认识的字段；用户只说“换品牌”时不能顺便清空预算和品类；用户没有明确撤销的偏好必须继续保留。

如果任意一项检查失败，整轮模型翻译都会被放弃，系统直接使用原来的离线规则结果，避免半对半错的信息污染后续对话。

## 效果

![官方 evaluator 离线成绩进展](../diagrams/official_score_progression.svg)
![自然语言 evaluator 中 DeepSeek 的收益](../diagrams/natural_language_deepseek_gain.svg)

在独立自然语言 evaluator v2 上，DeepSeek 带来明显提升：

| 指标 | 确定性 Agent | + DeepSeek 翻译层 | 变化 |
| --- | ---: | ---: | ---: |
| Hit@10 | 0.580 | **0.770** | +19 个百分点 |
| Exact Top-1 | 0.270 | **0.510** | +24 个百分点 |
| MRR | 0.365913 | **0.605179** | +0.239266 |
| MTTC | 7.08 | **3.72** | −3.36 轮 |

提升最明显的场景包括 Direct Search（0.800 → 1.000）、Intent Override（0.429 → 0.857）、Profile Hidden（0.143 → 0.500）和 Negative Constraint（0.643 → 0.786）。Multi Constraint 从 0.867 降至 0.800，说明模型层仍需更严格的多条件校验。

这两套 evaluator 的样本和协议不同，不能合并成一个排行榜。它们回答的是两个不同问题：官方 evaluator 衡量协议内的稳定完成度；自然语言 evaluator 衡量表达鲁棒性。

## 技术栈、工具与 API

- 开发工具：VS Code、Python 3.10+ 命令行、`unittest`；仓库保留 Jupyter/Colab 风格的 Qwen reranker 实验 notebook。
- 核心库：Python 标准库（`json`、`re`、`sqlite3`、`pathlib`）；SQLite FTS5/BM25 用于离线召回。
- 可选实验依赖：Qwen cross-encoder 路径可使用 `sentence-transformers`/PyTorch；不属于官方离线主路径。
- 外部 API：DeepSeek V4 Flash 的 OpenAI-compatible API，仅用于自然语言约束翻译；API key 通过环境变量注入，不写入仓库。
- 运行边界：官方提交可在关闭网络时使用确定性 fallback；DeepSeek 版本为可选增强路径。

## 复现

### 离线版：官方评分主路径

不设置模型凭据即可运行。该命令复现官方 evaluator 的确定性 Agent：

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator \
  --protocol-profile official \
  --catalog data/catalog.jsonl \
  --dataset data/public_set.jsonl \
  --output /tmp/techjam-official-offline.json
```

### 在线版：DeepSeek 自然语言增强路径

先在本地 `.env` 中填写 `SHOPPING_AGENT_DEEPSEEK_API_KEY`，再显式打开自然语言模型层。不要把 key 写入命令、代码或提交包：

```bash
set -a; source .env; set +a
export SHOPPING_AGENT_INTENT_MODEL_ENABLED=true
export SHOPPING_AGENT_INTENT_MODEL_MODE=model_first
export SHOPPING_AGENT_MODEL_TIMEOUT_SECONDS=30

python3 -m evaluator.local_evaluator \
  --protocol-profile natural_language \
  --catalog data/catalog.jsonl \
  --dataset data/public_set.jsonl \
  --output /tmp/techjam-deepseek-online.json
```

两条命令使用同一套官方公开集；自然语言 evaluator v2 的 100 题冻结集由独立 benchmark 仓库运行。

如果需要录制逐轮 Demo，可直接启动仓库内的 CLI：

```bash
python3 -m starter.cli --protocol-profile official
```

在线自然语言演示则使用：

```bash
set -a; source .env; set +a
export SHOPPING_AGENT_INTENT_MODEL_ENABLED=true
export SHOPPING_AGENT_INTENT_MODEL_MODE=model_first
python3 -m starter.cli --protocol-profile natural_language --show-diagnostics
```

CLI 支持 `:help`、`:reset`、`:exit`/`quit`；`--show-diagnostics` 只显示不含目标商品和密钥的安全摘要。


## 局限与下一步

1. 自然语言 evaluator 仍是冻结 benchmark，不等同于真实线上用户分布；下一步需要扩大表达覆盖并做跨域测试。
2. DeepSeek 增加延迟和 token 成本；100 题运行总耗时为 677.93 秒、reported tokens 为 924,200（DeepSeek 922,047 + 本地 fallback 2,153），成本为按缓存假设估算的区间而非账单。
3. 当前模型层主要解决意图翻译，尚未覆盖个性化解释、长期偏好学习和多商品比较。
4. 下一步优先做规则优先/低置信度触发，并记录失败请求的完整 usage 与 cache 命中情况。

## 提交链接

- GitHub：<https://github.com/ByteSize2026/techjam-conversational-search>
- Demo Video（YouTube 公共链接）：**提交前补充**

## 团队贡献

- 王彤鹭：负责项目整体架构设计与实现，提出并落地 LLM 辅助的自然语言交互方案；主导灵活 evaluator 的设计与开发，完成主要代码编写、项目统筹、测试和文档整理。
- 田宇甲：离线 Agent 优化，使 Technical Score 从约 `0.80` 提升至 `0.90`。
- 任思宇：Agent-loop 架构实验、测试与方案研究。
- 罗文杰：benchmark 数据集构建与 evaluator 测试优化。
- 江泽东：项目思路整理、方案讨论与协同测试。
