"""池托管的同步事件迭代器，支持从其他线程取消并等待资源归还。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextvars import copy_context
from typing import TYPE_CHECKING, Any

from deerflow.protocol import ClientStreamEvent
from deerflow.runtime.client_async import DrainingExecutor, StreamCleanupError, _sync_call, stream_managed_turn
from deerflow.runtime.turn_cancellation import TurnCancellation, turn_cancellation_scope
from deerflow.runtime.user_context import CurrentUser, reset_current_user, set_current_user
from deerflow.trace_context import bind_trace_id, generate_trace_id, get_current_trace_id, reset_trace_id

if TYPE_CHECKING:
    from deerflow.agent_client import DeerFlowClientPool, _ClientSlot


logger = logging.getLogger(__name__)


class StreamCloseTimeoutError(TimeoutError):
    """已请求停止，但执行或清理尚未完成，实例仍不可复用。"""


class ManagedStreamWorker:
    """每个槽位复用一个循环，缓存模型的异步连接不能跨轮迁移到新循环。"""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop
        self.executor: DrainingExecutor
        self._ready = threading.Event()
        self._close_lock = threading.Lock()
        self._stop_requested = False
        self._error: BaseException | None = None
        self.thread = threading.Thread(target=self._serve, name="deerflow-managed-stream", daemon=True)
        self.thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error

    def _serve(self) -> None:
        try:
            with DrainingExecutor() as executor, asyncio.Runner() as runner:
                self.executor = executor
                self.loop = runner.get_loop()
                self.loop.set_default_executor(executor)
                self._ready.set()
                self.loop.run_forever()
        except BaseException as error:
            self._error = error
        finally:
            self._ready.set()

    def start(self, stream: ManagedAgentStream) -> None:
        if self._error is not None:
            raise self._error
        self.loop.call_soon_threadsafe(stream._schedule)

    def close(self, *, wait: bool, timeout: float | None = None) -> bool:
        # loop.stop 只能提交一次，重复提交可能打断 Runner 自身的关闭阶段。
        with self._close_lock:
            if not self._stop_requested and not self.loop.is_closed():
                self._stop_requested = True
                self.loop.call_soon_threadsafe(self.loop.stop)
        if wait:
            self.thread.join(timeout)
            if not self.thread.is_alive() and self._error is not None:
                raise StreamCleanupError("托管执行线程关闭失败") from self._error
        return not self.thread.is_alive()


class ManagedAgentStream(Iterator[ClientStreamEvent]):
    """单消费者事件流；close 可从任意其他线程调用，不依赖下一帧到达。"""

    def __init__(
        self, pool: DeerFlowClientPool, slot: _ClientSlot, owner: object,
        user: CurrentUser, message: str, options: dict[str, Any], buffer_size: int,
    ) -> None:
        self._pool = pool
        self._slot = slot
        self._owner = owner
        self._user = user
        self._message = message
        self._options = options
        self._buffer_size = buffer_size
        self._events: deque[ClientStreamEvent] = deque()
        self._condition = threading.Condition()
        self._done = threading.Event()
        self._cancellation = TurnCancellation()
        self._stopping = False
        self._error: BaseException | None = None
        self._cleanup_error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._space: asyncio.Event | None = None
        self._trace_id = get_current_trace_id() or generate_trace_id()
        self._context = copy_context()
        self._worker: ManagedStreamWorker | None = None

    @property
    def closed(self) -> bool:
        return self._done.is_set()

    def _start(self, *, started_at: float, pool_wait: float) -> None:
        self._started_at = started_at
        self._pool_wait = pool_wait
        self._first_block_logged = False
        self._worker = self._pool._get_stream_worker(self._slot)
        self._worker.start(self)

    def _schedule(self) -> None:
        assert self._worker is not None
        self._worker.loop.create_task(self._run(), context=self._context)

    def __next__(self) -> ClientStreamEvent:
        with self._condition:
            while not self._events and not self.closed and not self._stopping:
                self._condition.wait()
            if self._stopping:
                raise StopIteration
            if self._events:
                event = self._events.popleft()
                self._wake_producer()
                return event
            if self._error is not None:
                raise self._error
            raise StopIteration

    def _wake_producer(self) -> None:
        if self._loop is not None and self._space is not None:
            self._loop.call_soon_threadsafe(self._space.set)

    def _request_stop(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            # 同步工具必须先收到停止请求，不能等事件循环执行 task.cancel 回调。
            self._cancellation.set()
            self._events.clear()
            self._condition.notify_all()
            if not self.closed and self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)

    def close(self, *, timeout: float | None = None) -> None:
        """成功返回才表示实例归还；超时只结束等待，后台仍负责清理。"""
        from deerflow.agent_client import _validate_timeout

        timeout = _validate_timeout(timeout)
        if self._worker is not None and threading.current_thread() is self._worker.thread:
            raise RuntimeError("不能在托管执行线程中等待自身关闭")
        self._request_stop()
        if not self._done.wait(timeout):
            raise StreamCloseTimeoutError("等待流关闭超时，执行或清理尚未结束")
        if self._cleanup_error is not None:
            raise self._cleanup_error

    async def _publish(self, event: ClientStreamEvent) -> None:
        space = self._space
        assert space is not None
        while True:
            with self._condition:
                if self._stopping:
                    raise asyncio.CancelledError
                if len(self._events) < self._buffer_size:
                    self._events.append(event)
                    self._condition.notify_all()
                    return
                space.clear()
            await space.wait()

    async def _produce(self, executor: DrainingExecutor) -> None:
        await _sync_call(self._pool._refresh, self._slot)
        stream = stream_managed_turn(self._slot.client, self._message, executor=executor, **self._options)
        try:
            async for event in stream:
                await self._publish(event)
                self._log_first_block(event)
        finally:
            await stream.aclose()

    def _log_first_block(self, event: ClientStreamEvent) -> None:
        # 标题、用量等历史元数据不能冒充首 token；仅记耗时，不记录用户输入或模型内容。
        if self._first_block_logged or event.type not in {"reasoning_content", "answer_content", "tool_chunk", "tool_calls"}:
            return
        if event.type in {"reasoning_content", "answer_content"} and not event.data.content:
            return
        if getattr(getattr(event.data, "additional_kwargs", None), "deerflow_error_fallback", False):
            return
        self._first_block_logged = True
        logger.info(
            "Managed stream first block: elapsed_ms=%.1f pool_wait_ms=%.1f event_type=%s",
            (time.monotonic() - self._started_at) * 1000,
            self._pool_wait * 1000,
            event.type,
        )

    async def _run(self) -> None:
        user_token = set_current_user(self._user)
        trace_token = bind_trace_id(self._trace_id)
        try:
            with self._condition:
                self._loop = asyncio.get_running_loop()
                self._task = asyncio.current_task()
                self._space = asyncio.Event()
                stopping = self._stopping
            if not stopping:
                assert self._worker is not None
                with turn_cancellation_scope(self._cancellation):
                    await self._produce(self._worker.executor)
        except asyncio.CancelledError:
            if not self._stopping:
                self._error = RuntimeError("托管执行被意外取消")
        except BaseException as error:
            self._error = error
            if isinstance(error, StreamCleanupError):
                self._cleanup_error = error
                self._pool._mark_failed(self._slot, error)
        finally:
            if self._cancellation.failure is not None and self._cleanup_error is None:
                self._cleanup_error = self._cancellation.failure
                self._error = self._cleanup_error
                self._pool._mark_failed(self._slot, self._cleanup_error)
            reset_trace_id(trace_token)
            reset_current_user(user_token)
            with self._condition:
                self._loop = None
                self._task = None
                self._space = None
            self._pool._release(self._slot, self._owner)
            with self._condition:
                self._done.set()
                self._condition.notify_all()

    def __enter__(self) -> ManagedAgentStream:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.close()
        except BaseException as error:
            if exc_value is None:
                raise
            exc_value.add_note(f"流关闭失败：{error!r}")
