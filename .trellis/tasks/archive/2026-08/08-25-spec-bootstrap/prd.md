# 初始化项目 Trellis 规范

## Goal

将当前与项目不匹配的通用 frontend/backend/Trellis CLI 模板，替换为一套基于真实 Python 竞赛代码、使用中文编写、可供后续开发直接执行的 `.trellis/spec/` 规范。

## Scope

### 范围内

- 清理并重组 `.trellis/spec/`。
- 按真实职责建立 `agent`、`evaluator`、`data-contracts`、`testing`、`guides` 五个规范层。
- 使用 `starter/agent.py`、`evaluator/local_evaluator.py`、`tests/test_evaluator.py`、`docs/`、`data/` 和 `.gitignore` 作为规范证据。
- 更新当前任务的 `design.md`、`implement.md`、`implement.jsonl` 和 `check.jsonl`。
- 验证规范发现、索引链接、占位文本、现有单元测试和 CLI 导入。

### 范围外

- 不修改 `starter/agent.py`、`evaluator/local_evaluator.py`、`tests/`、`data/`、`docs/` 或竞赛规则。
- 不修改 `.trellis/workflow.md`、hook、任务脚本或平台集成行为。
- 不引入第三方依赖、构建工具、格式化器、lint 或类型检查器。
- 不运行依赖缺失 `data/catalog.jsonl` 的完整评估，除非本地已按 README 准备该文件。

## Requirements

- 所有新增或重写的项目规范使用中文。
- 每条关键规则必须由真实源码、测试或项目文档支持，并标注相关路径或符号。
- 删除不适用的 `frontend/`、`backend/` 规范及与本项目无关的旧 guides。
- 每个规范层的 `index.md` 必须包含适用范围、开发前检查、专题链接和质量检查。
- 规范必须准确描述 Agent 生命周期、响应契约、会话状态、推荐标准化、四类评估场景、评分公式、JSONL/隐私/提交边界和 unittest fixture 模式。
- 任务 manifests 必须引用真实 spec/research 文件，不保留 `_example`。

## Acceptance Criteria

- [ ] `.trellis/spec/` 只包含 `agent`、`evaluator`、`data-contracts`、`testing`、`guides` 五个一级目录。
- [ ] 不再存在 frontend/backend 模板及旧 Trellis CLI 指南。
- [ ] 所有规范均为中文，无 `TBD`、`To fill`、模板注释、空章节或占位文本。
- [ ] 所有 index 链接与最终文件集合一致。
- [ ] Agent、Evaluator、数据/提交、测试和跨层影响规则均引用真实项目文件。
- [ ] `task.py validate` 与 `get_context.py --mode packages` 通过，并发现新的规范层。
- [ ] `python3 -m unittest discover -s tests -v` 全部通过。
- [ ] `python3 -m evaluator.local_evaluator --help` 成功。
- [ ] 产品源码、评估器、测试、数据、竞赛文档和 workflow runtime 未被修改。
