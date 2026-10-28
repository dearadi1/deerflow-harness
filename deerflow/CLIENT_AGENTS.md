# DeerFlowClient.stream 输入与输出

本文依据 [deerflow/client.py](client.py) 中 `stream()`、`_stream_turn()` 及其实际调用的辅助函数，说明同步 Python 客户端的调用约定。输出字段以 [deerflow/protocol/models.py](protocol/models.py) 为准。

## 1. 方法签名与执行方式

```python
def stream(
    self,
    message: str,
    *,
    thread_id: str | None = None,
    replay_last_turn: bool = False,
    **kwargs,
) -> Generator[ClientStreamEvent, None, None]:
    ...
```

返回同步生成器，逐项产出 `deerflow.protocol.ClientStreamEvent` 联合类型中的具体事件模型。调用 `stream()` 只创建生成器；参数检查、线程占用、模型调用等发生在迭代时。使用普通 `for` 消费，方法本身不生成 HTTP 响应或 SSE 文本。

内部调用链：

```text
stream
  → stream_client_turn：参数检查、客户端/线程锁、重生成恢复
  → _stream_turn：运行配置、上下文、HumanMessage、Agent 执行
  → ClientStreamParser.parse / finish：协议事件
```

## 2. 显式输入参数

| 参数 | 类型 / 默认值 | 代码行为 |
| --- | --- | --- |
| `message` | `str`，必填 | 作为本轮 `HumanMessage.content`；重生成时作为替换后的用户输入。入口没有额外的非空检查。 |
| `thread_id` | `str \| None = None` | `None` 时生成 UUID 字符串；显式值必须为 1–64 个 ASCII 字母、数字、下划线或连字符，否则抛出 `ValueError`。空字符串不会触发自动生成。 |
| `replay_last_turn` | `bool = False` | `False` 发起普通一轮；`True` 替换最近一轮输入并重新执行。非布尔值抛出 `TypeError`。 |

`thread_id` 和 `replay_last_turn` 必须以关键字传入。自动生成的 `thread_id` 可从输出事件的同名字段取得。

复用 `thread_id` 是否能够恢复对话，取决于是否存在可用的 checkpointer。代码先使用构造客户端时传入的 `checkpointer`，为 `None` 时再调用全局 `get_checkpointer()`；两者均不可用时没有持久化的多轮状态。

## 3. kwargs 中实际读取的参数

`**kwargs` 没有统一的输入 Schema。下表中的类型表示预期用法，不表示入口对所有值进行了类型校验。只有代码明确读取的键会生效，未知键不会自动透传到 Agent。

### 3.1 本轮运行配置

| 参数 | 预期类型 | 省略时的值 | 用途 |
| --- | --- | --- | --- |
| `model_name` | `str \| None` | 客户端的 `model_name`，构造默认 `None` | 写入 `configurable.model_name`；为 `None` 且配置有模型时，先选择配置中的第一个模型，再进行模型授权处理。 |
| `thinking_enabled` | `bool` | 客户端的 `thinking_enabled`，构造默认 `True` | 写入 `configurable.thinking_enabled`，用于模型和 Agent 构建。 |
| `plan_mode` | `bool` | 客户端的 `plan_mode`，构造默认 `False` | 映射为 `configurable.is_plan_mode`。 |
| `subagent_enabled` | `bool` | 客户端的 `subagent_enabled`，构造默认 `False` | 写入 `configurable.subagent_enabled`。 |
| `recursion_limit` | `int` | `100` | 写入 `RunnableConfig.recursion_limit`，限制图执行递归步数。 |

这些覆盖值使用 `kwargs.get(key, default)` 读取：显式传入 `None` 不等于省略参数。布尔配置应传入真正的布尔值，不能把字符串 `"false"` 当成关闭开关。

### 3.2 用户与授权上下文

以下键在提供时写入 `runtime.context`：

| 参数 | 预期类型 | 用途 |
| --- | --- | --- |
| `user_id` | `str` | 本轮用户及存储身份；值为空或未提供时，使用当前用户上下文中的 ID，没有当前用户时回退为 `"default"`。 |
| `user_role` | `str` | 授权角色。授权主体构建时，缺失或空字符串使用应用配置中的默认角色。 |
| `oauth_provider` | `str` | OAuth 提供方标识。 |
| `oauth_id` | `str` | OAuth 身份标识。 |
| `channel_user_id` | `str` | 渠道用户标识。 |
| `is_internal` | `bool` | 内部身份标志；授权主体构建时仅 `is True` 视为开启。 |
| `authz_attributes` | `Mapping[str, Any] \| None` | 扩展授权属性；授权主体构建时，非 Mapping 且非 `None` 的值抛出 `TypeError`。 |

这些参数由调用方提供，`stream()` 本身没有 HTTP 登录或凭据认证流程。有效 `user_id` 也参与 Agent 缓存及存储上下文，即使授权功能未开启仍会解析。

### 3.3 文件声明

`files` 用于声明本轮文件，典型输入为：

```python
files=[{"filename": "report.pdf", "size": 12345}]
```

`_stream_turn()` 将非 `None` 的 `files` 原样放入 `HumanMessage.additional_kwargs["files"]`。它不在这里上传文件，也不校验文件是否存在、大小或声明格式；文件处理交给后续中间件。实际使用时应先准备好对应线程的上传文件。

重生成时的区别：

| 输入 | 行为 |
| --- | --- |
| 省略 `files` 或传入 `None` | 新消息没有 `files` 键，继承原轮的文件声明（若有）。 |
| `files=[]` | 显式清空本轮文件声明，不删除已落盘文件。 |
| `files=[...]` | 使用新声明替换原轮文件声明。 |

### 3.4 知识库宿主参数

`knowledge_search_kwargs` 在提供时被深拷贝至 `runtime.context` 的同名字段，例如：

```python
knowledge_search_kwargs={"kb_ids": ["kb_001"]}
```

本方法只负责传递，不校验内部字段、不设置检索默认值，也不直接执行检索。`kb_ids` 等字段的最终解释由接收该上下文的工具决定。

### 3.5 不能通过 kwargs 覆盖的内容

当前入口不会读取 `context`、`configurable`、`callbacks`、`stream_mode`、`run_id`、`deerflow_trace_id`、`agent_name`、`available_skills`、`max_concurrent_subagents`、`max_total_subagents` 或 `suggestions_enabled` 等同名 kwargs。传入它们不会产生对应覆盖效果。

- `agent_name`、`available_skills`、`checkpointer` 等属于客户端构造参数。
- `run_id` 每轮自动生成；请求 trace ID 继承当前 trace 上下文，否则自动生成。
- 底层固定订阅 `stream_mode=["messages", "values", "custom"]`，再转换为对外事件。
- 推荐追问没有本轮 kwargs 开关；是否生成由应用配置及相关中间件决定。

## 4. 最近一轮重生成

```python
events = client.stream(
    "请改为只分析成本因素",
    thread_id="thread_001",
    replay_last_turn=True,
)
```

重生成要求显式提供已有 `thread_id`、可用的 checkpointer、最近一轮用户消息以及可恢复的轮前状态。线程存在待执行节点/任务、找不到完整基线、记录的智能体与当前智能体不一致等情况会被拒绝。

代码恢复到最近一轮之前的状态，再提交新的 `message`。传原问题相当于重试，传新问题相当于修改最近一轮输入；旧轮的回答及工具结果不会作为本次有效上下文继续使用。

主智能体 `end` 事件产生时，重生成被标记为已完成。在此之前发生异常或关闭流，会尝试恢复重生成前的有效线程状态。状态恢复不撤销文件变更、长期记忆写入或外部工具副作用。

实现见 [deerflow/runtime/client_replay.py](runtime/client_replay.py)。

## 5. 输出协议

直接使用协议子包：

```python
from deerflow.protocol import ClientStreamEvent, parse_stream_event
```

| 公共字段 | 含义 |
| --- | --- |
| `type` | 事件类型。 |
| `agent_id` | 事件所属智能体，主智能体为 `"main"`。 |
| `data` | 对应事件的载荷模型；工具分片和工具汇总为模型列表。 |
| `thread_id` | 本轮使用的会话 ID。 |
| `seq` | 本轮从 1 递增的发布序号，主子智能体共用，新一轮重新计数。 |

协议定义了 12 类事件：`reasoning_content`、`answer_content`、`title`、`suggestions`、`tool_chunk`、`tool_calls`、`tool_result`、`token_usage`、`context_compaction_start`、`context_compaction_end`、`context_used`、`end`。并非每轮都会出现全部类型。

字段结构、关联规则、用量与费用说明直接引用：

- [输出协议说明](protocol/AGENTS.md)
- [Python 使用与序列化说明](protocol/README.md)
- [Pydantic 模型定义](protocol/models.py)
- [JSON Schema](protocol/stream_event.schema.json)

消费时需要注意：

- 正文按 `(agent_id, data.id)` 累积 `answer_content.data.content`；思考和正文分别累积，同轮可能有多条 AI 消息。
- 只有 `type == "end"` 且 `agent_id == "main"` 才表示本轮结束；子智能体的结束事件不能作为整轮结束信号。
- 主 `end.data.id` 是本轮 `run_id`。主结束事件的用量与费用已汇总子智能体，不要再次叠加子结束事件。
- 正常耗尽底层流后才执行 `parser.finish()`。异常和提前关闭不保证收到主 `end`，异常会向迭代调用方传播。
- 成功生成追问时，尾部顺序为 `suggestions → 对应 token_usage（有可发布用量时）→ 主 end`。

## 6. 最小消费示例

以下示例假设运行所需的应用配置和模型已经配置好。按消息 ID 保存正文，并在结束或消费异常时关闭生成器：

```python
from contextlib import closing

from deerflow.client import DeerFlowClient
from deerflow.protocol import AnswerContentEvent, EndEvent


def consume_turn(client: DeerFlowClient, message: str, **kwargs) -> dict[str, str]:
    answers: dict[str, str] = {}
    with closing(client.stream(message, **kwargs)) as events:
        for event in events:
            if isinstance(event, AnswerContentEvent) and event.agent_id == "main":
                answers[event.data.id] = answers.get(event.data.id, "") + event.data.content
            elif isinstance(event, EndEvent) and event.agent_id == "main":
                print("本轮结束：", event.thread_id, event.data.usage_metadata)
    return answers


client = DeerFlowClient()
answers = consume_turn(client, "你好", thread_id="thread_001")
```

需要交给网络层或其他进程时，对每个事件进行序列化：

```python
payload = event.model_dump(mode="json", exclude_unset=True)
json_text = event.model_dump_json(exclude_unset=True)
restored_event = parse_stream_event(json_text)
```

`exclude_unset=True` 保留可选字段的稀疏形式；金额等特殊类型使用协议的 JSON 表示。SSE 封装由调用方自行完成。

## 7. 并发与资源释放

同一进程内，同一个客户端同时只能消费一个流；不同客户端消费同一个 `thread_id` 也会冲突。锁在开始迭代后持有，冲突立即抛出 `ReplayConflictError`，普通调用也受此限制。跨进程调用不受这些进程内锁协调。

主动停止消费时应调用生成器的 `close()`，仅 `break` 不等于显式关闭。关闭路径会释放调用占用、关闭内部迭代包装，并在进入相应执行阶段后清理沙箱执行租约；未完成的重生成还会尝试恢复状态。

请求 trace ID 只在每次推进内部迭代及关闭内部流期间绑定，向调用方交出事件时恢复原上下文。
