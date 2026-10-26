"""为嵌入式客户端提供有界、按需绑定用户的同步实例池。

用法::

    pool = DeerFlowClientPool(size=8, acquire_timeout=30, max_waiters=100)
    with pool.session(user_id="user_a") as client:
        models = client.list_models()
        with client.stream("你好", thread_id="example_thread_001") as events:
            for event in events:
                print(event)
    pool.close()

session 代理 DeerFlowClient 的全部公开方法；普通方法保留原返回值，流保留
迭代、send、throw、close，并增加上下文管理。提前结束迭代后，应关闭流或退出
session。租约及其流只能由借用线程使用，异步服务应在工作线程中完成整个租约，
不能只把获取实例或单次 next 分别派发到任意线程。

池只协调本进程的实例，不提供鉴权、同会话排队或跨进程限流。原接口的用户过滤、
全局配置及存储语义保持不变。相同 thread_id 仍受原客户端的冲突保护。不要为同一
进程配置不同的 config_path；外部修改配置后应调用 invalidate_agents。
自定义 checkpointer 和 middleware 对象按调用方传入的引用使用，若需要独立
middleware，请通过 client_factory 为每个实例创建独立对象。
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any

from deerflow.client import DeerFlowClient
from deerflow.client_config import load_client_config
from deerflow.config.app_config import get_app_config
from deerflow.runtime.user_context import CurrentUser, get_current_user, reset_current_user, set_current_user

__all__ = ["DeerFlowClientPool", "AgentClientSession", "PoolClosedError", "PoolTimeoutError", "PoolQueueFullError"]

_INITIALIZATION_LOCK = threading.Lock()
_INVALIDATING_METHODS = frozenset({
    "reset_agent", "update_mcp_config", "update_skill", "install_skill",
    "reload_memory", "import_memory", "clear_memory", "create_memory_fact",
    "delete_memory_fact", "update_memory_fact",
})


class _EnvironmentDefault(Enum):
    # 显式 None 表示不设上限，不能与省略参数、读取环境变量混为一谈。
    UNSET = "environment"


def _resolve_pool_option(
    value: int | float | None | _EnvironmentDefault,
    name: str,
    default: int | float | None,
    parser: Callable[[str], int | float],
) -> int | float | None:
    if value is not _EnvironmentDefault.UNSET:
        return value
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    if raw_value.lower() == "none":
        return None
    try:
        return parser(raw_value)
    except ValueError as error:
        raise ValueError(f"环境变量 {name} 的值无效：{raw_value!r}") from error


class PoolClosedError(RuntimeError):
    """池已关闭或流清理失败，不能继续分配实例。"""


class PoolTimeoutError(TimeoutError):
    """等待实例或等待池排空超过期限。"""


class PoolQueueFullError(RuntimeError):
    """等待人数已达上限，请由调用方实施背压。"""


def _validate_timeout(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("超时必须是有限的非负秒数或 None")
    return float(value)


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


@dataclass(frozen=True)
class _SessionUser:
    id: str


def _resolve_user(user_id: str | None) -> CurrentUser:
    current_user = get_current_user()
    if user_id is None:
        if current_user is None:
            raise ValueError("必须传入 user_id 或先设置当前用户")
        user_id = str(current_user.id)
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id 必须是非空字符串")
    if current_user is not None and str(current_user.id) == user_id:
        return current_user
    return _SessionUser(user_id)


@dataclass(eq=False)
class _ClientSlot:
    client: DeerFlowClient
    generation: int = 0
    healthy: bool = True


class DeerFlowClientPool:
    """固定容量的同步池，按等待者入队顺序分配空闲实例。

    除 config_path 外的 client_kwargs 原样传给 DeerFlowClient，配置仅在建池时
    加载一次。config_path 未传或为 None 时使用包所在项目根目录的 config.yaml，
    不受调用方工作目录影响；显式相对路径仍相对于调用方工作目录。
    client_factory 与 client_kwargs 互斥，工厂必须每次返回独立客户端。
    自定义工厂自行负责配置加载。
    acquire_timeout=None 表示无限等待，0 表示立即尝试；max_waiters=None
    表示不限制等待人数，0 表示不排队。size 限制借用数，不限制后台任务数量。

    三个参数省略时，分别读取 DEER_FLOW_CLIENT_POOL_SIZE、
    DEER_FLOW_CLIENT_POOL_ACQUIRE_TIMEOUT、DEER_FLOW_CLIENT_POOL_MAX_WAITERS。
    未设置或留空时默认使用 4、None、100。后两个环境变量支持 none（不区分
    大小写）表示无限制。显式传参优先，包括显式 None；环境变量在建池时读取，
    不会改变已有池。环境文件沿用框架加载机制，不额外覆盖进程环境变量。
    """

    def __init__(
        self,
        size: int | _EnvironmentDefault = _EnvironmentDefault.UNSET,
        *,
        acquire_timeout: float | None | _EnvironmentDefault = _EnvironmentDefault.UNSET,
        max_waiters: int | None | _EnvironmentDefault = _EnvironmentDefault.UNSET,
        client_factory: Callable[[], DeerFlowClient] | None = None,
        **client_kwargs: Any,
    ) -> None:
        size = _resolve_pool_option(size, "DEER_FLOW_CLIENT_POOL_SIZE", 4, int)
        acquire_timeout = _resolve_pool_option(acquire_timeout, "DEER_FLOW_CLIENT_POOL_ACQUIRE_TIMEOUT", None, float)
        max_waiters = _resolve_pool_option(max_waiters, "DEER_FLOW_CLIENT_POOL_MAX_WAITERS", 100, int)
        self._validate_options(size, max_waiters, client_factory, client_kwargs)
        self._acquire_timeout = _validate_timeout(acquire_timeout)
        self._size = size
        self._max_waiters = max_waiters
        self._condition = threading.Condition()
        self._waiters: deque[object] = deque()
        self._borrowers: set[int] = set()
        self._closed = False
        self._failure: BaseException | None = None
        self._generation = 0
        self._available = deque(self._create_slots(size, client_factory, client_kwargs))

    @staticmethod
    def _validate_options(size, max_waiters, client_factory, client_kwargs) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError("size 必须是正整数")
        if max_waiters is not None and (isinstance(max_waiters, bool) or not isinstance(max_waiters, int) or max_waiters < 0):
            raise ValueError("max_waiters 必须是非负整数或 None")
        if client_factory is not None and (not callable(client_factory) or client_kwargs):
            raise ValueError("client_factory 必须可调用，且不能同时传入客户端构造参数")

    @staticmethod
    def _create_slots(size, client_factory, client_kwargs) -> list[_ClientSlot]:
        # 配置加载会修改进程级状态，建池时串行初始化，避免池之间重复冷启动。
        with _INITIALIZATION_LOCK:
            options = dict(client_kwargs)
            if client_factory is None:
                load_client_config(options.pop("config_path", None))
            factory = client_factory or (lambda: DeerFlowClient(**options))
            clients = [factory() for _ in range(size)]
            if len({id(client) for client in clients}) != size:
                raise ValueError("client_factory 必须为每个槽位创建不同实例")
            return [_ClientSlot(client) for client in clients]

    @contextmanager
    def session(self, user_id: str | None = None, *, timeout: float | None = None) -> Iterator[AgentClientSession]:
        """独占借用；timeout=None 沿用池默认值，用户缺省时捕获当前用户。

        用户只在底层方法、生成器推进和关闭期间绑定，不跨 yield 保留 Token，
        防止调用方上下文泄漏。退出时关闭未消费完的流，之后才归还实例。
        """
        user = _resolve_user(user_id)
        wait_timeout = self._acquire_timeout if timeout is None else _validate_timeout(timeout)
        slot = self._acquire(wait_timeout)
        session = AgentClientSession(self, slot, user)
        try:
            yield session
        except BaseException as error:
            try:
                session.close()
            except BaseException as cleanup_error:
                error.add_note(f"归还客户端时清理失败：{cleanup_error!r}")
            raise
        else:
            session.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise PoolClosedError("客户端池已关闭" if self._failure is None else "流清理失败，客户端池已停止分配") from self._failure

    def _acquire(self, timeout: float | None) -> _ClientSlot:
        owner = threading.get_ident()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            self._ensure_open()
            if owner in self._borrowers:
                raise RuntimeError("同一线程不能嵌套借用同一个池，请复用已有 session")
            if self._available and not self._waiters:
                return self._take_slot(owner)
            if timeout == 0:
                raise PoolTimeoutError("当前没有空闲客户端")
            return self._wait_for_slot(owner, deadline)

    def _take_slot(self, owner: int) -> _ClientSlot:
        self._borrowers.add(owner)
        return self._available.popleft()

    def _wait_for_slot(self, owner: int, deadline: float | None) -> _ClientSlot:
        if self._max_waiters is not None and len(self._waiters) >= self._max_waiters:
            raise PoolQueueFullError("客户端池等待队列已满")
        ticket = object()
        self._waiters.append(ticket)
        try:
            while True:
                self._ensure_open()
                remaining = _remaining(deadline)
                if remaining == 0:
                    raise PoolTimeoutError("等待空闲客户端超时")
                if self._waiters[0] is ticket and self._available:
                    return self._take_slot(owner)
                self._condition.wait(remaining)
        finally:
            self._waiters.remove(ticket)
            self._condition.notify_all()

    def _release(self, slot: _ClientSlot, owner: int) -> None:
        with self._condition:
            self._borrowers.remove(owner)
            if not self._closed and slot.healthy:
                self._available.append(slot)
            self._condition.notify_all()

    def _mark_failed(self, slot: _ClientSlot, error: BaseException) -> None:
        # 关闭失败不能证明底层执行已退出，停止新分配，避免不安全地重用资源。
        with self._condition:
            slot.healthy = False
            self._failure = error
            self._closed = True
            self._available.clear()
            self._condition.notify_all()

    def invalidate_agents(self) -> None:
        """下一次方法调用前刷新缓存；正在运行的流保持原配置直至关闭。"""
        with self._condition:
            self._generation += 1

    def _refresh(self, slot: _ClientSlot) -> None:
        with self._condition:
            generation = self._generation
        if slot.generation != generation:
            config = get_app_config()
            slot.client.reset_agent()
            slot.client._app_config = config
            slot.generation = generation

    @property
    def stats(self) -> dict[str, int | bool]:
        with self._condition:
            return {"size": self._size, "available": len(self._available), "borrowed": len(self._borrowers),
                    "waiting": len(self._waiters), "closed": self._closed, "failed": self._failure is not None}

    def close(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """拒绝新租约并唤醒排队者；可等待现有租约归还，不强行中断执行。

        超时后池仍关闭，可再次调用 close 等待排空。仅释放池持有的实例引用，
        不关闭进程共享的数据库、MCP、记忆管理器或沙盒 Provider。
        """
        timeout = _validate_timeout(timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            if wait and threading.get_ident() in self._borrowers:
                raise RuntimeError("请先退出当前 session，再等待池关闭；或使用 wait=False")
            self._closed = True
            self._available.clear()
            self._condition.notify_all()
            while wait and self._borrowers:
                remaining = _remaining(deadline)
                if remaining == 0:
                    raise PoolTimeoutError("等待客户端池排空超时，执行中的实例尚未归还")
                self._condition.wait(remaining)

    def __enter__(self) -> DeerFlowClientPool:
        with self._condition:
            self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class AgentClientSession:
    """池借用期间的代理；禁止保存并跨租约复用，也不暴露底层 client。"""

    def __init__(self, pool: DeerFlowClientPool, slot: _ClientSlot, user: CurrentUser) -> None:
        self._pool = pool
        self._slot = slot
        self._user = user
        self._owner = threading.get_ident()
        self._closed = False
        self._in_call = False
        self._stream: _SessionStream | None = None

    def _check_owner(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("session 及其流必须在借用线程内使用和关闭")

    def _check_active(self) -> None:
        self._check_owner()
        if self._closed:
            raise RuntimeError("session 已归还，不能继续使用")
        if not self._slot.healthy:
            raise PoolClosedError("该实例的流清理失败，不能继续使用")
        if self._in_call:
            raise RuntimeError("session 不允许重入调用")

    @contextmanager
    def _bound_call(self) -> Iterator[None]:
        self._check_active()
        token = set_current_user(self._user)
        self._in_call = True
        try:
            yield
        finally:
            self._in_call = False
            reset_current_user(token)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        self._check_active()
        method = getattr(self._slot.client, name)
        if not callable(method):
            raise AttributeError(f"session 仅代理公开方法：{name}")

        @wraps(method)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            return self._invoke(name, args, kwargs)

        return invoke

    def _invoke(self, name: str, args: tuple, kwargs: dict) -> Any:
        with self._bound_call():
            if self._stream is not None:
                raise RuntimeError("请先消费完或关闭当前流，再调用其他客户端方法")
            if "user_id" in kwargs and kwargs["user_id"] != str(self._user.id):
                raise ValueError("调用的 user_id 必须与 session 绑定的用户一致")
            self._pool._refresh(self._slot)
            try:
                result = getattr(self._slot.client, name)(*args, **kwargs)
                if isinstance(result, Iterator):
                    self._stream = _SessionStream(self, result)
                    return self._stream
                return result
            finally:
                # 部分写操作可能已提交后才报错，失败路径同样需要使旧缓存失效。
                if name in _INVALIDATING_METHODS:
                    self._pool.invalidate_agents()

    def close(self) -> None:
        self._check_owner()
        if self._closed:
            return
        if self._in_call:
            raise RuntimeError("客户端操作仍在执行，不能归还实例")
        try:
            if self._stream is not None:
                self._stream.close()
        finally:
            self._closed = True
            self._pool._release(self._slot, self._owner)


class _SessionStream(Generator[Any, None, Any]):
    """每次推进或关闭时绑定用户，避免在生成器挂起期间污染调用方上下文。"""

    def __init__(self, session: AgentClientSession, iterator: Iterator) -> None:
        self._session = session
        self._iterator = iterator
        self._closed = False

    def __next__(self) -> Any:
        return self._advance("__next__")

    def send(self, value: None) -> Any:
        return self._advance("__next__") if value is None else self._advance("send", value)

    def throw(self, *args: Any) -> Any:
        return self._advance("throw", *args)

    def _advance(self, method: str, *args: Any) -> Any:
        self._session._check_owner()
        if self._closed:
            raise StopIteration
        with self._session._bound_call():
            try:
                return getattr(self._iterator, method)(*args)
            except StopIteration:
                self._close_bound()
                raise
            except BaseException as error:
                try:
                    self._close_bound()
                except BaseException as cleanup_error:
                    error.add_note(f"流关闭失败：{cleanup_error!r}")
                raise

    def _close_bound(self) -> None:
        try:
            close = getattr(self._iterator, "close", None)
            if close is not None:
                close()
        except BaseException as error:
            self._session._pool._mark_failed(self._session._slot, error)
            raise
        finally:
            self._closed = True
            self._session._stream = None

    def close(self) -> None:
        self._session._check_owner()
        if not self._closed:
            with self._session._bound_call():
                self._close_bound()

    def __enter__(self) -> _SessionStream:
        self._session._check_active()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_value is None:
            self.close()
        else:
            try:
                self.close()
            except BaseException as error:
                exc_value.add_note(f"流关闭失败：{error!r}")
