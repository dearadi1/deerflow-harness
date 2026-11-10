"""验证错误展示与执行状态分离，覆盖重试、保护分支和增量去重。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.runtime import Runtime
from pydantic import ValidationError

from deerflow.agents.middlewares.llm_error_handling_middleware import EmptyModelResponseError, LLMErrorHandlingMiddleware
from deerflow.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware
from deerflow.agents.middlewares.model_length_finish_reason_middleware import ModelLengthFinishReasonMiddleware
from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware
from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware
from deerflow.agents.middlewares.terminal_response_middleware import TerminalResponseMiddleware
from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware
from deerflow.answer_errors import ANSWER_ERRORS_KEY, mark_answer_error
from deerflow.client_stream import ClientStreamParser
from deerflow.config.app_config import AppConfig
from deerflow.config.error_handling_config import DEFAULT_ERROR_MESSAGE, ErrorHandlingConfig
from deerflow.config.token_budget_config import TokenBudgetConfig
from deerflow.models.request_admission import AdmissionError
from deerflow.protocol import parse_stream_event


def app_config(**kwargs):
    return AppConfig(sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"}, llm_call={"retry_base_delay_ms": 0, "retry_cap_delay_ms": 0, "burst_retry_base_delay_ms": 0}, **kwargs)


def tool_message(name="lookup", content="已有结论"):
    return AIMessage(id="answer", content=content, tool_calls=[{"id": "call", "name": name, "args": {}}])


def collect(message, *, previous="", error_message=DEFAULT_ERROR_MESSAGE, child=False):
    parser = ClientStreamParser("thread", "run", error_message=error_message)
    metadata = {"parent_tool_call_id": "child"} if child else {}
    message = message.model_copy(update={"id": message.id or "answer"})
    events = list(parser.parse(("messages", (AIMessageChunk(id=message.id, content=previous), metadata)))) if previous else []
    for _ in range(2):
        events.extend(parser.parse(("messages", (message, metadata))))
    if not child:
        events.extend(parser.parse(("values", {"messages": [HumanMessage(content="问题", additional_kwargs={"run_id": "run"}), message]})))
    events.extend(parser.finish())
    return events


class AnswerErrorTests(unittest.TestCase):
    def assert_error(self, message, *, previous="", error_message=DEFAULT_ERROR_MESSAGE, child=False):
        original = message.model_dump()
        events = collect(message, previous=previous, error_message=error_message, child=child)
        answers = [event for event in events if event.type == "answer_content"]
        text = "".join(event.data.content for event in answers)
        errors = [event.data.additional_kwargs.error for event in answers if event.data.additional_kwargs is not None]
        self.assertTrue(text.startswith(previous))
        self.assertEqual(text.count(error_message), 1)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0])
        self.assertEqual(events[-1].type, "end")
        self.assertNotIn("error", [event.type for event in events])
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertNotIn(ANSWER_ERRORS_KEY, answers[-1].model_dump_json())
        self.assertEqual(parse_stream_event(answers[-1].model_dump_json()), answers[-1])
        self.assertEqual(message.model_dump(), original)
        return text, errors[0]

    def test_llm_failure_categories_in_sync_and_async_wrappers(self):
        cases = [
            (RuntimeError("insufficient_quota"), "quota", 1),
            (RuntimeError("invalid api key"), "auth", 1),
            (AdmissionError("queue full"), "admission", 1),
            (RuntimeError("limit_burst_rate"), "burst_rate", 2),
            (RuntimeError("server busy"), "busy", 3),
            (RuntimeError("HTTP 503"), "transient", 3),
            (type("ReadTimeout", (RuntimeError,), {})("read timeout"), "transient", 2),
            (type("StreamChunkTimeoutError", (RuntimeError,), {})("stream timeout"), "transient", 2),
            (IndexError("empty generations"), "transient", 3),
            (ValueError("No generations found in stream."), "transient", 3),
            (EmptyModelResponseError(), "empty_response", 2),
            (ValueError("model does not support function calling; request_id=123"), "generic", 1),
        ]
        for asynchronous in (False, True):
            for failure, reason, attempts in cases:
                with self.subTest(asynchronous=asynchronous, reason=reason, failure=type(failure).__name__):
                    middleware = LLMErrorHandlingMiddleware(app_config=app_config())
                    request = SimpleNamespace(runtime=Runtime(context={}))
                    handler = AsyncMock(side_effect=failure) if asynchronous else Mock(side_effect=failure)
                    with patch.object(middleware, "_emit_retry_event"), patch.object(middleware, "_aemit_retry_event", new_callable=AsyncMock):
                        response = asyncio.run(middleware.awrap_model_call(request, handler)) if asynchronous else middleware.wrap_model_call(request, handler)
                    self.assertEqual(handler.call_count, attempts)
                    self.assertEqual(response.additional_kwargs["error_reason"], reason)
                    _, detail = self.assert_error(response)
                    self.assertEqual(detail, str(failure))

    def test_retry_success_does_not_receive_error_metadata(self):
        for asynchronous in (False, True):
            middleware = LLMErrorHandlingMiddleware(app_config=app_config())
            success = AIMessage(id="answer", content="正常答复")
            handler = AsyncMock(side_effect=[RuntimeError("HTTP 503"), success]) if asynchronous else Mock(side_effect=[RuntimeError("HTTP 503"), success])
            with patch.object(middleware, "_emit_retry_event"), patch.object(middleware, "_aemit_retry_event", new_callable=AsyncMock):
                response = asyncio.run(middleware.awrap_model_call(None, handler)) if asynchronous else middleware.wrap_model_call(None, handler)
            answers = [event for event in collect(response) if event.type == "answer_content"]
            self.assertIs(response, success)
            self.assertEqual(handler.call_count, 2)
            self.assertEqual([event.data.content for event in answers], ["正常答复"])
            self.assertIsNone(answers[0].data.additional_kwargs)

    def test_circuit_open_and_empty_terminal_keep_their_internal_markers(self):
        middleware = LLMErrorHandlingMiddleware(app_config=app_config())
        with patch.object(middleware, "_check_circuit", return_value=True):
            handler = Mock()
            response = middleware.wrap_model_call(None, handler)
        handler.assert_not_called()
        self.assertEqual(self.assert_error(response)[1], "LLM circuit breaker is open")
        state = {"messages": [HumanMessage(content="问题"), ToolMessage(content="完成", tool_call_id="call"), AIMessage(id="answer", content="")]}
        response = TerminalResponseMiddleware().after_model(state, Runtime(context={}))['messages'][0]
        self.assert_error(response)
        self.assertTrue(response.additional_kwargs["deerflow_error_fallback"])

    def test_control_signals_and_cancellation_still_propagate(self):
        middleware = LLMErrorHandlingMiddleware(app_config=app_config())
        for failure in (GraphInterrupt(), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__), self.assertRaises(type(failure)):
                asyncio.run(middleware.awrap_model_call(None, AsyncMock(side_effect=failure)))

    def test_length_and_safety_guards_preserve_stops_and_visible_prefix(self):
        for middleware, reason, stop_reason in ((ModelLengthFinishReasonMiddleware(), "length", "model_length_capped"), (SafetyFinishReasonMiddleware(), "content_filter", "safety_capped")):
            for content in ("已有结论", [{"type": "thinking", "thinking": "思考"}, {"type": "text", "text": "已有结论"}]):
                with self.subTest(middleware=type(middleware).__name__, content=content):
                    message = tool_message(content=content)
                    message.response_metadata = {"finish_reason": reason}
                    runtime = Runtime(context={"run_id": "run"})
                    response = middleware.after_model({"messages": [message]}, runtime)["messages"][0]
                    text, _ = self.assert_error(response, previous="已有结论")
                    self.assertNotIn("suppressed because", text)
                    self.assertNotIn("模型在生成完整", text)
                    self.assertEqual(response.tool_calls, [])
                    self.assertEqual(runtime.context["stop_reason"], stop_reason)
                    self.assertNotIn("deerflow_error_fallback", response.additional_kwargs)

    def test_loop_token_and_subagent_guards_keep_existing_control_flow(self):
        runtime = Runtime(context={"thread_id": "thread", "run_id": "run"})
        message = tool_message()
        loop = LoopDetectionMiddleware(warn_threshold=1, hard_limit=1)
        response = loop.after_model({"messages": [message]}, runtime)["messages"][0]
        self.assertIn("[FORCED STOP]", self.assert_error(response, previous="已有结论")[1])
        self.assertEqual(runtime.context["stop_reason"], "loop_capped")
        budget = TokenBudgetMiddleware(TokenBudgetConfig(enabled=True, max_tokens=1000))
        message.usage_metadata = {"input_tokens": 1000, "output_tokens": 1, "total_tokens": 1001}
        response = budget.after_model({"messages": [message]}, runtime)["messages"][0]
        self.assertIn("[TOKEN BUDGET EXCEEDED]", self.assert_error(response, previous="已有结论")[1])
        self.assertEqual(runtime.context["stop_reason"], "token_capped")
        state = {"messages": [tool_message("task")], "delegations": [{"id": "prior", "run_id": "run"}]}
        response = SubagentLimitMiddleware(max_total=1).after_model(state, runtime)["messages"][0]
        self.assertIn("[SUBAGENT LIMIT REACHED]", self.assert_error(response, previous="已有结论")[1])
        self.assertEqual(runtime.context["stop_reason"], "subagent_limit_capped")
        self.assertEqual(response.tool_calls, [])

    def test_skill_activation_failure_preserves_actual_file_exception(self):
        middleware = SkillActivationMiddleware(app_config=app_config(), slash_source_owner_token="test")
        skill = SimpleNamespace(name="demo", enabled=True, skill_file=Path("/missing/SKILL.md"))
        storage = Mock(load_skills=Mock(return_value=[skill]))
        request = SimpleNamespace(messages=[HumanMessage(id="user", content="/demo")], runtime=Runtime(context={}))
        with patch.object(middleware, "_storage", return_value=storage), patch.object(middleware, "_activation_allowed", return_value=True):
            with patch("deerflow.agents.middlewares.skill_activation_middleware.resolve_slash_skill", return_value=SimpleNamespace(skill=skill)):
                with patch.object(middleware, "_read_skill_content", side_effect=OSError("原始磁盘错误")):
                    response, activation = middleware._prepare_model_request(request, hook="wrap_model_call")
        self.assertIsNone(activation)
        self.assertEqual(self.assert_error(response)[1], "OSError: 原始磁盘错误")
        self.assertNotIn("deerflow_error_fallback", response.additional_kwargs)

    def test_multiple_guards_emit_one_notice_and_keep_all_details(self):
        message = mark_answer_error(AIMessage(id="answer", content="已有结论\n\n错误甲"), "错误甲")
        message = mark_answer_error(message.model_copy(update={"content": message.content + "\n\n错误乙"}), "错误乙")
        text, detail = self.assert_error(message, previous="已有结论", error_message="自定义提示")
        self.assertEqual(detail, "错误甲\n\n错误乙")
        self.assertNotIn("错误甲", text)
        self.assertNotIn("错误乙", text)

    def test_subagent_answer_is_projected_but_tool_result_is_untouched(self):
        message = mark_answer_error(AIMessage(id="answer", content="子任务原始错误"), "子任务原始错误")
        self.assert_error(message, child=True)
        parser = ClientStreamParser("thread", "run")
        result = ToolMessage(id="result", content="子任务原始错误", tool_call_id="child", status="error")
        events = list(parser.parse(("messages", (result, {}))))
        self.assertEqual(events[0].type, "tool_result")
        self.assertEqual(events[0].data.content, "子任务原始错误")

    def test_unmarked_error_looking_text_and_old_history_are_untouched(self):
        message = AIMessage(id="answer", content="LLM request failed: 示例说明")
        answers = [event for event in collect(message) if event.type == "answer_content"]
        self.assertEqual(answers[0].data.content, message.content)
        self.assertIsNone(answers[0].data.additional_kwargs)
        historical = mark_answer_error(message, message.content)
        parser = ClientStreamParser("thread", "run")
        events = list(parser.parse(("values", {"messages": [historical, HumanMessage(content="新问题", additional_kwargs={"run_id": "run"})]})))
        self.assertEqual(events, [])

    def test_invalid_error_markers_do_not_break_stream(self):
        for records in (None, {}, [None], [{"start": True, "text": "答复", "error": "错误"}], [{"start": 0, "text": "不存在", "error": "错误"}]):
            message = AIMessage(id="answer", content="答复", additional_kwargs={ANSWER_ERRORS_KEY: records})
            answers = [event for event in collect(message) if event.type == "answer_content"]
            self.assertEqual(answers[0].data.content, "答复")


class ErrorHandlingConfigTests(unittest.TestCase):
    def test_default_custom_yaml_and_invalid_values(self):
        self.assertEqual(app_config().error_handling.fallback_message, DEFAULT_ERROR_MESSAGE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text('sandbox:\n  use: deerflow.sandbox.local:LocalSandboxProvider\nerror_handling:\n  fallback_message: "自定义错误提示"\n', encoding="utf-8")
            self.assertEqual(AppConfig.from_file(str(path)).error_handling.fallback_message, "自定义错误提示")
        for value in ("", "  \n", None, True, 123):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ErrorHandlingConfig(fallback_message=value)
