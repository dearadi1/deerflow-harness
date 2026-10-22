客户端 `client.stream()` 返回 `deerflow_protocol` 中的事件模型。以下 JSON 表示模型序列化后的协议结构：

上下文事件

新增 `context_compaction_start`、`context_compaction_end`、`context_used`，协议共 12 类主类型。压缩不另设结果事件，成功结果放在结束事件中：

```json
{
  "type": "context_compaction_start",
  "agent_id": "main",
  "data": {"id": "compaction_001", "input_tokens": 12000, "compacted_message_count": 18, "kept_message_count": 6},
  "thread_id": "thread_001",
  "seq": 1
}
```

```json
{
  "type": "context_compaction_end",
  "agent_id": "main",
  "data": {"id": "compaction_001", "status": "completed", "summary_text": "已确认目标、约束及待办事项。", "input_tokens": 12000, "compacted_message_count": 18, "kept_message_count": 6, "llm_call_skipped": false},
  "thread_id": "thread_001",
  "seq": 2
}
```

结束事件的 `status`：

| 值 | 语义 | 其他字段 |
|---|---|---|
| `completed` | 已生成可用于状态替换的压缩结果 | `summary_text`；缓存复用时 `llm_call_skipped=true` |
| `failed` | 生成失败或执行异常 | `reason=summary_generation_failed`，或异常类名；无摘要 |
| `skipped` | 已触发评估，但本次没有执行完压缩 | `reason`；无摘要 |

跳过原因包括 `no_compactable_messages`、`retry_cooldown`、`input_budget_exceeded`、`chunk_limit_exceeded`。未到阈值时保持安静；没有可压缩消息时允许只有结束事件。开始与结束按 `(agent_id, data.id)` 关联。数量为选中分区的统计，失败和跳过不会据此删除历史。`input_tokens` 是估算，不参与费用累计。

`context_used.data` 为 `{"id": "context_001", "contexts": [...]}`。每项是 `ContextItem(source, content, content_sha256)`，分别对应本次实际组装的记忆、项目背景、文档索引、持久上下文。它在模型调用返回后确认发送，相同来源的内容本轮不重复发布，变化后再发；不包含后台记忆新增/更新事件。主/子智能体通过 `agent_id` 区分，普通子任务使用现有轮询转发，后台批任务不在此流中。完整语义见 [协议说明](../deerflow_protocol/README.md)。

思考块
```json
{
    "type": "reasoning_content",
    "agent_id": "main", // 主agent/子agent  主agent默认为：main，子agent由主agent创建
    "data": {
        "id": "xxxx",
        "content": ""
    },
    "thread_id": "xxxx",
    "seq": 1 // 请使用产生该数据时的真实seq号
}
```
回答块
```json
{
    "type": "answer_content",
    "agent_id": "main",
    "data": {
        "id": "xxxx",
        "content": ""
    },
    "thread_id": "xxxx",
    "seq": 1
}
```
标题块

```json
{
    "type": "title",
    "agent_id": "main",
    "data": {
        "id": "title_model_message_id",
        "content": "季度研发进展"
    },
    "thread_id": "xxxx",
    "seq": 1
}
```

LLM 标题的 `data.id` 使用标题模型响应的消息 ID（缺失时生成独立 ID），与该调用的 `token_usage.data.id` 相同；外层 `thread_id` 仍表示所属会话。本地回退和历史标题快照使用独立生成的标题 ID，不产生模型用量。

推荐追问块（统一由 `config.yaml` 的 `suggestions.enabled` 控制，默认启用）

```json
{
    "type": "suggestions",
    "agent_id": "main",
    "data": {
        "id": "suggestions_model_message_id",
        "questions": ["下一步应优先开展哪些实验？", "如何验证实验结果？", "有哪些需要提前控制的风险？"]
    },
    "thread_id": "xxxx",
    "seq": 12
}
```

新建的 `FollowUpSuggestionsMiddleware` 在 `after_agent` / `aafter_agent` 中读取本轮用户输入和最终回答，调用模型生成追问。`suggestions` 一次返回完整列表，尾部顺序为 `suggestions → 对应的 token_usage → 主 agent 的 end`（供应商未返回用量时省略对应的 `token_usage`）；其 `data.id` 与对应的 `token_usage.data.id` 相同。追问模型用量和费用计入主 `end`，追问文本不会混入回答，也不写入对话历史。

```python
for event in client.stream("帮我制定研发计划"):
    if event.type == "suggestions":
        print(event.data.questions)
```

`config.yaml` 的 `suggestions` 区块控制配置，示例见 `config.example.yaml`：

```yaml
suggestions:
  enabled: true
  max_suggestions: 3
  max_chars: 100
  model_name: null
```

`enabled` 是服务统一开关，`stream()` 无需传入启用参数，请求参数不能覆盖此配置。设为 `false` 时不调用追问模型，也不产生追问块及其用量。`max_suggestions` 默认 3，可配置 1–5；`max_chars` 限制每条追问长度。`model_name` 指定 `models` 中的配置名称，留空时使用本轮主模型。模型调用失败或输出数量、长度、格式不符合配置时，不发送建议块，主回答仍正常结束；模型已返回的用量仍计入汇总。配置变更后，嵌入式客户端需重新创建（传入 `config_path` 可显式重新加载）；持有旧客户端实例的常驻服务需重建实例或重启。

工具块
```json
{
    "type": "tool_chunk",
    "agent_id": "main",
    "data": [
        {
            "index": 0,
            "id": "ai_message_id", // 对应 token_usage.data.id
            "tool_call_id": "tool_call_id", // 对应 tool_result.data.tool_call_id
            "type": "function",
            "function": {
                "arguments": "",
                "name": "web_search"
                // 特殊的：同一帧中name与arguments不会同时有值
            }
        }
    ],
    "thread_id": "xxxx",
    "seq": 1
}
```

工具汇总块
```json
{
    "type": "tool_calls",
    "agent_id": "main",
    "data": [
        {
            "index": 0, // 当出现单次思考多次调用工具时，此字段才会增加
            "id": "ai_message_id", // 对应 token_usage.data.id
            "tool_call_id": "tool_call_id", // 对应 tool_result.data.tool_call_id
            "type": "function",
            "function": {
                "name": "web_search", // 使用的工具名称
                "arguments": "{\"query\": \"腾讯股票预测\"}" // arguments 必须是 JSON 字符串
            }
        },
        // 当出现工具并行时，会有额外的tool_call
    ],
    "thread_id": "xxxx",
    "seq": 1,
}
```

工具结果块
```json
{
    "type": "tool_result",
    "agent_id": "main",
    "data": {
        "id": "xxxx", // 工具结果消息 ID，不是发起调用的 AI 消息 ID
        "content": "xxxx", // 工具返回的内容
        "tool_call_id": "xxxx" // 本轮工具对应的id
    },
    "thread_id": "xxxx",
    "seq": 1,
}
```

token使用统计块
```json
{
    "type": "token_usage",
    "agent_id": "main",
    "data": {
        "id": "ai_message_id",
        "model_name": "doubao-seed-2-1-turbo-260628",
        "usage_metadata": {
            "input_tokens": 10085,
            "output_tokens": 174,
            "total_tokens": 10259,
            "input_token_details": {"cache_read": 9016},
            "output_token_details": {"reasoning": 0}
        },
        "cost": {
            "currency": "CNY",
            "status": "priced",
            "price": {
                "price_input_normal": "12.0",
                "price_output_normal": "36.0",
                "price_input_cache_hit": "1.5"
            },
            "input_normal_cost": "0.012828",
            "input_cache_hit_cost": "0.013524",
            "output_cost": "0.006264",
            "total_cost": "0.032616"
        }
    },
    "thread_id": "xxxx",
    "seq": 1
}
```

全部结束块
```json
{
    "type": "end",
    "agent_id": "main", // 这里需要单独注意不同agent的结束
    "data": {
        "id": "xxxx",
        "usage_metadata": { // 总token使用统计
            "input_tokens": 26534,
            "output_tokens": 127,
            "total_tokens": 26661
        }
    },
    "thread_id": "xxxx",
    "seq": 1,
}
```

## 当前实现约定

- `DeerFlowClient.stream()` 直接返回独立协议包 `deerflow_protocol` 的具体事件模型；消费方通过 `event.type`、`event.data` 读取。事件封装、载荷、工具条目及用量明细均已建模。使用 `event.model_dump(mode="json", exclude_unset=True)` 或 `event.model_dump_json(exclude_unset=True)` 序列化时保留本页 JSON 结构。`messages`、`values`、`custom` 均开启。旧的 `values` / `messages-tuple` / `custom` 包装不再对外输出。
- `seq` 在输出事件实际生成时递增，从 1 开始，主智能体与子智能体共用本轮的同一计数器。LangGraph 这些来源没有统一的原生 `seq`，因此这是客户端本轮的真实发布序号，不是模型执行序号，也不代表并行调用的因果顺序；不会用 `langgraph_step` 代替。
- 主智能体 `agent_id="main"`；当前 `task_*` 来源中，子智能体 `agent_id` 直接使用父智能体发起 `task` 的 `tool_call_id`。因此父工具请求、子智能体输出、子智能体结束及父工具结果可以用同一个 ID 关联。相同子智能体类型的多次执行仍拥有不同 ID。
- 思考、回答、用量使用原始 AI 消息 ID；首片缺少消息 ID 时，按调用上下文建立稳定替代 ID，后续原生 ID 与快照继续绑定该身份；工具分片和工具汇总中的 `id` 同样使用 AI 消息 ID，与 `token_usage.data.id` 绑定；原工具调用 ID 统一放到 `tool_call_id`，与工具结果的 `tool_call_id` 对应。工具 `index` 仅在一条 AI 消息内有效，新消息可从 0 开始。归并状态按 agent、消息 ID、index 隔离，不按工具名称匹配。
- `tool_chunk` 为增量，名称和参数分开发布；后续分片缺少工具调用 ID 时，按当前消息的 index 补齐 `tool_call_id`。工具调用 ID 尚未出现的分片先缓冲，完整消息可用于恢复 ID。无从安全绑定的分片不会猜测归属。`tool_calls` 输出已完整且可解析的 JSON 参数字符串，快照不重复发布同一组调用。
- 同一 `(agent_id, AI消息ID)` 只输出一次 `token_usage`。供应商的用量分片先内部归并（支持有符号修正），完整消息补足差额后一次发布；未收到完整快照时，在对应智能体结束前发布已采集的用量。工具调用汇总和工具执行结果不单独计量。保留来源提供的缓存、思考等嵌套明细，缺失明细不估算。
- 同步 `stream()` 与异步图均支持 `title.model_name`。标题模型通过 `custom` 来源发布独立的标题消息及用量，不写入对话 `messages`，不混入 `answer_content`。标题的 `token_usage` 使用标题模型自身的配置计价，并计入主智能体 `end` 的用量及费用汇总。模型返回空标题时仍保留实际花费的用量，标题正文采用本地回退；供应商不返回用量时不虚构统计。
- 子智能体 `end` 只结束对应的 agent，`data.id` 为父工具调用 ID；主智能体 `end` 才结束本轮，`data.id` 为 run ID，其用量是主智能体与各子智能体之和。子智能体生命周期报告的累计用量覆盖同一子智能体的步骤合计，避免双计；未提供的调用或明细无法推算。异常、取消及超时以子智能体结束块的 `status` 区分。仅观察到步骤而没有收到终态的来源，在主流正常结束时标为 `unknown`，不伪造成功。
- 当前子智能体来自 `task_running.message`，以完整步骤输出，不提供逐 token 的子智能体流。子智能体的调用、结果、思考、正文及用量仍按同一协议解析，同一消息 ID 在不同 agent 下分别绑定，避免跨智能体冲突。规划模式的 `write_todos` 作为普通工具处理。
- `values` 用于补齐完整调用、标题及未流式输出的消息；历史消息不重新输出或记账。正文只追加快照相对已输出文本的后缀，避免把完整快照重放一次。工具结果保留原始内容以及非空 `artifact`；错误消息保留 `additional_kwargs` 供 CLI 判断失败。
- 解析入口在 `deerflow/client_stream.py` 的 `ClientStreamParser`。后续数据源可通过 `register_source(mode, handler)` 注册；未知 mode 不会误作 `values`。新处理器应使用 `event()` 构造输出，以共享 seq，并保持上述 ID 和用量约定。

真实运行核对时需注意：`TokenUsageMiddleware` 会把子任务累计用量回写到派发 `task` 的主 AI 消息，并给对应工具结果标记 `subagent_token_usage_attributed=true`。协议中的逐消息 `token_usage` 保留该 AI 模型调用自身的用量，子智能体另外统计；对照最终线程快照时，需要按调用 ID 扣除已归属的子任务部分，不能再将快照总数与子任务数重复相加。


### 消息身份与工具身份

`tool_chunk`、`tool_calls` 每个条目包含两种身份：`id` 为发起工具调用的 AI 消息 ID，`tool_call_id` 为一次工具执行的 ID。思考、回答、工具请求和用量通过 `(agent_id, id)` 关联；工具分片、汇总及结果通过 `(agent_id, tool_call_id)` 关联。`tool_result.id` 仍是工具结果消息自身的 ID，不能用于匹配 AI 用量。

同一条 AI 消息并行请求多个工具时，各条目的 `id` 相同、`tool_call_id` 不同，合并为一次 `token_usage`；不能按工具数量重复累计，也无法从模型总量准确拆出每个工具的用量。串行工具由多条 AI 消息发起时，每个不同的 AI 消息 ID 分别输出一次 `token_usage`，各自记账；不能把整轮串行调用合并成一条用量。只有完整调用的子智能体步骤也保留消息 `id`，以免缺少分片时无法关联用量。

`tool_calls` 是调用汇总、`tool_result` 是执行结果，它们本身不额外产生模型用量；相邻的 `token_usage` 统计的是原 AI 模型调用，消费方应按身份绑定，不能靠事件相邻或到达顺序判断。工具结果被下一次模型读取时，计入下一条 AI 消息的输入用量。来源未提供用量时不会虚构统计。

本次字段迁移：旧 `tool_chunk[].id` / `tool_calls[].id` 的工具调用身份迁入 `tool_call_id`，新的 `id` 表示 AI 消息身份。旧消费方需要修改工具去重、参数归并和结果匹配的键；已有历史 JSONL 保留原样，验收脚本兼容旧字段。


### 独立模型入口

所有公共协议模型统一从 `deerflow_protocol` 导入，与 Harness 内部模型和运行时解耦：

```python
from deerflow_protocol import ToolCallsEvent, TokenUsageEvent, parse_stream_event

# 下游接收 JSON 后按 type 自动恢复为具体模型。
event = parse_stream_event(received_json)
if isinstance(event, ToolCallsEvent):
    for call in event.data:
        print(call.id, call.tool_call_id, call.function.name)
elif isinstance(event, TokenUsageEvent):
    print(event.data.id, event.data.usage_metadata.total_tokens)
```

公共基类为 `StreamEvent`，带 `type` 判别字段的联合类型为 `ClientStreamEvent`，完整类型与使用说明见 [协议包说明](../deerflow_protocol/README.md)。该包仅依赖 Pydantic 和标准库，不导入 `deerflow`。非 Python 下游可使用 [JSON Schema](../deerflow_protocol/stream_event.schema.json)。工具原始内容、artifact 和供应商扩展元数据内部结构继续保留原值。


### 费用计算

使用 `models[].price` 的三个单价，币种固定为人民币 CNY，单位均为元/百万 Token：

`client.list_models()` 返回的每个模型也包含 `price`，三个单价同样使用精确十进制字符串，例如 `{"price_input_normal": "12.0", "price_output_normal": "36.0", "price_input_cache_hit": "1.5"}`。配置为零时返回 `"0"`（或保留配置中的小数位），未配置整个价格块时返回 `price: null`。

```text
input_normal_cost    = (input_tokens - cache_read) × price_input_normal / 1000000
input_cache_hit_cost = cache_read × price_input_cache_hit / 1000000
output_cost          = output_tokens × price_output_normal / 1000000
total_cost           = input_normal_cost + input_cache_hit_cost + output_cost
```

`cache_read` 属于 `input_tokens`，先扣除后分别计费；思考 Token 已包含在 `output_tokens`，不另外加一次。所有金额和单价以 Decimal 计算，JSON 中输出精确十进制字符串，不舍入到分，也不经过 float 累加。

每条 `token_usage.data` 增加 `model_name` 和 `cost`，`data.id` 仍绑定同一 AI 消息。`cost` 的公共模型为 `deerflow_protocol.TokenCost`，其单价快照模型为 `ModelPrice`。配置名称与服务商模型名称可对应；同名服务商模型存在多个价格档案且无法确定实际配置时，不猜测单价。主智能体沿用当前有效配置，子智能体从 task 生命周期及消息来源绑定自己的模型。价格在本轮开始时从客户端配置建立快照，不受消费端修改返回对象影响。

`end.data.cost` 使用相同费用模型汇总。子智能体按各自模型的逐消息费用求和；主智能体结束块再加总主、子智能体，不能拿总 Token 乘以主模型单价。已回写到父消息的子用量会在客户端快照解析时扣除，图的原始状态保持不变。

无法完整计价时输出 `status="unpriced"`，金额字段为 `null`，并提供 `reason`：`missing_model`、`missing_price`、`missing_cache_usage`、`invalid_usage`、`invalid_cache_usage`、`incomplete_usage` 或汇总时的 `incomplete_pricing`。缺少缓存明细且普通输入与缓存命中单价不同时，不将未知缓存量冒充为零。缺少部分消息用量、子任务结束统计超过可观测步骤、或任一消息无法计价时，不把已知部分伪装成完整总价。`status="priced"` 且金额为零才表示确实计算为零。

费用是按配置单价和实际返回的 Token 用量计算的金额；未提供用量的隐藏模型调用不在此协议的计价范围内。单价配置会在模型工厂中排除，不传给服务商 API，也不改变 Agent 行为指纹。
