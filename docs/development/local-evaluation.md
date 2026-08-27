# 本地开发与评测

本指南用于验证代码改动是否保持 Agent 合同，并在冻结公开集上生成可复现指标。所有命令
都从仓库根目录运行。

## 前提

- Python 3.10 或更高版本。
- 运行完整评测前，`data/catalog.jsonl` 必须是发布的 50,000 行冻结 catalog。
- 默认确定性路径不要求第三方 Python 包、网络或模型凭据。

## 准备 catalog

下载发布的 `catalog.jsonl.gz` 到仓库根目录，计算摘要：

```bash
shasum -a 256 catalog.jsonl.gz
```

将结果与仓库根目录 `SHA256SUMS` 的对应行比较，然后解压：

```bash
gzip -dc catalog.jsonl.gz > data/catalog.jsonl
```

不要用缩减 catalog 代替正式公开集评测，也不要修改商品内容或添加合成 ID。

## 先运行回归测试

```bash
python3 -m unittest discover -s tests -v
```

回归测试使用 `tempfile.TemporaryDirectory()` 中的最小 JSONL fixture，因此不依赖完整
catalog、测试执行顺序或网络。修改状态、检索或排序时，至少保证正常路径以及对应的
Intent Override、Boundary、重复/无效 ID 或 miss 边界仍被覆盖。

## 运行公开集评测

```bash
python3 -m evaluator.local_evaluator \
  --catalog data/catalog.jsonl \
  --dataset data/public_set.jsonl \
  --output results.json
```

参数均可通过以下命令查询：

```bash
python3 -m evaluator.local_evaluator --help
```

评测器会为每个样本创建新会话，先调用 `reset`，再最多调用 10 次 `respond`。异常、非字典
响应或非字符串 `message` 会降级为空响应；这通常不会中止评测，但会造成 miss。

## 理解输出

- Hit@10：在最多 10 轮内进入前 10 的会话比例。
- MRR：首次命中时目标排名的倒数均值；miss 记 0。
- MTTC：首次命中轮数均值；miss 按第 11 轮计算。
- Efficiency：`clip((11 - MTTC) / 10, 0, 1)`。
- 推荐技术分：`0.50 × Hit@10 + 0.30 × MRR + 0.20 × Efficiency`。

指标也会按 Buying、Browsing、Intent Override 和 Boundary 分场景报告。token 用量与延迟
用于可行性披露，不改变核心技术分。机器可读配置见
[`docs/evaluation_config.json`](../evaluation_config.json)。

[`docs/baseline_results.json`](../baseline_results.json) 是已发布 weak starter 的历史基线，
不是当前工作区 Agent 的预期固定结果。比较改动时应保存各次实际运行结果，并记录所用代码、
配置、catalog 与数据集。

## 避免覆盖已有结果

`--output` 会写入指定路径。做临时验证时可写到 `/tmp`：

```bash
python3 -m evaluator.local_evaluator \
  --catalog data/catalog.jsonl \
  --dataset data/public_set.jsonl \
  --output /tmp/techjam-results.json
```

仓库根目录的 `results.json` 已被 Git 忽略，但仍可能是有价值的本地实验记录。

## 常见问题

### 找不到 catalog

确认文件位于 `data/catalog.jsonl`，而不是仍在仓库根目录。只运行 unittest 时不需要该文件。

### 未配置模型却看到模型失败诊断

没有模型配置时，语义层会保留“没有 backend”的诊断并使用确定性顺序。这不代表评测必须
联网。实际配置和回退顺序见[可选模型后端](model-backends.md)。

### 总分正常但单一场景退化

总分会掩盖场景差异。修改消息解析、追问或状态时，应分别检查四类场景，尤其是新意图到达
前不能转化的 Intent Override，以及可能回答“没有偏好”的 Boundary。
