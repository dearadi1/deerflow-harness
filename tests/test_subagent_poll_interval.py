"""验证轮询提速不会提前结束等待预算或跳过子代理收尾。"""

import asyncio
import importlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.executor import SubagentResult, SubagentStatus
from deerflow.tools.types import Runtime


task_module = importlib.import_module("deerflow.tools.builtins.task_tool")
_INTERVAL_ENV = "DEER_FLOW_SUBAGENT_POLL_INTERVAL_SECONDS"


class SubagentPollEnvironmentTests(unittest.TestCase):
    def test_default_blank_and_explicit_intervals(self):
        for raw_value, expected in ((None, 0.1), ("", 0.1), ("  ", 0.1), ("0.25", 0.25), (" 5 ", 5.0)):
            with self.subTest(raw_value=raw_value), patch.dict(os.environ):
                os.environ.pop(_INTERVAL_ENV, None)
                if raw_value is not None:
                    os.environ[_INTERVAL_ENV] = raw_value
                self.assertEqual(task_module._load_subagent_poll_interval_seconds(), expected)

    def test_invalid_intervals_are_rejected(self):
        for raw_value in ("0", "-0.1", "nan", "inf", "-inf", "1e999", "invalid"):
            with self.subTest(raw_value=raw_value), patch.dict(os.environ, {_INTERVAL_ENV: raw_value}):
                with self.assertRaisesRegex(ValueError, _INTERVAL_ENV):
                    task_module._load_subagent_poll_interval_seconds()


class SubagentPollLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.result = SubagentResult(task_id="execution_1", trace_id="poll_test", status=SubagentStatus.RUNNING)
        self.runtime = Runtime(state={}, context={"user_id": "poll_user"}, config={}, stream_writer=lambda value: None, tool_call_id="tool_1", store=None)
        self.enterContext(patch.object(task_module, "_SUBAGENT_POLL_INTERVAL_SECONDS", 0.1))
        self.enterContext(patch.object(task_module, "get_available_subagent_names", return_value=["probe"]))
        self.enterContext(patch.object(task_module, "get_subagent_config", return_value=SubagentConfig(name="probe", description="轮询验证", model="probe_model", timeout_seconds=5)))
        self.enterContext(patch.object(task_module, "resolve_run_extensions", return_value=None))
        self.enterContext(patch.object(task_module, "run_assembly", new=AsyncMock(return_value=[])))
        executor = self.enterContext(patch.object(task_module, "SubagentExecutor"))
        executor.return_value.execute_async.return_value = self.result.task_id
        self.lookup = self.enterContext(patch.object(task_module, "get_background_task_result", return_value=self.result))
        self.enterContext(patch.object(task_module, "get_stream_writer", return_value=lambda value: None))
        self.enterContext(patch.object(task_module, "aemit_custom_event", new=AsyncMock()))
        self.report_usage = self.enterContext(patch.object(task_module, "_report_subagent_usage"))
        self.cleanup = self.enterContext(patch.object(task_module, "cleanup_background_task"))
        self.cancel = self.enterContext(patch.object(task_module, "request_cancel_background_task"))
        self.defer_cleanup = self.enterContext(patch.object(task_module, "_schedule_deferred_subagent_cleanup"))
        self.enterContext(patch.object(task_module, "_task_result_command", side_effect=lambda **kwargs: kwargs))

    def invoke_task(self):
        return task_module.task_tool.coroutine(prompt="等待沙盒命令", subagent_type="probe", runtime=self.runtime, tool_call_id="tool_1")

    async def test_cancel_waits_for_terminal_then_returns_promptly(self):
        polling = asyncio.Event()

        def lookup(execution_id):
            polling.set()
            return self.result

        def request_cancel(execution_id):
            handle = asyncio.get_running_loop().call_later(0.02, self.result.try_set_terminal, SubagentStatus.CANCELLED)
            self.addCleanup(handle.cancel)

        self.lookup.side_effect = lookup
        self.cancel.side_effect = request_cancel
        task = asyncio.create_task(self.invoke_task())
        await asyncio.wait_for(polling.wait(), timeout=1)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.5)
        try:
            self.assertIn(task, done, "子代理已结束后不应再等待原有的 5 秒轮询")
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIs(self.result.status, SubagentStatus.CANCELLED)
            self.report_usage.assert_called_once_with(self.runtime, self.result)
            self.cleanup.assert_called_once_with(self.result.task_id)
            self.defer_cleanup.assert_not_called()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_faster_polling_preserves_execution_timeout_and_buffer(self):
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)

        with patch.object(task_module.asyncio, "sleep", new=sleep):
            outcome = await self.invoke_task()
        self.assertEqual(outcome["status"], "polling_timed_out")
        self.assertTrue(all(delay == 0.1 for delay in sleeps))
        self.assertGreaterEqual(sum(sleeps), 65)
        self.assertLess(sum(sleeps), 65.2)
        self.cancel.assert_called_once_with(self.result.task_id)
        self.defer_cleanup.assert_called_once()

    async def test_deferred_cleanup_uses_the_same_short_interval(self):
        async def sleep(delay):
            self.assertEqual(delay, 0.1)
            self.cleanup.assert_not_called()
            self.result.try_set_terminal(SubagentStatus.CANCELLED)

        with patch.object(task_module.asyncio, "sleep", new=sleep):
            await task_module._deferred_cleanup_subagent_task(None, self.result.task_id, "poll_test", 10)
        self.cleanup.assert_called_once_with(self.result.task_id)


if __name__ == "__main__":
    unittest.main()
