# TechJam 多轮对话电商搜索

本项目实现一个多轮购物搜索 Agent：它根据匿名偏好和顾客消息维护会话意图，提出必要的
澄清问题，并在最多 10 轮内返回冻结商品目录中的 Top 10 `parent_asin`。

正式入口只有 [`starter.agent.Agent`](starter/agent.py)。本地评估器、单元测试和离线
benchmark 都使用这一实现。

## 快速开始

项目建议使用 Python 3.10 或更高版本。默认的确定性路径只依赖 Python 标准库，不需要
API key 或网络连接。

### 1. 准备商品目录

从仓库对应的 GitHub Release 下载 `catalog.jsonl.gz`，放到仓库根目录。可先查看摘要：

```bash
shasum -a 256 catalog.jsonl.gz
```

结果应与 [`SHA256SUMS`](SHA256SUMS) 中的 `catalog.jsonl.gz` 一致。随后解压到 `data/`：

```bash
gzip -dc catalog.jsonl.gz > data/catalog.jsonl
```

`data/catalog.jsonl` 是本地大文件，不纳入 Git。数据字段和隐私边界见
[`data/README.md`](data/README.md)。

### 2. 运行回归测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时小型 catalog，不要求提前下载完整商品目录，也不访问网络。

### 3. 运行公开集评测

```bash
python3 -m evaluator.local_evaluator \
  --catalog data/catalog.jsonl \
  --dataset data/public_set.jsonl \
  --output results.json
```

`results.json` 是本地产物，已被 Git 忽略。指标定义和结果解释见
[本地开发与评测](docs/development/local-evaluation.md)。

## 核心接口

评估器会为每个会话先调用 `reset`，再逐轮调用 `respond`：

```python
class Agent:
    def reset(self, session_id: str, user_profile: dict) -> None:
        ...

    def respond(
        self,
        session_id: str,
        user_message: str,
        turn: int,
        top_k: int,
    ) -> dict:
        ...
```

请求、响应字段和枚举以 [`docs/agent_api_contract.json`](docs/agent_api_contract.json)
为准。系统如何从会话状态走到最终响应，见[系统架构](docs/architecture.md)。

## 文档导航

- [文档总览](docs/README.md)：按教程、操作指南、架构解释和公开参考查找文档。
- [系统架构](docs/architecture.md)：模块职责、每轮数据流和稳定边界。
- [本地开发与评测](docs/development/local-evaluation.md)：catalog、测试、评测和常见问题。
- [可选模型后端](docs/development/model-backends.md)：DeepSeek、本地兼容端点、Qwen 和回退。
- [离线 benchmark](docs/development/benchmarks.md)：adaptive recall 与 Qwen 实验工具。
- [Competition Specification](docs/competition_specification.md)：公开比赛协议与评分说明（英文）。
- [Submission Rules](docs/submission_rules.md)：公开提交要求（英文）。

[`docs/baseline_results.json`](docs/baseline_results.json) 保存的是已发布 weak starter 的
历史参考成绩，不代表当前持续演进的 `Agent` 实时成绩。

## 仓库结构

```text
starter/                 正式 Agent 入口与购物搜索组件
evaluator/               公开集模拟与评分
tests/                   unittest 回归
tests/benchmarks/        离线诊断与模型 benchmark
data/                    公开开发集与本地 catalog
docs/                    架构、开发指南与公开合同
notebooks/               可选实验 notebook
```

公开集和商品目录派生自 Amazon Reviews 2023。使用或再分发前请阅读
[`DATA_ATTRIBUTION.md`](DATA_ATTRIBUTION.md)。
