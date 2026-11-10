"""通过公开流入口和真实 SDK 验证统一提示，模型服务使用离线传输。"""

import asyncio
import unittest
from unittest.mock import patch

import httpx
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from deerflow.agent_client import DeerFlowClientPool
from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware
from deerflow.agents.middlewares.model_length_finish_reason_middleware import ModelLengthFinishReasonMiddleware
from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware
from deerflow.agents.thread_state import get_thread_state_schema
from deerflow.answer_errors import ANSWER_ERRORS_KEY
from test_agent_managed_stream import ControlledGraph
from test_answer_error_fallback import app_config
from test_write_file_length_recovery import ModelServer


class AnswerErrorStreamTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("deerflow.client.build_tracing_callbacks", return_value=[]))
        self.enterContext(patch("deerflow.client.inject_langfuse_metadata"))
        self.enterContext(patch("deerflow.sandbox.lease.release_sandbox_execution_lease"))

    def build_pool(self, mode, handler, *, middlewares=None):
        config = app_config(error_handling={"fallback_message": "服务忙，请重试"})
        transport = httpx.MockTransport(handler)
        sync_client = self.enterContext(httpx.Client(transport=transport))
        async_client = httpx.AsyncClient(transport=transport)
        self.addCleanup(lambda: asyncio.run(async_client.aclose()))
        model = ChatOpenAI(model="test-model", api_key="test", streaming=True, max_retries=0, http_client=sync_client, http_async_client=async_client)
        harness = ControlledGraph(InMemorySaver(), mode)
        harness.graph = create_agent(model=model, tools=[], middleware=middlewares or [LLMErrorHandlingMiddleware(app_config=config)], checkpointer=harness.saver, state_schema=get_thread_state_schema(mode, 10))

        def client_factory():
            client = harness.client()
            client._app_config = config
            return client

        return harness, self.enterContext(DeerFlowClientPool(size=1, client_factory=client_factory))

    def collect(self, pool, managed):
        if managed:
            return list(pool.start_stream("问题", thread_id="thread_001", user_id="user_001"))
        with pool.session(user_id="user_001") as session:
            return list(session.stream("问题", thread_id="thread_001"))

    def assert_answer(self, events, expected):
        answers = [event for event in events if event.type == "answer_content"]
        self.assertEqual("".join(event.data.content for event in answers), expected)
        self.assertEqual(events[-1].type, "end")
        self.assertNotIn("error", [event.type for event in events])
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        return answers

    def test_provider_failures_and_successful_retry_through_both_public_streams(self):
        for mode in ("full", "delta"):
            for managed in (False, True):
                for status, recover, expected_attempts in ((400, False, 1), (503, False, 3), (503, True, 2)):
                    with self.subTest(mode=mode, managed=managed, status=status, recover=recover):
                        requests = []

                        def handler(request):
                            requests.append(request)
                            if recover and len(requests) > 1:
                                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=ModelServer.encode((None, "重试成功", "stop"), 2))
                            return httpx.Response(status, json={"error": {"code": "InvalidParameter", "message": "the requested model does not support function calling; Request id: offline-123", "type": "BadRequest"}})

                        harness, pool = self.build_pool(mode, handler)
                        events = self.collect(pool, managed)
                        self.assertEqual(len(requests), expected_attempts)
                        answers = self.assert_answer(events, "重试成功" if recover else "服务忙，请重试")
                        if recover:
                            self.assertTrue(all(event.data.additional_kwargs is None for event in answers))
                        else:
                            self.assertIn("offline-123", answers[-1].data.additional_kwargs.error)
                            self.assertNotIn(ANSWER_ERRORS_KEY, answers[-1].model_dump_json())
                            stored = harness.snapshot().values["messages"][-1]
                            self.assertIn("offline-123", stored.additional_kwargs["error_detail"])
                            self.assertNotIn("服务忙，请重试", stored.content)
                        pool.close()

    def test_empty_length_and_safety_responses_use_shared_projection(self):
        for managed in (False, True):
            for reason, middleware in (("length", ModelLengthFinishReasonMiddleware()), ("content_filter", SafetyFinishReasonMiddleware())):
                with self.subTest(managed=managed, reason=reason):
                    server = ModelServer([(None, "", reason)])
                    harness, pool = self.build_pool("full", server.handle, middlewares=[middleware])
                    answers = self.assert_answer(self.collect(pool, managed), "服务忙，请重试")
                    self.assertTrue(answers[-1].data.additional_kwargs.error)
                    self.assertNotIn("deerflow_error_fallback", answers[-1].data.additional_kwargs.model_dump())
                    self.assertNotIn("服务忙，请重试", harness.snapshot().values["messages"][-1].content)
                    pool.close()
