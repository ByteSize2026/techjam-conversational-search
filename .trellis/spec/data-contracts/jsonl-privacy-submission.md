# JSONL、隐私与提交边界

## JSONL 与标识合同

`data/README.md` 规定 `public_set.jsonl` 是公开开发会话，`catalog.jsonl` 需由发布的 `catalog.jsonl.gz` 解压到 `data/`。读取方式应使用 UTF-8，逐行解析非空 JSON；`evaluator/local_evaluator.py:load_jsonl` 与 `catalog_index` 是本仓库的真实示例。

冻结 catalog 的评分标识是字符串 `parent_asin`，不是标题、变体或模型产生的别名。`docs/competition_specification.md` 列出可见商品字段，且说明只有 `parent_asin` 被评分。Agent 推荐和测试 fixture 都应使用这个字段；评估器会拒绝 catalog 外 ID，规则见 `normalize_recommendations`。

不得修改 catalog 以添加合成 ID、补标或改变商品内容。公开集可用于本地开发，但不要把从公开样本推导的隐藏意图卡、模拟器政策或私有标签包装进提交策略。

## 隐私与秘密

比赛数据已移除直接用户标识、购买时间戳、自由文本评论和原始购买记录；Agent 获得的是安全聚合 `user_profile`。此边界来自 `docs/competition_specification.md` 与 `data/README.md`。将 profile 仅用于会话内、最小必要的个性化；不要尝试重建个人身份或外部关联。

`data/README.md` 明确禁止在 `data/` 放置 API keys、私有评估数据或参与者输出。`docs/competition_specification.md` 要求模型凭据通过环境变量传入且绝不提交。密钥读取逻辑若存在，必须让缺失密钥有清晰的本地失败或离线回退行为，不能把密钥写到源码、JSONL、测试 fixture 或报告中。

## 提交边界

根据 `docs/submission_rules.md`，提交可包含 Python 源码、必要的本地辅助模块/轻量资源、依赖清单和安装说明；必须交付导出 `Agent` 的入口、复现说明、方法与限制报告，以及延迟、token 和成本披露。

不得提交私有评估数据、复制的组织方内部文件、API keys/秘密、需要特权主机访问的代码、修改 evaluator 的代码，或依赖未声明外部服务才能进行官方评分的实现。官方评分可能禁用网络，因此说明中必须明确网络需求；有离线 fallback 时描述它，没有时也要明确说明。最终只按冻结官方工件和该环境实际输出评分。

提交前将本规则与 [离线复现](../guides/offline-reproducibility.md) 和 [Agent 合同](../agent/contract-and-state.md) 一起检查。
