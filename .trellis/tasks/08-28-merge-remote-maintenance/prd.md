# 合并远端维护更新

## Goal

将本地文档整理提交与 `origin/main` 的维护重构安全合并到同一条 `main` 历史中，保留双方
全部有效提交和 Trellis 会话记录，验证后使用普通 push 更新远端。

## Background

- 本地 `main` 与 `origin/main` 共同基点为 `e054e04`，双方各领先 3 个提交。
- 普通 push dry-run 已确认会因 non-fast-forward 被拒绝，不会覆盖远端。
- 远端核心提交 `2abb908` 将意图解析拆到 `intent.py`；隔离环境中 92 项测试全部通过。
- 自动合并预检确认冲突集中在 `README.md`、Trellis workspace index 和 journal。
- 工作树另有两个未跟踪的意图适配 Trellis 任务，均不属于本次合并。

## Requirements

- 使用 merge 而非 rebase，避免改写本地 Trellis journal 已记录的提交哈希。
- 合并 `origin/main`，不得使用 `--force`、`--force-with-lease`、reset 或历史重写。
- `README.md` 保留本地新版中文信息架构；远端删除失效链接的意图已包含在该版本中。
- Trellis workspace 同时保留“修复仓库维护债务”和“整理仓库架构与项目文档”两次会话，
  将前者保留为 Session 7、后者调整为 Session 8，并同步 index 统计和顺序。
- 保留远端 `intent.py` / `state.py` 重构、Agent spec 更新和维护任务归档，不借合并修改行为。
- 不删除、暂存或改写 `.trellis/tasks/08-27-flexible-intent-adapter/` 与
  `.trellis/tasks/08-27-intent-interpreter/`。
- 合并完成并验证通过后，使用普通 `git push origin main`。

## Acceptance Criteria

- [ ] `git merge-base --is-ancestor 9752af5 HEAD` 成功，证明远端三个提交均保留。
- [ ] `git merge-base --is-ancestor 7693d3a HEAD` 成功，证明本地三个提交均保留。
- [ ] README 保持当前中文导航和有效链接，不恢复旧的重复英文段落或死链。
- [ ] Trellis journal/index 包含两次不同编号的 Session 7/8，工作提交哈希仍可解析。
- [ ] `python3 -m unittest discover -s tests -v`、Markdown 链接检查和 `git diff --check`
      全部通过。
- [ ] 普通 push 成功，远端 `main` 指向包含双方历史的合并结果。
- [ ] 两个任务外未跟踪目录仍存在且未进入提交。

## Out of Scope

- 不在本次合并中修复 `intent.py` 与 `state.py` 的双向依赖；该问题已评审记录，但当前测试
  与公开行为均通过，应另开任务处理。
- 不变更 Agent、evaluator、公开数据、评分合同或文档内容范围。
- 不归档或修改其他活跃/规划中的 Trellis 任务。
