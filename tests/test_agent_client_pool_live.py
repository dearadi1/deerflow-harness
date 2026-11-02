"""从 agent_client 公开接口验收真实模型下的池管理及独立断连。"""

import asyncio
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from agent_pool_live_support import LivePoolTestCase, _IDENTITY_PROMPTS, _LONG_PROMPT, _RETRY_PROMPT, _SHORT_PROMPT
from deerflow.agent_client import DeerFlowClientPool, PoolClosedError, PoolQueueFullError, PoolTimeoutError
from deerflow.runtime.client_replay import ReplayConflictError
from deerflow.runtime.user_context import get_current_user


class AgentClientPoolLiveTests(LivePoolTestCase):
    report_name = "agent_pool_live"

    def assert_complete(self, events):
        self.assertTrue(events)
        self.assertEqual(events[-1].type, "end")
        self.assertTrue(any(event.type == "answer_content" for event in events))
        self.assertFalse(any(event.type == "error" for event in events))

    def stall_stream(self, transport=None, **kwargs):
        transport = transport or self.transport
        transport.mode = "stall"
        stream = self.start(_SHORT_PROMPT, **kwargs)
        self.assertTrue(transport.injected.wait(30), "真实模型未进入受控响应等待")
        return stream

    def stop_and_resume_pool(self, stream):
        self.transport.mode = "healthy"
        stream.close(timeout=10)

    def test_01_default_constructor_runs_real_session_stream_and_chat(self):
        self.pool.close(timeout=10)
        self.pool = DeerFlowClientPool(
            size=2, config_path=str(self.config_path), checkpointer=self.saver,
            model_name=self.config.models[0].name, thinking_enabled=False,
            subagent_enabled=False, available_skills=set(),
        )
        self.pools.append(self.pool)
        with self.pool.session(user_id=self.user_id) as session:
            self.assertEqual(session.list_models()["models"][0]["name"], self.config.models[0].name)
            self.assertEqual(self.pool.stats["borrowed"], 1)
            events = list(session.stream(_SHORT_PROMPT, thread_id=self.thread_id))
            self.assert_complete(events)
            self.metrics["production_model_calls"] = 1
            answer = session.chat(_RETRY_PROMPT, thread_id=self.thread_id)
            self.metrics["production_model_calls"] += 1
            self.assertIn("RETRY_OK", answer)
        managed = self.start(thread_id=f"production_{uuid4().hex}")
        self.assert_complete(self.collect(managed))
        self.metrics["production_model_calls"] += 1
        self.assert_pool_idle()
        self.metrics["production_constructor"] = True

    def test_02_session_thread_ownership_lifecycle_and_identity(self):
        with self.pool.session(user_id=self.user_id) as session:
            method = session.list_models
            stream = session.stream(_SHORT_PROMPT, thread_id=self.thread_id)
            with ThreadPoolExecutor(max_workers=1) as workers:
                for operation in (method, lambda: next(stream), stream.close, session.close):
                    with self.assertRaisesRegex(RuntimeError, "借用线程"):
                        workers.submit(operation).result(5)
            with self.assertRaisesRegex(RuntimeError, "消费完或关闭"):
                session.list_models()
            self.assert_complete(list(stream))
            with self.assertRaisesRegex(ValueError, "绑定的用户"):
                session.list_threads(user_id="other_user")
            with self.assertRaisesRegex(RuntimeError, "嵌套"):
                with self.pool.session(user_id=self.user_id):
                    pass
            with self.assertRaisesRegex(RuntimeError, "先退出当前 session"):
                self.pool.close(wait=True, timeout=0)
            self.assertFalse(self.pool.stats["closed"])
        with self.assertRaisesRegex(RuntimeError, "已归还"):
            method()
        self.assertIsNone(get_current_user())
        self.assertEqual(self.transport.observed_users, [self.user_id])
        self.assert_pool_idle()

    def test_03_capacity_shared_with_session_and_queue_backpressure(self):
        self.replace_pool(max_waiters=1)
        active = self.stall_stream()
        with ThreadPoolExecutor(max_workers=1) as workers:
            try:
                with self.assertRaises(PoolTimeoutError):
                    self.start(acquire_timeout=0)
                waiting = workers.submit(self.session_answer, "queued_user", f"queued_{uuid4().hex}")
                self.wait_until(lambda: self.pool.stats["waiting"] == 1)
                with self.assertRaises(PoolQueueFullError):
                    self.start(thread_id=f"rejected_{uuid4().hex}", acquire_timeout=1)
                self.assertEqual(self.pool.stats["borrowed"], 1)
                self.stop_and_resume_pool(active)
                self.assertIn("OK", waiting.result(45))
            finally:
                active.close(timeout=10)
        self.assert_pool_idle()
        self.metrics["queue_limit_enforced"] = True

    def session_answer(self, user_id, thread_id):
        with self.pool.session(user_id=user_id, timeout=5) as session:
            return session.chat(_SHORT_PROMPT, thread_id=thread_id)

    def queued_round(self, position, order, *, managed):
        thread_id = f"fifo_{uuid4().hex}"
        if managed:
            stream = self.start(thread_id=thread_id, acquire_timeout=90)
            order.append(position)
            self.assert_complete(list(stream))
        else:
            with self.pool.session(user_id=self.user_id, timeout=90) as session:
                order.append(position)
                self.assertIn("OK", session.chat(_SHORT_PROMPT, thread_id=thread_id))

    def test_04_fifo_orders_mixed_session_and_managed_waiters(self):
        self.replace_pool(max_waiters=3)
        active = self.stall_stream()
        order = []
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = []
            try:
                for position in range(3):
                    futures.append(workers.submit(self.queued_round, position, order, managed=position == 1))
                    self.wait_until(lambda: self.pool.stats["waiting"] == position + 1)
                self.stop_and_resume_pool(active)
                for future in futures:
                    future.result(90)
            finally:
                active.close(timeout=10)
        self.assertEqual(order, [0, 1, 2])
        self.assert_pool_idle()
        self.metrics["admission_order"] = order

    def test_05_acquire_timeout_removes_ticket_and_allows_reuse(self):
        active = self.stall_stream()
        try:
            with self.assertRaises(PoolTimeoutError):
                self.start(thread_id=f"timeout_{uuid4().hex}", acquire_timeout=0.05)
            self.assertEqual(self.pool.stats["waiting"], 0)
            self.assertEqual(self.pool.stats["borrowed"], 1)
        finally:
            self.stop_and_resume_pool(active)
        self.assert_complete(self.collect(self.start()))
        self.assert_pool_idle()

    def exercise_independent_stop(self, *, other_user):
        transports = self.replace_pool(size=2)
        first = self.stall_stream(transports[0])
        second = self.stall_stream(transports[1], user_id=other_user, thread_id=f"other_{uuid4().hex}")
        with ThreadPoolExecutor(max_workers=2) as consumers, ThreadPoolExecutor(max_workers=1) as controller:
            first_result = consumers.submit(list, first)
            second_result = consumers.submit(list, second)
            try:
                controller.submit(first.close, timeout=10).result(12)
                self.assertFalse(second.closed)
                self.assertEqual(self.pool.stats["borrowed"], 1)
                self.assertFalse(any(event.type == "end" for event in first_result.result(5)))
                transports[1].resume.set()
                self.assert_complete(second_result.result(45))
            finally:
                first.close(timeout=10)
                second.close(timeout=10)
        self.assertEqual(transports[0].observed_users, [self.user_id])
        self.assertEqual(transports[1].observed_users, [other_user])
        self.assert_pool_idle()

    def test_06_independent_stop_between_threads_of_same_user(self):
        self.exercise_independent_stop(other_user=self.user_id)

    def test_07_independent_stop_between_users(self):
        self.exercise_independent_stop(other_user="pool_live_other")

    async def serve_http_connection(self, reader, writer, ready, finished, connections):
        request = await reader.readline()
        route = request.decode().split()[1]
        while await reader.readline() not in (b"\r\n", b""):
            pass
        stream = self.start(thread_id=f"http_{uuid4().hex}")
        connections[route] = stream
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
        await writer.drain()
        ready[route].set()
        consumer = asyncio.create_task(asyncio.to_thread(list, stream))
        disconnected = asyncio.create_task(reader.read())
        try:
            completed, _ = await asyncio.wait((consumer, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in completed:
                await asyncio.to_thread(stream.close, timeout=10)
                await consumer
            else:
                for event in consumer.result():
                    writer.write(f"data: {event.model_dump_json()}\n\n".encode())
                await writer.drain()
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            await asyncio.to_thread(stream.close, timeout=10)
            writer.close()
            await writer.wait_closed()
            finished[route].set()

    async def open_http_connection(self, port, route):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET {route} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        return reader, writer

    async def exercise_http_disconnect(self, transports):
        routes = ("/cancel", "/continue")
        ready = {route: asyncio.Event() for route in routes}
        finished = {route: asyncio.Event() for route in routes}
        connections = {}
        tasks = set()

        def handle_connection(reader, writer):
            task = asyncio.create_task(self.serve_http_connection(reader, writer, ready, finished, connections))
            tasks.add(task)

        server = await asyncio.start_server(handle_connection, "127.0.0.1", 0)
        writers = []
        try:
            port = server.sockets[0].getsockname()[1]
            _, first_writer = await self.open_http_connection(port, routes[0])
            writers.append(first_writer)
            self.assertTrue(await asyncio.to_thread(transports[0].injected.wait, 30))
            second_reader, second_writer = await self.open_http_connection(port, routes[1])
            writers.append(second_writer)
            self.assertTrue(await asyncio.to_thread(transports[1].injected.wait, 30))
            first_writer.close()
            await first_writer.wait_closed()
            await asyncio.wait_for(finished[routes[0]].wait(), 12)
            self.assertTrue(connections[routes[0]].closed)
            self.assertFalse(connections[routes[1]].closed)
            self.assertEqual(self.pool.stats["borrowed"], 1)
            transports[1].resume.set()
            response = await asyncio.wait_for(second_reader.read(), 45)
            events = [json.loads(line[6:]) for line in response.decode().splitlines() if line.startswith("data: ")]
            self.assertEqual(events[-1]["type"], "end")
            self.assertFalse(any(event["type"] == "error" for event in events))
        finally:
            for writer in writers:
                writer.close()
            for stream in connections.values():
                await asyncio.to_thread(stream.close, timeout=10)
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(asyncio.gather(*tasks), 15)

    def test_08_real_http_eof_cancels_only_disconnected_stream(self):
        transports = self.replace_pool(size=2)
        for transport in transports:
            transport.mode = "stall"
        asyncio.run(self.exercise_http_disconnect(transports))
        self.assert_pool_idle()
        self.metrics["loopback_http_eof_verified"] = True

    def test_09_same_thread_conflict_does_not_cancel_owner(self):
        self.replace_pool(size=2)
        active = self.stall_stream()
        try:
            contender = self.start()
            with self.assertRaises(ReplayConflictError):
                self.collect(contender)
            self.assertFalse(active.closed)
            self.assertEqual(self.pool.stats["borrowed"], 1)
        finally:
            self.stop_and_resume_pool(active)
        self.regenerate()

    def test_10_bounded_buffer_without_consumer_can_be_closed(self):
        stream = self.start(_LONG_PROMPT, buffer_size=1)
        self.assertTrue(self.transport.body_received.wait(30))
        with ThreadPoolExecutor(max_workers=1) as controller:
            controller.submit(stream.close, timeout=10).result(12)
        self.assertEqual(list(stream), [])
        self.assert_pool_idle()
        self.assert_complete(self.collect(self.start()))

    def test_11_concurrent_close_and_stale_handle_do_not_cancel_next_round(self):
        first = self.stall_stream()
        with ThreadPoolExecutor(max_workers=2) as controller:
            closes = [controller.submit(first.close, timeout=10) for _ in range(2)]
            for future in closes:
                future.result(12)
        self.transport.injected.clear()
        second = self.stall_stream()
        try:
            first.close(timeout=0)
            self.assertFalse(second.closed)
            self.transport.resume.set()
            self.assert_complete(self.collect(second))
        finally:
            second.close(timeout=10)
        self.assert_pool_idle()

    def test_12_invalidate_preserves_active_round_and_refreshes_next(self):
        active = self.stall_stream()
        self.assertEqual(self.transport.builds, 1)
        self.pool.invalidate_agents()
        self.assertFalse(active.closed)
        self.assertEqual(self.transport.builds, 1)
        self.transport.resume.set()
        self.assert_complete(self.collect(active))
        self.transport.mode = "healthy"
        self.assert_complete(self.collect(self.start()))
        self.assertEqual(self.transport.builds, 2)
        self.assert_pool_idle()
        self.metrics["graph_builds"] = self.transport.builds

    def test_13_close_wakes_waiter_and_drains_active_without_cancelling(self):
        active = self.stall_stream()
        with ThreadPoolExecutor(max_workers=1) as workers:
            try:
                waiting = workers.submit(self.session_answer, self.user_id, f"closing_{uuid4().hex}")
                self.wait_until(lambda: self.pool.stats["waiting"] == 1)
                self.pool.close(wait=False)
                with self.assertRaises(PoolClosedError):
                    waiting.result(5)
                self.assertFalse(active.closed)
                self.assertEqual(self.pool.stats["borrowed"], 1)
                self.transport.resume.set()
                self.assert_complete(self.collect(active))
            finally:
                active.close(timeout=10)
        self.pool.close(timeout=10)
        self.assertEqual(self.pool.stats, {"size": 1, "available": 0, "borrowed": 0, "waiting": 0, "closed": True, "failed": False})
        self.assertFalse(active._worker.thread.is_alive())
        with self.assertRaises(PoolClosedError):
            self.start()
        with self.assertRaises(PoolClosedError):
            with self.pool:
                pass

    def test_14_pool_close_timeout_keeps_ownership_until_cleanup(self):
        active = self.stall_stream()
        try:
            with self.assertRaises(PoolTimeoutError):
                self.pool.close(timeout=0.05)
            self.assertTrue(self.pool.stats["closed"])
            self.assertEqual(self.pool.stats["borrowed"], 1)
            self.assertFalse(active.closed)
            self.transport.resume.set()
            self.assert_complete(self.collect(active))
        finally:
            active.close(timeout=10)
        self.pool.close(timeout=10)
        self.assertEqual(self.pool.stats["borrowed"], 0)
        self.assertFalse(active._worker.thread.is_alive())

    def test_15_constructor_environment_and_invalid_requests_do_not_borrow(self):
        settings = {
            "DEER_FLOW_CLIENT_POOL_SIZE": "2", "DEER_FLOW_CLIENT_POOL_ACQUIRE_TIMEOUT": "0",
            "DEER_FLOW_CLIENT_POOL_MAX_WAITERS": "0",
        }
        with patch.dict("os.environ", settings):
            with DeerFlowClientPool(config_path=str(self.config_path)) as configured:
                self.assertEqual(configured.stats["size"], 2)
        for options in ({"size": 0}, {"size": True}, {"max_waiters": -1}, {"acquire_timeout": -1}, {"acquire_timeout": float("nan")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                DeerFlowClientPool(client_factory=lambda: self.client, **options)
        with self.assertRaisesRegex(ValueError, "不同实例"):
            DeerFlowClientPool(size=2, client_factory=lambda: self.client)
        for options in ({"buffer_size": 0}, {"disabled_tools": [""]}, {"replay_last_turn": "yes"}):
            with self.subTest(options=options), self.assertRaises((TypeError, ValueError)):
                self.start(**options)
        with self.assertRaises(ValueError):
            self.pool.start_stream(_SHORT_PROMPT)
        self.assert_pool_idle()
        self.metrics["model_independent_validation"] = True

    def test_16_session_reset_invalidates_both_cached_slots(self):
        transports = self.replace_pool(size=2)
        self.complete_parallel_round(2)
        self.assertEqual([transport.builds for transport in transports], [1, 1])
        with self.pool.session(user_id=self.user_id) as session:
            session.reset_agent()
        self.complete_parallel_round(2)
        self.assertEqual([transport.builds for transport in transports], [2, 2])
        self.assert_pool_idle()

    def complete_parallel_round(self, size):
        streams = [self.start(thread_id=f"parallel_{uuid4().hex}") for _ in range(size)]
        with ThreadPoolExecutor(max_workers=size) as consumers:
            results = [consumers.submit(list, stream) for stream in streams]
            for result in results:
                self.assert_complete(result.result(45))
        return streams

    def assert_checkpoint_identity(self, thread_id, marker):
        from deerflow.runtime.checkpoint_state import CheckpointStateAccessor

        accessor = CheckpointStateAccessor.bind(self.client._agent, self.saver, mode=self.mode)
        snapshot = accessor.get({"configurable": {"thread_id": thread_id}})
        self.assertFalse(snapshot.next)
        self.assertFalse(snapshot.tasks)
        contents = " ".join(str(message.content) for message in snapshot.values["messages"])
        self.assertIn(marker, contents)
        for other in _IDENTITY_PROMPTS.keys() - {marker}:
            self.assertNotIn(other, contents)

    def test_17_repeated_parallel_rounds_preserve_user_state_and_join_workers(self):
        for _ in range(2):
            self.replace_pool(size=3)
            threads = {marker: f"identity_{uuid4().hex}" for marker in _IDENTITY_PROMPTS}
            handles = []
            for round_number in range(2):
                streams = [self.start(
                    message if round_number == 0 else _SHORT_PROMPT,
                    user_id=marker, thread_id=threads[marker],
                ) for marker, message in _IDENTITY_PROMPTS.items()]
                handles.extend(streams)
                self.assertLessEqual(self.pool.stats["borrowed"], 3)
                with ThreadPoolExecutor(max_workers=3) as consumers:
                    results = [consumers.submit(list, stream) for stream in streams]
                    for result in results:
                        self.assert_complete(result.result(45))
                self.assert_pool_idle()
                for marker, thread_id in threads.items():
                    self.assert_checkpoint_identity(thread_id, marker)
            self.pool.close(timeout=10)
            self.assertTrue(all(not stream._worker.thread.is_alive() for stream in handles))
            self.assertEqual(self.pool.stats["borrowed"], 0)
        self.metrics["lifecycle_rounds"] = 2
        self.metrics["parallel_model_rounds"] = 12


if __name__ == "__main__":
    unittest.main()
