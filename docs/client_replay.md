# 同步客户端最近一轮重新生成

实现目标：为 `DeerFlowClient.stream()` 添加 `replay_last_turn=True`，替换最近一次
用户输入及其派生线程状态，继续使用原有增量事件格式。

```python
# 传原问题，重新生成答复。
for event in client.stream("原问题", thread_id=thread_id, replay_last_turn=True, user_id=user_id):
    consume(event)

# 传修改后的问题，替换最后一轮输入和答复。
for event in client.stream("修改后的问题", thread_id=thread_id, replay_last_turn=True, user_id=user_id):
    consume(event)
```

`chat()` 透传此参数。`thread_id` 必须已存在，且线程不能存在待执行节点。
`message` 仍为必填参数；系统不会通过比较问题文本来判断是否追加一轮。
省略 `files` 会继承原轮附件声明；显式传 `files=[]` 会清空本轮声明，不删除文件。

## 实现与复用

- 主逻辑外挂在 `deerflow/runtime/client_replay.py`，客户端仅增加参数及执行前接入。
- 使用 `CheckpointStateAccessor` 读取完整状态，兼容 `full` 和 `delta`。
- 使用 `build_state_mutation_graph` 及 worker 已有的完整状态替换规则，保留自定义
  middleware 通道；两种模式均在当前 head 线性写入，避免 delta 的旧分支重放污染。
- 使用原图 `update_state(None, as_node=END)` 清理失败生成遗留的待执行任务。
- 新轮次在 checkpoint metadata 中记录用户、Agent、run ID 与输入前的基线 ID。
  旧线程沿当前父链定位基线，找不到完整基线或父链异常时拒绝重放。
- 新生成使用新消息 ID 和 run ID；旧 checkpoint 保留在祖先链，不再属于当前有效消息。
- 保留用户当前目标与预算，清除旧目标达成记录。异常、取消或提前关闭流时恢复重放前状态。

## 边界

- 并发保护覆盖当前进程中的 `stream()` / `chat()`：同一客户端或同一线程的重叠生成
  抛出 `ReplayConflictError`。跨进程及其他直接写 checkpoint 的入口由宿主协调。
- 新线程校验持久化用户标记。旧线程无该标记，宿主仍必须完成线程归属鉴权；不要直接
  信任外部请求传入的 `user_id`。Agent 标记不匹配时拒绝重放。
- 只恢复线程状态，不撤销已提取的长期记忆、上传文件、产物或外部工具副作用。
  工具可能重新执行，宿主需要按业务要求提供幂等性。
- 流消费方负责将新结果展示为最后一轮的替换版本，并在收到主 `error` 或迭代抛出异常时恢复旧展示；提前结束消费
  必须调用生成器的 `close()`。正常消费到 `end` 后即视为提交完成。
- 池的 `start_stream()` 使用托管执行：图完整结束即提交重生成，避免消费端在交付
  `end` 前断连而撤销已完成结果；图未完成时关闭仍恢复原状态。它会在执行与同步节点
  排空后，清理普通中断轮次的待执行调度，允许重新生成已经落盘的最近一轮。
  尚未保存输入的轮次不能重生成，应重新提交普通对话。原同步 `stream()` 保持原规则，
  不会自动修复过去遗留的未完成轮次。跨线程关闭及超时说明见
  [客户端池](../deerflow/AGENT_AGENTS.md)。
- checkpoint 已记录生成过程，进程被强杀时无法执行内存中的恢复逻辑；宿主如需跨进程
  崩溃恢复，应使用持久化运行管理。

## 验证

`tests/test_client_replay.py` 使用真实 LangGraph 图、Agent 及内存/SQLite checkpointer，
不调用真实模型。验证轮次替换、连续重生成、摘要/注入边界、扩展状态、附件、失败恢复、
并发冲突、身份隔离及 SQLite 重新打开后的重放。PostgreSQL 与生产模型需要部署侧验证。

未处理的运行异常在清理及状态恢复尝试结束后返回终止 `error` 块，不补发主 `end`；恢复本身失败会记录日志，因此错误块不保证持久化状态已恢复。主 `end` 只在必要清理成功后发布。已定义的重放条件、并发及线程归属拒绝仍抛出异常。
