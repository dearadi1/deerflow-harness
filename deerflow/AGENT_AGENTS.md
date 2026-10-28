# DeerFlowClientPool 使用说明

实现文件：[agent_client.py](agent_client.py)。原始客户端：[client.py](client.py)。

`DeerFlowClientPool` 管理固定数量的 `DeerFlowClient`，实例不永久绑定用户。调用时通过 `session(user_id=...)` 独占借用，退出 `with` 后归还；没有空闲实例时按 FIFO 顺序排队。

**推荐用法：每个服务进程在启动时创建一个池；每次操作借用一个 session；流式调用再使用一层 `with`。借用、调用、消费流和归还必须在同一个工作线程内完成。**

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
            # 当需要退出时，直接在这里 break 即可，池会自动触发close。
            # if xxx:
            #     break
            print(event.model_dump(mode="json"))
```

事件模型沿用原 `DeerFlowClient.stream()`，池不修改事件内容。也可以直接 `for event in client.stream(...)`，session 退出时会兜底关闭流。

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

session 代理当前 `DeerFlowClient` 的全部 30 个公开方法。参数和普通返回值沿用原接口；流返回可迭代、可关闭、可用于 `with` 的包装对象。

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

`available` 是空闲实例数，`borrowed` 是当前借用数，`waiting` 是排队人数。`failed` 表示是否因流清理失败而停止分配，不代表所有模型调用的健康状态。关闭后 `available` 会清零，`size` 仍保留初始配置值。

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

`pool.close()` 可以重复调用，但已关闭的池不能重新打开。关闭池只释放其持有的实例引用，不会关闭进程共享的数据库、MCP、Memory Manager 或沙盒 Provider；这些资源的生命周期仍由宿主服务负责。
