# 运行离线 benchmark

仓库把可复用的诊断和实验工具放在 `tests/benchmarks/` Python 包中。它们与 unittest 使用
同一正式 `starter.agent.Agent`，但会生成实验产物，因此不属于日常回归测试入口。

所有命令都从仓库根目录运行，输出建议写到 `/tmp` 或显式的本地 artifact 目录。

## Adaptive category recall 诊断

该工具比较自适应类目召回路径，并输出 target-free trace 和召回诊断：

```bash
python3 -m tests.benchmarks.adaptive_recall \
  --catalog data/catalog.jsonl \
  --public-set data/public_set.jsonl \
  --sample-limit 10 \
  --output /tmp/adaptive-recall.json
```

省略 `--sample-limit` 会运行完整公开集。查询当前参数：

```bash
python3 -m tests.benchmarks.adaptive_recall --help
```

## Qwen reranker 实验

Qwen harness 分为 `manifest`、`baseline`、`rerank` 和 `compare` 四步。顶层帮助：

```bash
python3 -m tests.benchmarks.qwen_reranker --help
```

### 1. 冻结实验 split

```bash
python3 -m tests.benchmarks.qwen_reranker manifest \
  --public-set data/public_set.jsonl \
  --output /tmp/qwen-manifest.json
```

manifest 以固定 seed 建立 target-free 的 dev、validation 和 locked split。需要自定义比例时，
先查看 `manifest --help`。

### 2. 运行确定性 baseline

```bash
python3 -m tests.benchmarks.qwen_reranker baseline \
  --catalog data/catalog.jsonl \
  --public-set data/public_set.jsonl \
  --manifest /tmp/qwen-manifest.json \
  --split validation \
  --sample-limit 5 \
  --output /tmp/qwen-baseline.json
```

### 3. 运行本地 Qwen 重排

`--model-path` 必须指向已经存在的绝对本地 checkpoint：

```bash
python3 -m tests.benchmarks.qwen_reranker rerank \
  --catalog data/catalog.jsonl \
  --public-set data/public_set.jsonl \
  --manifest /tmp/qwen-manifest.json \
  --split validation \
  --sample-limit 5 \
  --model-path /absolute/path/to/checkpoint \
  --device mps \
  --output /tmp/qwen-reranked.json
```

工具不会下载 checkpoint。设备可选 `mps`、`cpu` 或 `cuda`；实际可用性取决于本地模型
运行时。`rerank --help` 列出 batch size、候选上限、超时、融合权重和 frozen trace 参数。

### 4. 比较结果

```bash
python3 -m tests.benchmarks.qwen_reranker compare \
  --baseline /tmp/qwen-baseline.json \
  --reranked /tmp/qwen-reranked.json \
  --manifest /tmp/qwen-manifest.json \
  --split validation \
  --sample-limit 5 \
  --output /tmp/qwen-comparison.json
```

baseline、rerank 和 compare 必须使用相同 manifest、split 与 `--sample-limit`。如果前两步只
跑 5 个样本而 compare 省略 limit，工具会按完整 split 校验并报告缺失样本。

## 产物与复现边界

- benchmark 输出不是正式公开基线，不应覆盖 `docs/baseline_results.json`。
- 不把本地 checkpoint、私有 holdout、API key 或临时结果提交到仓库。
- 记录 manifest、split、sample limit、模型路径对应的版本、设备和完整 CLI 参数。
- smoke 子集只验证工具链，不代表完整 split 或最终评测表现。
- 若要在正式 Agent 中启用已验证 checkpoint，见[可选模型后端](model-backends.md)。

仓库还保留 [`notebooks/qwen3_reranker_colab.ipynb`](../../notebooks/qwen3_reranker_colab.ipynb)
作为可选实验入口；可复现结论仍应落到上述命令及其 JSON 产物，而不是只保留 notebook 状态。
