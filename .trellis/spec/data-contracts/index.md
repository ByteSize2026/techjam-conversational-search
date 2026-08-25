# 数据与提交合同

## 适用范围

适用于读取 catalog/公开集 JSONL、处理 `parent_asin`、使用匿名 profile，以及组织最终提交内容的工作。它约束数据和隐私边界，不规定检索算法本身。

## 真实来源

- 本地数据文件、下载位置和禁放内容：`data/README.md`。
- 可见字段、私有评估边界、模型政策：`docs/competition_specification.md`。
- 请求/响应字段限制：`docs/agent_api_contract.json`。
- 提交允许/禁止内容与离线要求：`docs/submission_rules.md`。

## 开发前检查

- [ ] 确认读取的是 UTF-8、每行一个 JSON 对象的 JSONL，并以 `parent_asin` 作为唯一评分 ID。
- [ ] 本地 `data/catalog.jsonl` 是否已经从发行包解压；若不存在，不以伪数据替代真实评估。
- [ ] 只使用提供的匿名聚合 profile，不引入直接身份或私有评估数据。
- [ ] 审核输出包内没有密钥、私有数据、组织方文件或依赖未声明的在线服务。

## 专题链接

- [JSONL、隐私与提交边界](./jsonl-privacy-submission.md)
- [Agent 输入输出合同](../agent/contract-and-state.md)
- [离线复现](../guides/offline-reproducibility.md)
- [临时 JSONL fixture](../testing/unittest-fixtures.md)

## 质量检查

- [ ] JSONL 空行被跳过，非空行可解析为对象；ID 用字符串比较。
- [ ] catalog 不被改写，推荐 ID 均来自冻结 catalog。
- [ ] 不提交 API keys、私有评估数据、参与者输出或需要特权主机访问的代码。
- [ ] 提交说明给出 Python 版本、依赖安装、官方 harness 命令和非显而易见的环境变量。

## 交叉引用

推荐有效性由 [Evaluator](../evaluator/index.md) 过滤；离线与网络限制的实施检查见 [Guides](../guides/index.md)。
