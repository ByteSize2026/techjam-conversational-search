# 离线复现

## 原则

优先使用 Python 标准库和仓库已存在的执行路径。当前基线的 `starter/agent.py` 使用 `json`、`re`、`sqlite3`、`pathlib`，评估器使用标准库模块；不要为了普通检索、JSONL 读取或 unittest 额外引入依赖。若确有第三方或模型依赖，提交说明必须给出版本、安装步骤、使用条件和离线行为。

## 本地可重复路径

1. 按 `data/README.md` 从发布包下载 `catalog.jsonl.gz`，解压为 `data/catalog.jsonl`；该文件预期为 50,000 行，不能以任意缩减 catalog 代替正式本地评估。
2. 使用固定公开集 `data/public_set.jsonl` 和冻结 catalog 运行：

   ```bash
   python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output results.json
   ```

   CLI 参数与写出 `results.json` 的行为来自 `evaluator/local_evaluator.py:main`。
3. 无论是否有完整 catalog，都可运行不依赖它的回归：

   ```bash
   python3 -m unittest discover -s tests -v
   ```

   `tests/test_evaluator.py` 使用 `tempfile.TemporaryDirectory()` 构造小 JSONL catalog，因此测试不要求下载文件或联网。

## 网络、凭据与回退

`docs/submission_rules.md` 说明官方最终评分可能关闭网络；`docs/competition_specification.md` 要求 API key 由环境变量传入且不提交。因此实现必须：

- 不把 key、token 或端点机密写入 Python、JSONL、测试或报告；
- 明确所需环境变量和依赖安装命令；
- 明确提供离线 fallback，或明确声明没有有效凭据/网络时不能运行的能力；
- 不将未经声明的外部服务视为官方评分必然可用。

当前 `starter/agent.py` 是无 LLM、内存 SQLite FTS 的离线基线；若替换检索器，保留可验证的资源需求与失败方式。不要伪报 usage：仅在实际可得时返回非负整数 `prompt_tokens` 和 `completion_tokens`。

## 提交前检查

- [ ] 从干净环境按说明安装并运行 Agent/harness。
- [ ] 用冻结数据验证 ID 只来自 catalog，且结果可由同一命令复现。
- [ ] 不把 `results.json`、私有数据或密钥作为提交必需输入。
- [ ] 报告模型、近似成本、token、延迟、限制和网络需求。

关联的提交内容边界见 [数据与提交规则](../data-contracts/jsonl-privacy-submission.md)，协议正确性见 [评估协议](../evaluator/protocol-and-scoring.md)。
