"""显式启用的真实模型验收；真实 HTTP 响应上的故障注入不访问业务数据。"""

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import yaml

_SYSTEM_PROMPT = "你正在进行接口测试。严格遵守本轮测试指令。"
_SHORT_PROMPT = "只回复 OK，不要调用工具。"
_RETRY_PROMPT = "只回复 RETRY_OK，不调用工具。"
_LONG_PROMPT = "逐行列出从 1 到 200 的数字，每行附加一句测试说明，不要调用工具。"
_TOOL_PROMPT = "必须先调用 slow_probe 工具，value 参数填写 test，然后只回复 TOOL_OK。"


def validate_outbound_payload(request, payload):
    """真实请求只允许已核对的服务与合成测试输入，禁止混入业务提示或工具。"""
    if request.url.host != "ark.cn-beijing.volces.com":
        raise ValueError("真实测试目的地与已核对的火山方舟地址不符")
    for message in payload["messages"]:
        role = message["role"]
        content = message.get("content")
        if role == "system" and content != _SYSTEM_PROMPT:
            raise ValueError("拒绝发送非测试系统提示")
        if role == "user" and content not in {_SHORT_PROMPT, _RETRY_PROMPT, _LONG_PROMPT, _TOOL_PROMPT}:
            raise ValueError("拒绝发送非测试用户输入")
    if any(tool["function"]["name"] != "slow_probe" for tool in payload.get("tools", [])):
        raise ValueError("拒绝发送业务工具定义")


class FaultStream(httpx.AsyncByteStream):
    def __init__(self, source, control, request):
        self.source = source
        self.control = control
        self.request = request

    async def __aiter__(self):
        async for chunk in self.source:
            self.control.body_received.set()
            if self.control.mode == "stall":
                self.control.injected.set()
                await asyncio.Event().wait()
            if self.control.mode == "disconnect":
                self.control.injected.set()
                raise httpx.ReadError("测试注入：模型响应连接中断", request=self.request)
            if self.control.mode == "timeout":
                self.control.injected.set()
                raise httpx.ReadTimeout("测试注入：模型响应读取超时", request=self.request)
            yield chunk

    async def aclose(self):
        try:
            await self.source.aclose()
        finally:
            self.control.responses_closed += 1


class FaultTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()
        self.mode = "healthy"
        self.request_started = threading.Event()
        self.body_received = threading.Event()
        self.injected = threading.Event()
        self.requests = 0
        self.responses_closed = 0
        self.preview = None

    async def handle_async_request(self, request):
        self.requests += 1
        self.request_started.set()
        payload = json.loads(request.content)
        validate_outbound_payload(request, payload)
        if self.mode == "preview":
            self.preview = {
                "destination": str(request.url.copy_with(query=None)),
                "model": payload["model"], "messages": payload["messages"],
                "tools": payload.get("tools", []),
                "body_fields": sorted(payload), "network_sent": False,
            }
            raise RuntimeError("出站预览已拦截请求，未执行网络发送")
        response = await self.inner.handle_async_request(request)
        response.stream = FaultStream(response.stream, self, request)
        return response

    async def aclose(self):
        await self.inner.aclose()


def build_live_client(config, saver, transport, *, slow_tool=None):
    from langchain.agents import create_agent

    from deerflow.agents.thread_state import get_thread_state_schema
    from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware
    from deerflow.client import DeerFlowClient
    from deerflow.models import create_chat_model

    class LiveClient(DeerFlowClient):
        """仅替换能力装配，实际使用生产模型、池、流解析、存储和取消路径。"""

        def _ensure_agent(self, runnable_config, *, context=None):
            if self._agent is None:
                model = create_chat_model(
                    config.models[0].name, app_config=config, thinking_enabled=False,
                    attach_tracing=False,
                    model_overrides={"http_async_client": httpx.AsyncClient(transport=transport, timeout=30)},
                )
                self._agent = create_agent(
                    model=model, tools=[slow_tool] if slow_tool else [],
                    middleware=[DanglingToolCallMiddleware()],
                    checkpointer=saver,
                    state_schema=get_thread_state_schema(self._checkpoint_channel_mode, 10),
                    system_prompt=_SYSTEM_PROMPT,
                )

    return LiveClient(checkpointer=saver, model_name=config.models[0].name, thinking_enabled=False)


@unittest.skipUnless(os.getenv("DEER_FLOW_RUN_LIVE_TESTS") == "1", "真实模型验收需显式设置 DEER_FLOW_RUN_LIVE_TESTS=1")
class ManagedStreamLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dotenv import load_dotenv

        cls.directory = tempfile.TemporaryDirectory(prefix="deerflow-managed-live-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        os.environ["DEER_FLOW_HOME"] = str(cls.root / "home")
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        cls.mode = os.getenv("DEER_FLOW_LIVE_CHECKPOINT_MODE", "full")
        source = Path(os.getenv("DEER_FLOW_LIVE_CONFIG", "config.yaml")).resolve()
        original = yaml.safe_load(source.read_text())
        name = os.getenv("DEER_FLOW_LIVE_MODEL", original["models"][0]["name"])
        model = next(model for model in original["models"] if model["name"] == name).copy()
        model.update(max_tokens=1024, max_retries=0, timeout=30, stream_chunk_timeout=10)
        isolated = {
            "models": [model], "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "database": {"backend": "memory", "checkpoint_channel_mode": cls.mode},
            "skills": {"path": str(cls.root / "skills")}, "tools": [],
            "memory": {"enabled": False}, "title": {"enabled": False},
            "suggestions": {"enabled": False}, "summarization": {"enabled": False},
        }
        (cls.root / "skills").mkdir()
        cls.config_path = cls.root / "config.yaml"
        descriptor = os.open(cls.config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            yaml.safe_dump(isolated, output, allow_unicode=True)
        from deerflow.client_config import load_client_config

        cls.config = load_client_config(str(cls.config_path))
        cls.results = []
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)
        print(json.dumps({"live_model": name, "checkpoint_mode": cls.mode}, ensure_ascii=False), flush=True)

    @classmethod
    def tearDownClass(cls):
        report = Path(os.getenv("DEER_FLOW_LIVE_REPORT", f"/private/tmp/deerflow_managed_live_{cls.mode}.json"))
        report.write_text(json.dumps({"model": cls.config.models[0].name, "mode": cls.mode, "results": cls.results}, ensure_ascii=False, indent=2))

    def setUp(self):
        from langgraph.checkpoint.sqlite import SqliteSaver

        from deerflow.agent_client import DeerFlowClientPool

        self.saver = self.enterContext(SqliteSaver.from_conn_string(str(self.root / f"{uuid4().hex}.sqlite")))
        self.saver.setup()
        self.transport = FaultTransport()
        self.client = build_live_client(self.config, self.saver, self.transport)
        self.transports = {id(self.client): self.transport}
        self.pool = DeerFlowClientPool(size=1, client_factory=lambda: self.client)
        self.streams = []
        self.thread_id = f"live_{uuid4().hex}"
        self.user_id = "managed_live_test"
        self.metrics = {"case": self._testMethodName}
        self.started = time.monotonic()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for stream in self.streams:
            stream.close(timeout=35)
        # 在所属循环中关闭测试创建的 HTTP 客户端，随后再退出池的后台线程。
        closed_clients = set()
        for stream in self.streams:
            client_id = id(stream._slot.client)
            if client_id not in closed_clients and stream._worker is not None:
                transport = self.transports[client_id]
                asyncio.run_coroutine_threadsafe(transport.aclose(), stream._worker.loop).result(10)
                closed_clients.add(client_id)
        self.pool.close(timeout=10)
        self.metrics.update(elapsed_seconds=round(time.monotonic() - self.started, 3), requests=sum(transport.requests for transport in self.transports.values()))
        result = self._outcome.result
        failures = [test for test, _ in result.errors + result.failures]
        self.metrics["status"] = "failed" if self in failures else "passed"
        self.results.append(self.metrics)
        print(json.dumps(self.metrics, ensure_ascii=False), flush=True)

    def start(self, message=_SHORT_PROMPT, **kwargs):
        options = {"user_id": self.user_id, "thread_id": self.thread_id, "acquire_timeout": 2, **kwargs}
        stream = self.pool.start_stream(message, **options)
        self.streams.append(stream)
        return stream

    def collect(self, stream):
        with ThreadPoolExecutor() as workers:
            result = workers.submit(list, stream)
            try:
                return result.result(45)
            except BaseException:
                stream.close(timeout=35)
                raise

    def assert_released(self):
        from deerflow.runtime.checkpoint_state import CheckpointStateAccessor

        self.assertEqual(self.pool.stats["borrowed"], 0)
        self.assertEqual(self.pool.stats["available"], self.pool.stats["size"])
        self.assertFalse(self.pool.stats["failed"])
        accessor = CheckpointStateAccessor.bind(self.client._agent, self.saver, mode=self.mode)
        snapshot = accessor.get({"configurable": {"thread_id": self.thread_id}})
        self.assertFalse(snapshot.next)
        self.assertFalse(snapshot.tasks)
        return snapshot

    def close_and_check(self, stream):
        started = time.monotonic()
        stream.close(timeout=10)
        self.metrics["close_seconds"] = round(time.monotonic() - started, 4)
        self.assertTrue(stream.closed)
        self.assert_released()

    def regenerate(self):
        self.transport.mode = "healthy"
        events = self.collect(self.start(_RETRY_PROMPT, replay_last_turn=True))
        self.assertEqual(events[-1].type, "end")
        answer = "".join(event.data.content for event in events if event.type == "answer_content")
        self.assertIn("RETRY_OK", answer)
        self.assert_released()
        self.metrics["regenerate_ok"] = True

    def test_01_normal_completion_and_cached_connection_reuse(self):
        for _ in range(2):
            events = self.collect(self.start())
            self.assertEqual(events[-1].type, "end")
            self.assertTrue(any(event.type == "answer_content" for event in events))
            self.assert_released()
        self.metrics["completed_rounds"] = 2

    @staticmethod
    def collect_signalling_answer(stream, received):
        events = []
        for event in stream:
            events.append(event)
            if event.type == "answer_content":
                received.set()
        return events

    def test_02_stop_while_real_model_is_streaming(self):
        received = threading.Event()
        stream = self.start(_LONG_PROMPT)
        with ThreadPoolExecutor() as workers:
            consumer = workers.submit(self.collect_signalling_answer, stream, received)
            try:
                self.assertTrue(received.wait(25), "真实模型未返回首段文本")
                self.close_and_check(stream)
                events = consumer.result(5)
            finally:
                stream.close(timeout=35)
        self.assertFalse(any(event.type == "end" for event in events))
        self.assertGreaterEqual(self.transport.responses_closed, 1)
        self.regenerate()

    def test_03_stop_without_next_frame(self):
        self.transport.mode = "stall"
        stream = self.start(_SHORT_PROMPT)
        with ThreadPoolExecutor() as workers:
            consumer = workers.submit(list, stream)
            try:
                self.assertTrue(self.transport.injected.wait(25), "未进入真实响应的无输出等待")
                self.close_and_check(stream)
                self.assertFalse(any(event.type == "end" for event in consumer.result(5)))
            finally:
                stream.close(timeout=35)
        self.assertGreaterEqual(self.transport.responses_closed, 1)
        self.regenerate()

    def test_04_real_response_disconnect_after_first_text(self):
        received = threading.Event()
        stream = self.start(_LONG_PROMPT)
        with ThreadPoolExecutor() as workers:
            consumer = workers.submit(self.collect_signalling_answer, stream, received)
            try:
                self.assertTrue(received.wait(25), "真实模型未返回首段文本")
                self.transport.mode = "disconnect"
                with self.assertRaises(Exception) as caught:
                    consumer.result(25)
                self.metrics["exception_type"] = type(caught.exception).__name__
                self.assertIn(self.metrics["exception_type"], {"OpenAIConnectionError", "APIConnectionError", "ReadError"})
            finally:
                stream.close(timeout=35)
        self.assertTrue(self.transport.injected.is_set())
        self.assert_released()
        self.regenerate()

    def test_05_real_response_read_timeout(self):
        self.transport.mode = "timeout"
        with self.assertRaises(Exception) as caught:
            self.collect(self.start())
        self.metrics["exception_type"] = type(caught.exception).__name__
        self.assertIn(self.metrics["exception_type"], {"OpenAITimeoutError", "APITimeoutError", "ReadTimeout"})
        self.assertTrue(self.transport.injected.is_set())
        self.assert_released()
        self.regenerate()

    def test_06_backend_consumer_failure_then_continue(self):
        stream = self.start(_LONG_PROMPT)

        def failed_consumer():
            try:
                for event in stream:
                    if event.type == "answer_content":
                        raise BrokenPipeError("测试注入：前端连接断开")
            finally:
                stream.close(timeout=10)

        with ThreadPoolExecutor() as workers:
            with self.assertRaises(BrokenPipeError):
                workers.submit(failed_consumer).result(35)
        self.assert_released()
        self.assertEqual(self.collect(self.start())[-1].type, "end")
        self.assert_released()
        self.metrics["continue_ok"] = True

    def install_tool(self, tool):
        from deerflow.agent_client import DeerFlowClientPool

        self.pool.close(timeout=5)
        self.client = build_live_client(self.config, self.saver, self.transport, slow_tool=tool)
        self.transports = {id(self.client): self.transport}
        self.pool = DeerFlowClientPool(size=1, client_factory=lambda: self.client)

    def test_07_close_timeout_during_real_model_selected_sync_tool(self):
        from langchain_core.tools import StructuredTool

        from deerflow.agent_client import PoolTimeoutError, StreamCloseTimeoutError

        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_probe(value: str) -> str:
            """等待测试端释放后返回固定字符串，没有业务副作用。"""
            entered.set()
            if not release.wait(30):
                raise TimeoutError("测试工具等待超时")
            return "SLOW_TOOL_OK"

        self.install_tool(StructuredTool.from_function(slow_probe))
        stream = self.start(_TOOL_PROMPT)
        with ThreadPoolExecutor() as workers:
            consumer = workers.submit(list, stream)
            try:
                self.assertTrue(entered.wait(25), "真实模型未调用测试工具")
                with self.assertRaises(StreamCloseTimeoutError):
                    stream.close(timeout=0.05)
                self.assertEqual(self.pool.stats["borrowed"], 1)
                with self.assertRaises(PoolTimeoutError):
                    self.start(acquire_timeout=0)
                self.metrics["timeout_kept_slot"] = True
            finally:
                release.set()
                self.close_and_check(stream)
                consumer.result(5)
        self.regenerate()

    def test_08_stop_during_real_model_selected_async_tool_then_continue(self):
        from langchain_core.tools import StructuredTool

        entered = threading.Event()
        exited = threading.Event()

        async def slow_probe(value: str) -> str:
            """异步等待取消，没有业务副作用。"""
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()
            return "SLOW_TOOL_OK"

        self.install_tool(StructuredTool.from_function(coroutine=slow_probe))
        stream = self.start(_TOOL_PROMPT)
        with ThreadPoolExecutor() as workers:
            consumer = workers.submit(list, stream)
            try:
                self.assertTrue(entered.wait(25), "真实模型未调用测试工具")
                self.close_and_check(stream)
                consumer.result(5)
            finally:
                stream.close(timeout=35)
        self.assertTrue(exited.is_set())
        self.assertEqual(self.collect(self.start())[-1].type, "end")
        self.assert_released()
        self.metrics["continue_ok"] = True

    def test_09_stop_regeneration_restores_previous_answer(self):
        from copy import deepcopy

        self.collect(self.start())
        original = deepcopy(self.assert_released().values)
        received = threading.Event()
        stream = self.start(_LONG_PROMPT, replay_last_turn=True)
        with ThreadPoolExecutor() as workers:
            consumer = workers.submit(self.collect_signalling_answer, stream, received)
            try:
                self.assertTrue(received.wait(25), "真实模型未返回重生成文本")
                self.close_and_check(stream)
                consumer.result(5)
            finally:
                stream.close(timeout=35)
        self.assertEqual(self.assert_released().values, original)
        self.metrics["original_answer_restored"] = True
        self.regenerate()

    def test_10_cancel_one_user_does_not_interrupt_another(self):
        from deerflow.agent_client import DeerFlowClientPool

        self.pool.close(timeout=5)
        other_transport = FaultTransport()
        other_client = build_live_client(self.config, self.saver, other_transport)
        self.transports[id(other_client)] = other_transport
        clients = iter((self.client, other_client))
        self.pool = DeerFlowClientPool(size=2, client_factory=lambda: next(clients))
        self.transport.mode = "stall"
        first = self.start(_LONG_PROMPT)
        second = self.start(_SHORT_PROMPT, user_id="managed_live_other", thread_id=f"live_{uuid4().hex}")
        with ThreadPoolExecutor() as workers:
            first_consumer = workers.submit(list, first)
            second_consumer = workers.submit(list, second)
            try:
                self.assertTrue(self.transport.injected.wait(25), "第一位用户未进入等待")
                first.close(timeout=10)
                self.assertEqual(second_consumer.result(35)[-1].type, "end")
                first_consumer.result(5)
            finally:
                first.close(timeout=35)
                second.close(timeout=35)
        self.assert_released()
        self.metrics["other_user_completed"] = True
        self.regenerate()

    def test_11_cancel_async_http_waiter_closes_background_stream(self):
        self.transport.mode = "stall"
        stream = self.start()
        consumer_finished = threading.Event()

        def consume():
            try:
                return list(stream)
            finally:
                consumer_finished.set()

        async def disconnect_http_request():
            request_task = asyncio.create_task(asyncio.to_thread(consume))
            try:
                self.assertTrue(await asyncio.to_thread(self.transport.injected.wait, 25))
                request_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request_task
            finally:
                await asyncio.to_thread(stream.close, timeout=10)

        asyncio.run(disconnect_http_request())
        self.assertTrue(consumer_finished.wait(3))
        self.assert_released()
        self.metrics["cancelled_waiter_drained"] = True
        self.regenerate()


if __name__ == "__main__":
    unittest.main()
