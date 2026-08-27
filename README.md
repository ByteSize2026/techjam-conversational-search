# TechJam Conversational E-Commerce Search Challenge

ByteSize 小组叉。**`main` 的评分入口是 ContestAgent PUBLIC**（`starter.agent.Agent`）。组员 Qwen / 自适应召回实验在分支 **`legacy/qwen`**，不要在 `main` 上把 `Agent` 改回那套管道。

| 分支 | `starter.agent.Agent` | 用途 |
|---|---|---|
| `main` | ContestAgent PUBLIC | 提交 / 公开集计分 |
| `legacy/qwen` | 组员检索 + Qwen rerank | 实验，测试走这套 |

本地公开 200（需 `data/catalog.jsonl`）：

```bash
python eval_contest.py --only public
# 或
python -m evaluator.local_evaluator
```

当前本地分数（0 token）：公开 200 Hit@10 **1.000** / 技术分 **0.9534**；自建 holdout 200 Hit **0.980** / **0.8888**。公开 0.95 **不能**当私有 800 的预测。说明见 `report/README.md`。

---

Build an AI shopping agent that asks useful follow-up questions and recommends the customer's hidden target product within at most 10 turns.

## What You Receive

- A frozen catalog of 50,000 products from the `Clothing_Shoes_and_Jewelry` category of Amazon Reviews 2023.
- 200 labeled public sessions for local development.
- A weak BM25 starter agent and deterministic local evaluator.
- The Agent API contract and scoring rules.

The organizer keeps 800 additional sessions private for final evaluation.

## Task

For each session, your agent receives an anonymized preference profile and a short customer message. Raw user IDs, review text, timestamps, and purchase history are never disclosed. On every turn the agent may:

- ask a natural clarification question in `message` and identify one requested field in `ask_attribute`;
- return a ranked list of up to 10 catalog `parent_asin` values;
- do both in the same response.

The session ends when the target product appears in the scored Top 10 or after turn 10. Sessions cover Buying, Browsing, Intent Override, and Boundary behavior.

## Download the Catalog

Download `catalog.jsonl.gz` from the GitHub Release attached to this repository, then run:

```bash
gzip -dk catalog.jsonl.gz
mv catalog.jsonl data/catalog.jsonl
```

Verify the downloaded file using the published `SHA256SUMS` file.

## Run the Starter

Python 3.10 or later is recommended. The starter uses only the Python standard library.

```bash
python3 -m evaluator.local_evaluator
```

Edit `starter/agent.py` to implement your system. Do not edit the evaluator or public labels when reporting your local score.
The command writes per-session results and aggregate metrics to `results.json`.

The included weak BM25 starter scores Hit Rate@10 `0.125`, MRR `0.068034`, and
MTTC `9.81` on the released public set. See `docs/baseline_results.json`.

## Agent Interface

```python
class Agent:
    def reset(self, session_id: str, user_profile: dict) -> None:
        ...

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        return {
            "message": "Do you have a material preference?",
            "ask_attribute": "material",
            "recommendations": [
                {"parent_asin": "B000..."},
                {"parent_asin": "B001..."}
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30}
        }
```

`ask_attribute` is one of `category`, `material`, `color`, `size`, `style`, `brand`, `budget`, `feature`, `use_case`, `other`, or `null`. See `docs/agent_api_contract.json`.

## Technical Metrics

- **Hit Rate@10:** fraction of sessions that find the target within 10 turns.
- **MRR:** mean reciprocal rank of the target; a miss contributes zero.
- **MTTC:** mean first-hit turn; a miss is assigned turn 11.
- **Reported token usage:** prompt and completion tokens returned by the team's model client.

```text
TechnicalScore = 0.50 × HitRate@10 + 0.30 × MRR + 0.20 × Efficiency
Efficiency = clip((11 - MTTC) / 10, 0, 1)
```

Only exact `parent_asin` equality produces a hit. Core metrics are also reported by scenario.

## Model Choice and Cost

Teams may use any legally accessible LLM API or local model. Teams manage their own credentials and must never commit API keys. Model choice, estimated cost, token usage, and latency must be disclosed. Token usage is a feasibility metric, not part of the core technical score. The organizer does not provide or reimburse model API credits; teams are responsible for any costs incurred through optional external services.

### Optional Tiered Model Backend

The optional model-assisted path is explicitly tiered and remains usable offline:

```text
DeepSeek API -> local OpenAI-compatible endpoint -> deterministic fallback
```

With no relevant environment variables, no model backend is constructed and no network request is made. The deterministic path still returns catalog-valid recommendations. If a configured backend times out, returns an HTTP/JSON/schema error, or fails validation, the next tier is attempted; the semantic ranker then repairs invalid, duplicate, unknown, or omitted IDs using the original deterministic candidate order.

Supported environment variables are:

```text
SHOPPING_AGENT_DEEPSEEK_API_KEY       # enables the DeepSeek tier
SHOPPING_AGENT_DEEPSEEK_BASE_URL      # default: https://api.deepseek.com
SHOPPING_AGENT_DEEPSEEK_MODEL         # default: deepseek-v4-flash
SHOPPING_AGENT_LOCAL_BASE_URL         # enables local tier when paired with LOCAL_MODEL
SHOPPING_AGENT_LOCAL_MODEL            # model name sent to the local endpoint
SHOPPING_AGENT_LOCAL_API_KEY          # optional Authorization token for local servers
SHOPPING_AGENT_MODEL_TIMEOUT_SECONDS  # per-request hard timeout; default: 8
SHOPPING_AGENT_MODEL_CANDIDATE_LIMIT  # semantic-ranker candidate cap; default: 30
SHOPPING_AGENT_CANDIDATE_LIMIT        # compatibility alias for the previous setting
SHOPPING_AGENT_RETRIEVAL_LIMIT        # retrieval budget; default: 100
SHOPPING_AGENT_MODEL_MAX_TOKENS       # completion cap; default: 512
SHOPPING_AGENT_MODEL_TEMPERATURE      # default: 0
```

Both `SHOPPING_AGENT_LOCAL_BASE_URL` and `SHOPPING_AGENT_LOCAL_MODEL` must be set to enable the local tier. A local server only needs to expose an OpenAI-compatible `POST /chat/completions` endpoint; for example:

```bash
export SHOPPING_AGENT_LOCAL_BASE_URL=http://127.0.0.1:8000/v1
export SHOPPING_AGENT_LOCAL_MODEL=my-local-model
python3 -m evaluator.local_evaluator
```

This example intentionally does not prescribe a checkpoint or server launch command. Token usage is reported only when the successful backend supplies valid non-negative `prompt_tokens` and `completion_tokens`; failed tiers never contribute usage. Browsing currently uses the broad lexical plus category-diversity fallback. A dense route will be enabled only after frozen assets pass the documented resource and candidate-recall gates.

## Files

```text
data/public_set.jsonl             200 labeled development sessions
docs/competition_specification.md participant rules and evaluation protocol
docs/agent_api_contract.json      machine-readable Agent contract
docs/evaluation_config.json       scoring configuration
docs/baseline_results.json        reproducible weak-starter reference score
starter/agent.py                  Agent = ContestAgent PUBLIC; LegacyAgent = 组员旧管道
starter/shopping_agent/contest_*.py  计分实现
evaluator/local_evaluator.py      public-set simulator and scorer
eval_contest.py                   公开 200 计分（写 results_contest_public.json）
eval_holdout.py                   自建 holdout 200
eval_shard.py                     随机 800 的单片评测
report/                           方法、holdout、知识库结论
holdout/                          自建测试集与对照 JSON（不是官方私有 800）
```

## Judging and Submission Policy

- Participant submission requirements: `docs/submission_rules.md`
- Participant release checklist: `docs/participant_release_checklist.md`
- Organizer-only final judging controls: `organizer/JUDGING_RUNBOOK.md`
- Organizer private release checklist: `organizer/private_release_checklist.md`
- Judging day operations SOP: `organizer/JUDGING_DAY_SOP.md`

## Data Source

The catalog and sessions are derived from Amazon Reviews 2023 by McAuley Lab, UCSD. See `DATA_ATTRIBUTION.md` before using or redistributing the data.
Sessions are sampled deterministically from the official Clothing 5-core leave-last-out split and joined to the frozen catalog.
