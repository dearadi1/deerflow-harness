# DeerFlowClientPool 使用说明

实现文件：[agent_client.py](agent_client.py)。原始客户端：[client.py](client.py)。

`start_stream()`、session 的 `stream()` / `chat()` 均支持显式参数
`auto_routing_enabled: bool = False`。与 `config.yaml -> auto_routing.enabled`
同时开启后，在创建 Agent 前自动选择模型和模式，覆盖手动模型及模式参数。
流事件通过与 `thread_id` 同级的 `agent_setting` 返回实际配置；分析调用按自己的
模型价格发送 `token_usage`，并计入主 `end` 的 Token 和成本汇总。
配置、兼容回退与长对话预算见[自动路由说明](../docs/auto_routing.md)。

`DeerFlowClientPool` 管理固定数量的 `DeerFlowClient`，实例不永久绑定用户。通过 `start_stream()` 托管一轮生成，或通过 `session(user_id=...)` 独占借用；两种方式共享容量和 FIFO 等待队列。

**推荐用法：每个服务进程启动时创建一个池；需要停止/断连取消的流式对话使用 `start_stream()`；管理操作及原有同步调用继续使用 `session()`。原 session 的借用、调用、消费和关闭仍必须在同一线程；新托管流支持跨线程 `close()`。**

## 托管流：支持独立停止线程

```python
events = pool.start_stream(
    "请分析这个问题",
    user_id="user_001",
    thread_id="thread_001",
    replay_last_turn=False,
    disabled_tools=["web_search"],
    acquire_timeout=5,
    buffer_size=64,
)

# 消费线程：后端保存本轮 events，供停止和断连处理器使用。
try:
    for event in events:
        send_to_frontend(event)
finally:
    events.close(timeout=10)
```

收到用户停止或连接断开时，在另一个线程直接调用同一个对象：

```python
events.close(timeout=10)
```

不需要等待下一帧，也不要通过关闭整个 pool 来停止一轮。`close()` 成功返回表示本轮执行与必要清理结束、对话锁释放、池实例归还；随后可使用相同 `thread_id` 继续对话或重新生成。重复关闭安全，旧对象不会取消下一轮。返回类型及异常可从 `deerflow.agent_client` 导入：`ManagedAgentStream`、`StreamCloseTimeoutError`、`StreamCleanupError`。

| 接口 | 约定 |
| --- | --- |
| `pool.start_stream(message, *, user_id=None, thread_id=None, replay_last_turn=False, disabled_tools=None, auto_routing_enabled=False, acquire_timeout=None, buffer_size=64, **kwargs)` | 同步等待实例，成功后立即启动本轮；无需先 `next()`，参数规则沿用原 stream |
| `for event in events` | 单消费者同步迭代；共用 `ClientStreamEvent`，包含本轮 `agent_setting` |
| `events.close(timeout=None)` | 可跨线程、幂等；默认等待清理完成；显式超时只限制本次等待 |
| `events.closed` | 实例已归还时为 `True`；收到停止请求但清理未完成时仍为 `False` |
| `with pool.start_stream(...) as events` | 退出时关闭，适合保证提前 break 或消费异常后的收尾 |

`acquire_timeout=None` 沿用池默认值，`0` 表示立即尝试；`buffer_size` 是正整数，限制待消费事件数。队列满时暂停交付，关闭信号不经过该队列。正常执行结束后自动归还实例，未读取的事件仍可继续消费；显式关闭丢弃未消费事件并结束迭代。主动停止不伪造正常完成的 `end` 事件。

模型或工具执行异常通过迭代传播，完成收尾后实例仍可复用；`close()` 单独报告收尾失败。`StreamCleanupError` 表示资源或状态收尾失败，池停止新分配。`StreamCloseTimeoutError` 表示尚未清理完毕，此时不得假定对话可重新执行；后台继续持有实例和对话锁，调用方可以再次调用 `close()` 等待。不要因超时而创建新池绕过占用。

内部每个槽位按需创建并复用后台线程和事件循环，独占运行本轮 `agent.astream()`，避免缓存模型的异步连接跨循环复用，原同步 checkpointer 通过异步适配器复用，不创建另一份存储。普通对话取消后保留已落盘内容并清理待执行调度；重生成中断时恢复之前的有效状态。尚未落盘输入的轮次没有可重生成的历史，应再次按普通对话提交。已完成的外部工具副作用不回滚。

`close()` 首先立即停止向消费端交付事件，再直接通知本轮正在等待的 bash 请求取消。AIO bash 保留远端同步执行和单个长请求，托管路径使用原生异步 HTTP 等待；不设置 `async_mode=True`，也不轮询结果。取消本地请求后，定向关闭已知的 shell/bash 会话并终止远端命令；普通 shell、带环境变量的技能脚本及子任务独立 shell 均覆盖。命令参数、原输出截断策略和命令超时沿用原路径，不新增 2 秒的执行/结果请求限制。旧同步 session 调用仍使用原同步客户端。

`close()` 成功返回前必须完成会话终止、所有本轮任务退出、checkpoint 收尾和租约释放；随后相同 `thread_id` 可以立即重入。不会丢弃仍在执行的旧任务后提前复用实例。终止会话的收尾任务受到保护，图清理不能再次取消它；终止请求使用原有 5 秒网络等待上限且不自动重试。无法确认终止时报告 `StreamCleanupError`，停止池分配，不能伪装成功并允许旧任务与新轮次并发。

本地 bash 每 50 毫秒检查停止信号，停止时终止整个进程组（Windows 终止进程树）。这里只停止本轮正在执行的操作，不撤销已完成的外部副作用，也不保证终止脚本自行脱离会话启动的后台服务。

子代理 `task` 的状态轮询、取消后的终态等待及延后清理默认每 0.1 秒检查一次。可在启动进程前通过 `DEER_FLOW_SUBAGENT_POLL_INTERVAL_SECONDS=0.1` 配置，单位为秒；未设置或空白使用默认值，非有限正数会报错，修改后需重启进程。轮询次数按间隔换算，仍保留执行超时加 60 秒缓冲的等待预算。该设置控制子代理状态检查频率，不是 AIO 命令结果轮询，也不改变沙盒终止请求的网络超时；`close()` 实际耗时仍取决于子代理和沙盒的收尾耗时。

停止信号的交付与 `close()` 成功返回不同：后者仍等待工具退出、checkpoint 和沙盒清理。正常网络下取消通常无需等到命令超时，但服务器断网、数据库阻塞等故障下没有整个关闭过程的固定时间上限；`timeout` 仅限制调用方等待。任意同步 Python/第三方 SDK 调用仍不能安全地强制杀死，默认执行器会等待它退出。同步自定义工具可通过 `deerflow.runtime.turn_cancellation.get_turn_cancellation_event()` 取得本轮停止事件，并在可中断处检查，或调用 `raise_if_turn_cancelled()`。同步接口还应配置自身网络超时。自行创建线程、独立执行器或远端后台作业的工具，必须自行实现取消和清理。

身份和 trace 上下文在启动时捕获，并传到执行及清理任务；池仍不负责宿主鉴权。自定义 `client_factory` 需返回支持现有同步存储协议的 `DeerFlowClient`；仅有异步方法的 saver 不适用。跨进程停止由宿主把停止请求路由到持有本轮对象的进程。

异步 HTTP 后端应在工作线程创建和消费该同步流；停止处理器可 `await asyncio.to_thread(events.close, timeout=10)`。请求断连本身不会自动通知池，后端必须把停止、断连和请求异常都接到本轮 `close()`。若关闭等待协程也被取消，已发出的停止请求和后台清理仍继续进行。

## 1. 最小使用示例

需要先安装项目依赖，并配置好 `config.yaml` 和模型凭据。实例池不会自动补齐模型或沙盒配置。

```python
from uuid import uuid4

from deerflow.agent_client import DeerFlowClientPool

with DeerFlowClientPool() as pool:
    with pool.session(user_id="user_001") as client:
        models = client.list_models()
        print(models)

        answer = client.chat(
            "请介绍一下你能提供哪些帮助。",
            thread_id=uuid4().hex,
        )
        print(answer)
```

这里有两个生命周期：

- `with DeerFlowClientPool()`：管理整个池，退出时停止接收新借用并等待已有 session 归还。
- `with pool.session(...)`：管理一次借用，退出时清理未结束的流并归还实例；不会关闭整个池。

服务中应复用同一个池，不要每个请求都重新建池。`with ... as client` 得到的是临时代理 `AgentClientSession`，不要在退出后继续使用它或保存它的方法供以后调用。

## 2. 环境变量与初始化

将以下配置加入部署环境或框架加载的 `.env` 文件；示例也已写入根目录 [.env.example](../.env.example)：

```dotenv
DEER_FLOW_CLIENT_POOL_SIZE=4
DEER_FLOW_CLIENT_POOL_ACQUIRE_TIMEOUT=none
DEER_FLOW_CLIENT_POOL_MAX_WAITERS=100
```

| 构造参数 | 环境变量 | 缺省值 | 说明 |
| --- | --- | --- | --- |
| `size` | `DEER_FLOW_CLIENT_POOL_SIZE` | `4` | 正整数，池内实例数，也是最大同时借用数 |
| `acquire_timeout` | `DEER_FLOW_CLIENT_POOL_ACQUIRE_TIMEOUT` | `None` | 等待实例的秒数，可使用小数；`0` 表示立即尝试，`None` 表示无限等待 |
| `max_waiters` | `DEER_FLOW_CLIENT_POOL_MAX_WAITERS` | `100` | 最大排队人数，不含已借用者；`0` 表示不排队，`None` 表示不限制 |

优先级为：**显式构造参数 > 环境变量 > 缺省值**。

环境变量未设置或留空时使用缺省值。后两个环境变量可以写 `none`，不区分大小写；`size` 不能使用 `none`。非法数值会在建池时抛出 `ValueError`。

```python
from deerflow.agent_client import DeerFlowClientPool

# 使用环境变量配置容量和排队规则。
pool = DeerFlowClientPool(agent_name="voris")

# 服务退出、所有请求处理结束后关闭。
pool.close()
```

也可以覆盖部分环境配置：

```python
from deerflow.agent_client import DeerFlowClientPool

with DeerFlowClientPool(
    size=8,
    acquire_timeout=30,
    max_waiters=100,
    agent_name="voris",
    thinking_enabled=True,
    subagent_enabled=True,
) as pool:
    print(pool.stats)
```

构造时显式传入 `acquire_timeout=None` 或 `max_waiters=None` 会覆盖环境变量，表示不设相应上限。环境变量在建池时读取，修改后不会自动调整已有池；直接编辑 `.env` 文件也不会热更新已有进程的环境。

**`acquire_timeout` 只限制排队等待，不限制模型生成、工具执行或 session 持有时间。** `size` 也不限制流产生的后台任务数量。

### 传入原客户端参数

除池参数外，其他关键字参数传给每个 `DeerFlowClient`：

| 参数 | 缺省值 | 用途 |
| --- | --- | --- |
| `config_path` | `None` | 显式路径优先，否则读取 `DEERFLOW_CONFIG_PATH`；池内统一加载一次 |
| `checkpointer` | `None` | 自定义 checkpoint 存储；未指定时沿用框架默认配置 |
| `model_name` | `None` | 默认模型名称 |
| `thinking_enabled` | `True` | 是否启用思考 |
| `subagent_enabled` | `False` | 是否启用子 Agent |
| `plan_mode` | `False` | 是否启用计划模式 |
| `agent_name` | `None` | 命名 Agent |
| `available_skills` | `None` | 可用技能名称集合，未指定时沿用原客户端默认范围 |
| `middlewares` | `None` | 自定义中间件序列 |
| `environment` | `None` | 追踪信息使用的运行环境标签 |

池初始化时不接收 `user_id`；用户在借用时指定。同一进程内应使用一致的应用配置，`config_path` 不是每个实例的私有配置隔离机制。

`config_path` 未传或为 `None` 时必须设置 `DEERFLOW_CONFIG_PATH`，例如 `export DEERFLOW_CONFIG_PATH=/absolute/path/config.yaml`。两者均未提供或环境变量为空时抛出 `ValueError`，不再自动查找根目录 `config.yaml`。相对路径按调用方工作目录解析。自定义 `client_factory` 仍自行负责配置加载。

## 3. 流式调用

后续示例中的 `pool` 均指应用已经创建的池。

```python
from uuid import uuid4

thread_id = uuid4().hex

with pool.session(user_id="user_001") as client:
    with client.stream("请帮助我分析这个问题。", thread_id=thread_id) as events:
        for event in events:
            # break 后退出内层 with 才会关闭流；仅 break 不会触发关闭。
            # if xxx:
            #     break
            print(event.model_dump(mode="json"))
```

事件模型沿用原 `DeerFlowClient.stream()`，池不修改事件内容。`AgentClientSession.stream()` 和 `chat()` 显式支持 `disabled_tools: list[str] | None`，按底层客户端规则将名称列表传给本轮主智能体：

```python
with pool.session(user_id="user_001") as client:
    with client.stream(
        "仅使用已有资料回答",
        thread_id="thread_001",
        disabled_tools=["web_search", "web_fetch"],
    ) as events:
        for event in events:
            print(event)

    answer = client.chat("继续总结", disabled_tools=["web_search"])
```

禁用名单只影响本次调用，不写入池配置；下一轮省略该参数会恢复原工具集合。工具匹配、校验和对子智能体的范围说明见[客户端调用约定](CLIENT_AGENTS.md)。也可以直接 `for event in client.stream(...)`，session 退出时会兜底关闭流。

**创建生成器不代表执行完成。流没有消费完或关闭前，实例仍被占用，而且同一 session 不能调用其他客户端方法或创建第二个流。**

### 提前停止

```python
with pool.session(user_id="user_001") as client:
    with client.stream("你好") as events:
        for event in events:
            print(event)
            break

    # 内层 with 已关闭流，此时可继续使用当前实例。
    models = client.list_models()
```

退出流的 `with` 只关闭流；退出 session 的 `with` 才归还实例。不使用内层 `with` 时，提前 `break` 后应显式调用 `events.close()`，或者直接退出 session。

关闭流不会撤销已经完成的文件写入、长期记忆更新或外部工具副作用，也不保证模型或工具的远端任务被立即取消。

### 多轮对话与重生成

同一会话复用相同 `thread_id`，不同会话使用全局唯一 ID。跨借用延续历史依赖正确配置的共享 checkpointer，不依赖借到同一个 client。

```python
from uuid import uuid4

thread_id = uuid4().hex

with pool.session(user_id="user_001") as client:
    answer = client.chat("请列出三个研究方向。", thread_id=thread_id)

with pool.session(user_id="user_001") as client:
    with client.stream("详细说明第二个方向。", thread_id=thread_id) as events:
        for event in events:
            print(event)

with pool.session(user_id="user_001") as client:
    with client.stream(
        "换一种方式说明第二个方向。",
        thread_id=thread_id,
        replay_last_turn=True,
    ) as events:
        for event in events:
            print(event)
```

重生成的前提和副作用边界沿用 [client_replay 使用说明](../docs/client_replay.md)。

## 4. 用户身份与现有上下文兼容

推荐显式传入经过应用认证的用户 ID：

```python
with pool.session(user_id="user_001") as client:
    memory = client.get_memory()
    skills = client.list_skills()
```

如果宿主已经使用 `set_current_user()`，可省略 `user_id`，由 session 捕获当前用户：

```python
from types import SimpleNamespace

from deerflow.runtime.user_context import reset_current_user, set_current_user

token = set_current_user(SimpleNamespace(id="user_001"))
try:
    with pool.session() as client:
        memory = client.get_memory()
finally:
    reset_current_user(token)
```

同时未传 `user_id`、未设置当前用户时，会抛出 `ValueError`，不会自动落入默认用户。若在 `stream()` 等调用中额外传入 `user_id`，必须与 session 绑定用户一致。

池在每次底层方法调用、流推进及关闭期间绑定身份，随后恢复调用前的上下文；不会让用户 Token 跨流的 `yield` 保留。因此，在 `with pool.session(...)` 内直接调用其他模块的函数，不代表那些函数也获得了 session 的用户身份。

身份绑定不是鉴权。`list_models()` 仍读取应用模型配置；线程查询、目标读写等方法仍沿用原客户端的过滤和归属检查能力。对外提供多用户接口时，宿主仍需验证用户身份和资源归属。

## 5. 多线程并发

把完整的 session 放进工作函数，不要在主线程借用后将 session 或流交给其他线程。

```python
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from deerflow.agent_client import DeerFlowClientPool


def ask(pool, user_id, message):
    with pool.session(user_id=user_id) as client:
        return client.chat(message, thread_id=uuid4().hex)


with DeerFlowClientPool(size=4, acquire_timeout=30, max_waiters=100) as pool:
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(ask, pool, f"user_{index}", "你好")
            for index in range(12)
        ]
        for future in futures:
            print(future.result())
```

最多 4 个工作线程同时持有实例，其余等待。线程池限制工作线程数量，客户端池限制借用数量，两者是独立的。

注意以下边界：

- 同一个线程不能嵌套借用同一个池，即使池还有空位；应复用当前 session。
- 同一个 `thread_id` 的并发调用仍可能触发 `ReplayConflictError`，池不会按会话排队。
- 不同进程的池彼此独立；例如 4 个 worker 各建容量为 8 的池，最多可同时借出 32 个实例。
- 多个池会共享框架的部分进程级资源，增加池不会自动增加模型配额或子 Agent 执行额度。

### 异步应用

这是同步池，不支持 `async with pool.session(...)`。异步应用可以把完整的非流式操作放到工作线程：

```python
import asyncio
from uuid import uuid4


def ask_sync(pool, user_id, message):
    with pool.session(user_id=user_id) as client:
        return client.chat(message, thread_id=uuid4().hex)


async def ask_async(pool, user_id, message):
    return await asyncio.to_thread(ask_sync, pool, user_id, message)
```

取消等待 `asyncio.to_thread()` 的协程不会自动终止工作线程；实例要等实际执行结束和 session 清理后才归还。

异步流式 HTTP 接入需要额外的队列桥接：在同一工作线程中借用、消费和关闭流，再把事件转交给响应协程。不要直接把 session 包裹的同步生成器交给可能逐次切换工作线程的消费者，也不要分别对获取实例、`next()` 和关闭操作调用 `to_thread()`。

## 6. 可调用的方法

session 代理当前 `DeerFlowClient` 的全部 30 个公开方法。参数和普通返回值沿用原接口；流返回可迭代、可关闭、可用于 `with` 的包装对象。`pool.start_stream()` 是池上的新增入口，不通过 session 调用。

| 类别 | 方法 |
| --- | --- |
| 对话 | `chat`、`stream` |
| 模型 | `list_models`、`get_model` |
| 线程 | `list_threads`、`get_thread` |
| 目标 | `get_goal`、`set_goal`、`clear_goal` |
| 技能 | `list_skills`、`get_skill`、`update_skill`、`install_skill` |
| MCP | `get_mcp_config`、`update_mcp_config` |
| 记忆 | `get_memory`、`export_memory`、`import_memory`、`reload_memory`、`clear_memory` |
| 记忆事实 | `create_memory_fact`、`delete_memory_fact`、`update_memory_fact` |
| 记忆配置 | `get_memory_config`、`get_memory_status` |
| 上传与产物 | `upload_files`、`list_uploads`、`delete_upload`、`get_artifact` |
| Agent 缓存 | `reset_agent` |

例如查询模型和上传文件：

```python
from uuid import uuid4

thread_id = uuid4().hex

with pool.session(user_id="user_001") as client:
    models = client.list_models()
    uploads = client.upload_files(thread_id, files=["/absolute/path/report.pdf"])
    print(uploads)
    print(client.list_uploads(thread_id))
```

示例中的本地文件必须真实存在。上传后若需声明本轮新增附件，仍应按原 `stream(..., files=...)` 协议传入元数据；池不会自动添加附件声明。

## 7. 等待超时与异常处理

单次借用可覆盖池的默认等待时间：

```python
with pool.session(user_id="user_001", timeout=5) as client:
    models = client.list_models()
```

这里的 `timeout=None` 表示沿用池默认值，与构造函数中显式 `acquire_timeout=None` 的含义不同。`timeout=0` 表示立即尝试；没有可立即分配的实例时抛出 `PoolTimeoutError`。

```python
from deerflow.agent_client import PoolClosedError, PoolQueueFullError, PoolTimeoutError
from deerflow.runtime.client_replay import ReplayConflictError

try:
    with pool.session(user_id="user_001", timeout=5) as client:
        answer = client.chat("你好", thread_id="existing_thread_001")
except PoolQueueFullError:
    print("排队人数已满，请稍后再试。")
except PoolTimeoutError:
    print("等待空闲实例超时。")
except PoolClosedError:
    print("实例池已停止接收请求。")
except ReplayConflictError:
    print("该会话正在执行，请等待上一轮结束。")
```

| 异常 | 场景 |
| --- | --- |
| `PoolQueueFullError` | 需要排队，但等待人数达到上限 |
| `PoolTimeoutError` | 等待实例超时，或等待池关闭排空超时 |
| `PoolClosedError` | 池已关闭，或底层流清理失败后池停止分配 |
| `StreamCloseTimeoutError` | 托管流已请求停止，但仍在执行或清理，尚未归还实例 |
| `StreamCleanupError` | 托管流的资源或 checkpoint 收尾失败，池停止新分配 |
| `ValueError` | 配置无效、缺少用户、调用身份与 session 不一致 |
| `RuntimeError` | 跨线程使用、使用已归还 session、嵌套借用、流未结束时调用其他方法等 |
| 原客户端异常 | 模型、工具、存储、权限或同会话冲突等错误，继续向调用方传播 |

普通操作异常会触发 session 清理并归还实例。若底层流关闭失败，池会停止新分配并唤醒排队者；不会把清理结果未知的实例重新交给其他用户。应先处理失败原因和仍在运行的操作，再决定服务恢复方式。

池不会自动重试模型或工具调用，以免重复产生外部副作用。

## 8. 缓存、工厂与状态

### Agent 缓存

归还实例时保留已有 Agent 缓存。下一次调用的用户或模型等缓存键不同时，原客户端会重建 Agent 和主模型对象。池不按用户固定分配，也不保证优先分配曾服务过该用户的实例。

通过 session 调用以下方法时，会标记本池内的 Agent 缓存失效，即使写操作报错也会标记，以覆盖已写入后才报错的情况：

`reset_agent`、`update_mcp_config`、`update_skill`、`install_skill`、`reload_memory`、`import_memory`、`clear_memory`、`create_memory_fact`、`delete_memory_fact`、`update_memory_fact`。

失效实例在下一次客户端方法调用前刷新；正在运行的流不会被强制重置。外部代码更新相关配置后，可手动通知这个池：

```python
pool.invalidate_agents()
```

它只更新当前池的失效标记，不会调整池大小，也不替代框架对必须重启的配置项的要求。若同时使用多个池，需要分别通知。

### 自定义工厂

`client_factory` 是无参数函数，建池时调用 `size` 次，每次必须返回不同的客户端。它与所有原客户端构造参数互斥，但可以和 `size`、`acquire_timeout`、`max_waiters` 同时使用。

```python
from deerflow.client import DeerFlowClient
from deerflow.agent_client import DeerFlowClientPool


def create_client():
    return DeerFlowClient(agent_name="voris", thinking_enabled=True)


with DeerFlowClientPool(size=4, client_factory=create_client) as pool:
    print(pool.stats)
```

需要独立的有状态 middleware 时，在工厂内部创建新的 middleware 对象。直接传入 `middlewares=[...]` 时，客户端虽然各自持有列表，但列表中的对象仍共享；显式传入的 `checkpointer` 也按同一引用使用。

### 状态查询

```python
print(pool.stats)
```

返回当前快照：

```python
{
    "size": 4,
    "available": 3,
    "borrowed": 1,
    "waiting": 2,
    "closed": False,
    "failed": False,
}
```

`available` 是空闲实例数，`borrowed` 是 session 与托管流的当前借用总数，`waiting` 是排队人数。`failed` 表示是否因流清理失败而停止分配，不代表所有模型调用的健康状态。关闭后 `available` 会清零，`size` 仍保留初始配置值。

## 9. 服务退出与关闭

```python
pool.close(wait=True, timeout=30)
```

关闭会拒绝新借用，唤醒排队者使其收到 `PoolClosedError`，并按配置等待已借出实例归还。超时抛出 `PoolTimeoutError`，但池仍保持关闭状态；可再次调用 `close()` 等待剩余工作完成。

```python
# 停止分配，但不等待已有 session 结束。
pool.close(wait=False)
```

`wait=False` 不会中断正在执行的请求，也不会提前回收它们的实例。当前线程仍持有 session 时，不能调用 `pool.close(wait=True)`，应先退出 session，避免等待自身归还。

服务退出时先对持有的托管流调用 `events.close(timeout=...)`，再关闭池并等待后台线程退出；`pool.close()` 保留原语义，不主动取消托管流，也不替代断连通知。停止消费但没有关闭、且事件队列已满的流，会继续占用实例。

`pool.close()` 可以重复调用，但已关闭的池不能重新打开。关闭池只释放其持有的实例引用，不会关闭进程共享的数据库、MCP、Memory Manager 或沙盒 Provider；这些资源的生命周期仍由宿主服务负责。

## 10. 真实模型与实例池验收

AIO 长命令停止另有不调用模型的真实沙盒验收，使用独立测试沙盒地址：

```bash
DEER_FLOW_RUN_LIVE_TESTS=1 DEER_FLOW_TEST_AIO_URL=http://127.0.0.1:8080 \
  .venv/bin/python -m unittest discover -s tests -p 'test_aio_managed_stream_live.py' -v
```

脚本覆盖 full/delta 与普通 shell、环境变量 bash、子任务 shell 的六个停止场景，以及正常输出、shell 状态、命令超时和复用。每次停止先确认 60 秒合成脚本已启动，再检查远端子进程确实退出、SQLite 无待执行节点、池容量归还，并立即使用同一池槽位和同一对话执行新一轮。2026-11-03 在本机 AIO 1.11.0 隔离容器中，无轮询实现的六个场景 `close()` 实测为 12–15 毫秒；该结果不代表远程部署的网络与清理时延上限。测试不验证模型请求和宿主 HTTP 断连接入。

验收脚本为 [test_agent_managed_stream_live.py](../tests/test_agent_managed_stream_live.py) 和 [test_agent_client_pool_live.py](../tests/test_agent_client_pool_live.py)，默认跳过。显式开启后使用配置中的真实模型和凭据。故障注入场景只发送白名单合成提示及 `slow_probe` 测试工具定义，出站白名单为 `ark.cn-beijing.volces.com`；默认构造场景使用生产 Agent 装配、空业务工具配置和合成测试输入。运行前应安装项目声明的依赖并配置有效模型。

```bash
DEER_FLOW_RUN_LIVE_TESTS=1 DEER_FLOW_LIVE_CHECKPOINT_MODE=full \
  .venv/bin/python -m unittest discover -s tests -p 'test_agent*live.py' -v

DEER_FLOW_RUN_LIVE_TESTS=1 DEER_FLOW_LIVE_CHECKPOINT_MODE=delta \
  .venv/bin/python -m unittest discover -s tests -p 'test_agent*live.py' -v
```

两个模式必须分进程运行。默认读取根目录 `config.yaml` 中的第一个模型；可用 `DEER_FLOW_LIVE_CONFIG`、`DEER_FLOW_LIVE_MODEL` 指定来源和模型。保留真实 provider、endpoint、model 与凭据，测试将输出上限设为 1024、重试设为 0、请求超时设为 30 秒、分片超时设为 10 秒，并关闭 thinking。临时配置权限为 0600；报告只记录模型来源、非敏感参数、请求数、耗时及结果。

中断报告默认写入 `/private/tmp/deerflow_managed_live_{full|delta}.json`，池管理报告默认写入 `/private/tmp/deerflow_agent_pool_live_{full|delta}.json`。运行单个文件时，可用 `DEER_FLOW_LIVE_REPORT` 指定报告路径。临时配置、SQLite checkpoint 和运行目录在测试结束时删除，不读取或修改业务对话。

中断验收每个模式覆盖 11 项：连续两轮与客户端复用、真实输出中主动停止、无下一帧时停止、响应中途断流、响应读取超时、消费端异常、同步工具关闭超时、异步工具取消后继续、重生成取消恢复原答案、双用户隔离、HTTP 等待协程取消后关闭后台流。断流和超时通过真实 HTTP 响应上的受控注入复现；耗时工具由真实模型选择调用。收尾后检查池容量与 checkpoint 待执行节点，并真实继续或重新生成。

池管理验收位于 `tests/test_agent_client_pool_live.py`，每个模式覆盖 17 项：

| 范围 | 验证内容 |
| --- | --- |
| 默认构造 | 使用真实 `config_path` 构造池，执行生产 Agent 的 session stream、chat 和托管 stream |
| session 归属 | 跨线程调用与关闭被拒绝、嵌套借用被拒绝、身份恢复、归还后旧代理失效 |
| 容量与排队 | session 和托管流共用容量、混合等待者 FIFO、立即超时、等待超时清票、队列上限 |
| 独立停止 | 同一用户不同线程、不同用户独立关闭；双客户端本地 HTTP 连接中，一端 EOF 只取消其流 |
| 冲突与背压 | 同 thread_id 冲突不影响已有流、有界缓冲无人消费时仍可关闭 |
| 幂等与缓存 | 并发 close、旧句柄不影响新轮次、运行中手工失效不打断、session reset 使全部槽位刷新 |
| 关闭与复用 | 关闭唤醒等待者、等待排空超时仍保留占用、关闭后拒绝新请求、重复三槽位并发与线程退出 |
| 状态隔离 | 三个合成用户各自连续两轮，checkpoint 无其他用户标识、无悬空节点 |
| 参数约束 | 环境默认值、非法参数和重复 factory 实例；这些入口验证不发模型请求 |

共享夹具为 `tests/agent_pool_live_support.py`。除默认构造验收外，真实模型故障场景使用固定测试提示、无业务能力的 Agent 装配，以便稳定触发慢工具、网络停顿和读取异常。本地 HTTP 用例使用测试专用桥接器，验证断连接入方式，不代表宿主 HTTP 服务的完整验收。故障注入传输不保留空闲 HTTP 连接，覆盖的是客户端及槽位复用。

测试使用生产池、模型适配器、LangGraph、事件解析和 SQLite；未覆盖进程被强杀后的恢复。

2026-11-02 使用 `doubao-seed-2-1-pro-260915` 验证 full、delta 各 28 项，共 56 项全部通过；最终双模式验收各发出 68 次真实模型请求，共 136 次。两项参数校验场景不发模型请求。池与客户端离线回归另有 119 项全部通过。本次汇总报告为 `/private/tmp/deerflow_agent_pool_acceptance_20261102.json`。
