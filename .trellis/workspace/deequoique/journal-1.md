# Journal - deequoique (Part 1)

> AI development session journal
> Started: 2026-08-25

---



## Session 1: 初始化中文 Trellis 项目规范

**Date**: 2026-08-25
**Task**: 初始化中文 Trellis 项目规范
**Branch**: `main`

### Summary

基于真实 Python 竞赛代码重建中文 Trellis 规范，覆盖 Agent、Evaluator、数据合同、测试与离线复现，并完成任务校验和回归测试。

### Git Commits

| Hash | Message |
|------|---------|
| `3376bce` | (see git log) |

### Status

[OK] **Completed**


## Session 2: Qwen3 reranker benchmark 收尾

**Date**: 2026-08-26
**Task**: Qwen3 reranker benchmark 收尾
**Branch**: `main`

### Summary

完成 Qwen3 reranker 的 Colab benchmark 对照流程，修复 smoke 子集 comparison，补充测试、实验报告与 evaluator 协议；validation 40 上 TechnicalScore 从 0.681467 提升至 0.734271，并以暂不默认集成、后续补 locked 40 的结论归档。

### Git Commits

| Hash | Message |
|------|---------|
| `5362c67` | (see git log) |

### Status

[OK] **Completed**


## Session 3: Deterministic dialogue ranking and exhausted reopen

**Date**: 2026-08-27
**Task**: Deterministic dialogue ranking and exhausted reopen
**Branch**: `main`

### Summary

Implemented target-preserving structured pools, deterministic ranking evidence, dynamic recommendation commit policy, exhaustion state, other-first clarification, and one-turn clarification bypass after explicit recommendation rejection. Final offline evaluation: score 0.884923, Hit@10 0.99, MRR 0.775411, MTTC 3.135, usage 0; unittest 75/75.

### Git Commits

| Hash | Message |
|------|---------|
| `a9ed63f` | (see git log) |

### Status

[OK] **Completed**


## Session 4: Refactor shopping agent into focused modules

**Date**: 2026-08-27
**Task**: Refactor shopping agent into focused modules
**Branch**: `main`

### Summary

Split the 1,025-line starter Agent into retrieval, deterministic ranking, and response-boundary modules while preserving public and internal compatibility. Verified 75 unittests, Python compilation, independent review, and old-vs-new differential parity; updated the Agent architecture spec.

### Git Commits

| Hash | Message |
|------|---------|
| `b3d5ab7` | (see git log) |

### Status

[OK] **Completed**


## Session 5: Provenance-aware scoped Intent Override

**Date**: 2026-08-27
**Task**: Provenance-aware scoped Intent Override
**Branch**: `main`

### Summary

Implemented deterministic scoped Intent Override for LegacyAgent with query evidence provenance, hard override upgrades, epoch cleanup, active-only retrieval/ranking projections, diagnostics, regression coverage, and seed-2026 fixed-200 validation (score 0.863357; Override Hit 0.900). Independent Trellis check passed 109 tests.

### Git Commits

| Hash | Message |
|------|---------|
| `18e7d02` | (see git log) |

### Status

[OK] **Completed**


## Session 6: Restore single Agent and consolidate benchmarks

**Date**: 2026-08-28
**Task**: Restore single Agent and consolidate benchmarks
**Branch**: `main`

### Summary

Restored the original retrieval/state pipeline as the only starter.agent.Agent, removed ContestAgent and tracked contest/holdout/report/result artifacts, deleted the DeepSeek parallel runner, and moved Qwen/adaptive recall tools into tests.benchmarks. Validation: independent review PASS, unittest 92/92, public-200 Hit@10 1.000000, MRR 0.805024, MTTC 3.005000, technical score 0.901407, token usage 0.

### Git Commits

| Hash | Message |
|------|---------|
| `04699f4` | (see git log) |

### Status

[OK] **Completed**


## Session 7: 修复仓库维护债务

**Date**: 2026-08-28
**Task**: 修复仓库维护债务
**Branch**: `main`

### Summary

删除 README 死链，补全 Agent 模块规范，将确定性意图解析拆分到 intent.py 并保留 state 兼容导出；92 项测试通过。

### Git Commits

| Hash | Message |
|------|---------|
| `2abb908` | (see git log) |

### Status

[OK] **Completed**


## Session 8: 整理仓库架构与项目文档

**Date**: 2026-08-28
**Task**: 整理仓库架构与项目文档
**Branch**: `main`

### Summary

精简中文 README，新增 Diátaxis 文档索引、系统架构、本地评测、模型后端和 benchmark 指南；保留英文公开合同与现有单 Agent 源码布局，并隔离本地私有/生成产物。92 项测试、CLI、链接、checksum 和 200 会话离线评测全部通过。

### Git Commits

| Hash | Message |
|------|---------|
| `deb69d9` | (see git log) |

### Status

[OK] **Completed**
