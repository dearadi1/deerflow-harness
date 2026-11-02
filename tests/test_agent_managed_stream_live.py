"""显式启用真实模型，验证托管流停止、故障与 checkpoint 恢复。"""

import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from agent_pool_live_support import (
    FaultTransport, LivePoolTestCase, build_live_client,
    _LONG_PROMPT, _SHORT_PROMPT, _TOOL_PROMPT,
)


class ManagedStreamLiveTests(LivePoolTestCase):
    def test_01_normal_completion_and_cached_client_reuse(self):
        for _ in range(2):
            events = self.collect(self.start())
            self.assertEqual(events[-1].type, "end")
            self.assertTrue(any(event.type == "answer_content" for event in events))
            self.assert_released()
        self.metrics["completed_rounds"] = 2

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
