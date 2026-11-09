# Agent 管理器使用指南

`DeerFlowClientPool` 管理一组可复用的客户端，通过 `session()` 借用实例，或通过 `start_stream()` 执行一轮托管对话。实现见 [agent_client.py](agent_client.py)。

| 入口 | 适用场景 | 生命周期 |
| --- | --- | --- |
| `pool.start_stream()` | 流式对话，需要独立的停止或断连处理入口 | 执行及清理结束后自动归还实例，支持跨线程关闭 |
| `pool.session()` | 查询、管理、`chat()` 或同线程消费的流式对话 | 退出 `with` 时关闭剩余流并归还实例 |

## 初始化

服务启动时创建池，在进程内复用；服务关闭时调用 `pool.close()`。运行对话前，需配置可用的模型、凭据和沙箱。

```python
from deerflow.agent_client import DeerFlowClientPool

pool = DeerFlowClientPool(
    config_path="config.yaml",
    size=4,
    acquire_timeout=30,
    max_waiters=100,
)
```

| 参数 | 未指定时的默认值 | 说明 |
| --- | --- | --- |
| `config_path` | 读取 `DEERFLOW_CONFIG_PATH` | 两者均未提供或环境变量为空时抛出 `ValueError`；相对路径按当前工作目录解析 |
| `size` | `4` | 正整数，同时可借用的客户端数量 |
| `acquire_timeout` | `None` | 等待空闲实例的秒数；`None` 无限等待，`0` 立即尝试 |
| `max_waiters` | `100` | 等待队列人数上限；`None` 不限人数，`0` 不排队 |
| `client_factory` | `None` | 可选的无参工厂，每次必须返回独立客户端；由工厂负责配置加载 |
| `**client_kwargs` | 使用底层客户端默认值 | 除 `config_path` 外原样传给 `DeerFlowClient`；不能与 `client_factory` 同时使用 |

`client_kwargs` 可设置 `model_name`、`thinking_enabled`、`subagent_enabled`、`plan_mode`、`agent_name`、`available_skills`、`middlewares`、`checkpointer`、`environment`，完整定义见 [client.py](client.py)。传入的对象按引用复用；需要每个实例各自持有 middleware 等对象时，使用 `client_factory`。

池容量参数也可通过环境变量设置，优先级为显式参数、环境变量、默认值：

```dotenv
DEERFLOW_CONFIG_PATH=/absolute/path/config.yaml
DEER_FLOW_CLIENT_POOL_SIZE=4
DEER_FLOW_CLIENT_POOL_ACQUIRE_TIMEOUT=30
DEER_FLOW_CLIENT_POOL_MAX_WAITERS=100
```

环境变量未设置或为空时采用默认值。后两个环境变量支持 `none`，表示不设上限。池参数在初始化时读取，运行中修改环境变量不会调整现有池。配置是进程级共享状态，同一进程的池和管理器应使用同一个配置文件。

## 托管流：start_stream

```python
from uuid import uuid4

thread_id = uuid4().hex
with pool.start_stream(
    "请整理这份资料",
    user_id="user_001",
    thread_id=thread_id,
    disabled_tools=["web_search"],
    acquire_timeout=5,
) as events:
    for event in events:
        print(event.model_dump(mode="json"))
```

`start_stream()` 先等待空闲实例，取得实例后启动执行，返回 `ManagedAgentStream`。使用同步 `for` 消费；每个流只允许一个消费者。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `message` | 必填 | 本轮用户输入 |
| `user_id` | `None` | 不传时捕获当前用户上下文；上下文也没有用户时抛出 `ValueError` |
| `thread_id` | `None` | 自动生成；继续对话时复用同一 ID，并配置可用的 checkpointer |
| `replay_last_turn` | `False` | 替换最近一轮输入并重新生成；必须提供已有 `thread_id` |
| `disabled_tools` | `None` | 本轮主智能体禁用的工具名称列表；不修改全局工具配置 |
| `skill_preset` | `None` | 公共技能预设 ID；不传时使用当前默认预设 |
| `acquire_timeout` | `None` | `None` 沿用池的等待时间；非负数字覆盖本次等待时间 |
| `buffer_size` | `64` | 正整数，未消费事件的缓冲数量上限；缓冲满时执行端等待消费 |
| `**kwargs` | 无 | 透传底层流式参数，见 [CLIENT_AGENTS.md](CLIENT_AGENTS.md) |

`user_id` 必须是非空字符串。显式 `thread_id` 必须为 1～64 个 ASCII 字母、数字、下划线或连字符。`disabled_tools` 按实际工具名称精确匹配，重复名称去重，未知名称不产生禁用效果。

对话步数上限优先读取非空的 `DEER_FLOW_RECURSION_LIMIT`，其次使用本轮 `recursion_limit` 参数，两者都未设置时为 `100`。

同一 `thread_id` 的并发执行会受到冲突保护，池不会替调用方排队同一会话。重生成需要可恢复的对话状态；输入 `message` 是替换后的用户消息，相关约束见 [重生成说明](../docs/client_replay.md)。

### 停止与断连

需要停止入口时，保存 `start_stream()` 返回的对象。消费端应保证流被关闭：

```python
events = pool.start_stream("请分析资料", user_id="user_001", thread_id=thread_id)
try:
    for event in events:
        print(event.model_dump(mode="json"))
finally:
    events.close(timeout=10)
```

停止处理线程可对同一个对象调用：

```python
events.close(timeout=10)
```

- `close(timeout=None)` 请求停止并等待清理，成功返回才表示实例已归还。`None` 不限制等待时间，`0` 只立即检查是否完成。
- 超时抛出 `StreamCloseTimeoutError`；停止请求仍然有效，后台继续清理，实例不会提前释放。可再次调用 `close()` 等待。
- 关闭会丢弃尚未消费的缓冲事件。主动停止不保证产出 `end` 或 `error` 事件。
- `events.closed` 表示流已结束，不能代替对执行和清理异常的处理。
- 自定义同步工具必须自行支持协作取消或配置超时；关闭流不撤销已经完成的外部操作。
- HTTP 断连不会自动调用 `close()`，宿主服务需要将断连、停止和异常处理接到该对象。

异步服务中，在工作线程执行同步消费；停止端可使用 `await asyncio.to_thread(events.close, timeout=10)`。不要在事件循环线程中阻塞迭代或等待关闭。

## 独占借用：session

```python
with pool.session(user_id="user_001", timeout=5) as client:
    models = client.list_models()
    answer = client.chat("介绍一下你的能力", thread_id=thread_id)
```

流式调用同样使用上下文管理：

```python
with pool.session(user_id="user_001") as client:
    with client.stream("继续分析", thread_id=thread_id) as events:
        for event in events:
            print(event.model_dump(mode="json"))
```

| 方法 | 返回值 |
| --- | --- |
| `client.chat(message, *, thread_id=None, disabled_tools=None, **kwargs)` | 主智能体最终回答的字符串；没有回答正文时为空字符串 |
| `client.stream(message, *, thread_id=None, replay_last_turn=False, disabled_tools=None, skill_preset=None, **kwargs)` | 支持上下文管理的同步事件迭代器 |
| `client.close()` | 关闭当前流并归还实例，返回 `None`；通常由退出 `with` 自动调用 |
| 其他 `DeerFlowClient` 公开方法 | 保留底层方法的返回值，如 `list_models()`、`get_thread_state()`、`upload_files()` |

`session(user_id=None, timeout=None)` 的用户解析与托管流相同，`timeout=None` 沿用池的等待时间。`chat()` 也可以通过关键字参数传入 `skill_preset` 和 `replay_last_turn`。

使用约束：

- session 的借用、方法调用、流消费和关闭必须在同一线程。需要跨线程停止时使用 `start_stream()`。
- 一个 session 同时只能有一个未结束的流。消费完成或关闭当前流后，才能调用其他方法。
- 同一线程不能嵌套借用同一个池；应复用已有 session。
- 退出 `with` 后，不能继续使用该 session 或缓存它的方法；退出会关闭未消费完的流。
- session 只代理公开方法，不暴露底层客户端属性。方法中显式传入的 `user_id` 必须与 session 用户一致。

异步服务使用 session 时，应将整个 `with pool.session(...)` 及其内部操作放入同一次工作线程调用，不能把借用、`next()`、关闭分别派发到不同线程。

## 每轮选择公共技能预设

使用 Skill 管理器创建并配置预设，然后将返回的 ID 传入对话。两种管理器必须使用同一套技能目录和预设文件。

```python
from deerflow import DeerFlowSkillClient

skills = DeerFlowSkillClient(config_path="config.yaml")
preset = skills.public.create_preset(name="资料整理")
skills.public.update_preset(
    preset["preset_id"],
    skill_states={"shared-summary": True},
)

with pool.start_stream(
    "整理资料",
    user_id="user_001",
    thread_id=thread_id,
    skill_preset=preset["preset_id"],
) as events:
    for event in events:
        print(event.model_dump(mode="json"))
```

示例要求公共技能 `shared-summary` 已存在。预设管理及文件格式见 [SKILL_AGENTS.md](SKILL_AGENTS.md#公共技能预设)。

- 不传或传 `None`，每轮读取当前默认预设，不继承上一轮的显式选择。
- 显式选择只影响本轮，不修改文件中的默认值；不存在的 ID 会报错。
- 每轮固定公共开关快照。修改预设、切换默认值或删除预设影响后续轮次，已有池下一轮会自动识别，无需调用 `invalidate_agents()`。
- 私有技能不参与预设；技能文件内容、Agent 技能范围和授权规则仍按各自规则生效。
- 运行预设需要沙箱支持线程级技能文件隔离。开启宿主机 bash 的 `LocalSandboxProvider` 不支持该隔离方式，需使用支持隔离的沙箱或关闭宿主机 bash。

预设文件默认位于 `.deer-flow/skill_presets.json`；设置 `DEER_FLOW_HOME` 时位于该数据目录。可通过 `DEER_FLOW_SKILL_PRESETS_PATH` 显式指定其他路径。

初始化时会校验预设文件；格式错误时先在同目录保存 `skill_presets.json.invalid-<随机值>.bak`，再恢复为空 `default` 预设，所有公共 Skill 关闭，私有状态保持不变。备份或替换失败会阻止初始化，原文件保留；运行中的格式错误仍会报错。

## 事件与异常

两种流式入口均产出 `ClientStreamEvent` 事件模型，可以读取 `event.type`、`event.thread_id`、`event.data`，或用 `event.model_dump(mode="json")` 转为字典。池不直接返回 SSE 文本。完整事件格式见 [CLIENT_AGENTS.md](CLIENT_AGENTS.md) 和 [protocol/models.py](protocol/models.py)。

消费方同时处理协议事件和 Python 异常：整轮执行失败可能通过 `error` 事件报告，参数错误、会话冲突及清理失败等也可能直接抛出异常。`chat()` 收到 `error` 事件时会抛出 `RuntimeError`。

| 异常 | 含义与处理 |
| --- | --- |
| `PoolTimeoutError` | 等待实例或等待池排空超时；前者可稍后重试，后者不代表池恢复开放 |
| `PoolQueueFullError` | 等待队列已满，调用方应排队、限流或返回忙碌状态 |
| `PoolClosedError` | 池已关闭或发生清理故障，不再接收新调用 |
| `StreamCloseTimeoutError` | 等待流关闭超时，不能将实例视为已释放 |
| `StreamCleanupError` | 执行清理失败；池停止新分配，应处理故障后重建 |
| `ValueError` / `TypeError` | 参数、配置、用户或预设无效 |
| `RuntimeError` | session 跨线程、重入、未结束当前流等不符合调用约束，或底层执行错误 |

以上池与流异常可从 `deerflow.agent_client` 导入。池只协调本进程的容量，不承担宿主鉴权、跨进程限流或请求重试。

## 状态、缓存与关闭

```python
pool.stats
pool.invalidate_agents()
pool.close(wait=True, timeout=30)
```

`stats` 返回 `size`、`available`、`borrowed`、`waiting`、`closed`、`failed`。`size` 是池容量，不代表模型请求、工具或子代理的并发上限。

`invalidate_agents()` 使缓存于下一次客户端调用前刷新，正在运行的流不被强制重建。公共预设、私有开关及 Skill 管理器成功发布的技能变更在下一轮自动生效，覆盖同步和托管入口、顺序复用及多个池槽位，无需调用此方法。通过 session 调用会修改配置或记忆的管理方法时，池也会自动失效相关 Agent 缓存；其他从池外进行且需要重建 Agent 的修改仍需调用此方法。

`close(wait=True, timeout=None)` 拒绝新借用，唤醒等待者，并等待已借出的实例归还。`wait=False` 只停止接收新调用，不等待，也不会强行取消正在运行的对话。要主动停止对话，应先关闭对应的 `ManagedAgentStream`。

关闭超时后池仍处于关闭状态，可再次调用 `close()` 等待。不要在尚未退出的 session 内等待同一个池关闭。池可以作为上下文管理器使用，退出时执行 `close()`；进程共享的数据库、MCP、记忆管理器和沙箱 Provider 由其所有者负责关闭。
