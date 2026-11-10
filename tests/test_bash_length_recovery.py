"""验证 bash 截断只产生重生成结果，且流、历史及真实工具调用保持一致。"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock

import httpx
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.bash_length_recovery import BASH_REGENERATION_PROMPT, recover_bash_arguments
from deerflow.agents.middlewares.model_length_finish_reason_middleware import ModelLengthFinishReasonMiddleware, _MODEL_LENGTH_CAPPED_CONTENT
from deerflow.agents.middlewares.model_length_recovery import RECOVERY_MARKER, pending_write_recovery, recover_tool_call
from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware
from deerflow.agents.middlewares.tool_receipt_middleware import ToolReceiptMiddleware
from deerflow.agents.thread_state import get_thread_state_schema
from deerflow.client_stream import ClientStreamParser
from deerflow.subagents.step_events import capture_new_step_messages, capture_step_message
from test_write_file_length_recovery import ModelServer, truncated_message


def bash_message(arguments, **kwargs):
    return truncated_message(arguments, name="bash", call_id="bash_1", **kwargs)


def recovery_request(message):
    return ToolCallRequest(tool_call=message.tool_calls[0], tool=None, state={"messages": [message]}, runtime=None)


class BashArgumentRecoveryTests(unittest.TestCase):
    def test_every_boundary_in_both_argument_orders_is_recoverable(self):
        arguments = {"description": "生成脚本", "command": 'echo "你好"\n' + "内容😀" * 120 + "\\end"}
        for ordered in [arguments, dict(reversed(arguments.items()))]:
            raw = json.dumps(ordered, ensure_ascii=True)
            for position in range(len(raw) + 1):
                with self.subTest(order=list(ordered), position=position):
                    recovered = recover_bash_arguments(raw[:position])
                    self.assertIsNotNone(recovered)
                    self.assertTrue(arguments["description"].startswith(recovered["description"]))
                    self.assertTrue(arguments["command"].startswith(recovered["command"]))
                    self.assertLessEqual(len(recovered["command"]), 100)
                    json.dumps(recovered, ensure_ascii=False).encode("utf-8")

    def test_missing_fields_complete_json_and_trailing_comma(self):
        cases = [
            ("", {"description": "", "command": ""}),
            ("{}", {"description": "", "command": ""}),
            ('{ "description": "未完成', {"description": "未完成", "command": ""}),
            ('{"command":"echo hi", "description":', {"description": "", "command": "echo hi"}),
            ('{"command":"echo hi", ', {"description": "", "command": "echo hi"}),
            ({"command": "中" * 101}, {"description": "", "command": "中" * 100}),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(recover_bash_arguments(raw), expected)

    def test_malformed_json_and_wrong_types_stay_rejected(self):
        cases = [
            None, [], {"command": None}, {"description": 12}, {"command": ["echo"]},
            '{"command":"bad\\q', '{"command":"bad\nraw', '{"command":"\\ud800"}',
            '{"command":"a","command":"b', '{"command":"a","command',
            '{"command":"a"} trailing', '{"unknown":"a', '{"command":true}',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(recover_bash_arguments(raw))

    def test_recovery_cleans_all_native_copies_without_changing_original(self):
        raw = '{"command":"' + "中" * 200
        original = bash_message(raw)
        original.content = [{"type": "text", "text": "运行中"}, {"type": "function_call", "call_id": "bash_1", "name": "bash", "arguments": raw}]
        original.additional_kwargs["function_call"] = {"name": "bash", "arguments": raw}
        original.response_metadata["id"] = "resp_original"
        before = original.model_dump()
        recovered = recover_tool_call(original, [original])
        self.assertEqual(original.model_dump(), before)
        self.assertEqual(recovered.content, [{"type": "text", "text": "运行中"}])
        self.assertEqual(recovered.tool_calls[0]["args"]["command"], "中" * 100)
        self.assertEqual(recovered.invalid_tool_calls, [])
        self.assertNotIn("tool_calls", recovered.additional_kwargs)
        self.assertNotIn("function_call", recovered.additional_kwargs)
        self.assertNotIn("id", recovered.response_metadata)
        self.assertNotIn("中" * 101, json.dumps(recovered.model_dump(), ensure_ascii=False))

    def test_result_closes_same_call_without_executing_sync_or_async_handler(self):
        original = bash_message('{"description":"未完')
        recovered = recover_tool_call(original, [original])
        middleware = ModelLengthFinishReasonMiddleware()
        handler, async_handler = Mock(), AsyncMock()
        request = recovery_request(recovered)
        results = [middleware.wrap_tool_call(request, handler), asyncio.run(middleware.awrap_tool_call(request, async_handler))]
        for result in results:
            self.assertEqual((result.name, result.tool_call_id, result.status), ("bash", "bash_1", "error"))
            self.assertEqual(result.content, BASH_REGENERATION_PROMPT)
            self.assertEqual(result.additional_kwargs[RECOVERY_MARKER], recovered.additional_kwargs[RECOVERY_MARKER])
            self.assertTrue(result.additional_kwargs["deerflow_tool_meta"]["recoverable_by_model"])
        handler.assert_not_called()
        async_handler.assert_not_awaited()
        self.assertIsNone(middleware.after_model({"messages": [recovered]}, Runtime(context={})))

    def test_unmarked_or_unmatched_bash_uses_normal_handler(self):
        original = bash_message('{"command":"echo hi')
        recovered = recover_tool_call(original, [original])
        mismatched = recovery_request(recovered)
        mismatched = mismatched.override(tool_call={**mismatched.tool_call, "id": "other"})
        normal = recovered.model_copy(update={"additional_kwargs": {}})
        middleware = ModelLengthFinishReasonMiddleware()
        expected = ToolMessage(content="executed", tool_call_id="bash_1")
        for request in [mismatched, recovery_request(normal)]:
            handler, async_handler = Mock(return_value=expected), AsyncMock(return_value=expected)
            self.assertIs(middleware.wrap_tool_call(request, handler), expected)
            self.assertIs(asyncio.run(middleware.awrap_tool_call(request, async_handler)), expected)
            handler.assert_called_once_with(request)
            async_handler.assert_awaited_once_with(request)

    def test_all_length_detectors_recover_but_normal_and_safety_stops_do_not(self):
        metadata = [
            {"finish_reason": "length"}, {"finish_reason": "MAX_TOKENS"}, {"stop_reason": "max_tokens"},
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        ]
        middleware = ModelLengthFinishReasonMiddleware()
        for evidence in metadata:
            message = bash_message('{"command":"echo hi')
            message.response_metadata = evidence
            context = {}
            updated = middleware.after_model({"messages": [message]}, Runtime(context=context))
            self.assertIn(RECOVERY_MARKER, updated["messages"][0].additional_kwargs)
            self.assertNotIn("stop_reason", context)
            self.assertNotIn(_MODEL_LENGTH_CAPPED_CONTENT, str(updated))
        for reason in ["tool_calls", "stop", "content_filter"]:
            message = bash_message('{"command":"echo hi', reason=reason)
            self.assertIsNone(middleware.after_model({"messages": [message]}, Runtime(context={})))
        message = bash_message('{"command":"echo hi')
        message.additional_kwargs["safety_termination"] = {"reason": "blocked"}
        self.assertIsNone(recover_tool_call(message, [message]))

    def test_ambiguous_or_unsupported_siblings_keep_terminal_fallback(self):
        for name, raw, identity in [("str_replace", '{"path":"', "other"), ("bash", '{"command":"', "other"), ("read_file", '{}', "bash_1")]:
            message = bash_message('{"command":"echo hi')
            message.additional_kwargs["tool_calls"].append({"id": identity, "type": "function", "function": {"name": name, "arguments": raw}})
            self.assertIsNone(recover_tool_call(message, [message]))

    def test_bash_after_pending_write_does_not_use_write_path_or_append(self):
        write = truncated_message('{"path":"/tmp/a","content":"abc')
        write = recover_tool_call(write, [write])
        messages = [write, ToolMessage(content="OK", tool_call_id="write_1")]
        self.assertIsNotNone(pending_write_recovery(messages))
        bash = bash_message('{"command":"echo hi')
        recovered = recover_tool_call(bash, [*messages, bash])
        self.assertEqual(recovered.tool_calls[0]["args"], {"description": "", "command": "echo hi"})

    def test_subagent_capture_waits_for_compact_repaired_snapshot(self):
        original = bash_message('{"command":"' + "x" * 200)
        recovered = recover_tool_call(original, [original])
        captured, seen = [], set()
        self.assertFalse(capture_step_message(original, captured, seen))
        self.assertTrue(capture_step_message(recovered, captured, seen))
        self.assertFalse(capture_step_message(recovered, captured, seen))
        self.assertEqual(captured[0]["tool_calls"][0]["args"]["command"], "x" * 100)


class BashRecoveryGraphTests(unittest.TestCase):
    def build_graph(self, responses, *, channel_mode="full"):
        server = ModelServer(responses)
        transport = httpx.MockTransport(server.handle)
        sync_client, async_client = httpx.Client(transport=transport), httpx.AsyncClient(transport=transport)
        self.addCleanup(sync_client.close)
        self.addCleanup(lambda: asyncio.run(async_client.aclose()))
        model = ChatOpenAI(model="test-model", api_key="test", streaming=True, max_retries=0, http_client=sync_client, http_async_client=async_client)
        executed = []

        @tool("bash")
        def bash(description: str, command: str) -> str:
            """记录真正到达工具边界的命令，验证截断命令未执行。"""
            executed.append(command)
            return "executed"

        @tool("read_file")
        def read_file(path: str) -> str:
            """验证同批参数完整的其他工具仍正常运行。"""
            return "file content"

        middlewares = [ToolReceiptMiddleware(render_mode="delegation_only"), ModelLengthFinishReasonMiddleware(), SafetyFinishReasonMiddleware()]
        graph = create_agent(model, [bash, read_file], middleware=middlewares, checkpointer=InMemorySaver(), state_schema=get_thread_state_schema(channel_mode))
        return graph, server, executed

    def collect(self, graph, *, asynchronous=False):
        parser, events = ClientStreamParser("thread", "run"), []
        initial = {"messages": [HumanMessage(content="执行任务")]}
        options = {"config": {"configurable": {"thread_id": "thread"}}, "context": {"run_id": "run"}, "stream_mode": ["messages", "values", "custom"]}
        if asynchronous:
            async def run():
                async for item in graph.astream(initial, **options):
                    events.extend(parser.parse(item))
            asyncio.run(run())
        else:
            for item in graph.stream(initial, **options):
                events.extend(parser.parse(item))
        events.extend(parser.finish())
        return events

    def assert_matched_calls(self, events):
        seen = set()
        results = set()
        for event in events:
            if event.type == "tool_calls":
                for call in event.data:
                    self.assertNotIn(call.tool_call_id, seen)
                    seen.add(call.tool_call_id)
            elif event.type == "tool_result":
                self.assertIn(event.data.tool_call_id, seen)
                self.assertNotIn(event.data.tool_call_id, results)
                results.add(event.data.tool_call_id)
        self.assertEqual(seen, results)
        self.assertTrue(seen)

    def test_sync_async_stream_regenerates_in_same_turn_with_bounded_history(self):
        raw_calls = [
            '{"description":"执行脚本","command":"' + "x" * 10000,
            '{"command":"' + "x" * 10000 + '","description":"未完',
            '{"description":"未生成 command',
        ]
        complete = {"description": "重新生成", "command": "echo done"}
        for asynchronous in [False, True]:
            for raw in raw_calls:
                with self.subTest(asynchronous=asynchronous, raw=raw[:40]):
                    graph, server, executed = self.build_graph([("bash", raw, "length"), ("bash", json.dumps(complete), "tool_calls"), (None, "已完成", "stop")])
                    events = self.collect(graph, asynchronous=asynchronous)
                    self.assertEqual(executed, ["echo done"])
                    self.assertEqual(len(server.requests), 3)
                    self.assertEqual(sum(event.type == "end" for event in events), 1)
                    self.assertEqual(events[-1].type, "end")
                    self.assertTrue(any(event.type == "tool_chunk" for event in events))
                    self.assert_matched_calls(events)
                    results = [event.data for event in events if event.type == "tool_result"]
                    self.assertEqual(results[0].content, BASH_REGENERATION_PROMPT)
                    self.assertEqual(results[0].status, "error")
                    calls = [call for event in events if event.type == "tool_calls" for call in event.data]
                    self.assertNotIn("x" * 101, calls[0].function.arguments)
                    for request in server.requests[1:]:
                        serialized = json.dumps(request, ensure_ascii=False)
                        self.assertNotIn("x" * 101, serialized)
                        self.assertNotIn(_MODEL_LENGTH_CAPPED_CONTENT, serialized)
                        self.assertIn(BASH_REGENERATION_PROMPT, serialized)
                        self.assertEqual(sum(message["role"] == "user" for message in request["messages"]), 1)
                    state = graph.get_state({"configurable": {"thread_id": "thread"}}).values
                    self.assertNotIn("x" * 101, json.dumps([message.model_dump() for message in state["messages"]], ensure_ascii=False))

    def test_delta_checkpoint_preserves_recovery_across_repeated_caps(self):
        responses = [
            ("bash", '{"command":"' + "x" * 200, "length"),
            ("bash", '{"description":"再次截断', "length"),
            ("bash", '{"command":"echo done","description":"执行"}', "tool_calls"),
            (None, "已完成", "stop"),
        ]
        for asynchronous in [False, True]:
            graph, server, executed = self.build_graph(responses, channel_mode="delta")
            events = self.collect(graph, asynchronous=asynchronous)
            self.assertEqual(executed, ["echo done"])
            self.assertEqual(len(server.requests), 4)
            self.assert_matched_calls(events)
            self.assertEqual(sum(event.type == "tool_result" and event.data.content == BASH_REGENERATION_PROMPT for event in events), 2)
            self.assertNotIn("x" * 101, json.dumps(server.requests[1:], ensure_ascii=False))

    def test_complete_sibling_executes_and_receives_a_result(self):
        responses = [([("read_file", '{"path":"/tmp/a"}'), ("bash", '{"command":"echo unfinished')], None, "length"), (None, "已完成", "stop")]
        for asynchronous in [False, True]:
            graph, server, executed = self.build_graph(responses)
            events = self.collect(graph, asynchronous=asynchronous)
            self.assertEqual(executed, [])
            self.assertEqual(len(server.requests), 2)
            self.assert_matched_calls(events)
            self.assertTrue(any(event.type == "tool_result" and event.data.content == "file content" for event in events))

    def test_complete_bash_at_length_boundary_is_also_regenerated(self):
        graph, server, executed = self.build_graph([("bash", '{"command":"echo complete","description":"执行"}', "length"), (None, "已完成", "stop")])
        events = self.collect(graph)
        self.assertEqual(executed, [])
        self.assertEqual(len(server.requests), 2)
        self.assert_matched_calls(events)

    def test_subagent_values_stream_emits_repaired_call_before_result(self):
        graph, _, executed = self.build_graph([("bash", '{"command":"' + "x" * 200, "length"), (None, "已完成", "stop")])
        captured, seen, events = [], set(), []
        processed = delivered = 0
        parser = ClientStreamParser("thread", "run")
        for state in graph.stream({"messages": [HumanMessage(content="执行任务")]}, config={"configurable": {"thread_id": "child"}}, context={"is_subagent": True}, stream_mode="values"):
            processed = capture_new_step_messages(state["messages"], captured, seen, processed)
            for index in range(delivered, len(captured)):
                events.extend(parser.parse(("custom", {"type": "task_running", "task_id": "child", "message_index": index, "message": captured[index]})))
            delivered = len(captured)
        self.assertEqual(executed, [])
        self.assert_matched_calls(events)
        self.assertTrue(all(event.agent_id == "child" for event in events))
