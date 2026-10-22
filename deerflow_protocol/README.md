# DeerFlow 客户端流协议

公共入口统一为 `deerflow_protocol`。该目录只依赖 Pydantic 2，不导入 `deerflow`、LangChain、LangGraph、配置、模型服务或沙箱。可将整个目录单独复用到下游项目；安装 Harness 时也会一并提供该包。

十二类事件都有具体模型：`ReasoningContentEvent`、`AnswerContentEvent`、`TitleEvent`、`SuggestionsEvent`、`ToolChunkEvent`、`ToolCallsEvent`、`ToolResultEvent`、`TokenUsageEvent`、`ContextCompactionStartEvent`、`ContextCompactionEndEvent`、`ContextUsedEvent`、`EndEvent`。公共基类是 `StreamEvent`，判别联合类型是 `ClientStreamEvent`。

```python
from deerflow_protocol import ToolCallsEvent, TokenUsageEvent, parse_stream_event

# 接收 JSON 字符串、bytes 或字典，按 type 恢复具体事件与嵌套数据模型。
event = parse_stream_event(received_json)
if isinstance(event, ToolCallsEvent):
    for call in event.data:
        print(call.id, call.tool_call_id, call.function.name)
elif isinstance(event, TokenUsageEvent):
    print(event.data.id, event.data.usage_metadata.total_tokens)
```

`DeerFlowClient.stream()` 直接产出上述模型实例，使用属性访问：

```python
for event in client.stream("你好", thread_id="thread_001"):
    print(event.type, event.agent_id, event.seq)
    payload = event.model_dump(mode="json", exclude_unset=True)
    json_line = event.model_dump_json(exclude_unset=True)
```

`exclude_unset=True` 保留原协议的稀疏字段：未提供的可选字段不额外写成 null，来源显式提供的 null 仍会保留。不要直接 `json.dumps(event)` 或使用 `dict(event)` 代替嵌套序列化。

思考、正文、工具请求及用量通过 `(agent_id, id)` 关联；工具请求与结果通过 `(agent_id, tool_call_id)` 关联。同一 AI 消息发起的并行工具共用一次 `token_usage`，不同 AI 消息发起的串行工具分别记账。`tool_result.id` 是工具结果自身的消息 ID。子智能体 `end` 只结束对应 agent，主智能体 `end` 才结束本轮。

LLM 生成的标题与对应的 `token_usage` 共用标题模型的消息 ID，使用 `agent_id="main"`，其用量及费用计入本轮主 `end` 汇总。标题 ID 与外层会话 `thread_id` 分别表示消息和会话。本地标题及历史快照使用独立生成的标题 ID，不产生模型用量；供应商未返回用量时不虚构统计。

推荐追问统一由 `config.yaml` 中的 `suggestions.enabled` 控制，`stream()` 无需传入启用参数，请求参数不能覆盖配置。成功时尾部按 `suggestions → 对应的 token_usage → 主 end` 顺序发布（供应商未返回用量时省略对应的 `token_usage`）。`SuggestionsEvent` 的 `data` 是 `SuggestionsData(id=模型消息ID, questions=[问题列表])`，默认包含 3 条追问。对应的 `token_usage` 共用该 ID，按追问模型计费并纳入主 `end`。生成失败或格式不符合配置时跳过建议块；已返回的模型用量仍保留。追问不写入对话历史。

协议结构字段均有明确模型，`additional_kwargs` 是允许扩展字段的 `MessageMetadata`。工具原始 `content`、`artifact` 以及供应商扩展元数据的内部结构由来源定义，保留原始载荷，不强行转换或删减。

上下文压缩通过 `context_compaction_start` / `context_compaction_end` 表示，同一次尝试共享 `data.id`。开始携带输入 Token 估算、待压缩与保留消息数量。结束的 `status` 为 `completed`、`failed` 或 `skipped`；成功携带 `summary_text`，失败/跳过携带 `reason`，不发送未生效摘要。尚未达到触发阈值不产生事件；达到阈值但没有可压缩消息时只产生 skipped 结束事件。开始之后触发冷却、输入预算或分块上限时仍有对应结束事件。缓存复用仍会完成状态压缩，使用 `completed` + `llm_call_skipped=true`，不冒充跳过整个压缩。消息数量描述本次选中的分区，不代表失败时发生了删除；`input_tokens` 是压缩前上下文估算，不是计费用量。

`context_used` 在带上下文的模型调用返回后发送，`data.contexts` 列出实际组装的内容。每项含 `source`（`memory`、`project`、`documents`、`durable_context`）、渲染后的 `content` 和 `content_sha256`。`documents` 是注入的文档索引，不能据此认为模型已经读取了文档全文；`durable_context` 是本次渲染的摘要、任务延续等上下文块，遵循原有裁剪和脱敏规则。该事件不读取或导出完整记忆库，也不表示后台记忆写入。解析器按本轮、智能体和来源去重，内容改变时重新发送。由于它在调用返回后确认，可能排在该次回答增量之后。

普通 `task` 子任务通过原有轮询通道转发这三类事件，`agent_id` 为父级工具调用 ID，可能有轮询延迟；后台批任务不转发到当前客户端。客户端中断或传输终止时不保证收到结束事件。新增事件不改变摘要模型的 Token 计费覆盖范围。

其他语言可使用 [stream_event.schema.json](stream_event.schema.json)。Python 可调用 `stream_event_json_schema()` 获取同一份 JSON Schema。新增协议事件时应同时更新具体模型、判别联合和 Schema；仅新增来源解析器时复用现有事件模型即可。


`TokenUsageEvent.data` 还包含实际绑定的 `model_name` 和费用模型 `TokenCost`。例如 `event.data.cost.total_cost` 为 Decimal，JSON 中为精确十进制字符串，币种为 CNY。`ModelPrice` 保存本次元/百万 Token 单价；普通输入、缓存命中输入、输出分别计价。`EndEvent.data.cost` 汇总各模型独立计算的费用。无法确定完整金额时 `cost.status="unpriced"`，`total_cost=None`，原因见 `cost.reason`；不会用零伪装未知费用。旧 JSON 未携带这些新增可选字段时仍可解析。
