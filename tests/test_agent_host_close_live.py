"""通过真实 Voris 接入层、真实模型和隔离 Redis 验证停止及过期接手。"""

import asyncio
import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from agent_pool_live_support import _LONG_PROMPT, _RETRY_PROMPT, LivePoolTestCase


@unittest.skipUnless(
    os.getenv("DEER_FLOW_TEST_VORIS_ROOT"), "需提供包含本次修复的 Voris 工作区"
)
class HostCloseLiveTests(LivePoolTestCase):
    report_name = "host_close_live_20261104"

    def setUp(self):
        super().setUp()
        from src.agent import llm_client as host_module
        from src.agent.llm_client import HoursAgentClient
        from src.voris_agent_bootstrap import ensure_voris_agent_on_path

        ensure_voris_agent_on_path()
        from app.interaction import VorisChatInteraction

        self.assertEqual(
            Path(host_module.__file__).resolve(),
            Path(os.environ["DEER_FLOW_TEST_VORIS_ROOT"]).resolve()
            / "api/src/agent/llm_client.py",
        )
        self.host = HoursAgentClient()
        self.interaction = VorisChatInteraction(client_pool=self.pool)
        self.enterContext(
            patch.object(
                self.host, "_get_chat_interaction", return_value=self.interaction
            )
        )
        original_start = self.pool.start_stream

        def remember_stream(*args, **kwargs):
            stream = original_start(*args, **kwargs)
            self.streams.append(stream)
            return stream

        self.enterContext(
            patch.object(self.pool, "start_stream", side_effect=remember_stream)
        )

    async def wait_async(self, predicate, timeout=35):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    async def consume_host(self, prompt=_LONG_PROMPT):
        return [
            event
            async for event in self.host.stream_chat(
                prompt, self.thread_id, self.user_id
            )
        ]

    def test_01_real_host_close_bypasses_full_default_executor(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
            self.transport.mode = "stall"
            first = asyncio.create_task(self.consume_host())
            second = None
            try:
                await self.wait_async(self.transport.injected.is_set)
                second = asyncio.create_task(self.consume_host(_RETRY_PROMPT))
                await self.wait_async(lambda: self.pool.stats["waiting"] == 1)
                self.assertEqual(self.pool.stats["borrowed"], 1)
                self.transport.mode = "healthy"
                started = time.monotonic()
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(first, 15)
                self.metrics["host_close_seconds"] = round(
                    time.monotonic() - started, 4
                )
                events = await asyncio.wait_for(second, 45)
                self.assertEqual(events[-1]["type"], "end")
                self.assertIn(
                    "RETRY_OK",
                    "".join(
                        event["data"].get("content", "")
                        for event in events
                        if event["type"] == "answer_content"
                    ),
                )
                self.assert_released()
                self.metrics["saturated_executor_close_and_same_thread_retry"] = True
            finally:
                self.transport.resume.set()
                first.cancel()
                if second is not None:
                    second.cancel()
                await asyncio.gather(
                    first, *([second] if second else []), return_exceptions=True
                )

        asyncio.run(scenario())

    def test_02_cancel_queued_startup_does_not_leave_a_hidden_run(self):
        async def scenario():
            asyncio.get_running_loop().set_default_executor(
                ThreadPoolExecutor(max_workers=2)
            )
            self.transport.mode = "stall"
            first = asyncio.create_task(self.consume_host())
            queued = None
            try:
                await self.wait_async(self.transport.injected.is_set)
                queued = asyncio.create_task(self.consume_host(_RETRY_PROMPT))
                await self.wait_async(lambda: self.pool.stats["waiting"] == 1)
                queued.cancel()
                await asyncio.sleep(0.05)
                self.assertFalse(
                    queued.done(), "尚未取得并关闭排队的流句柄，不能宣称停止完成"
                )
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(first, 15)
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(queued, 15)
                self.assert_released()
                self.assertTrue(all(stream.closed for stream in self.streams))
                self.transport.mode = "healthy"
                events = await self.consume_host(_RETRY_PROMPT)
                self.assertEqual(events[-1]["type"], "end")
                self.metrics["cancelled_startup_drained_and_retried"] = True
            finally:
                self.transport.resume.set()
                first.cancel()
                if queued is not None:
                    queued.cancel()
                await asyncio.gather(
                    first, *([queued] if queued else []), return_exceptions=True
                )

        asyncio.run(scenario())

    @unittest.skipUnless(
        os.getenv("DEER_FLOW_TEST_REDIS_URL"), "需显式提供隔离 Redis 地址"
    )
    def test_03_expired_real_model_worker_cannot_write_into_peer_generation(self):
        from redis.asyncio import Redis
        from src.agent.chat_service import ChatService
        from src.agent.generation_lease import GenerationLease, GenerationLeaseLost

        async def scenario():
            conversation_id = f"live_lease_{uuid4().hex}"
            redis_url = os.environ["DEER_FLOW_TEST_REDIS_URL"]
            async with Redis.from_url(redis_url, decode_responses=True) as redis:
                old = GenerationLease(redis, conversation_id)
                current = GenerationLease(redis, conversation_id)
                self.assertTrue(await old.acquire(2))
                service = ChatService(AsyncMock(), None)
                accumulation = service._init_accumulation(self.thread_id)
                accumulation["generation_lease"] = old
                self.transport.mode = "stall"
                with (
                    patch("src.agent.chat_service.llm_client", self.host),
                    patch("src.agent.chat_service.redis_client", redis),
                ):
                    worker = asyncio.create_task(
                        service._stream_to_redis(
                            self.user_id,
                            conversation_id,
                            _LONG_PROMPT,
                            [],
                            self.thread_id,
                            None,
                            accumulation,
                            old.keys[2],
                        )
                    )
                    try:
                        await self.wait_async(self.transport.injected.is_set)
                        while await redis.exists(old.keys[0]):
                            await asyncio.sleep(0.05)
                        script = (
                            "import asyncio,sys; from redis.asyncio import Redis; "
                            "from src.agent.generation_lease import GenerationLease\n"
                            "async def run():\n"
                            " async with Redis.from_url(sys.argv[1],decode_responses=True) as redis:\n"
                            "  lease=GenerationLease(redis,sys.argv[2],sys.argv[3])\n"
                            "  assert await lease.acquire(60)\n"
                            "  await lease.publish({'type':'peer_event'},answer='PEER_ONLY')\n"
                            "asyncio.run(run())\n"
                        )
                        peer = await asyncio.create_subprocess_exec(
                            sys.executable,
                            "-P",
                            "-c",
                            script,
                            redis_url,
                            conversation_id,
                            current.token,
                        )
                        self.assertEqual(await asyncio.wait_for(peer.wait(), 15), 0)
                        await redis.set(current.keys[1], "PEER_STOP")
                        self.transport.resume.set()
                        with self.assertRaises(GenerationLeaseLost):
                            await asyncio.wait_for(worker, 15)
                        self.assertFalse(await old.renew(600))
                        await old.finish(failed=True)
                        self.assertEqual(
                            await redis.get(current.keys[0]), current.token
                        )
                        self.assertEqual(await redis.get(current.keys[1]), "PEER_STOP")
                        self.assertEqual(await redis.get(current.keys[3]), "PEER_ONLY")
                        self.assertEqual(await redis.xlen(current.keys[2]), 1)
                        self.assert_released()
                        self.metrics[
                            "real_redis_peer_process_fenced_stale_model_output"
                        ] = True
                    finally:
                        self.transport.resume.set()
                        worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                        await redis.delete(*old.keys)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
