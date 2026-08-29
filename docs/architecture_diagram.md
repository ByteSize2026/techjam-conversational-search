# Shopping Agent 架构图（Router / Value-Node 图）

本图对应当前代码库里实际运行的图结构 `starter/shopping_agent/graph.py`（`ROUTERS`/`NODES`），
配合文字说明见 `AGENT_ARCHITECTURE.md`。三种颜色对应三种节点角色：

- 🔵 **蓝色 = Router**：纯代码，只决定"下一个节点是谁"，从不调用模型。
- 🟢 **绿色 = 确定性 Value Node**：算法/查表产出一个事实（检索、排序、放宽约束、查详情）。
- 🟠 **橙色 = LLM Value Node**：一次结构化输入输出的模型调用产出一个事实；离线或解析失败时退化为
  各自的确定性 fallback（见 `AGENT_ARCHITECTURE.md` §4）。
- ⚪ **灰色圆角 = 终止节点** `Render`。

虚线是全图唯一允许重复经过的一条边（`LoosenConstraints → Search`），由 `search_retry_count`
计数器守卫、封顶 1 次、每个官方轮次开始时清零——这是唯一的环，其余部分是一张 DAG。

```mermaid
flowchart TD
    classDef router fill:#e0ecff,stroke:#3b5bdb,stroke-width:1.5px,color:#1c2b4a
    classDef vnDet fill:#e6f6ec,stroke:#2f9e44,stroke-width:1.5px,color:#1a3a26
    classDef vnLLM fill:#fff3e0,stroke:#e8590c,stroke-width:1.5px,color:#4a2a10
    classDef terminal fill:#f1f3f5,stroke:#495057,stroke-width:2px,color:#212529

    Entry["Entry\n(Router)"]:::router
    ClassifyIntent["ClassifyIntent\n(LLM)"]:::vnLLM
    IntentRouter2["IntentRouter2\n(Router)"]:::router
    ExtractConstraints["ExtractConstraints\n(LLM)"]:::vnLLM
    DistillTriggerRouter["DistillTriggerRouter\n(Router)"]:::router
    DistillProfile["DistillProfile\n(LLM)"]:::vnLLM
    SlotCheckRouter["SlotCheckRouter\n(Router)"]:::router
    Search["Search\n(Deterministic)"]:::vnDet
    CandidatePoolRouter["CandidatePoolRouter\n(Router)"]:::router
    LoosenConstraints["LoosenConstraints\n(Deterministic)"]:::vnDet
    NoMatch["NoMatch\n(Deterministic)"]:::vnDet
    Rank["Rank\n(Deterministic)"]:::vnDet
    RankRouter["RankRouter\n(Router)"]:::router
    SemanticRank["SemanticRank\n(LLM)"]:::vnLLM
    Explain["Explain\n(LLM)"]:::vnLLM
    FetchDetails["FetchDetails\n(Deterministic)"]:::vnDet
    Compare["Compare\n(LLM)"]:::vnLLM
    AskAttribute["AskAttribute\n(LLM, wording only)"]:::vnLLM
    Render(("Render\n(终止)")):::terminal

    Entry -->|"首轮 / 正在回答上一轮追问"| ExtractConstraints
    Entry -->|"本 epoch 已展示过推荐"| ClassifyIntent

    ClassifyIntent --> IntentRouter2
    IntentRouter2 -->|"compare_details"| FetchDetails
    IntentRouter2 -->|"refine_search / new_search"| ExtractConstraints
    IntentRouter2 -->|"confirm_choice"| Render

    ExtractConstraints -->|"commit via StateReducer"| DistillTriggerRouter
    DistillTriggerRouter -->|"diff 值得蒸馏"| DistillProfile
    DistillTriggerRouter -->|"no-op"| SlotCheckRouter
    DistillProfile --> SlotCheckRouter

    SlotCheckRouter -->|"记一个候选追问到 scratch\n（若有），但始终继续"| Search

    Search --> CandidatePoolRouter
    CandidatePoolRouter -->|"空池，首次"| LoosenConstraints
    CandidatePoolRouter -->|"空池，已重试过，还有约束可放宽"| AskAttribute
    CandidatePoolRouter -->|"空池，无约束可放宽 / 轮数耗尽"| NoMatch
    CandidatePoolRouter -->|"非空（过泛化则记追问到 scratch）"| Rank
    LoosenConstraints -.->|"重试，封顶 1 次"| Search
    NoMatch --> Render

    Rank --> RankRouter
    RankRouter -->|"scratch 里有待问属性"| AskAttribute
    RankRouter -->|"没有"| SemanticRank
    SemanticRank --> Explain
    Explain --> Render

    FetchDetails --> Compare
    Compare --> Render
    AskAttribute --> Render
```

## 场景对照

`artifacts/scenario_showcase/` 下的六段真实多轮对话 trace，每一段都能在这张图上按边追踪：

| 场景文件 | 在图上走的路径（简化） |
|---|---|
| `01_vague_clarify_converge` | `SlotCheckRouter`/`RankRouter` 反复把待问属性递给 `AskAttribute`，直到证据/预算耗尽转向 `SemanticRank` |
| `02_over_general_fill_missing` | `CandidatePoolRouter` 记过泛化追问 → `Rank` → `RankRouter` → `AskAttribute` |
| `03_intent_override` | `IntentRouter2` 的 `new_search` 分支（带 `global_override`）回到 `ExtractConstraints` |
| `04_multiturn_rank_reorder` | 多轮 `Search → Rank → RankRouter → SemanticRank` 循环，观察排序随约束累积变化 |
| `05_empty_search_retry_relax` | 唯一的环：`CandidatePoolRouter → LoosenConstraints → Search`，重试后走 `AskAttribute(relax_conflict)` |
| `06_true_dead_end_no_match` | `CandidatePoolRouter` 重试后仍为空且无约束可放 → `NoMatch` |

## 如何导出为图片

这是标准 Mermaid 语法，可以用以下任意一种方式直接导出 PNG/SVG：

- VS Code 装 "Markdown Preview Mermaid Support" 插件后直接预览本文件并导出。
- 粘贴到 [mermaid.live](https://mermaid.live) 在线编辑器导出。
- 命令行：`npx @mermaid-js/mermaid-cli -i docs/architecture_diagram.md -o architecture.png`（需要先把 mermaid 代码块单独存成 `.mmd` 文件）。
- GitHub 会在仓库页面直接原生渲染这个代码块，无需任何额外步骤。
