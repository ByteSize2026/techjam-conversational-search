# Diagnostic Findings — 2026-08-29

## External benchmark evidence

Frozen dataset：独立仓库 `techjam-natural-language-benchmark/outputs/offline-7.jsonl`，7 个场景各 1 题。

| Mode | Exact Top-1 | Hit@10 | MRR | MTTC |
| --- | ---: | ---: | ---: | ---: |
| rules-only | 0.000 | 0.000 | 0.000 | 11.00 |
| DeepSeek current trigger/merge | 0.000 | 0.000 | 0.000 | 11.00 |
| every-turn DeepSeek + confidence override 1.0 | 0.143 | 0.429 | 0.229 | 8.14 |

强制实验验证 32/32 轮使用 `deepseek-api`，0 backend failure，19 轮出现 accepted model fields。
命中 direct_search Top-1、clarification_required rank 2、profile_hidden rank 10。

## Failure signatures

- Current merge：模型识别 `Mighty Fine Juniors`、`Skechers`、`Solocute` 等值，但 mutation confidence
  为 0 或整体 confidence 为 0，全部低于 floor。
- Confidence override 后：`replace brand=Skechers` 仍因 `replacement_not_supported` 被拒绝；
  `Solocute` / `SOCIETY NEW YORK` 被模型归为 `other/replace` 并被拒绝。
- Intent override：`brand=Luoika` 被接受，但明确删除旧 budget 被 `remove_not_supported_by_message`
  拒绝，旧 `Backpacks` evidence 残留。
- Commit policy：多轮目标已处于 retrieval/feature/semantic Top-1，最终 recommendation 仍为空并追问 `other`。
- Boundary：rules-only 的 “no additional preferences” 会清空 active query terms，使此前 Top-1 目标消失；
  强制实验则可能从模型 snapshot 重新注入旧 terms，两种行为都需要明确、确定性的生产语义。

## Planning implication

简单降低阈值或硬编码 confidence=1.0 不足以解决问题。需要同时修改 model trigger、action/attribute
normalization、evidence provenance、state confirmation、boundary semantics 和 target-free commit policy，
同时把 frozen deterministic update 作为任何失败的原子 fallback。

## 2026-08-30 natural-language precision pass

本轮先处理“用户原话没有变成精确筛选条件”，没有扩大召回预算、修改提交时机或 Top-1
策略。主要修复：

- DeepSeek/canonicalizer 失败统一回退到 catalog-grounded deterministic update；例如网络失败时
  `Please look for Roamans` 仍保留 `brand=roamans`。
- `Yes — please look for Boys` 只提取当前 payload，不再把开场词 `Yes` 当作品牌。
- referenced override（忽略早先 X、改成 Y）只解析新 payload，旧 decoy 不再回流为 budget/brand/category。
- 短回答继承刚被询问的字段；逗号/冠词组成的完整 catalog label 优先整体匹配。
- canonicalizer 结果必须有当前句文字/数字证据、命中 catalog 字段，且不能与本地已识别值重复或冲突；
  模型不再把 `mens` 当作 `use_case`，也不能从历史 state 复制 `Snap`。

验证结果：

| 版本 | Exact Top-1 | Hit@10 | MRR | MTTC |
| --- | ---: | ---: | ---: | ---: |
| 修复前离线 100 题 | 0.460 | 0.770 | 0.572 | 3.64 |
| 第一轮精确解析离线 | 0.560 | 0.830 | 0.651 | 3.17 |
| 第二轮精确解析离线 | 0.580 | 0.870 | 0.686 | 2.75 |
| 最终 DeepSeek canonicalizer 在线 | 0.630 | 0.870 | 0.716 | 2.74 |

最终在线运行共有 303 个回合：145 个模型翻译被接受，43 个模型结果因 catalog/当前句证据
校验失败而安全降级；无新增回归样本。最终剩余 13 个 miss 按任意计分轮最远阶段统计为：
5 个未召回、4 个 feature ranking、2 个 semantic Top-30/gate、1 个进入 feature 前丢失、1 个
后段排序；因此“初始召回一下切掉太多”目前不是第一弱项。
