"""托管客户端的异步图执行与收尾，复用同步客户端的存储及重生成语义。"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from deerflow.environment import temporary_environment_scope, validate_temporary_environment
from deerflow.protocol import ClientStreamEvent
from deerflow.runtime.client_replay import ClientTurnReplay, _reserve_turn
from deerflow.runtime.turn_cancellation import get_turn_cancellation_event, turn_cancellation_scope
from deerflow.skills.presets import preset_store_for_config, public_skill_preset_scope
from deerflow.tracing.first_model_timing import with_first_model_timing


logger = logging.getLogger(__name__)


class StreamCleanupError(RuntimeError):
    """本轮资源或状态收尾失败，不能安全地重新分配实例。"""


async def _settle(task: asyncio.Future) -> Any:
    """保留收尾所有权，调用方反复取消也不能留下仍在写入的任务。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _sync_call(function, *args, **kwargs) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _settle(task)
        raise


class DrainingExecutor(ThreadPoolExecutor):
    """记录本轮同步节点的真实 Future，取消 asyncio 包装不代表线程退出。"""

    def __init__(self) -> None:
        super().__init__(thread_name_prefix="deerflow-managed-io")
        self._pending: set[Future] = set()
        self._pending_lock = threading.Lock()

    def submit(self, function, /, *args, **kwargs):
        future = super().submit(function, *args, **kwargs)
        with self._pending_lock:
            self._pending.add(future)
        future.add_done_callback(self._discard)
        return future

    def _discard(self, future: Future) -> None:
        with self._pending_lock:
            self._pending.discard(future)

    async def drain(self) -> None:
        while True:
            with self._pending_lock:
                pending = tuple(self._pending)
            if not pending:
                return
            await asyncio.gather(*(asyncio.wrap_future(future) for future in pending), return_exceptions=True)


class _AsyncCheckpointAdapter(BaseCheckpointSaver):
    """复用原同步 saver，避免新建内存库或连接导致同步和托管对话历史分裂。"""

    def __init__(self, saver: BaseCheckpointSaver) -> None:
        super().__init__(serde=saver.serde)
        self._saver = saver

    @property
    def config_specs(self):
        return self._saver.config_specs

    def get_next_version(self, current, channel):
        return self._saver.get_next_version(current, channel)

    def with_allowlist(self, extra_allowlist):
        return _AsyncCheckpointAdapter(self._saver.with_allowlist(extra_allowlist))

    async def aget_tuple(self, config):
        return await _sync_call(self._saver.get_tuple, config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        # LangGraph 的历史读取在工作线程中完整消费，避免游标跨线程推进。
        items = await _sync_call(lambda: list(self._saver.list(config, filter=filter, before=before, limit=limit)))
        for item in items:
            yield item

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await _sync_call(self._saver.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await _sync_call(self._saver.put_writes, config, writes, task_id, task_path)

    async def aget_delta_channel_history(self, *, config, channels):
        return await _sync_call(self._saver.get_delta_channel_history, config=config, channels=channels)


async def _drain_run_tasks(parent: asyncio.Task) -> list[BaseException]:
    # 托管轮次独占事件循环；收拢图或工具派生的任务后才能恢复 checkpoint。
    excluded = {parent, asyncio.current_task()}
    errors: list[BaseException] = []
    while pending := asyncio.all_tasks() - excluded:
        cancellation = get_turn_cancellation_event()
        for task in pending:
            # AIO 已收到停止并正在终止远端会话，不能在收拢派生任务时打断它的收尾。
            if cancellation is None or task not in cancellation.cleanup_tasks:
                task.cancel()
        results = await asyncio.gather(*pending, return_exceptions=True)
        errors.extend(result for result in results if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError))
    return errors


async def _finish_turn(stream, turn, replay, completed, executor, parent) -> None:
    errors: list[BaseException] = []
    if stream is not None:
        try:
            await stream.aclose()
        except BaseException as error:
            errors.append(error)
    errors.extend(await _drain_run_tasks(parent))
    await executor.drain()
    # 工具退出后解除停止标记，状态恢复和租约清理不能被同一信号再次中断。
    with turn_cancellation_scope(None):
        errors.extend(await _finish_turn_state(turn, replay, completed))
    if errors:
        raise StreamCleanupError("托管流收尾失败，实例池已停止分配") from BaseExceptionGroup("流收尾错误", errors)


async def _finish_turn_state(turn, replay, completed) -> list[BaseException]:
    errors: list[BaseException] = []
    try:
        await _sync_call(
            replay.finish_managed_turn,
            run_id=turn.context["run_id"] if turn is not None else None,
            completed=completed,
        )
    except BaseException as error:
        errors.append(error)
    if turn is not None:
        from deerflow.sandbox.lease import release_sandbox_execution_lease

        try:
            await _sync_call(release_sandbox_execution_lease, turn.context)
        except BaseException as error:
            errors.append(error)
    return errors


async def stream_managed_turn(
    client: Any,
    message: str,
    *,
    thread_id: str,
    replay_last_turn: bool,
    disabled_tools: list[str] | None,
    executor: DrainingExecutor,
    skill_preset: str | None = None,
    temporary_env: dict[str, str] | None = None,
    **kwargs: Any,
) -> AsyncIterator[ClientStreamEvent]:
    """仅在本轮独占的事件循环中调用，锁覆盖图退出、存储恢复和后台任务排空。"""
    disabled_tools = client._validate_disabled_tools(disabled_tools)
    temporary_env = validate_temporary_environment(temporary_env)
    preset_started_at = time.monotonic()
    preset = await _sync_call(preset_store_for_config(getattr(client, "_app_config", None)).resolve, skill_preset)
    preset_elapsed = time.monotonic() - preset_started_at
    with _reserve_turn(client, thread_id), public_skill_preset_scope(preset), temporary_environment_scope(temporary_env):
        replay = ClientTurnReplay(enabled=replay_last_turn)
        turn = None
        stream = None
        completed = False
        parent = asyncio.current_task()
        preparation_started_at = time.monotonic()
        preparation = asyncio.create_task(asyncio.to_thread(
            client._prepare_stream_turn, message, thread_id=thread_id,
            _turn_replay=replay, disabled_tools=disabled_tools, **kwargs,
        ))
        try:
            try:
                turn = await asyncio.shield(preparation)
            except asyncio.CancelledError:
                turn = await _settle(preparation)
                raise
            logger.info(
                "Managed stream prepared: preset_ms=%.1f preparation_ms=%.1f",
                preset_elapsed * 1000, (time.monotonic() - preparation_started_at) * 1000,
            )
            # 保留客户端缓存图及同步 accessor 的 saver，适配只作用于本轮图副本。
            graph = copy.copy(turn.agent)
            if graph.checkpointer is not None:
                graph.checkpointer = _AsyncCheckpointAdapter(graph.checkpointer)
            stream = graph.astream(
                turn.state, config=with_first_model_timing(turn.config), context=turn.context,
                stream_mode=["messages", "values", "custom"],
            )
            async for item in stream:
                for event in turn.parser.parse(item):
                    yield event
            # 图完成后先提交重生成，再交付 end；消费端取消不能回滚已经完成的生成。
            replay.committed = True
            completed = True
            for event in turn.parser.finish():
                yield event
        finally:
            cleanup = asyncio.create_task(_finish_turn(stream, turn, replay, completed, executor, parent))
            await _settle(cleanup)
