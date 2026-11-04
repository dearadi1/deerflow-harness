"""跨事件循环记录本轮实际工作，取消 Future 不等于协程或线程已退出。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from functools import partial


class TurnTasks:
    def __init__(self, cancellation) -> None:
        self._cancellation = cancellation
        self._lock = threading.Lock()
        self._pending: dict[Future, Callable[[], None] | None] = {}
        self._roots: set[Future] = set()

    def track(self, completion: Future, cancel=None, *, root=False) -> None:
        with self._lock:
            self._pending[completion] = cancel
            if root:
                self._roots.add(completion)
        completion.add_done_callback(self._discard)

    def _discard(self, completion: Future) -> None:
        with self._lock:
            self._pending.pop(completion, None)
            self._roots.discard(completion)

    def track_task(self, task: asyncio.Task) -> None:
        completion = Future()
        self.track(completion, partial(self._cancel_task, task))
        task.add_done_callback(lambda _: completion.set_result(None))

    def _cancel_task(self, task: asyncio.Task) -> None:
        def cancel_once():
            # 根任务的取消会沿 await 传播，不能再次打断子任务已经开始的收尾。
            if (
                not task.done()
                and not task.cancelling()
                and task not in self._cancellation.cleanup_tasks
            ):
                task.cancel()

        if not task.get_loop().is_closed():
            task.get_loop().call_soon_threadsafe(cancel_once)

    def submit(
        self, coroutine, loop: asyncio.AbstractEventLoop, *, cancel_on_drain=True
    ) -> Future:
        result = Future()
        completion = Future()
        self.track(completion, result.cancel if cancel_on_drain else None, root=True)
        context = copy_context()
        try:
            loop.call_soon_threadsafe(
                self._start,
                coroutine,
                loop,
                result,
                completion,
                context,
                cancel_on_drain,
            )
        except BaseException:
            coroutine.close()
            completion.set_result(None)
            raise
        return result

    def _start(
        self, coroutine, loop, result, completion, context, cancel_on_drain
    ) -> None:
        try:
            task = loop.create_task(coroutine, context=context)
        except BaseException as error:  # noqa: BLE001 - 启动异常必须传回等待线程并解除完成屏障。
            coroutine.close()
            if result.set_running_or_notify_cancel():
                result.set_exception(error)
            completion.set_result(None)
            return
        task.add_done_callback(partial(self._finished, result, completion))
        result.add_done_callback(
            lambda future: self._cancel_task(task) if future.cancelled() else None
        )
        if cancel_on_drain and self._cancellation.is_set():
            self._cancel_task(task)

    @staticmethod
    def _finished(result: Future, completion: Future, task: asyncio.Task) -> None:
        # set_running_or_notify_cancel 与外部 Future.cancel 原子仲裁，避免完成时竞态。
        try:
            if task.cancelled():
                result.cancel()
            elif result.set_running_or_notify_cancel():
                if task.exception() is not None:
                    result.set_exception(task.exception())
                else:
                    result.set_result(task.result())
            elif not task.cancelled():
                task.exception()
        finally:
            completion.set_result(None)

    async def drain(self) -> None:
        while True:
            with self._lock:
                # 先让根协程完整退出，再取消其遗留任务，保留根协程的收尾所有权。
                pending = (
                    {future: self._pending[future] for future in self._roots}
                    if self._roots
                    else dict(self._pending)
                )
            if not pending:
                return
            for cancel in pending.values():
                if cancel is not None:
                    cancel()
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in pending),
                return_exceptions=True,
            )


def isolated_task_factory(loop, coroutine, **kwargs):
    from deerflow.runtime.turn_cancellation import (
        _TURN_CANCELLATION,
        get_turn_cancellation_event,
    )

    context = kwargs.get("context")
    cancellation = (
        context.get(_TURN_CANCELLATION)
        if context is not None
        else get_turn_cancellation_event()
    )
    task = asyncio.Task(coroutine, loop=loop, **kwargs)
    if cancellation is not None:
        cancellation.tasks.track_task(task)
    return task


class TurnTrackingExecutor(ThreadPoolExecutor):
    """共享子任务循环的线程按轮次记账，不能等待或取消其他用户的工作。"""

    def submit(self, function, /, *args, **kwargs):
        from deerflow.runtime.turn_cancellation import get_turn_cancellation_event

        cancellation = get_turn_cancellation_event()
        future = super().submit(function, *args, **kwargs)
        if cancellation is not None:
            cancellation.tasks.track(future)
        return future
