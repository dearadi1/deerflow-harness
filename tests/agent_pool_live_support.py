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
from unittest.mock import patch
from uuid import uuid4

import httpx
import yaml

_SYSTEM_PROMPT = "你正在进行接口测试。严格遵守本轮测试指令。"
_SHORT_PROMPT = "只回复 OK，不要调用工具。"
_RETRY_PROMPT = "只回复 RETRY_OK，不调用工具。"
_LONG_PROMPT = "逐行列出从 1 到 200 的数字，每行附加一句测试说明，不要调用工具。"
_TOOL_PROMPT = "必须先调用 slow_probe 工具，value 参数填写 test，然后只回复 TOOL_OK。"
_IDENTITY_PROMPTS = {
    f"POOL_USER_{suffix}": f"记住测试标识 POOL_USER_{suffix}，且只回复 POOL_USER_{suffix}，不要调用工具。"
    for suffix in ("A", "B", "C")
}


def validate_outbound_payload(request, payload):
    """真实请求只允许已核对的服务与合成测试输入，禁止混入业务提示或工具。"""
    if request.url.host != "ark.cn-beijing.volces.com":
        raise ValueError("真实测试目的地与已核对的火山方舟地址不符")
    for message in payload["messages"]:
        role = message["role"]
        content = message.get("content")
        if role == "system" and content != _SYSTEM_PROMPT:
            raise ValueError("拒绝发送非测试系统提示")
        if role == "user" and content not in {_SHORT_PROMPT, _RETRY_PROMPT, _LONG_PROMPT, _TOOL_PROMPT, *_IDENTITY_PROMPTS.values()}:
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
                while not self.control.resume.is_set():
                    await asyncio.sleep(0.01)
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


class GuardedSyncTransport(httpx.BaseTransport):
    """session 使用同步模型通路，出站输入与托管通路遵守同一约束。"""

    def __init__(self, control):
        self.control = control
        self.inner = httpx.HTTPTransport(limits=httpx.Limits(max_keepalive_connections=0))

    def handle_request(self, request):
        self.control.record_request(request)
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


class FaultTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        # 关闭池的场景会销毁所属循环，测试传输不保留跨场景的空闲连接。
        self.inner = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0))
        self.sync = GuardedSyncTransport(self)
        self.mode = "healthy"
        self.request_started = threading.Event()
        self.body_received = threading.Event()
        self.injected = threading.Event()
        self.resume = threading.Event()
        self.requests = 0
        self.responses_closed = 0
        self.preview = None
        self.builds = 0
        self.observed_users = []

    def record_request(self, request):
        from deerflow.runtime.user_context import get_current_user

        self.requests += 1
        self.request_started.set()
        payload = json.loads(request.content)
        validate_outbound_payload(request, payload)
        user = get_current_user()
        self.observed_users.append(None if user is None else str(user.id))
        if self.mode == "preview":
            self.preview = {
                "destination": str(request.url.copy_with(query=None)),
                "model": payload["model"], "messages": payload["messages"],
                "tools": payload.get("tools", []),
                "body_fields": sorted(payload), "network_sent": False,
            }
            raise RuntimeError("出站预览已拦截请求，未执行网络发送")

    async def handle_async_request(self, request):
        self.record_request(request)
        response = await self.inner.handle_async_request(request)
        response.stream = FaultStream(response.stream, self, request)
        return response

    async def aclose(self):
        await self.inner.aclose()
        self.sync.close()


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
                transport.builds += 1
                model = create_chat_model(
                    config.models[0].name, app_config=config, thinking_enabled=False,
                    attach_tracing=False,
                    model_overrides={
                        "http_client": httpx.Client(transport=transport.sync, timeout=30),
                        "http_async_client": httpx.AsyncClient(transport=transport, timeout=30),
                    },
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
class LivePoolTestCase(unittest.TestCase):
    report_name = "managed_live"

    @classmethod
    def setUpClass(cls):
        from dotenv import load_dotenv

        cls.directory = tempfile.TemporaryDirectory(prefix="deerflow-managed-live-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.enterClassContext(patch.dict(os.environ, {
            "DEER_FLOW_HOME": str(cls.root / "home"),
            "DEER_FLOW_PROJECT_ROOT": str(cls.root),
        }))
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        for name in ("LANGFUSE_TRACING", "LANGSMITH_TRACING", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2", "MONOCLE_TRACING"):
            os.environ[name] = "false"
        cls.mode = os.getenv("DEER_FLOW_LIVE_CHECKPOINT_MODE", "full")
        source = Path(os.getenv("DEER_FLOW_LIVE_CONFIG", str(Path(__file__).resolve().parents[1] / "config.yaml"))).resolve()
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
        from deerflow.config.paths import Paths

        cls.enterClassContext(patch("deerflow.config.paths._paths", Paths(cls.root / "home")))
        cls.config = load_client_config(str(cls.config_path))
        cls.model_summary = {
            "name": name, "use": model["use"], "model": model.get("model", name),
            "source": str(source), "credential_logged": False,
            "test_overrides": {"max_tokens": 1024, "max_retries": 0, "timeout": 30, "stream_chunk_timeout": 10, "thinking_enabled": False},
        }
        cls.results = []
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)
        print(json.dumps({"live_model": name, "checkpoint_mode": cls.mode}, ensure_ascii=False), flush=True)

    @classmethod
    def tearDownClass(cls):
        report = Path(os.getenv("DEER_FLOW_LIVE_REPORT", f"/private/tmp/deerflow_{cls.report_name}_{cls.mode}.json"))
        report.write_text(json.dumps({"model": cls.config.models[0].name, "model_configuration": cls.model_summary, "mode": cls.mode, "results": cls.results}, ensure_ascii=False, indent=2))

    def setUp(self):
        from langgraph.checkpoint.sqlite import SqliteSaver

        from deerflow.agent_client import DeerFlowClientPool

        self.saver = self.enterContext(SqliteSaver.from_conn_string(str(self.root / f"{uuid4().hex}.sqlite")))
        self.saver.setup()
        self.transport = FaultTransport()
        self.client = build_live_client(self.config, self.saver, self.transport)
        self.transports = {id(self.client): self.transport}
        self.pool = DeerFlowClientPool(size=1, client_factory=lambda: self.client)
        self.pools = [self.pool]
        self.streams = []
        self.thread_id = f"live_{uuid4().hex}"
        self.user_id = "managed_live_test"
        self.metrics = {"case": self._testMethodName}
        self.started = time.monotonic()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        errors = []
        for stream in self.streams:
            try:
                stream.close(timeout=35)
            except Exception as error:
                errors.append(error)
        self.close_transports(errors)
        for pool in set(self.pools + [self.pool]):
            try:
                pool.close(timeout=10)
            except Exception as error:
                errors.append(error)
        self.metrics.update(elapsed_seconds=round(time.monotonic() - self.started, 3), requests=sum(transport.requests for transport in self.transports.values()) + self.metrics.get("production_model_calls", 0))
        result = self._outcome.result
        failures = [test for test, _ in result.errors + result.failures]
        failed = any(test is self or getattr(test, "test_case", None) is self for test in failures)
        self.metrics["status"] = "failed" if failed or errors else "passed"
        self.metrics["cleanup_errors"] = [type(error).__name__ for error in errors]
        self.results.append(self.metrics)
        print(json.dumps(self.metrics, ensure_ascii=False), flush=True)
        if errors:
            raise ExceptionGroup("真实测试清理失败", errors)

    def close_transports(self, errors):
        for client_id, transport in self.transports.items():
            workers = [stream._worker for stream in self.streams if id(stream._slot.client) == client_id and stream._worker is not None]
            loop = workers[0].loop if workers else None
            try:
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(transport.aclose(), loop).result(10)
                else:
                    asyncio.run(transport.aclose())
            except Exception as error:
                errors.append(error)

    def replace_pool(self, *, size=1, max_waiters=100):
        from deerflow.agent_client import DeerFlowClientPool

        self.pool.close(timeout=10)
        transports = [FaultTransport() for _ in range(size)]
        clients = [build_live_client(self.config, self.saver, transport) for transport in transports]
        self.transports.update({id(client): transport for client, transport in zip(clients, transports)})
        factories = iter(clients)
        self.pool = DeerFlowClientPool(size=size, max_waiters=max_waiters, client_factory=lambda: next(factories))
        self.pools.append(self.pool)
        self.client, self.transport = clients[0], transports[0]
        return transports

    def wait_until(self, predicate, *, timeout=5, message="条件未在期限内满足"):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(message)
            time.sleep(0.01)

    def assert_pool_idle(self):
        self.assertEqual(self.pool.stats["borrowed"], 0)
        self.assertEqual(self.pool.stats["waiting"], 0)
        self.assertEqual(self.pool.stats["available"], self.pool.stats["size"])
        self.assertFalse(self.pool.stats["failed"])

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

        self.assert_pool_idle()
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

    @staticmethod
    def collect_signalling_answer(stream, received):
        events = []
        for event in stream:
            events.append(event)
            if event.type == "answer_content":
                received.set()
        return events

    def install_tool(self, tool):
        from deerflow.agent_client import DeerFlowClientPool

        self.pool.close(timeout=5)
        self.client = build_live_client(self.config, self.saver, self.transport, slow_tool=tool)
        self.transports = {id(self.client): self.transport}
        self.pool = DeerFlowClientPool(size=1, client_factory=lambda: self.client)
