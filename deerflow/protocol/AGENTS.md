# SSE 输出协议

事件由 `deerflow/client.py` 的 `stream()` 产生，字段定义见 [models.py](models.py)。`stream()` 返回事件对象，HTTP 服务层将其序列化为 SSE；本文只介绍输出结构。

## 公共结构

SSE 帧可采用以下格式，以空行结束，`data` 是完整事件 JSON：

```text
event: answer_content
data: {"type":"answer_content","agent_id":"main","data":{"id":"msg_1","content":"你好"},"thread_id":"thread_1","seq":1}

```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | string | 事件类型，见下文 12 类事件。 |
| `agent_id` | string | 智能体身份；主智能体为 `main`，普通子任务为父级 `tool_call_id`。 |
| `data` | object / array | 事件载荷；只有 `tool_chunk`、`tool_calls` 为数组。 |
| `thread_id` | string | 所属会话，主子智能体共享。 |
| `seq` | integer | 本轮发布序号，从 1 递增，主子智能体共用；新一轮重新计数。 |

以上字段均必填，身份字符串非空。下文未标注“可选”的字段均必填；可选字段默认 `null`，有其他默认值时单独注明。字段缺失、`null` 和零含义不同。除元数据、Token 扩展明细及工具原始内容外，协议结构不接受未知字段。

## 1. 思考块 `reasoning_content`

模型返回的思考文本增量，不保证每次调用都有。

| `data` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | AI 消息 ID，非空。 |
| `content` | string | 新增思考文本，按同一 `(agent_id, id)` 追加。 |
| `additional_kwargs` | object / null | 可选，扩展元数据，见末尾说明。 |

## 2. 回答块 `answer_content`

AI 正文增量，字段与思考块相同：`id`、`content`、可选的 `additional_kwargs`。

按 `(agent_id, id, type)` 分别追加文本。完整快照只补发新增后缀，不重复输出全文。同轮可能有多条 AI 消息，子智能体正文也不能直接拼入主回答。错误元数据变化时，可能出现 `content=""` 的事件，仍需处理 `additional_kwargs`。

## 3. 标题块 `title`

完整会话标题，用于替换当前标题，不做增量拼接。

| `data` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 标题模型消息 ID；本地回退或历史标题使用独立生成的 ID。 |
| `content` | string | 完整标题。 |

当前属于 `main`。模型生成的标题与对应 `token_usage` 共用消息 ID，用量计入主 `end`；本地标题没有模型用量。标题不混入回答正文。

## 4. 追问块 `suggestions`

最终回答后的完整推荐问题列表。

| `data` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 追问模型消息 ID，与对应 `token_usage` 相同。 |
| `questions` | string[] | 1–5 条，每条 1–500 字符；去除首尾空白后不得为空或重复。实际数量和长度还受运行配置限制。 |

当前属于 `main`。成功时尾部顺序为 `suggestions → 对应 token_usage → 主 end`；没有来源用量时省略中间事件。追问关闭或生成失败时不发送此块，已返回的模型用量仍可计入汇总。

## 5. 工具分片块 `tool_chunk`

工具调用生成过程中的名称或参数片段。`data` 是数组，每项字段如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `index` | integer | 当前 AI 消息内的工具位置，`>= 0`。 |
| `id` | string | 发起调用的 AI 消息 ID。 |
| `tool_call_id` | string | 工具执行 ID，与工具结果关联。 |
| `type` | string | 可选，默认且仅允许 `function`。 |
| `function` | object | 包含下面两个字段。 |
| `function.name` | string | 工具名称片段，可为空。 |
| `function.arguments` | string | 参数字符串片段，可为空，尚不要求是完整 JSON。 |

名称和参数不能同时非空。按 `(agent_id, id, index)` 分别累积名称与参数；`index` 只在当前 AI 消息内有效。缺少工具 ID 的来源片段会先缓冲，不猜测归属。

## 6. 工具汇总块 `tool_calls`

完整工具调用集合。`data` 同样为数组，包含 `index`、`id`、`tool_call_id`、`type`、`function.name`、`function.arguments`，类型和必填性同工具分片，但：

- `function.name` 必须非空，表示完整工具名称。
- `function.arguments` 必须是可解析的 JSON **字符串**，例如 `"{\"query\":\"实验方案\"}"`，不是直接嵌套的对象。

可能没有前置分片，直接出现汇总。汇总用于更新完整调用记录，不能继续追加到已累积参数后。相同集合去重，集合变化时可能重新发布。并行工具共用 AI 消息 `id`，各自拥有不同 `tool_call_id`。

## 7. 工具结果块 `tool_result`

一次工具执行的返回消息。

| `data` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 工具结果消息自己的 ID，不是 AI 消息 ID。 |
| `content` | string / array | 原始结果；数组项可为字符串或对象，其内部结构由工具定义。 |
| `tool_call_id` | string | 对应工具执行 ID。 |
| `name` | string / null | 可选，工具名称。 |
| `status` | string | 可选，默认 `success`；常见 `success`、`error`，协议不限制为这两个值。 |
| `artifact` | any | 可选，原生附加结果，结构由工具定义。 |
| `additional_kwargs` | object / null | 可选，扩展元数据。 |

`present_files` 的 `content` 为对象数组，每项包含 `path`（`/mnt/user-data/outputs/` 下的容器绝对路径）和 `msg`。每个文件独立报告成功或 `Error: ...`，失败项不影响成功项。

按 `(agent_id, tool_call_id)` 匹配调用并去重。工具调用和结果本身不额外产生模型用量；结果被下次模型读取时，计入下一条 AI 消息的输入量。

## 8. 用量块 `token_usage`

一条 AI 消息对应的模型调用用量，按 `(agent_id, id)` 归并后发布一次。

| `data` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | AI 消息 ID，也可能是标题或追问模型消息 ID。 |
| `usage_metadata` | object | Token 用量，见下表。 |
| `model_name` | string / null | 可选，绑定的配置模型名或供应商模型名。 |
| `cost` | object / null | 可选，费用，见“费用结构”。 |

### Token 用量结构

| `usage_metadata` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `input_tokens` | integer | 输入总量，包含缓存命中部分。 |
| `output_tokens` | integer | 输出总量，包含思考部分。 |
| `total_tokens` | integer | 来源报告的总量，通常为输入加输出。 |
| `input_token_details` | object / null | 可选，输入分类明细。 |
| `output_token_details` | object / null | 可选，输出分类明细。 |

两类明细的字段均可选，类型为 `integer / null`，默认 `null`：

| 明细对象 | 字段 | 含义 |
| --- | --- | --- |
| `input_token_details` | `cache_read` | 缓存命中输入量，计价使用此字段。 |
| `input_token_details` | `cache_creation` | 创建缓存的 Token 量。 |
| `input_token_details` | `audio`、`image`、`text` | 音频、图像、文本输入量。 |
| `output_token_details` | `reasoning` | 思考 Token 量。 |
| `output_token_details` | `audio`、`image`、`text` | 音频、图像、文本输出量。 |
| 两者均支持 | 供应商扩展字段 | 额外整数计数或 `null`。 |

明细可能不完整或重叠，不应再次加到总量上。模型不强制总量等式或非负约束；内部支持有符号分片修正，计价另行检查数量合法性。缺失用量不估算；没有完整快照时，可延迟到智能体结束前发布。一个 AI 消息发起多个工具仍只计一份模型用量。

## 9. 压缩开始块 `context_compaction_start`

表示开始尝试压缩上下文。

| `data` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 本次压缩 ID，与结束块关联。 |
| `input_tokens` | integer | 压缩前上下文估算，`>= 0`，不用于计费。 |
| `compacted_message_count` | integer | 选中待压缩消息数，`>= 0`。 |
| `kept_message_count` | integer | 选中保留消息数，`>= 0`。 |

## 10. 压缩结束块 `context_compaction_end`

压缩结果直接放在结束块中。

| `data` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 本次压缩 ID。 |
| `status` | string | 仅允许 `completed`、`failed`、`skipped`。 |
| `reason` | string / null | 可选；失败或跳过时必须非空。 |
| `summary_text` | string / null | 可选；成功时必须有非空摘要，失败或跳过时必须为空或省略。 |
| `input_tokens` | integer / null | 可选，压缩前估算；有值时 `>= 0`。 |
| `compacted_message_count` | integer | 可选，默认 `0`，`>= 0`。 |
| `kept_message_count` | integer | 可选，默认 `0`，`>= 0`。 |
| `llm_call_skipped` | boolean | 可选，默认 `false`；复用摘要而未调用模型时为 `true`，仍可是 `completed`。 |

常见原因：`summary_generation_failed` 或异常类名表示失败；`no_compactable_messages`、`retry_cooldown`、`input_budget_exceeded`、`chunk_limit_exceeded` 表示跳过。

按 `(agent_id, id)` 关联开始和结束。未达到阈值不发事件；没有可压缩消息时可只有结束块。失败或跳过中的计数不代表已删除历史消息。

## 11. 上下文使用块 `context_used`

模型调用返回后，报告实际组装的上下文，因此可能晚于回答增量到达。

| `data` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 观测事件 ID，不是 AI 消息 ID。 |
| `contexts` | object[] | 至少一项，各项结构如下。 |
| `contexts[].source` | string | 仅允许下表四种来源。 |
| `contexts[].content` | string | 非空的实际渲染文本。 |
| `contexts[].content_sha256` | string | 文本 UTF-8 字节的 SHA-256，64 位小写十六进制。 |

| `source` | 含义 |
| --- | --- |
| `memory` | 本次注入的记忆，不是完整记忆库或记忆写入通知。 |
| `project` | 项目背景。 |
| `documents` | 文档索引，不代表已读取文档全文。 |
| `durable_context` | 摘要、任务延续等持久上下文。 |

按本轮 `(agent_id, source)` 的最新内容指纹去重，变化后重新发送；每个事件可能只包含变化的来源。

## 12. 结束块 `end`

结束一个智能体或整轮，并提供累计用量。

| `data` 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 主智能体为本轮 run ID；普通子任务为父级工具调用 ID。 |
| `usage_metadata` | object | 汇总用量，字段与 `token_usage` 相同，包含可选明细。 |
| `model_name` | string / null | 可选；当前结束事件不设置，汇总可能涉及多个模型。 |
| `cost` | object / null | 可选，汇总费用。 |
| `status` | string / null | 可选，子智能体终态；当前主结束块不设置。 |

子任务常见状态为 `completed`、`failed`、`cancelled`、`timed_out`；缺少终态时为 `unknown`，不代表成功。协议允许其他状态字符串。

**只有 `type="end"` 且 `agent_id="main"` 表示整轮结束。** 主汇总已包含子智能体及已观测的标题、追问用量，不要再加子 `end` 或逐消息用量。子任务累计统计可覆盖步骤合计；费用按各模型分别计算后求和。异常、中断或提前关闭不保证收到结束块。

## 费用结构

`token_usage.data.cost` 与 `end.data.cost` 共用以下结构。金额在 JSON 中为非负、有限的十进制字符串，单位元。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `currency` | string | 可选，默认且仅允许 `CNY`。 |
| `status` | string | 必填，`priced` 或 `unpriced`。 |
| `reason` | string / null | 可选；`unpriced` 时必须说明原因。 |
| `price` | object / null | 可选，单价快照；汇总可为空。 |
| `input_normal_cost` | decimal string / null | 可选，普通输入费用。 |
| `input_cache_hit_cost` | decimal string / null | 可选，缓存命中输入费用。 |
| `output_cost` | decimal string / null | 可选，输出费用。 |
| `total_cost` | decimal string / null | 可选，三项之和。 |

`priced` 要求四个金额完整且总额等于分项之和；`unpriced` 要求金额全为空。未知费用不能当作零。

`price` 非空时包含三个必填的十进制字符串，单位均为元／百万 Token：

| 字段 | 含义 |
| --- | --- |
| `price_input_normal` | 普通输入单价。 |
| `price_input_cache_hit` | 缓存命中输入单价。 |
| `price_output_normal` | 输出单价。 |

```text
input_normal_cost    = (input_tokens - cache_read) × price_input_normal / 1000000
input_cache_hit_cost = cache_read × price_input_cache_hit / 1000000
output_cost          = output_tokens × price_output_normal / 1000000
total_cost           = 三项费用之和
```

缓存缺失且两种输入单价不同时，非零输入无法准确计价；思考 Token 不重复收费。当前没有独立的缓存写入计价档。

| `reason` 常见值 | 含义 |
| --- | --- |
| `missing_model` | 无法确定模型。 |
| `missing_price` | 缺少可用单价。 |
| `missing_cache_usage` | 缺少计价所需缓存量。 |
| `invalid_usage` | 输入或输出数量非法。 |
| `invalid_cache_usage` | 缓存量非法，例如超过输入总量。 |
| `incomplete_usage` | 缺少消息用量，或子任务累计量与可观察步骤不一致。 |
| `incomplete_pricing` | 汇总中存在无法计价项。 |

## 扩展元数据与关联规则

`additional_kwargs` 是开放对象，字段由来源决定。常见字段：

| 字段 | 常见类型 | 含义 |
| --- | --- | --- |
| `deerflow_error_fallback` | boolean | AI 错误回退标志。 |
| `error_type`、`error_reason`、`error_detail` | string | 错误类别、原因、详情，可能只提供部分。 |
| `subagent_token_usage` | object | 父工具结果携带的子任务累计用量。 |
| `subagent_status` | string | 子任务终态。 |
| `subagent_token_usage_attributed` | boolean | 子用量已回写父消息快照；解析器据此避免重复计量。 |

AI 内容只在错误回退元数据变化时附带相关扩展信息；工具结果保留非空元数据。工具参数、结果对象和 artifact 的内部字段由具体工具定义。

- 思考、正文、工具请求和用量通过 `(agent_id, AI 消息 id)` 关联；工具请求与结果通过 `(agent_id, tool_call_id)` 关联。
- 普通子任务的 `agent_id` 等于父工具调用 ID；子 `end.data.id` 也使用该 ID。子事件目前通过轮询转发完整步骤，可能延迟，不保证逐 Token 输出；后台批任务不进入当前流。
- 不要求每轮出现全部事件，也不能依赖相邻事件关联身份；并行工具、上下文和子任务事件可交错。
- SSE 网络分片不等于完整事件。协议没有定义 `[DONE]`、独立 `error` 类型或断点续传；`seq` 不能作为跨轮恢复游标。
