"""真实主/子模型、生产 task 轮询与真实 AIO 命令的跨线程关闭验收。"""

import asyncio
import importlib
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import agent_pool_live_support as support
import httpx
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool

from deerflow.agent_client import PoolTimeoutError, StreamCloseTimeoutError
from deerflow.agents.thread_state import get_thread_state_schema
from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.runtime.turn_cancellation import TurnCancellation, turn_cancellation_scope
from deerflow.sandbox.lease import sandbox_command_scope
from deerflow.sandbox.sandbox_provider import SandboxProvider
from deerflow.sandbox.tools import ensure_sandbox_initialized
from deerflow.subagents.executor import SubagentExecutor, _get_isolated_subagent_loop
from deerflow.tools.types import Runtime

_LEAD_PROMPT = (
    "这是生命周期测试。必须且只调用一次 task 工具，subagent_type 填 general-purpose，"
    "prompt 填：必须调用 slow_probe，value 为 test。收到工具结果后只回复 TEST_DONE，不要重试。"
)
_BACKGROUND_PROMPT = (
    "必须依次调用两次 slow_probe：第一次 value=start；收到 STARTED 后第二次 value=wait。"
    "两次调用必须串行，不能省略，最后只回复 TOOL_OK。"
)


class FixedTestSandboxProvider(SandboxProvider):
    """仅用显式测试容器替代容器创建，租约和命令会话清理仍走生产代码。"""

    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.identity = None
        self.releases = 0

    def acquire(self, thread_id=None, *, user_id=None):
        self.identity = (user_id, thread_id)
        return self.sandbox.id

    def get(self, sandbox_id):
        return self.sandbox if sandbox_id == self.sandbox.id else None

    def get_scoped(self, sandbox_id, *, thread_id, user_id):
        return self.get(sandbox_id) if self.identity == (user_id, thread_id) else None

    def release(self, sandbox_id):
        self.releases += 1


@unittest.skipUnless(
    os.environ.get("DEER_FLOW_TEST_AIO_URL"), "需显式提供隔离 AIO 地址"
)
class SubagentCloseLiveTests(support.LivePoolTestCase):
    report_name = "subagent_close_live_20261104"

    def setUp(self):
        super().setUp()
        self.tool_entered = threading.Event()
        self.cleanup_entered = threading.Event()
        self.cleanup_release = threading.Event()
        self.tool_exited = threading.Event()
        self.poll_failed = threading.Event()
        self.executors = []
        self.child_prompt = support._TOOL_PROMPT
        self.tool_values = []
        self.marker = f"/tmp/deerflow-child-live-{uuid4().hex}.pid"
        self.command = (
            f"sleep 90 & child=$!; printf '%s' $child > {self.marker}; wait $child"
        )
        self.sandbox = AioSandbox(
            "live-subagent-sandbox",
            os.environ["DEER_FLOW_TEST_AIO_URL"],
            home_dir="/home/gem",
        )
        self.observer = AioSandbox(
            "live-subagent-observer",
            os.environ["DEER_FLOW_TEST_AIO_URL"],
            home_dir="/home/gem",
        )
        self.provider = FixedTestSandboxProvider(self.sandbox)
        self.child_transport = support.FaultTransport()
        self.enterContext(
            patch(
                "deerflow.sandbox.sandbox_provider._default_sandbox_provider",
                self.provider,
            )
        )
        self.install_test_capabilities()
        self.addCleanup(self.cleanup_real_child)

    def install_test_capabilities(self):
        case = self
        original_validate = support.validate_outbound_payload

        def validate(request, payload):
            checked = {
                **payload,
                "messages": [dict(message) for message in payload["messages"]],
            }
            for message in checked["messages"]:
                if message.get("content") == _LEAD_PROMPT:
                    message["content"] = support._TOOL_PROMPT
                if message.get("content") == _BACKGROUND_PROMPT:
                    message["content"] = support._TOOL_PROMPT
            names = {tool["function"]["name"] for tool in checked.get("tools", [])}
            if not names <= {"task", "slow_probe"}:
                raise ValueError("测试不得发送业务工具")
            checked["tools"] = []
            original_validate(request, checked)

        def slow_probe(value: str, runtime: Runtime) -> str:
            """执行固定测试命令；test 为长命令，start 启动后台进程，wait 执行后续长命令。"""
            case.assertIn(value, ("test", "start", "wait"))
            sandbox = ensure_sandbox_initialized(runtime)
            case.tool_values.append(value)
            if value == "start":
                command = f"sleep 90 > /dev/null 2>&1 < /dev/null & child=$!; printf '%s' $child > {case.marker}; printf STARTED"
                return sandbox.execute_command_in_scope(
                    command, timeout=10, scope_id=sandbox_command_scope(runtime.context)
                )
            case.tool_entered.set()
            try:
                return sandbox.execute_command_in_scope(
                    "sleep 90" if value == "wait" else case.command,
                    timeout=110,
                    scope_id=sandbox_command_scope(runtime.context),
                )
            finally:
                case.cleanup_entered.set()
                if not case.cleanup_release.wait(35):
                    raise TimeoutError("测试未释放子工具清理屏障")
                case.tool_exited.set()

        slow_tool = StructuredTool.from_function(slow_probe)

        class TestExecutor(SubagentExecutor):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                case.executors.append(self)

            async def _build_initial_state(self, task):
                # 只替换能力装配，执行、取消、状态发布与租约释放均复用 _aexecute。
                return (
                    {
                        "messages": [
                            SystemMessage(support._SYSTEM_PROMPT),
                            HumanMessage(case.child_prompt),
                        ]
                    },
                    [slow_tool],
                    None,
                )

            async def _create_agent(self, tools=None, **kwargs):
                from deerflow.models import create_chat_model

                model = create_chat_model(
                    case.config.models[0].name,
                    app_config=case.config,
                    thinking_enabled=False,
                    attach_tracing=False,
                    model_overrides={
                        "http_async_client": httpx.AsyncClient(
                            transport=case.child_transport, timeout=30
                        )
                    },
                )
                return create_agent(
                    model=model,
                    tools=[slow_tool],
                    checkpointer=False,
                    state_schema=get_thread_state_schema(case.mode, 10),
                )

        task_module = importlib.import_module("deerflow.tools.builtins.task_tool")
        self.enterContext(patch.object(support, "validate_outbound_payload", validate))
        self.enterContext(patch.object(task_module, "SubagentExecutor", TestExecutor))
        self.enterContext(
            patch("deerflow.tools.get_available_tools", return_value=[slow_tool])
        )
        self.install_tool(task_module.task_tool)

    def observe(self, command):
        with turn_cancellation_scope(TurnCancellation()):
            return self.observer.execute_command(command, timeout=5).strip()

    def wait_for_remote_process(self):
        self.assertTrue(self.tool_entered.wait(30), "真实子模型未调用测试工具")
        self.wait_until(
            lambda: (
                self.observe(f"test -s {self.marker} && printf started") == "started"
            ),
            timeout=15,
        )
        self.metrics["lead_model_requested_task"] = bool(self.executors)
        self.metrics["child_model_requested_tool"] = self.child_transport.requests > 0

    def assert_remote_stopped(self):
        state = self.observe(
            f"pid=$(cat {self.marker}); if kill -0 $pid 2>/dev/null; then ps -p $pid -o stat=; else printf stopped; fi"
        )
        self.assertTrue(state == "stopped" or state.startswith("Z"), state)
        self.metrics["remote_child_stopped"] = True

    def cleanup_real_child(self):
        self.cleanup_release.set()
        for stream in self.streams:
            stream.close(timeout=35)
        self.metrics["production_model_calls"] = self.child_transport.requests
        asyncio.run_coroutine_threadsafe(
            self.child_transport.aclose(), _get_isolated_subagent_loop()
        ).result(10)
        self.observe(f"rm -f {self.marker}")
        self.observer.close()
        self.sandbox.close()

    def finish_and_continue(self, stream, consumer):
        self.cleanup_release.set()
        self.close_and_check(stream)
        consumer.result(10)
        self.assertTrue(self.tool_exited.is_set())
        self.assertGreater(self.provider.releases, 0)
        self.assert_remote_stopped()
        self.metrics["sandbox_lease_released"] = True
        self.assertEqual(
            self.collect(self.start(support._RETRY_PROMPT))[-1].type, "end"
        )
        stream.close(timeout=0)
        self.assert_released()
        self.metrics["same_thread_followup_completed"] = True

    def test_01_external_concurrent_close_drains_real_subagent_command(self):
        stream = self.start(_LEAD_PROMPT)
        with ThreadPoolExecutor(max_workers=4) as workers:
            consumer = workers.submit(list, stream)
            try:
                self.wait_for_remote_process()
                closers = [workers.submit(stream.close, timeout=0.1) for _ in range(3)]
                for closer in closers:
                    with self.assertRaises(StreamCloseTimeoutError):
                        closer.result(3)
                self.assertTrue(self.cleanup_entered.wait(5))
                self.assertFalse(stream.closed)
                self.assertEqual(self.pool.stats["borrowed"], 1)
                with self.assertRaises(PoolTimeoutError):
                    self.start(acquire_timeout=0)
                self.metrics["concurrent_close_timeouts_kept_slot"] = True
                self.finish_and_continue(stream, consumer)
            finally:
                self.cleanup_release.set()
                stream.close(timeout=35)

    def test_02_poller_failure_cannot_outlive_parent_slot(self):
        task_module = importlib.import_module("deerflow.tools.builtins.task_tool")
        original = task_module.get_background_task_result
        trigger = threading.Event()

        def fail_once(execution_id):
            if trigger.is_set() and not self.poll_failed.is_set():
                self.poll_failed.set()
                raise RuntimeError("真实子任务运行时注入轮询故障")
            return original(execution_id)

        self.enterContext(
            patch.object(task_module, "get_background_task_result", fail_once)
        )
        stream = self.start(_LEAD_PROMPT)
        with ThreadPoolExecutor() as workers:
            consumer = workers.submit(list, stream)
            try:
                self.wait_for_remote_process()
                trigger.set()
                self.assertTrue(self.poll_failed.wait(10))
                self.assertTrue(self.cleanup_entered.wait(15))
                # 跨过生产代码的五秒异常收尾宽限期，确认不会提前归还实例。
                time.sleep(5.2)
                self.assertFalse(stream.closed)
                self.assertEqual(self.pool.stats["borrowed"], 1)
                with self.assertRaises(StreamCloseTimeoutError):
                    stream.close(timeout=0.05)
                self.metrics["past_grace_kept_slot"] = True
                self.finish_and_continue(stream, consumer)
            finally:
                self.cleanup_release.set()
                stream.close(timeout=35)

    def test_03_background_command_then_next_tool_closes_both(self):
        self.child_prompt = _BACKGROUND_PROMPT
        stream = self.start(_LEAD_PROMPT)
        with ThreadPoolExecutor() as workers:
            consumer = workers.submit(list, stream)
            try:
                self.wait_for_remote_process()
                self.assertEqual(self.tool_values, ["start", "wait"])
                self.assertEqual(
                    self.observe(f"kill -0 $(cat {self.marker}) && printf alive"),
                    "alive",
                )
                with self.assertRaises(StreamCloseTimeoutError):
                    stream.close(timeout=0.1)
                self.assertTrue(self.cleanup_entered.wait(5))
                self.finish_and_continue(stream, consumer)
                self.metrics[
                    "background_process_alive_during_next_tool_then_stopped"
                ] = True
            finally:
                self.cleanup_release.set()
                stream.close(timeout=35)


if __name__ == "__main__":
    unittest.main()
