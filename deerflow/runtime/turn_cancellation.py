"""向本轮同步工具传递停止请求，避免依赖事件循环处理取消回调。"""

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import contextmanager
from contextvars import ContextVar


class TurnCancellation(threading.Event):
    """直接通知正在等待的操作，取消不依赖工具输出或定时检查。"""

    def __init__(self) -> None:
        super().__init__()
        self._callbacks: dict[object, Callable[[], None]] = {}
        self._callbacks_lock = threading.Lock()
        self.cleanup_tasks: set[asyncio.Task] = set()
        self.failure: BaseException | None = None

    def set(self) -> None:
        with self._callbacks_lock:
            super().set()
            callbacks = tuple(self._callbacks.values())
        for callback in callbacks:
            callback()

    def subscribe(self, callback: Callable[[], None]) -> object:
        token = object()
        with self._callbacks_lock:
            stopping = self.is_set()
            if not stopping:
                self._callbacks[token] = callback
        if stopping:
            callback()
        return token

    def unsubscribe(self, token: object) -> None:
        with self._callbacks_lock:
            self._callbacks.pop(token, None)

    def protect_cleanup(self, task: asyncio.Task) -> None:
        self.cleanup_tasks.add(task)
        task.add_done_callback(self.cleanup_tasks.discard)


_TURN_CANCELLATION: ContextVar[TurnCancellation | None] = ContextVar("turn_cancellation", default=None)
_TURN_LOOP: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar("turn_cancellation_loop", default=None)


def get_turn_cancellation_event() -> TurnCancellation | None:
    return _TURN_CANCELLATION.get()


def raise_if_turn_cancelled() -> None:
    cancellation = get_turn_cancellation_event()
    if cancellation is not None and cancellation.is_set():
        raise asyncio.CancelledError


@contextmanager
def turn_cancellation_scope(cancellation: TurnCancellation | None) -> Iterator[None]:
    token = _TURN_CANCELLATION.set(cancellation)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    loop_token = _TURN_LOOP.set(loop if cancellation is not None else None)
    try:
        yield
    finally:
        _TURN_LOOP.reset(loop_token)
        _TURN_CANCELLATION.reset(token)


async def _invoke_cancellable[T](cancellation: TurnCancellation, operation: Callable[[], Awaitable[T]]) -> T:
    raise_if_turn_cancelled()
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    token = cancellation.subscribe(lambda: loop.call_soon_threadsafe(task.cancel))
    try:
        raise_if_turn_cancelled()
        return await operation()
    finally:
        cancellation.unsubscribe(token)


def run_cancellable_async[T](operation: Callable[[], Awaitable[T]]) -> T:
    """同步工具等待原生异步 I/O；取消收尾完成后才唤醒同步调用方。"""
    cancellation = get_turn_cancellation_event()
    assert cancellation is not None
    loop = _TURN_LOOP.get()
    if loop is None:
        return asyncio.run(_invoke_cancellable(cancellation, operation))
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is loop:
        raise RuntimeError("同步沙盒方法必须在工作线程调用，不能阻塞托管事件循环")
    future = asyncio.run_coroutine_threadsafe(_invoke_cancellable(cancellation, operation), loop)
    try:
        return future.result()
    except FutureCancelledError as error:
        raise asyncio.CancelledError from error
