# 配置可选模型后端

Agent 默认使用确定性召回与排序。模型只负责对有界候选集进行可选语义重排；没有配置、
请求失败、JSON 无效或输出 ID 不合法时，系统继续使用原确定性候选顺序。

## 选择一种模式

```text
未配置 Qwen 本地路径
  └─ LLM tier：DeepSeek API → 本地 OpenAI-compatible endpoint → 确定性顺序

配置了 Qwen 绝对本地路径
  └─ Qwen cross-encoder → 确定性顺序
```

Qwen 是 Agent 环境配置中的显式优先路径。若同时设置 Qwen checkpoint 和 LLM tier 变量，
Agent 使用 Qwen，不再构造 LLM semantic ranker。

## DeepSeek API

只有非空 API key 才启用 DeepSeek：

```bash
export SHOPPING_AGENT_DEEPSEEK_API_KEY=your-key
export SHOPPING_AGENT_DEEPSEEK_BASE_URL=https://api.deepseek.com
export SHOPPING_AGENT_DEEPSEEK_MODEL=deepseek-v4-flash
```

`SHOPPING_AGENT_DEEPSEEK_BASE_URL` 和 `SHOPPING_AGENT_DEEPSEEK_MODEL` 有上述默认值；API key
没有默认值。不要把 key 写入源码、JSONL、测试、报告或提交包。

## 本地 OpenAI-compatible 端点

本地 tier 需要同时设置 URL 和模型名：

```bash
export SHOPPING_AGENT_LOCAL_BASE_URL=http://127.0.0.1:8000/v1
export SHOPPING_AGENT_LOCAL_MODEL=my-local-model
python3 -m evaluator.local_evaluator
```

服务端需要提供 `POST /chat/completions`。如服务要求 Authorization header，再设置：

```bash
export SHOPPING_AGENT_LOCAL_API_KEY=your-local-token
```

本仓库不规定 checkpoint、推理服务器或启动命令；这些属于部署环境，必须在实际提交说明中
单独披露。

## LLM 共同参数

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `SHOPPING_AGENT_MODEL_TIMEOUT_SECONDS` | `8` | 每个 backend 请求的硬超时 |
| `SHOPPING_AGENT_MODEL_CANDIDATE_LIMIT` | `30` | 语义重排候选上限 |
| `SHOPPING_AGENT_RETRIEVAL_LIMIT` | `100` | 基础召回预算 |
| `SHOPPING_AGENT_MODEL_MAX_TOKENS` | `512` | completion 上限 |
| `SHOPPING_AGENT_MODEL_TEMPERATURE` | `0` | 请求温度 |

`SHOPPING_AGENT_CANDIDATE_LIMIT` 是模型候选上限的兼容别名。即使配置更大的通用上限，
Agent 的语义阶段仍把输入限制在 Top 30。

## 本地 Qwen reranker

Qwen 只接受显式的绝对本地 checkpoint 路径，不会根据模型 ID 自动下载：

```bash
export SHOPPING_AGENT_QWEN_RERANKER_MODEL_PATH=/absolute/path/to/checkpoint
export SHOPPING_AGENT_QWEN_RERANKER_DEVICE=mps
```

可选参数：

| 环境变量 | 默认值 |
| --- | ---: |
| `SHOPPING_AGENT_QWEN_RERANKER_REVISION` | 未设置 |
| `SHOPPING_AGENT_QWEN_RERANKER_DEVICE` | `mps`；无效值回退到 `cpu` |
| `SHOPPING_AGENT_QWEN_RERANKER_BATCH_SIZE` | `8` |
| `SHOPPING_AGENT_QWEN_RERANKER_CANDIDATE_LIMIT` | `30` |
| `SHOPPING_AGENT_QWEN_RERANKER_TIMEOUT_SECONDS` | `15` |
| `SHOPPING_AGENT_QWEN_RERANKER_FUSION_WEIGHT` | `1.0` |

运行环境还需要提供 Qwen adapter 实际导入的模型依赖。仓库没有为它声明统一安装命令，
因此不要在复现说明中假定该依赖已经存在。可先通过[离线 benchmark](benchmarks.md)验证
本地 checkpoint、设备、延迟和结果边界，再把路径接入正式 Agent。

## 回退与 usage

- LLM tier 固定先尝试 DeepSeek，再尝试本地端点。
- 请求、响应 JSON 或 schema 校验失败会记录失败原因，并尝试下一 tier。
- 所有 tier 都失败时，语义结果回到原确定性顺序。
- Qwen 加载或预测失败时也回到确定性顺序。
- `usage.prompt_tokens` 和 `usage.completion_tokens` 只来自成功且返回合法非负整数的 backend；
  失败 tier 不累计 usage。

官方评分可能关闭网络。提交时必须明确模型依赖、凭据、延迟和成本，并说明离线 fallback。
公开限制见 [Submission Rules](../submission_rules.md)。
