"""用真实 LangGraph 与内存/SQLite 存储验证跨线程停止及资源归还。"""

import asyncio
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from deerflow.agent_client import DeerFlowClientPool, PoolClosedError, PoolTimeoutError, StreamCleanupError, StreamCloseTimeoutError
from deerflow.agents.thread_state import get_thread_state_schema
from deerflow.client import DeerFlowClient
from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.protocol import parse_stream_event
from deerflow.runtime.checkpoint_state import CheckpointStateAccessor
from deerflow.runtime.checkpoint_cache.memory import MemoryCheckpointHistoryCache
from deerflow.runtime.checkpointer.cached_saver import CachedHistorySaver
from deerflow.runtime.client_replay import ReplayConflictError
from deerflow.runtime.user_context import get_current_user
from deerflow.sandbox import tools as sandbox_tools
from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.trace_context import get_current_trace_id


class ControlledGraph:
    def __init__(self, saver, mode, *, synchronous=False):
        self.saver = saver
        self.mode = mode
        self.entered = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.cleanup_release = threading.Event()
        self.cleanup_release.set()
        self.fail = False
        self.observed = []
        self.loops = []
        builder = StateGraph(get_thread_state_schema(mode, 10))
        builder.add_node("answer", self.sync_answer if synchronous else self.async_answer)
        builder.add_edge(START, "answer")
        builder.add_edge("answer", END)
        self.graph = builder.compile(checkpointer=saver)

    def observe(self):
        self.observed.append((get_current_user().id, get_current_trace_id()))
        self.entered.set()

    def result(self, state):
        if self.fail:
            raise RuntimeError("模拟模型失败")
        message = next(message for message in reversed(state["messages"]) if isinstance(message, HumanMessage))
        return {"messages": [AIMessage(content=f"答复：{message.content}")]}

    async def async_answer(self, state):
        self.observe()
        self.loops.append(asyncio.get_running_loop())
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            return self.result(state)
        finally:
            while not self.cleanup_release.is_set():
                await asyncio.sleep(0.01)
            self.exited.set()

    def sync_answer(self, state):
        self.observe()
        try:
            if not self.release.wait(10):
                raise TimeoutError("测试未释放同步节点")
            return self.result(state)
        finally:
            self.exited.set()

    def client(self):
        client = object.__new__(DeerFlowClient)
        client._model_name = None
        client._thinking_enabled = False
        client._plan_mode = False
        client._subagent_enabled = False
        client._agent_name = None
        client._environment = None
        client._checkpointer = self.saver
        client._checkpoint_channel_mode = self.mode
        client._agent = self.graph
        client._app_config = SimpleNamespace(models=[])
        client._ensure_agent = Mock()
        return client

    def snapshot(self, thread_id="thread_001"):
        accessor = CheckpointStateAccessor.bind(self.graph, self.saver, mode=self.mode)
        return accessor.get({"configurable": {"thread_id": thread_id}})


class BlockingSaver(InMemorySaver):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def put(self, config, checkpoint, metadata, new_versions):
        if not self.entered.is_set():
            self.entered.set()
            if not self.release.wait(10):
                raise TimeoutError("测试未释放 checkpoint 写入")
        return super().put(config, checkpoint, metadata, new_versions)


class ManagedStreamTests(unittest.TestCase):
    def setUp(self):
        self.graphs = []
        self.pools = []
        self.streams = []
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch("deerflow.client.build_tracing_callbacks", return_value=[]))
        self.enterContext(patch("deerflow.client.inject_langfuse_metadata"))
        self.sandbox_release = self.enterContext(patch("deerflow.sandbox.lease.release_sandbox_execution_lease"))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for graph in self.graphs:
            graph.release.set()
            graph.cleanup_release.set()
        for stream in self.streams:
            try:
                stream.close(timeout=5)
            except StreamCleanupError:
                pass
        for pool in self.pools:
            pool.close(timeout=5)

    def harness(self, mode="full", *, backend="memory", synchronous=False, size=1, saver=None):
        if saver is not None:
            pass
        elif backend == "sqlite":
            saver = self.enterContext(SqliteSaver.from_conn_string(f"{self.directory}/{len(self.graphs)}.db"))
            saver.setup()
        else:
            saver = InMemorySaver()
        graph = ControlledGraph(saver, mode, synchronous=synchronous)
        pool = DeerFlowClientPool(size=size, client_factory=graph.client)
        self.graphs.append(graph)
        self.pools.append(pool)
        return graph, pool

    def start(self, pool, message="问题", **kwargs):
        options = {"thread_id": "thread_001", "user_id": "user_001", **kwargs}
        stream = pool.start_stream(message, **options)
        self.streams.append(stream)
        return stream

    def tool_harness(self, tool, arguments, mode="full", *, backend="memory"):
        graph, pool = self.harness(mode, backend=backend)
        builder = StateGraph(get_thread_state_schema(mode, 10))
        builder.add_node("choose", lambda state: {"messages": [AIMessage(
            content="", tool_calls=[{"name": tool.name, "args": arguments, "id": "tool_1", "type": "tool_call"}],
        )]})
        builder.add_node("tools", ToolNode([tool]))
        builder.add_edge(START, "choose")
        builder.add_edge("choose", "tools")
        builder.add_edge("tools", END)
        graph.graph = builder.compile(checkpointer=graph.saver)
        for slot in pool._available:
            slot.client._agent = graph.graph
        return graph, pool

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("等待工具进入耗时操作超时")
            time.sleep(0.01)

    def assert_prompt_close(self, stream):
        started = time.monotonic()
        stream.close(timeout=1.5)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(stream.closed)
        self.assertFalse(any(event.type == "end" for event in stream))

    def assert_process_stopped(self, process_id):
        try:
            os.kill(int(process_id), 0)
        except ProcessLookupError:
            return
        try:
            status = Path(f"/proc/{process_id}/stat").read_text().rsplit(") ", 1)[1].split()[0]
        except FileNotFoundError:
            with self.assertRaises(ProcessLookupError):
                os.kill(int(process_id), 0)
        else:
            # Linux 容器的 PID 1 可能尚未回收孤儿僵尸；它已退出，不会再执行脚本。
            self.assertEqual(status, "Z")

    def test_close_interrupts_aio_shell_env_and_scoped_commands(self):
        for variant in ("shell", "bash_env", "scoped_shell", "stop_failure"):
            with self.subTest(variant=variant):
                entered = threading.Event()
                request_cancelled = threading.Event()
                killed = threading.Event()
                sandbox = AioSandbox("test", "http://127.0.0.1:1", home_dir="/home/test")
                self.addCleanup(sandbox.close)
                client = Mock()
                self.enterContext(patch.object(sandbox, "_client", client))
                self.enterContext(patch.object(sandbox, "_create_shell_session", return_value="shell-owned"))
                self.enterContext(patch.object(sandbox, "_create_bash_session", return_value="bash-owned"))

                async def execute(**kwargs):
                    entered.set()
                    try:
                        await asyncio.sleep(60)
                    finally:
                        request_cancelled.set()

                async def close_session(*args, **kwargs):
                    self.assertTrue(request_cancelled.is_set())
                    await asyncio.sleep(0.05)
                    if variant == "stop_failure":
                        raise OSError("无法连接 AIO")
                    killed.set()
                    return SimpleNamespace(success=True)

                async_client = SimpleNamespace(
                    shell=SimpleNamespace(exec_command=AsyncMock(side_effect=execute), cleanup_session=AsyncMock(side_effect=close_session)),
                    bash=SimpleNamespace(exec=AsyncMock(side_effect=execute), close_session=AsyncMock(side_effect=close_session)),
                )
                self.enterContext(patch("deerflow.community.aio_sandbox.aio_sandbox.AsyncAioSandboxClient", return_value=async_client))
                graph, pool = self.harness()

                def answer(state):
                    return {"messages": [AIMessage(content=sandbox.execute_command_in_scope(
                        "sleep 60", env={"TEST": "1"} if variant == "bash_env" else None,
                        scope_id="scope_1" if variant == "scoped_shell" else None,
                    ))]}

                builder = StateGraph(get_thread_state_schema("full", 10))
                builder.add_node("answer", answer)
                builder.add_edge(START, "answer")
                builder.add_edge("answer", END)
                graph.graph = builder.compile(checkpointer=graph.saver)
                pool._available[0].client._agent = graph.graph
                stream = self.start(pool)
                self.assertTrue(entered.wait(5))
                if variant == "stop_failure":
                    with self.assertRaises(StreamCleanupError):
                        stream.close(timeout=3)
                    self.assertTrue(pool.stats["failed"])
                    with self.assertRaises(PoolClosedError):
                        self.start(pool)
                    continue
                self.assert_prompt_close(stream)
                self.assertTrue(killed.is_set())
                if variant == "bash_env":
                    self.assertNotIn("async_mode", async_client.bash.exec.call_args.kwargs)
                    async_client.bash.close_session.assert_awaited_once_with("bash-owned", request_options=sandbox._bounded_cleanup_request_options())
                else:
                    self.assertNotIn("async_mode", async_client.shell.exec_command.call_args.kwargs)
                    async_client.shell.cleanup_session.assert_awaited_once_with("shell-owned", request_options=sandbox._bounded_cleanup_request_options())
                self.assertFalse(graph.snapshot().next)
                self.assertEqual(pool.stats["available"], 1)

    @unittest.skipIf(os.name == "nt", "真实进程组终止在 POSIX 验证")
    def test_close_kills_local_bash_and_children_before_returning_slot(self):
        sandbox = LocalSandbox("local")
        self.enterContext(patch.object(sandbox_tools, "ensure_sandbox_initialized_async", new_callable=AsyncMock))
        self.enterContext(patch.object(sandbox_tools, "ensure_sandbox_initialized", return_value=sandbox))
        self.enterContext(patch.object(sandbox_tools, "ensure_thread_directories_exist"))
        self.enterContext(patch.object(sandbox_tools, "is_local_sandbox", return_value=True))
        self.enterContext(patch.object(sandbox_tools, "is_host_bash_allowed", return_value=True))
        self.enterContext(patch.object(sandbox_tools, "validate_local_bash_command_paths"))
        self.enterContext(patch.object(sandbox_tools, "get_thread_data", return_value={"workspace_path": self.directory}))
        self.enterContext(patch("deerflow.config.app_config.get_app_config", return_value=SimpleNamespace(sandbox=None)))
        for mode in ("full", "delta"):
            for backend in ("memory", "sqlite"):
                with self.subTest(mode=mode, backend=backend):
                    marker = Path(self.directory) / f"{mode}_{backend}.pid"
                    script = (
                        "import os, subprocess, sys, time; from pathlib import Path; "
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                        f"Path({str(marker)!r}).write_text(str(os.getpid()) + ' ' + str(child.pid)); time.sleep(60)"
                    )
                    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
                    graph, pool = self.tool_harness(sandbox_tools.bash_tool, {"command": command}, mode, backend=backend)
                    stream = self.start(pool)
                    self.wait_until(marker.exists)
                    process_ids = marker.read_text().split()
                    self.assert_prompt_close(stream)
                    for process_id in process_ids:
                        self.assert_process_stopped(process_id)
                    self.assertFalse(graph.snapshot().next)
                    self.assertEqual(pool.stats["available"], 1)
                    retry = ControlledGraph(graph.saver, mode)
                    retry.release.set()
                    pool._available[0].client._agent = retry.graph
                    self.assertEqual(list(self.start(pool, "停止后继续"))[-1].type, "end")

    def test_cross_thread_close_without_next_frame_and_immediate_retry(self):
        for mode in ("full", "delta"):
            for backend in ("memory", "sqlite"):
                with self.subTest(mode=mode, backend=backend):
                    graph, pool = self.harness(mode, backend=backend)
                    stream = self.start(pool)
                    with ThreadPoolExecutor() as workers:
                        consumer = workers.submit(list, stream)
                        self.assertTrue(graph.entered.wait(5))
                        workers.submit(stream.close, timeout=3).result(4)
                        self.assertFalse(any(event.type == "end" for event in consumer.result(1)))
                    self.assertTrue(graph.exited.is_set())
                    self.assertTrue(stream.closed)
                    self.assertEqual(pool.stats["available"], 1)
                    snapshot = graph.snapshot()
                    self.assertFalse(snapshot.next)
                    self.assertFalse(snapshot.tasks)
                    self.assertEqual(snapshot.values["messages"][0].content, "问题")
                    graph.release.set()
                    events = list(self.start(pool, "重试", replay_last_turn=True, acquire_timeout=0))
                    self.assertEqual(events[-1].type, "end")
                    self.assertEqual(graph.snapshot().values["messages"][0].content, "重试")
                    self.assertIsNone(get_current_user())

    def test_normal_completion_releases_without_explicit_close_and_preserves_protocol(self):
        graph, pool = self.harness()
        graph.release.set()
        stream = self.start(pool, disabled_tools=["web_search"])
        events = list(stream)
        for event in events:
            self.assertEqual(parse_stream_event(event.model_dump(mode="json")), event)
        self.assertEqual(events[-1].type, "end")
        self.assertEqual(pool.stats["borrowed"], 0)
        self.assertTrue(stream.closed)
        self.assertTrue(graph.observed[0][1])
        self.assertEqual(graph.observed[0][0], "user_001")
        self.sandbox_release.assert_called_once()

    def test_sync_node_close_timeout_keeps_slot_and_thread_lock_until_worker_exits(self):
        graph, pool = self.harness(synchronous=True, size=2)
        stream = self.start(pool)
        self.assertTrue(graph.entered.wait(5))
        with self.assertRaises(StreamCloseTimeoutError):
            stream.close(timeout=0.05)
        self.assertFalse(stream.closed)
        self.assertEqual(pool.stats["borrowed"], 1)
        contender = self.start(pool)
        with self.assertRaises(ReplayConflictError):
            list(contender)
        graph.release.set()
        stream.close(timeout=3)
        self.assertTrue(graph.exited.is_set())
        self.assertEqual(pool.stats["borrowed"], 0)
        self.assertFalse(graph.snapshot().next)

    def test_repeated_close_does_not_cancel_cleanup_again(self):
        graph, pool = self.harness()
        graph.cleanup_release.clear()
        stream = self.start(pool)
        self.assertTrue(graph.entered.wait(5))
        for _ in range(2):
            with self.assertRaises(StreamCloseTimeoutError):
                stream.close(timeout=0.03)
        self.assertEqual(pool.stats["borrowed"], 1)
        graph.cleanup_release.set()
        stream.close(timeout=3)
        stream.close(timeout=0)
        self.assertEqual(pool.stats["available"], 1)

    def test_close_allows_required_sandbox_cleanup_commands_to_finish(self):
        graph, pool = self.harness()
        outputs = []
        self.sandbox_release.side_effect = lambda context: outputs.append(LocalSandbox("local").execute_command("printf cleaned"))
        stream = self.start(pool)
        self.assertTrue(graph.entered.wait(5))
        stream.close(timeout=3)
        self.assertEqual(outputs, ["cleaned"])
        self.assertEqual(pool.stats["available"], 1)

    def test_bounded_buffer_can_close_without_any_consumer(self):
        graph, pool = self.harness()
        graph.release.set()
        stream = self.start(pool, buffer_size=1)
        self.assertTrue(graph.entered.wait(5))
        stream.close(timeout=3)
        self.assertEqual(list(stream), [])
        self.assertEqual(pool.stats["borrowed"], 0)

    def test_consumer_failure_context_manager_closes_run(self):
        graph, pool = self.harness()
        with self.assertRaisesRegex(RuntimeError, "断连"):
            with self.start(pool) as stream:
                self.assertTrue(graph.entered.wait(5))
                raise RuntimeError("断连")
        self.assertTrue(stream.closed)
        self.assertEqual(pool.stats["available"], 1)

    def test_model_error_releases_slot_and_allows_regenerate(self):
        graph, pool = self.harness()
        graph.release.set()
        graph.fail = True
        with self.assertRaisesRegex(RuntimeError, "模拟模型失败"):
            list(self.start(pool))
        self.assertFalse(pool.stats["failed"])
        graph.fail = False
        self.assertEqual(list(self.start(pool, replay_last_turn=True))[-1].type, "end")

    def test_cancelled_regeneration_restores_previous_state(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                graph, pool = self.harness(mode)
                graph.release.set()
                list(self.start(pool, "原问题"))
                original = deepcopy(graph.snapshot().values)
                graph.release.clear()
                graph.entered.clear()
                stream = self.start(pool, "修改", replay_last_turn=True)
                self.assertTrue(graph.entered.wait(5))
                stream.close(timeout=3)
                self.assertEqual(graph.snapshot().values, original)
                self.assertFalse(graph.snapshot().next)

    def test_close_old_handle_does_not_stop_new_run_or_another_user(self):
        graph, pool = self.harness(size=2)
        old = self.start(pool)
        self.assertTrue(graph.entered.wait(5))
        old.close(timeout=3)
        graph.entered.clear()
        current = self.start(pool)
        other = self.start(pool, user_id="user_002", thread_id="thread_002")
        self.assertTrue(graph.entered.wait(5))
        old.close(timeout=0)
        self.assertEqual(pool.stats["borrowed"], 2)
        current.close(timeout=3)
        self.assertFalse(other.closed)
        other.close(timeout=3)
        self.assertEqual(pool.stats["available"], 2)

    def test_cancel_before_start_or_during_completion_never_leaks_slot(self):
        graph, pool = self.harness()
        graph.release.set()
        for index in range(20):
            stream = self.start(pool, thread_id=f"thread_{index}")
            stream.close(timeout=3)
            self.assertEqual(pool.stats["available"], 1)

    def test_pool_close_waits_for_managed_run_and_rejects_admission(self):
        graph, pool = self.harness()
        stream = self.start(pool)
        self.assertTrue(graph.entered.wait(5))
        with self.assertRaises(PoolTimeoutError):
            pool.close(timeout=0.01)
        with self.assertRaises(PoolClosedError):
            self.start(pool)
        stream.close(timeout=3)
        pool.close(timeout=1)

    def test_cleanup_failure_is_visible_and_stops_pool(self):
        graph, pool = self.harness()
        stream = self.start(pool)
        self.assertTrue(graph.entered.wait(5))
        with patch("deerflow.runtime.client_replay.ClientTurnReplay.finish_managed_turn", side_effect=OSError("存储故障")):
            with self.assertRaises(StreamCleanupError):
                stream.close(timeout=3)
        self.assertTrue(pool.stats["failed"])
        self.assertEqual(pool.stats["borrowed"], 0)

    def test_invalid_options_and_thread_start_failure_do_not_leak(self):
        _, pool = self.harness()
        for options in ({"buffer_size": 0}, {"disabled_tools": "tool"}, {"replay_last_turn": 1}):
            with self.assertRaises((ValueError, TypeError)):
                self.start(pool, **options)
        with patch("deerflow.agent_stream.ManagedAgentStream._start", side_effect=RuntimeError("启动失败")):
            with self.assertRaisesRegex(RuntimeError, "启动失败"):
                self.start(pool)
        self.assertEqual(pool.stats["available"], 1)

    def test_checkpoint_write_is_drained_before_slot_is_returned(self):
        saver = BlockingSaver()
        graph, pool = self.harness(saver=saver)
        self.addCleanup(saver.release.set)
        stream = self.start(pool)
        self.assertTrue(saver.entered.wait(5))
        with self.assertRaises(StreamCloseTimeoutError):
            stream.close(timeout=0.03)
        self.assertEqual(pool.stats["borrowed"], 1)
        saver.release.set()
        stream.close(timeout=3)
        self.assertEqual(pool.stats["borrowed"], 0)
        self.assertFalse(graph.snapshot().next)
        graph.release.set()
        self.assertEqual(list(self.start(pool, "继续"))[-1].type, "end")

    def test_sync_and_managed_interfaces_share_cached_delta_history(self):
        saver = CachedHistorySaver(InMemorySaver(), MemoryCheckpointHistoryCache(), key_prefix="managed-test")
        graph, pool = self.harness("delta", synchronous=True, saver=saver)
        graph.release.set()
        with pool.session(user_id="user_001") as client:
            self.assertIn("答复：第一轮", client.chat("第一轮", thread_id="thread_001"))
        list(self.start(pool, "第二轮"))
        list(self.start(pool, "替换第二轮", replay_last_turn=True))
        users = [message.content for message in graph.snapshot().values["messages"] if isinstance(message, HumanMessage)]
        self.assertEqual(users, ["第一轮", "替换第二轮"])
        with pool.session(user_id="user_001") as client:
            self.assertIn("答复：第三轮", client.chat("第三轮", thread_id="thread_001"))

    def test_real_agent_model_stream_keeps_events_and_replay(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                graph, pool = self.harness(mode)
                graph.graph = create_agent(
                    model=FakeListChatModel(responses=["真实 Agent 回复"]),
                    tools=[], checkpointer=graph.saver,
                    state_schema=get_thread_state_schema(mode, 10),
                )
                for slot in pool._available:
                    slot.client._agent = graph.graph
                events = list(self.start(pool))
                self.assertEqual(events[-1].type, "end")
                answer = "".join(event.data.content for event in events if event.type == "answer_content")
                self.assertEqual(answer, "真实 Agent 回复")
                self.assertEqual(list(self.start(pool, "修改", replay_last_turn=True))[-1].type, "end")

    def test_same_thread_can_start_multiple_managed_runs_with_shared_capacity(self):
        graph, pool = self.harness(size=2)
        first = self.start(pool)
        second = self.start(pool, thread_id="thread_002")
        with self.assertRaises(PoolTimeoutError):
            self.start(pool, thread_id="thread_003", acquire_timeout=0)
        first.close(timeout=3)
        second.close(timeout=3)
        self.assertEqual(pool.stats["available"], 2)

    def test_cancel_during_synchronous_preparation_never_starts_graph(self):
        graph, pool = self.harness()
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        client = pool._available[0].client
        prepare = client._prepare_stream_turn

        def slow_prepare(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("测试未释放初始化")
            return prepare(*args, **kwargs)

        with patch.object(client, "_prepare_stream_turn", side_effect=slow_prepare):
            stream = self.start(pool)
            self.assertTrue(entered.wait(5))
            with self.assertRaises(StreamCloseTimeoutError):
                stream.close(timeout=0.03)
            self.assertEqual(pool.stats["borrowed"], 1)
            release.set()
            stream.close(timeout=3)
        self.assertFalse(graph.entered.is_set())
        self.assertEqual(pool.stats["available"], 1)

    def test_legacy_session_keeps_thread_ownership_and_rejects_nested_admission(self):
        _, pool = self.harness(size=2)
        with pool.session(user_id="user_001") as client:
            with self.assertRaisesRegex(RuntimeError, "嵌套"):
                self.start(pool)
            with ThreadPoolExecutor() as worker:
                with self.assertRaisesRegex(RuntimeError, "借用线程"):
                    worker.submit(client.close).result(1)
        self.assertEqual(pool.stats["available"], 2)

    def test_cached_client_reuses_event_loop_and_pool_close_joins_worker(self):
        graph, pool = self.harness()
        graph.release.set()
        list(self.start(pool))
        worker = pool._available[0].worker
        list(self.start(pool, "下一轮"))
        self.assertEqual(len(graph.loops), 2)
        self.assertIs(graph.loops[0], graph.loops[1])
        self.assertTrue(worker.thread.is_alive())
        pool.close(timeout=3)
        pool.close(timeout=0)
        self.assertFalse(worker.thread.is_alive())
        self.assertTrue(graph.loops[0].is_closed())


if __name__ == "__main__":
    unittest.main()
