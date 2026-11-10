"""验证单轮变量在同步、托管、并发、重生成和结束清理中的生命周期。"""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from deerflow.environment import get_temporary_environment, temporary_environment_scope
from temporary_environment_support import EnvironmentChatHarness, PROBE_NAMES


class ClientTemporaryEnvironmentTests(EnvironmentChatHarness):
    def test_managed_reuse_replaces_and_removes_temporary_variables(self):
        self.assert_environment(self.managed(temporary_env={"DF_ENV_PUBLIC": "override", "DF_ENV_OLD_ONLY": "only-first"}), DF_ENV_PUBLIC="override", DF_ENV_OLD_ONLY="only-first")
        self.assert_environment(self.managed(temporary_env={"DF_ENV_TEMP": "second"}), DF_ENV_TEMP="second")
        self.assert_environment(self.managed(temporary_env={}))
        self.assert_environment(self.managed())
        self.assertEqual(get_temporary_environment(), {})
        self.assertEqual(self.pool.stats["borrowed"], 0)

    def test_session_chat_stream_and_direct_client_capture_call_arguments(self):
        with self.pool.session(user_id="alice") as client:
            first = json.loads(client.chat("probe", thread_id="sync_env", temporary_env={"DF_ENV_PRIVATE": "temporary-private"}))
            self.assert_environment(first, DF_ENV_PRIVATE="temporary-private")
            variables = {"DF_ENV_TEMP": "captured-value"}
            stream = client.stream("probe", thread_id="sync_env", temporary_env=variables)
            variables["DF_ENV_TEMP"] = "changed-by-caller"
            list(stream)
            result = self.saver.get_tuple({"configurable": {"thread_id": "sync_env"}}).checkpoint["channel_values"]["messages"][-1].content
            self.assert_environment(json.loads(result), DF_ENV_TEMP="captured-value")
            self.assert_environment(json.loads(client.chat("probe", thread_id="sync_env")))
        direct = self.pool._available[0].client
        variables = {"DF_ENV_TEMP": "direct-snapshot"}
        stream = direct.stream("probe", user_id="alice", thread_id="direct_env", temporary_env=variables)
        variables.clear()
        list(stream)
        result = self.saver.get_tuple({"configurable": {"thread_id": "direct_env"}}).checkpoint["channel_values"]["messages"][-1].content
        self.assert_environment(json.loads(result), DF_ENV_TEMP="direct-snapshot")

    def test_invalid_inputs_fail_before_slot_acquisition_or_stream_execution(self):
        invalid = [[], "text", {"NAME": None}, {"NAME": 1}, {None: "x"}, {"A=B": "x"}, {"BAD\n": "x"}, {"NAME": "a\0b"}]
        for variables in invalid:
            with self.subTest(variables=variables):
                with self.assertRaises((TypeError, ValueError)):
                    self.pool.start_stream("probe", user_id="alice", temporary_env=variables)
                with self.pool.session(user_id="alice") as client:
                    with self.assertRaises((TypeError, ValueError)):
                        client.stream("probe", temporary_env=variables)
                    with self.assertRaises((TypeError, ValueError)):
                        client.chat("probe", temporary_env=variables)
                self.assertEqual(self.pool.stats["borrowed"], 0)
        self.assertEqual(self.observations, [])

    def test_concurrent_users_and_parallel_tools_do_not_mix_variables(self):
        pool = self.new_pool(size=2)
        barrier = threading.Barrier(2)
        self.before_choose = lambda: barrier.wait(timeout=10)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.managed, "parallel", pool=pool, thread_id="parallel_a", temporary_env={"DF_ENV_TEMP": "alice-turn"})
            second = executor.submit(self.managed, "parallel", pool=pool, thread_id="parallel_b", user_id="bob", temporary_env={"DF_ENV_TEMP": "bob-turn"})
            self.assert_environment(first.result(timeout=20), DF_ENV_TEMP="alice-turn")
            self.assert_environment(second.result(timeout=20), user="bob", DF_ENV_TEMP="bob-turn")
        self.before_choose = None
        self.assert_environment(self.managed(pool=pool, thread_id="parallel_a"))

    def test_failure_cancel_and_replay_do_not_recover_old_temporary_values(self):
        self.managed(temporary_env={"DF_ENV_OLD_ONLY": "previous"})
        self.assert_environment(self.managed(replay_last_turn=True, temporary_env={"DF_ENV_TEMP": "replayed"}), DF_ENV_TEMP="replayed")
        self.assert_environment(self.managed(replay_last_turn=True))
        with self.assertRaisesRegex(RuntimeError, "合成测试失败"):
            self.managed("fail", temporary_env={"DF_ENV_TEMP": "failed"})
        self.assert_environment(self.managed())
        stream = self.pool.start_stream("wait", user_id="alice", thread_id="env_thread", temporary_env={"DF_ENV_TEMP": "cancelled"})
        try:
            deadline = time.monotonic() + 10
            while not self.started_marker().exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
        finally:
            stream.close(timeout=10)
        self.assert_environment(self.managed())
        self.assertEqual(get_temporary_environment(), {})

    def test_values_are_literal_and_do_not_persist_in_files_checkpoints_or_context(self):
        special = "中文 ' \" \\ \n $HOME $(touch should_not_exist) `echo no` ; |"
        before = {path: path.read_bytes() for path in self.data_root.rglob(".bashrc")}
        variables = {"DF_ENV_SPECIAL": special, "DF_ENV_EMPTY": "", "DF_ENV_TOKEN": "sensitive-temporary-token"}
        self.assert_environment(self.managed(temporary_env=variables), **variables)
        self.assertEqual(before, {path: path.read_bytes() for path in self.data_root.rglob(".bashrc")})
        persisted = repr(list(self.saver.list(None))) + repr(self.observations)
        for value in (special, variables["DF_ENV_TOKEN"]):
            self.assertNotIn(value, persisted)
        self.assertFalse(any(self.root.rglob("should_not_exist")))
        self.assertTrue(all(name not in os.environ for name in PROBE_NAMES))
        with self.pool.session(user_id="alice") as client:
            output = client.chat("raw", thread_id="redaction_env", temporary_env={"DF_ENV_TOKEN": "sensitive-temporary-token"})
        self.assertNotIn("sensitive-temporary-token", output)
        self.assertIn("[redacted]", output)

    def test_sync_yield_close_and_nested_default_restore_context(self):
        direct = self.pool._available[0].client
        with temporary_environment_scope({"DF_ENV_TEMP": "outer"}):
            stream = direct.stream("probe", user_id="alice", thread_id="scoped_env", temporary_env={"DF_ENV_TEMP": "inner"})
            next(stream)
            self.assertEqual(get_temporary_environment(), {"DF_ENV_TEMP": "outer"})
            stream.close()
            self.assertEqual(get_temporary_environment(), {"DF_ENV_TEMP": "outer"})
            self.assert_environment(self.managed())
        self.assertEqual(get_temporary_environment(), {})

    def test_managed_snapshot_is_copied_before_background_execution(self):
        release = threading.Event()
        self.before_choose = lambda: release.wait(timeout=10)
        variables = {"DF_ENV_TEMP": "captured-managed"}
        stream = self.pool.start_stream("probe", user_id="alice", thread_id="env_thread", temporary_env=variables)
        try:
            self.assertTrue(self.choose_entered.wait(5))
            variables["DF_ENV_TEMP"] = "mutated"
            release.set()
            list(stream)
        finally:
            release.set()
            stream.close(timeout=10)
        result = self.saver.get_tuple({"configurable": {"thread_id": "env_thread"}}).checkpoint["channel_values"]["messages"][-1].content
        self.assert_environment(json.loads(result), DF_ENV_TEMP="captured-managed")

    def test_isolated_subagent_loop_inherits_and_restores_parent_environment(self):
        from deerflow.subagents.executor import _copy_isolated_subagent_context, _submit_to_isolated_loop_in_context

        async def read_child():
            return await asyncio.to_thread(get_temporary_environment)

        for value in ("parent-one", "parent-two"):
            with temporary_environment_scope({"DF_ENV_TEMP": value}):
                future = _submit_to_isolated_loop_in_context(_copy_isolated_subagent_context(), read_child)
                self.assertEqual(future.result(timeout=5), {"DF_ENV_TEMP": value})
        future = _submit_to_isolated_loop_in_context(_copy_isolated_subagent_context(), read_child)
        self.assertEqual(future.result(timeout=5), {})
