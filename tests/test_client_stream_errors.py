"""验证终止错误、既有容错及清理边界，防止失败被当作正常答复。"""

import asyncio
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphInterrupt, GraphRecursionError

from deerflow.client import DeerFlowClient, _close_stream_iterator
from deerflow.skills.presets import PublicSkillPreset
from deerflow.client_stream import ClientStreamParser
from deerflow.protocol import ErrorData, ErrorEvent, parse_stream_event
from deerflow.runtime.client_replay import ReplayConflictError, ReplayUnavailableError
from deerflow.tui.cli import _RunOutcome, _run_json
from deerflow.tui.runtime import stream_actions
from deerflow.tui.view_state import AssistantError, RunEnded


def answer():
    return parse_stream_event({"type": "answer_content", "agent_id": "main", "thread_id": "thread_001", "seq": 1, "data": {"id": "answer", "content": "部分回答"}})


def end():
    return parse_stream_event({"type": "end", "agent_id": "main", "thread_id": "thread_001", "seq": 2, "data": {"id": "run", "usage_metadata": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}})


class ClientStreamErrorTests(unittest.TestCase):
    def setUp(self):
        # 本组只验证流协议，独立隔离新增的预设文件边界。
        preset = PublicSkillPreset("default", "默认预设", frozenset(), "stream_fixture")
        self.enterContext(patch("deerflow.client.preset_store_for_config", return_value=SimpleNamespace(resolve=lambda _: preset)))
        self.client = object.__new__(DeerFlowClient)
        self.client._app_config = SimpleNamespace()

    def collect(self, implementation, **kwargs):
        with patch.object(self.client, "_stream_turn", side_effect=implementation):
            return list(self.client.stream("问题", **kwargs))

    def test_initialization_failure_has_resolved_identity_and_no_sensitive_detail(self):
        implementation = Mock(side_effect=TimeoutError("secret=provider-token"))
        events = self.collect(implementation)
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], ErrorEvent)
        self.assertEqual(events[0].seq, 1)
        self.assertEqual(parse_stream_event(events[0].model_dump_json(exclude_unset=True)), events[0])
        self.assertEqual(events[0].thread_id, implementation.call_args.kwargs["thread_id"])
        self.assertNotIn("provider-token", events[0].model_dump_json())

    def test_runtime_and_parser_failures_follow_last_published_sequence(self):
        for failure in (TimeoutError(), OSError(), ValueError(), GraphRecursionError()):
            with self.subTest(failure=type(failure).__name__):
                def implementation(*args, **kwargs):
                    yield answer()
                    raise failure
                events = self.collect(implementation, thread_id="thread_001")
                self.assertEqual([event.type for event in events], ["answer_content", "error"])
                self.assertEqual([event.seq for event in events], [1, 2])

    def test_protocol_validation_failure_does_not_reenter_broken_parser(self):
        def implementation(*args, **kwargs):
            parser = ClientStreamParser("thread_001", "run")
            yield from parser.parse(("messages", (AIMessage(id="answer", content="部分回答"), {})))
            yield from parser.parse(("custom", {"type": "context_used", "data": {}}))
        events = self.collect(implementation, thread_id="thread_001")
        self.assertEqual([event.type for event in events], ["answer_content", "error"])
        self.assertEqual(events[-1].seq, 2)

    def test_end_is_not_published_before_generator_cleanup_succeeds(self):
        def implementation(*args, **kwargs):
            yield answer()
            try:
                yield end()
            finally:
                raise OSError("清理失败")
        events = self.collect(implementation, thread_id="thread_001")
        self.assertEqual([event.type for event in events], ["answer_content", "error"])

    def test_existing_fallbacks_and_child_failure_do_not_create_terminal_error(self):
        def implementation(*args, **kwargs):
            parser = ClientStreamParser("thread_001", "run")
            messages = [
                ToolMessage(id="result", content="Error: 检索超时", tool_call_id="lookup", status="error"),
                AIMessage(id="fallback", content="服务暂时不可用", additional_kwargs={"deerflow_error_fallback": True}),
            ]
            for message in messages:
                yield from parser.parse(("messages", (message, {})))
            yield from parser.parse(("custom", {"type": "task_failed", "task_id": "child", "error": "超时"}))
            yield from parser.finish()
        events = self.collect(implementation, thread_id="thread_001")
        self.assertNotIn("error", [event.type for event in events])
        self.assertEqual(events[-1].type, "end")
        self.assertEqual(next(event for event in events if event.agent_id == "child").data.status, "failed")

    def test_rejections_and_control_signals_keep_their_exception_contract(self):
        failures = (ReplayConflictError(), ReplayUnavailableError(), GraphInterrupt(), asyncio.CancelledError(), KeyboardInterrupt(), SystemExit())
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), self.assertRaises(type(failure)):
                self.collect(Mock(side_effect=failure), thread_id="thread_001")
        for kwargs, error_type in (({"thread_id": "../invalid"}, ValueError), ({"replay_last_turn": "yes"}, TypeError), ({"replay_last_turn": True}, ReplayUnavailableError)):
            with self.subTest(kwargs=kwargs), self.assertRaises(error_type):
                list(self.client.stream("问题", **kwargs))

    def test_early_close_does_not_yield_error_and_releases_reservation(self):
        closed = Mock()
        def implementation(*args, **kwargs):
            try:
                yield answer()
            finally:
                closed()
        with patch.object(self.client, "_stream_turn", side_effect=implementation):
            stream = self.client.stream("问题", thread_id="thread_001")
            next(stream)
            stream.close()
            closed.assert_called_once()
            self.assertEqual(len(list(self.client.stream("问题", thread_id="thread_001"))), 1)

    def test_original_failure_survives_restore_failure_and_error_is_unique(self):
        def implementation(*args, **kwargs):
            yield answer()
            raise RuntimeError("原始异常")
        with patch("deerflow.runtime.client_replay.ClientTurnReplay.restore_if_needed", side_effect=OSError("恢复失败")) as restore:
            with self.assertLogs("deerflow.runtime.client_replay", level="ERROR") as logs:
                events = self.collect(implementation, thread_id="thread_001")
        restore.assert_called_once()
        self.assertEqual([event.type for event in events], ["answer_content", "error"])
        self.assertIn("原始异常", logs.output[-1])
        self.assertIn("恢复失败", "\n".join(logs.output))

    def test_graph_close_failure_preserves_active_failure_or_cancellation(self):
        for active_error in (RuntimeError("原始异常"), asyncio.CancelledError()):
            with self.subTest(active_error=type(active_error).__name__), self.assertLogs("deerflow.client", level="ERROR"):
                _close_stream_iterator(SimpleNamespace(close=Mock(side_effect=OSError("清理失败"))), active_error)
        with self.assertRaisesRegex(OSError, "清理失败"):
            _close_stream_iterator(SimpleNamespace(close=Mock(side_effect=OSError("清理失败"))), None)

    def test_chat_rejects_partial_answer_and_consumers_show_failure(self):
        event = ErrorEvent(type="error", agent_id="main", thread_id="thread_001", seq=2, data=ErrorData(error="任务无法继续"))
        with patch.object(self.client, "stream", return_value=iter([answer(), event])):
            with self.assertRaisesRegex(RuntimeError, "任务无法继续"):
                self.client.chat("问题")
        outcome = _RunOutcome()
        outcome.observe(answer())
        outcome.observe(event)
        self.assertEqual(outcome.error_text(), "任务无法继续")
        client = SimpleNamespace(stream=Mock(return_value=iter([event])))
        actions = list(stream_actions(client, "问题"))
        self.assertEqual(sum(isinstance(action, AssistantError) for action in actions), 1)
        self.assertEqual(sum(isinstance(action, RunEnded) for action in actions), 1)

    def test_json_cli_publishes_terminal_error_once_and_exits_unsuccessfully(self):
        event = ErrorEvent(type="error", agent_id="main", thread_id="thread_001", seq=1, data=ErrorData(error="任务无法继续"))
        session = SimpleNamespace(client=SimpleNamespace(stream=Mock(return_value=iter([event]))), resolve_thread=Mock(return_value="thread_001"))
        with patch("deerflow.tui.cli._make_session", return_value=session), patch("deerflow.tui.cli._resolve_message", return_value="问题"), patch("deerflow.tui.cli._run_overrides", return_value={}):
            with patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(_run_json(Mock()), 1)
                self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [event.model_dump(exclude_unset=True)])

    def test_success_inside_callers_exception_handler_still_commits(self):
        def implementation(*args, **kwargs):
            yield answer()
            yield end()
        with patch("deerflow.runtime.client_replay.ClientTurnReplay") as replay_class:
            try:
                raise RuntimeError("调用方此前的异常")
            except RuntimeError:
                events = self.collect(implementation, thread_id="thread_001")
        self.assertEqual(events[-1].type, "end")
        self.assertTrue(replay_class.return_value.committed)
