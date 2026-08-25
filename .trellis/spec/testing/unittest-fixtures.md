# unittest 与最小 fixture

## 使用标准库测试基线

测试使用 `unittest`，入口命令是：

```bash
python3 -m unittest discover -s tests -v
```

遵循 `tests/test_evaluator.py`：从 `unittest.TestCase` 派生测试类，直接导入待测符号，并用清晰的标准库断言比较外部可见结果。不要为本任务引入新的测试运行器、插件或 fixture 框架。

## 临时 catalog JSONL

当测试需要文件输入时，使用：

```python
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    catalog_path = root / "catalog.jsonl"
```

这是 `tests/test_evaluator.py:test_evaluate_derives_hidden_fields_when_public_set_omits_them` 的既有模式。向 `catalog_path` 写入 UTF-8 JSONL，每行一个 `json.dumps(row)` 加换行符；随后以该路径调用 `catalog_index` 或 Agent。上下文退出时目录会自动删除，因而不污染 `data/`、不依赖已下载的 50,000 行 catalog，也不会把测试输出纳入提交。

最小商品行应包含被测路径真实读取的字段。`catalog_index` 必需 `parent_asin` 并读取 `categories`；`intent_card`/`searchable_text` 还可能读取 `title`、`features`、`details`、`description`、`store`、`price`。不要无理由复制完整真实数据。

## 确定性 Agent 替身

`EchoTargetAgent` 展示了最小替身：`reset` 接受评估器参数，`respond` 接受完整调用签名，并根据输入返回稳定的 `message`、`ask_attribute` 与 `recommendations`。替身只应表达测试目标，例如验证新意图消息能使推荐从 A 切换到 B；不要调用网络、读取真实 catalog 或依赖随机 UUID。

优先分别测试纯函数 `normalize_recommendations`、`metric_summary`，再用小 catalog 覆盖 `evaluate` 的端到端协议。必须保留以下已知边界：首个有效唯一 ID 的顺序、catalog 外和重复 ID 被过滤、miss 的 MTTC 值为 11、公开样本缺少隐藏字段时由 `materialize_hidden_fields` 补全。

若修改会话状态、场景或评分，结合 [评估协议](../evaluator/protocol-and-scoring.md) 增加确定性测试；若修改 JSONL 读写，结合 [数据合同](../data-contracts/jsonl-privacy-submission.md) 检查临时文件内容和隐私边界。
