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
  --protocol-profile official \
  --catalog data/catalog.jsonl \
  --dataset data/public_set.jsonl \
  --output results.json
```

`results.json` 是本地产物，已被 Git 忽略。指标定义和结果解释见
[本地开发与评测](docs/development/local-evaluation.md)。

### 4. 逐轮 CLI 演示

仓库也提供一个直接从终端输入消息的 REPL；它复用同一个 `Agent`，不改变评测逻辑。
离线版无需模型或网络：

```bash
python3 -m starter.cli --protocol-profile official
```

自然语言在线版先在本地 `.env` 配置 DeepSeek key，再运行：

```bash
set -a; source .env; set +a
export SHOPPING_AGENT_INTENT_MODEL_ENABLED=true
export SHOPPING_AGENT_INTENT_MODEL_MODE=model_first
python3 -m starter.cli --protocol-profile natural_language --show-diagnostics
```

输入 `:help` 查看命令，输入 `:reset` 重置会话，输入 `:exit` 或 `quit` 退出。

## 协议 Profile

同一个 `Agent` 支持两个显式协议 profile；它不会根据消息内容或数据分布猜测当前
evaluator：

| Profile | 输入与澄清适配 | 共享部分 |
| --- | --- | --- |
| `official` | 冻结的官方消息 adapter、稳定顺序 `protocol_aware` 澄清与官方结构化过滤字段；零参数默认值 | 状态 reducer、候选池实现、召回、排序、推荐提交、响应守卫 |
| `natural_language` | `IntentInterpreter`、catalog profile grounding、扩展结构化过滤与基于候选信息量的澄清 | 状态 reducer、候选池实现、召回、排序、推荐提交、响应守卫 |

Python 调用可直接传参：

```python
from starter.agent import Agent

official_agent = Agent(protocol_profile="official")
natural_language_agent = Agent(protocol_profile="natural_language")
```

官方本地 evaluator 使用一条命令启动：

```bash
python3 -m evaluator.local_evaluator --protocol-profile official
```

独立的自然语言 benchmark 在其仓库中使用同名参数：

```bash
python3 -m nl_benchmark evaluate \
  --protocol-profile natural_language \
  --agent-repo /path/to/techjam-conversational-search-main \
  --catalog /path/to/techjam-conversational-search-main/data/catalog.jsonl \
  --dataset /path/to/dataset.jsonl \
  --output /path/to/results.json
```

也可通过 `AgentConfig` 或 `SHOPPING_AGENT_PROTOCOL_PROFILE` 配置 profile。profile 只选择
协议输入和策略配置，不会隐式开启网络模型；DeepSeek、本地模型和 Qwen 仍需各自显式
配置。官方 adapter 保留公开集已验证的旧协议语义，避免自然语言 parser 演进改变官方成绩。

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

`starter/` 是主要源码目录，其他目录用于评测、验证、实验或项目管理：

| 目录 | 职责 | 正式运行时 |
| --- | --- | --- |
| `starter/` | 唯一 Agent 入口、会话状态、意图解释、召回、排序和响应组件 | 是 |
| `evaluator/` | 官方公开集的本地模拟、响应过滤和评分 CLI | 否，外部调用 Agent |
| `tests/` | 标准库 `unittest` 合同与回归测试 | 否 |
| `tests/benchmarks/` | adaptive recall、Qwen 等离线诊断工具 | 否 |
| `data/` | 公开开发集及本地冻结 catalog；大文件和结果不提交 | 数据输入 |
| `docs/` | 架构、接口合同、开发指南和比赛规则 | 否 |
| `notebooks/` | 可选模型实验 notebook | 否 |
| `holdout/` | 本地留出集和历史实验结果，不属于正式提交 | 否 |
| `report/` | 本地报告预留目录；当前没有受 Git 跟踪的正式实现 | 否 |
| `scripts/` | 本地或历史脚本预留目录；当前没有受 Git 跟踪的可执行源码 | 否 |
| `.trellis/` | 任务、规范、工作流和开发记录 | 否 |
| `.claude/`、`.cursor/` | Trellis 在不同开发工具中的命令、skill 和 hook | 否 |

协议 profile 解决的是运行时兼容，不等同于 Git 分支合并。本任务不会自动 merge、rebase
或 cherry-pick `main`；验证后的代码是否推进到其他分支应作为单独发布决策。

公开集和商品目录派生自 Amazon Reviews 2023。使用或再分发前请阅读
[`DATA_ATTRIBUTION.md`](DATA_ATTRIBUTION.md)。
