# 模块维护约定

用户要求“同步原版”“合并上游更新”或调用上游同步提示词时，先阅读并执行根目录 [SYNC_UPSTREAM_PROMPT.md](SYNC_UPSTREAM_PROMPT.md)。该流程覆盖验证、推送、PR 合并及本地同步；以用户本次明确指定的执行范围为准。

- `deerflow/` 与 `pyproject.toml` 来自上游 `backend/packages/harness`，业务定制在功能分支上进行。
- 禁止直接在 `main` / `master` 提交；通过分支提交和 PR 合入。
- 分支采用 `feat/`、`fix/`、`chore/` 等前缀，名称使用小写和下划线。
- 新增提交采用中文约定式消息，标题不超过 50 字符。保留上游原始提交消息。
- 新增注释及 docstring 使用中文，解释意图；不批量改写上游原始代码或注释。
- `chore/upstream_module` 只接收固定参数生成的拆分历史，禁止加入本地定制或强制推送。
- 同步 PR 使用 merge commit，保留上游祖先关系；禁止 squash 或 rebase 同步 PR。
- 修改同步工具时运行 `python3 -m unittest discover -s tests -p 'test_sync_upstream.py' -v`。
- 修改框架代码时阅读对应的 `deerflow/**/AGENTS.md`，补充与改动相关的验证。
- `UPSTREAM.json` 的 `initial_*` 记录初始来源，不随日常同步修改；后续来源见合并提交正文。
