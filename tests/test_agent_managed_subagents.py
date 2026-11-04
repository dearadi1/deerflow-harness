"""用真实池、图和独立子任务循环验证外部线程关闭的生命周期边界。"""

import asyncio
import importlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from types import MethodType, SimpleNamespace

import test_agent_managed_stream as support
from langgraph.graph import END, START, StateGraph

from deerflow.agent_client import (
    PoolClosedError,
    PoolTimeoutError,
    StreamCleanupError,
    StreamCloseTimeoutError,
)
from deerflow.agents.thread_state import get_thread_state_schema
from deerflow.runtime.client_replay import ReplayConflictError
from deerflow.subagents.executor import (
    SubagentExecutor,
    SubagentStatus,
    cleanup_background_task,
    request_cancel_background_task,
)


class ManagedSubagentTests(unittest.TestCase):
    def setUp(self):
        self.support = support.ManagedStreamTests()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.unwinding = threading.Event()
        self.execution_ids = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        self.release.set()
        for stream in self.support.streams:
            with suppress(StreamCleanupError):
                stream.close(timeout=5)
        for execution_id in self.execution_ids:
            cleanup_background_task(execution_id)

    def launch(self, body):
        executor = object.__new__(SubagentExecutor)
        executor.config = SimpleNamespace(name="controlled", timeout_seconds=60)
        executor.trace_id = "managed-child-test"
        executor._aexecute = MethodType(body, executor)
        execution_id = executor.execute_async("受控子任务")
        self.execution_ids.append(execution_id)
        return execution_id

    async def slow_child(self, executor, prompt, result):
        result.status = SubagentStatus.RUNNING
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.unwinding.set()
            while not self.release.is_set():
                await asyncio.sleep(0.005)
            self.finished.set()

    def harness(self, node, *, mode="full", size=1):
        graph, pool = self.support.harness(mode, size=size)
        builder = StateGraph(get_thread_state_schema(mode, 10))
        builder.add_node("delegate", node)
        builder.add_edge(START, "delegate")
        builder.add_edge("delegate", END)
        graph.graph = builder.compile(checkpointer=graph.saver)
        for slot in pool._available:
            slot.client._agent = graph.graph
        return graph, pool

    async def wait_started(self):
        while not self.started.is_set():
            await asyncio.sleep(0.005)

    async def delegate(self, state):
        execution_id = self.launch(self.slow_child)
        try:
            await self.wait_started()
            await asyncio.Event().wait()
        finally:
            request_cancel_background_task(execution_id)

    def assert_retry(self, pool, mode="full"):
        graph = support.ControlledGraph(pool._available[0].client._checkpointer, mode)
        graph.release.set()
        for slot in pool._available:
            slot.client._agent = graph.graph
        self.assertEqual(
            list(self.support.start(pool, "停止后立即继续"))[-1].type, "end"
        )

    def test_external_close_waits_for_isolated_child_and_keeps_same_thread_locked(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                self.started.clear()
                self.release.clear()
                self.finished.clear()
                self.unwinding.clear()
                _, pool = self.harness(self.delegate, mode=mode, size=2)
                stream = self.support.start(pool)
                with ThreadPoolExecutor(max_workers=4) as threads:
                    consumer = threads.submit(list, stream)
                    self.assertTrue(self.started.wait(5))
                    closers = [
                        threads.submit(stream.close, timeout=0.05) for _ in range(3)
                    ]
                    for closer in closers:
                        with self.assertRaises(StreamCloseTimeoutError):
                            closer.result(2)
                    self.assertTrue(self.unwinding.wait(2))
                    self.assertFalse(self.finished.is_set())
                    self.assertFalse(stream.closed)
                    self.assertEqual(pool.stats["borrowed"], 1)
                    contender = self.support.start(pool)
                    with self.assertRaises(ReplayConflictError):
                        list(contender)
                    self.release.set()
                    threads.submit(stream.close, timeout=3).result(4)
                    self.assertFalse(
                        any(event.type == "end" for event in consumer.result(1))
                    )
                self.assertTrue(self.finished.is_set())
                self.assert_retry(pool, mode)
                stream.close(timeout=0)

    def test_poller_error_grace_does_not_release_slot_while_child_unwinds(self):
        task_tool = importlib.import_module("deerflow.tools.builtins.task_tool")
        self.enterContext(
            support.patch.object(task_tool, "_SUBAGENT_POLL_INTERVAL_SECONDS", 0.005)
        )
        grace_finished = threading.Event()

        async def node(state):
            execution_id = self.launch(self.slow_child)
            await self.wait_started()
            request_cancel_background_task(execution_id)
            await task_tool._finalize_interrupted_subagent(
                SimpleNamespace(config={}),
                execution_id,
                "controlled",
                1000,
                grace_seconds=0.02,
            )
            grace_finished.set()
            raise RuntimeError("模拟轮询失败")

        _, pool = self.harness(node)
        stream = self.support.start(pool)
        self.assertTrue(grace_finished.wait(5))
        with ThreadPoolExecutor() as threads:
            consumer = threads.submit(list, stream)
            with self.assertRaises(TimeoutError):
                consumer.result(0.05)
            with self.assertRaises(PoolTimeoutError):
                self.support.start(pool, acquire_timeout=0)
            self.release.set()
            with self.assertRaisesRegex(RuntimeError, "模拟轮询失败"):
                consumer.result(3)
        self.assertTrue(self.finished.is_set())
        self.assert_retry(pool)

    def test_isolated_sync_tool_must_exit_after_its_async_wrapper_is_cancelled(self):
        def sync_tool():
            self.started.set()
            if not self.release.wait(5):
                raise TimeoutError("测试未释放同步子工具")
            self.finished.set()

        async def child(executor, prompt, result):
            result.status = SubagentStatus.RUNNING
            await asyncio.to_thread(sync_tool)
            return result

        async def node(state):
            self.launch(child)
            await self.wait_started()
            await asyncio.Event().wait()

        _, pool = self.harness(node)
        stream = self.support.start(pool)
        self.assertTrue(self.started.wait(5))
        with self.assertRaises(StreamCloseTimeoutError):
            stream.close(timeout=0.05)
        self.assertFalse(self.finished.is_set())
        self.assertEqual(pool.stats["borrowed"], 1)
        self.release.set()
        stream.close(timeout=3)
        self.assertTrue(self.finished.is_set())
        self.assert_retry(pool)

    def test_closing_one_turn_does_not_cancel_another_turn_on_shared_child_loop(self):
        _, pool = self.harness(self.delegate, size=2)
        first = self.support.start(pool)
        self.assertTrue(self.started.wait(5))
        self.started.clear()
        second = self.support.start(pool, thread_id="thread_002", user_id="user_002")
        self.assertTrue(self.started.wait(5))
        self.release.set()
        first.close(timeout=3)
        self.assertFalse(second.closed)
        self.assertEqual(pool.stats["borrowed"], 1)
        second.close(timeout=3)
        self.assertEqual(pool.stats["available"], 2)

    def test_cancel_before_isolated_submission_runs_does_not_lose_completion(self):
        from deerflow.subagents.executor import _get_isolated_subagent_loop

        loop = _get_isolated_subagent_loop()
        loop_blocked = threading.Event()
        submission_registered = threading.Event()

        def block_loop():
            loop_blocked.set()
            self.release.wait(5)

        loop.call_soon_threadsafe(block_loop)
        self.assertTrue(loop_blocked.wait(3))

        async def node(state):
            self.launch(self.slow_child)
            submission_registered.set()
            await asyncio.Event().wait()

        _, pool = self.harness(node)
        stream = self.support.start(pool)
        self.assertTrue(submission_registered.wait(3))
        with self.assertRaises(StreamCloseTimeoutError):
            stream.close(timeout=0.03)
        self.assertEqual(pool.stats["borrowed"], 1)
        self.release.set()
        stream.close(timeout=3)
        self.assertEqual(pool.stats["available"], 1)

    def test_isolated_cleanup_failure_reaches_external_closer_and_stops_pool(self):
        from deerflow.runtime.turn_cancellation import record_turn_cleanup_failure

        async def child(executor, prompt, result):
            result.status = SubagentStatus.RUNNING
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                record_turn_cleanup_failure(OSError("模拟子任务沙盒释放失败"))
                self.finished.set()

        async def node(state):
            self.launch(child)
            await self.wait_started()
            await asyncio.Event().wait()

        _, pool = self.harness(node)
        stream = self.support.start(pool)
        self.assertTrue(self.started.wait(3))
        with ThreadPoolExecutor() as threads:
            closer = threads.submit(stream.close, timeout=3)
            with self.assertRaises(StreamCleanupError):
                closer.result(4)
        self.assertTrue(self.finished.is_set())
        self.assertTrue(stream.closed)
        self.assertTrue(pool.stats["failed"])
        with self.assertRaises(PoolClosedError):
            self.support.start(pool)

    def test_detached_isolated_descendant_is_drained_before_returning_slot(self):
        descendant_started = threading.Event()

        async def descendant():
            descendant_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.unwinding.set()
                while not self.release.is_set():
                    await asyncio.sleep(0.005)
                self.finished.set()

        async def child(executor, prompt, result):
            asyncio.create_task(descendant())
            while not descendant_started.is_set():
                await asyncio.sleep(0.005)
            result.try_set_terminal(SubagentStatus.COMPLETED, result="子任务主体已结束")
            self.started.set()
            return result

        async def node(state):
            self.launch(child)
            await self.wait_started()
            await asyncio.Event().wait()

        _, pool = self.harness(node)
        stream = self.support.start(pool)
        self.assertTrue(self.started.wait(3))
        with self.assertRaises(StreamCloseTimeoutError):
            stream.close(timeout=0.03)
        self.assertTrue(self.unwinding.wait(2))
        self.assertFalse(self.finished.is_set())
        self.release.set()
        stream.close(timeout=3)
        self.assertTrue(self.finished.is_set())
        self.assertEqual(pool.stats["available"], 1)


if __name__ == "__main__":
    unittest.main()
