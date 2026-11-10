"""验证真实流解析、文件落盘和同轮续写，避免只覆盖补括号函数。"""

import asyncio
import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.agents.middlewares.model_length_finish_reason_middleware import ModelLengthFinishReasonMiddleware, _MODEL_LENGTH_CAPPED_CONTENT
from deerflow.agents.middlewares.model_length_recovery import CONTINUATION_PROMPT, FALLBACK_CONTINUATION_PROMPT, RECOVERY_MARKER, RawToolCallCapture, continue_write_result, pending_write_recovery, recover_tool_call
from deerflow.agents.middlewares.read_before_write_middleware import ReadBeforeWriteMiddleware
from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware
from deerflow.agents.middlewares.tool_receipt_middleware import ToolReceiptMiddleware
from deerflow.agents.middlewares.write_file_length_recovery import recover_write_file_arguments
from deerflow.client_stream import ClientStreamParser
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.subagents.step_events import capture_new_step_messages, capture_step_message


def truncated_message(arguments, *, name="write_file", call_id="write_1", reason="length"):
    return AIMessage(
        id="message_1",
        content="",
        additional_kwargs={"tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]},
        response_metadata={"finish_reason": reason},
    )


class ArgumentRecoveryTests(unittest.TestCase):
    def test_missing_path_uses_isolated_workspace_file_and_reports_actual_location(self):
        original = truncated_message('{"content":"第一段\\n未完')
        recovered = recover_tool_call(original, [original])
        arguments = recovered.tool_calls[0]["args"]
        path = arguments["path"]
        self.assertTrue(path.startswith(f"{VIRTUAL_PATH_PREFIX}/workspace/recovered_write_"))
        self.assertEqual(arguments["content"], "第一段\n未完")
        self.assertFalse(arguments["append"])
        self.assertEqual(recovered.additional_kwargs[RECOVERY_MARKER]["fallback_path"], path)
        self.assertEqual(recover_tool_call(original, [original]).tool_calls[0]["args"]["path"], path)
        other = truncated_message('{"content":"另一个片段', call_id="../other")
        other_path = recover_tool_call(other, [other]).tool_calls[0]["args"]["path"]
        self.assertNotEqual(other_path, path)
        self.assertNotIn("..", other_path)
        request = ToolCallRequest(tool_call=recovered.tool_calls[0], tool=None, state={"messages": [recovered]}, runtime=None)
        result = continue_write_result(request, ToolMessage(content="OK", tool_call_id="write_1"))
        self.assertEqual(result.content, f"OK\n\n{FALLBACK_CONTINUATION_PROMPT.format(path=path)}")
        self.assertEqual(result.additional_kwargs[RECOVERY_MARKER]["fallback_path"], path)
        self.assertIsNotNone(pending_write_recovery([recovered, result]))

    def test_fallback_is_limited_to_missing_paths_and_generated_content(self):
        fallback = f"{VIRTUAL_PATH_PREFIX}/workspace/recovered.txt"
        for raw in ['{"content":"part', '{"path":"","append":true,"content":"part', '{"path":null,"content":"part']:
            recovered = recover_write_file_arguments(raw, fallback_path=fallback)
            self.assertEqual(recovered["path"], fallback)
            self.assertFalse(recovered["append"])
        original = recover_write_file_arguments('{"path":"/tmp/original","append":true,"content":"part', fallback_path=fallback)
        self.assertEqual(original["path"], "/tmp/original")
        self.assertTrue(original["append"])
        for raw in ['{"content":"', '{"content":"part","path":"/tmp/incomplete', '{"content":"bad\\q', '{"path":42,"content":"part', '{"content":"part","append":"true"}']:
            self.assertIsNone(recover_write_file_arguments(raw, fallback_path=fallback))

    def test_content_boundaries_preserve_only_complete_characters(self):
        cases = [
            ('{"path":"/tmp/a","content":"第一行', "第一行"),
            ('{"path":"/tmp/a","content":"第一行\\n第二行', "第一行\n第二行"),
            ('{"path":"/tmp/a","content":"abc\\', "abc"),
            ('{"path":"/tmp/a","content":"abc\\u4e', "abc"),
            ('{"path":"/tmp/a","content":"abc\\ud83d\\ude', "abc"),
            ('{"path":"/tmp/a","content":"abc\\\\', "abc\\"),
            ('{"path":"/tmp/a","content":"abc"', "abc"),
            ('{"path":"/tmp/a","append":true,"content":"abc', "abc"),
            ('{"path":"/tmp/a","content":"abc", ', "abc"),
            ('{"path":"/tmp/a","content":"abc","description":"未完成', "abc"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                recovered = recover_write_file_arguments(raw)
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered["content"], expected)

    def test_ambiguous_or_malformed_arguments_are_not_written(self):
        cases = [
            '{"content":"abc","path":"/tmp/incomplete',
            '{"path":"/tmp/a","content":"bad\\q',
            '{"path":"/tmp/a","content":"bad\nraw',
            '{"path":"/tmp/a","path":"/tmp/b","content":"abc',
            '{"path":"/tmp/a","content":"abc","append":t',
            '{"path":"/tmp/a","content":"abc"} trailing',
            '{"path":"/tmp/a","content":"abc","unknown":1}',
            '{"path":"/tmp/a","content":"',
            '{"path":"/tmp/a","content":"abc","append":"true"}',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(recover_write_file_arguments(raw))

    def test_other_tools_safety_and_duplicate_calls_keep_length_fallback(self):
        middleware = ModelLengthFinishReasonMiddleware()
        for name in ["read_file", "str_replace"]:
            message = truncated_message('{"command":"partial', name=name)
            updated = middleware.after_model({"messages": [message]}, Runtime(context={}))
            self.assertEqual(updated["messages"][0].tool_calls, [])
            self.assertIn("model_length_termination", updated["messages"][0].additional_kwargs)
        message = truncated_message('{"path":"/tmp/a","content":"partial')
        message.additional_kwargs["tool_calls"].append(message.additional_kwargs["tool_calls"][0])
        self.assertIsNone(recover_tool_call(message, [message]))
        message = truncated_message('{"path":"/tmp/a","content":"partial', reason="content_filter")
        self.assertIsNone(middleware.after_model({"messages": [message]}, Runtime(context={})))
        self.assertIsNone(recover_tool_call(message, [message]))

    def test_complete_sibling_calls_survive_but_other_truncated_tools_are_not_repaired(self):
        message = truncated_message('{"path":"/tmp/a","content":"partial')
        sibling = {"id": "todo_1", "type": "function", "function": {"name": "write_todos", "arguments": '{"todos":[]}'}}
        message.additional_kwargs["tool_calls"].insert(0, sibling)
        recovered = recover_tool_call(message, [message])
        self.assertEqual([call["id"] for call in recovered.tool_calls], ["todo_1", "write_1"])
        self.assertEqual(recovered.tool_calls[0]["args"], {"todos": []})
        self.assertEqual(recovered.tool_calls[1]["args"]["content"], "partial")
        sibling["function"]["arguments"] = '{"todos":['
        self.assertIsNone(recover_tool_call(message, [message]))

    def test_success_boundary_and_new_turn_do_not_invent_completed_writes(self):
        original = truncated_message('{"path":"/tmp/a","content":"partial')
        recovered = recover_tool_call(original, [original])
        user = HumanMessage(content="写文件")
        failed = ToolMessage(content="Error: denied", status="error", tool_call_id="write_1")
        succeeded = ToolMessage(content="OK", tool_call_id="write_1")
        self.assertIsNone(pending_write_recovery([user, recovered, failed]))
        self.assertIsNotNone(pending_write_recovery([user, recovered, succeeded]))
        self.assertIsNone(pending_write_recovery([user, recovered, succeeded, HumanMessage(content="新任务")]))

    def test_continuation_result_requires_matching_success_and_preserves_command(self):
        original = truncated_message('{"path":"/tmp/a","content":"partial')
        recovered = recover_tool_call(original, [original])
        request = ToolCallRequest(tool_call=recovered.tool_calls[0], tool=None, state={"messages": [recovered]}, runtime=None)
        succeeded = ToolMessage(content="OK", tool_call_id="write_1", id="result_1", name="write_file", artifact={"path": "/tmp/a"})
        result = continue_write_result(request, succeeded)
        self.assertEqual(result.content, f"OK\n\n{CONTINUATION_PROMPT}")
        self.assertEqual((result.id, result.name, result.artifact), (succeeded.id, succeeded.name, succeeded.artifact))
        self.assertEqual(succeeded.content, "OK")
        self.assertEqual(result.additional_kwargs["deerflow_tool_transforms"][0]["kind"], "write_file_continuation")
        self.assertEqual(result.additional_kwargs[RECOVERY_MARKER]["tool_call_id"], "write_1")
        self.assertIsNotNone(pending_write_recovery([recovered, result]))
        self.assertIs(continue_write_result(request, result), result)
        failures = [
            succeeded.model_copy(update={"status": "error"}),
            succeeded.model_copy(update={"additional_kwargs": {"deerflow_tool_meta": {"status": "error"}}}),
            succeeded.model_copy(update={"content": "Error: failed"}),
            succeeded.model_copy(update={"tool_call_id": "another_call"}),
        ]
        for failure in failures:
            self.assertIs(continue_write_result(request, failure), failure)
        for messages in [succeeded, [failures[-1], succeeded], (failures[-1], succeeded)]:
            command = Command(update={"messages": messages, "extra": 42}, goto="next")
            patched = continue_write_result(request, command)
            self.assertEqual(patched.goto, "next")
            self.assertEqual(patched.update["extra"], 42)
            self.assertIsInstance(patched.update["messages"], type(messages))
            target = patched.update["messages"] if isinstance(messages, ToolMessage) else patched.update["messages"][-1]
            self.assertEqual(target.content, result.content)

    def test_child_step_capture_waits_for_repaired_call(self):
        original = truncated_message('{"path":"/tmp/a","content":"partial')
        recovered = recover_tool_call(original, [original])
        captured, seen = [], set()
        self.assertFalse(capture_step_message(original, captured, seen))
        self.assertTrue(capture_step_message(recovered, captured, seen))
        self.assertFalse(capture_step_message(recovered, captured, seen))
        parser = ClientStreamParser("thread", "run")
        events = list(parser.parse(("custom", {"type": "task_running", "task_id": "child", "message": captured[0], "message_index": 0})))
        calls = [event for event in events if event.type == "tool_calls"]
        self.assertEqual(calls[0].agent_id, "child")

    def test_recovery_clears_native_payloads_and_preserves_original_message(self):
        original = truncated_message('{"path":"/tmp/a","content":"partial')
        original.content = [{"type": "text", "text": "正在写入"}, {"type": "function_call", "call_id": "write_1", "id": "fc_1", "name": "write_file", "arguments": "broken"}]
        original.response_metadata["id"] = "resp_original"
        before = original.model_dump()
        recovered = recover_tool_call(original, [original])
        self.assertEqual(original.model_dump(), before)
        self.assertEqual(recovered.content, [{"type": "text", "text": "正在写入"}])
        self.assertNotIn("tool_calls", recovered.additional_kwargs)
        self.assertNotIn("id", recovered.response_metadata)
        self.assertEqual(recovered.response_metadata["finish_reason"], "length")

    def test_unassigned_chunks_do_not_fall_back_to_guessed_arguments(self):
        capture = RawToolCallCapture()
        fragment = AIMessageChunk(content="", tool_call_chunks=[{"id": "write_1", "index": None, "name": "write_file", "args": '{"content":"text","path":"partial'}])
        capture.on_llm_new_token("", chunk=ChatGenerationChunk(message=fragment))
        message = AIMessage(content="", tool_calls=fragment.tool_calls, response_metadata={"finish_reason": "length"})
        captured = capture.attach(message, lambda message: True)
        self.assertIsNone(recover_tool_call(captured, [captured]))


class ModelServer:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def handle(self, request):
        self.requests.append(json.loads(request.content))
        response = self.responses[len(self.requests) - 1]
        if callable(response):
            response = response(self.requests[-1])
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=self.encode(response, len(self.requests)))

    @staticmethod
    def call_deltas(name, arguments, call_id, position):
        midpoint = max(1, len(arguments) // 2)
        return [
            {"tool_calls": [{"index": position, "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments[:midpoint]}}]},
            {"tool_calls": [{"index": position, "function": {"arguments": arguments[midpoint:]}}]},
        ]

    @staticmethod
    def encode(response, index):
        name, arguments, reason = response
        if name is None:
            deltas = [{"content": arguments}]
        elif isinstance(name, list):
            deltas = [delta for position, (tool_name, raw) in enumerate(name) for delta in ModelServer.call_deltas(tool_name, raw, f"call_{index}_{position}", position)]
        else:
            deltas = ModelServer.call_deltas(name, arguments, f"call_{index}", 0)
        chunks = [{"id": f"msg_{index}", "object": "chat.completion.chunk", "model": "test-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
        chunks.append({"id": f"msg_{index}", "object": "chat.completion.chunk", "model": "test-model", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
        return "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks).encode() + b"data: [DONE]\n\n"


def mapped_file_path(directory, path):
    target = Path(path)
    if target.is_absolute():
        target = target.relative_to(f"{VIRTUAL_PATH_PREFIX}/workspace")
    return directory / target


def last_write_path(request):
    for message in reversed(request["messages"]):
        for call in message.get("tool_calls", []):
            if call["function"]["name"] == "write_file":
                return json.loads(call["function"]["arguments"])["path"]
    raise AssertionError("缺少已恢复的写入调用")


def file_tools(directory, writes):
    @tool("write_file")
    def write_file(path: str, content: str, append: bool = False, description: str = "") -> str:
        """写入测试文件以验证实际执行顺序。"""
        target = mapped_file_path(directory, path)
        with target.open("a" if append else "w") as output:
            output.write(content)
        writes.append((path, content, append))
        return "OK"

    @tool("read_file")
    def read_file(path: str) -> str:
        """读取已落盘内容，供续写确认边界。"""
        return mapped_file_path(directory, path).read_text()

    @tool("write_todos")
    def write_todos(todos: list[str]) -> str:
        """验证完整的同批工具仍能正常执行。"""
        return "todos saved"

    @tool("move_file")
    def move_file(source: str, destination: str) -> str:
        """在测试目录内移动已保存的正文，验证随后使用新路径续写。"""
        mapped_file_path(directory, source).rename(mapped_file_path(directory, destination))
        return "moved"

    return [write_file, read_file, write_todos, move_file]


class DenyWrites(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        return ToolMessage(content="Error: denied", tool_call_id=request.tool_call["id"], status="error")

    async def awrap_tool_call(self, request, handler):
        return self.wrap_tool_call(request, handler)


class RecoveryGraphTests(unittest.TestCase):
    def build_graph(self, directory, responses, *, deny=False):
        server = ModelServer(responses)
        transport = httpx.MockTransport(server.handle)
        sync_client = httpx.Client(transport=transport)
        async_client = httpx.AsyncClient(transport=transport)
        model = ChatOpenAI(model="test-model", api_key="test", streaming=True, max_retries=0, http_client=sync_client, http_async_client=async_client)
        writes = []
        read_gate = ReadBeforeWriteMiddleware(content_reader=lambda runtime, path: mapped_file_path(directory, path).read_text())
        middlewares = [ToolReceiptMiddleware(render_mode="delegation_only"), *([DenyWrites()] if deny else []), read_gate, ModelLengthFinishReasonMiddleware(), SafetyFinishReasonMiddleware()]
        for name in ("sandbox_authorization_scope", "sandbox_authorization_scope_async"):
            mocked = patch(f"deerflow.agents.middlewares.read_before_write_middleware.{name}", side_effect=lambda runtime: nullcontext())
            mocked.start()
            self.addCleanup(mocked.stop)
        graph = create_agent(model, file_tools(directory, writes), middleware=middlewares, checkpointer=InMemorySaver())
        self.addCleanup(sync_client.close)
        self.addCleanup(lambda: asyncio.run(async_client.aclose()))
        return graph, server, writes

    def collect(self, graph, *, asynchronous=False):
        parser = ClientStreamParser("thread", "run")
        kwargs = {"config": {"configurable": {"thread_id": "thread"}, "recursion_limit": 100}, "context": {"run_id": "run"}, "stream_mode": ["messages", "values", "custom"]}
        initial = {"messages": [HumanMessage(content="完整写入文件", additional_kwargs={"run_id": "run"})]}
        events = []
        if asynchronous:
            async def run():
                async for item in graph.astream(initial, **kwargs):
                    events.extend(parser.parse(item))
            asyncio.run(run())
        else:
            for item in graph.stream(initial, **kwargs):
                events.extend(parser.parse(item))
        events.extend(parser.finish())
        return events

    def test_truncated_stream_writes_then_continues_in_same_sync_and_async_run(self):
        responses = [
            ("write_file", '{"path":"output.txt","content":"第一段', "length"),
            ("read_file", '{"path":"output.txt"}', "tool_calls"),
            ("write_file", '{"path":"output.txt","content":"第二段', "length"),
            ("read_file", '{"path":"output.txt"}', "tool_calls"),
            ("write_file", '{"path":"output.txt","append":true,"content":"第三段"}', "tool_calls"),
            (None, "已完成", "stop"),
        ]
        for asynchronous in [False, True]:
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory() as directory:
                graph, server, writes = self.build_graph(Path(directory), responses)
                events = self.collect(graph, asynchronous=asynchronous)
                self.assertEqual((Path(directory) / "output.txt").read_text(), "第一段第二段第三段")
                self.assertEqual([write[2] for write in writes], [False, True, True])
                self.assertEqual([event.type for event in events].count("end"), 1)
                self.assertEqual(events[-1].type, "end")
                self.assertEqual(len(server.requests), 6)
                self.assert_tool_result_continuation(server, events, expected=2)
                self.assert_call_result_order(events)
                state = graph.get_state({"configurable": {"thread_id": "thread"}}).values
                self.assertFalse(any(message.name == "write_file_continuation" for message in state["messages"]))
                self.assertEqual(sum(isinstance(message, HumanMessage) for message in state["messages"]), 1)
                stored_results = [message for message in state["messages"] if isinstance(message, ToolMessage) and message.name == "write_file"]
                self.assertEqual([message.content for message in stored_results], [f"OK\n\n{CONTINUATION_PROMPT}", f"OK\n\n{CONTINUATION_PROMPT}", "OK"])
                self.assertIsNone(pending_write_recovery(state["messages"]))

    def assert_tool_result_continuation(self, server, events, *, expected):
        for request in server.requests:
            users = [message for message in request["messages"] if message["role"] == "user"]
            self.assertEqual([message["content"] for message in users], ["完整写入文件"])
            self.assertNotIn(_MODEL_LENGTH_CAPPED_CONTENT, json.dumps(request, ensure_ascii=False))
        results = [event.data for event in events if event.type == "tool_result" and CONTINUATION_PROMPT in str(event.data.content)]
        self.assertEqual(len(results), expected)
        for result in results:
            self.assertEqual(result.content, f"OK\n\n{CONTINUATION_PROMPT}")
            receipt = result.additional_kwargs.model_dump()["deerflow_tool_receipt"]
            self.assertEqual(receipt["output_sha256"], hashlib.sha256(result.content.encode()).hexdigest()[:16])
            self.assertTrue(any(message.get("role") == "tool" and message.get("tool_call_id") == result.tool_call_id and message["content"] == result.content for request in server.requests for message in request["messages"]))
            raw_arguments = "".join(call.function.arguments for event in events if event.type == "tool_chunk" for call in event.data if call.tool_call_id == result.tool_call_id)
            with self.assertRaises(ValueError):
                json.loads(raw_arguments)
        self.assertFalse(any(event.type == "answer_content" and _MODEL_LENGTH_CAPPED_CONTENT in event.data.content for event in events))

    def test_parallel_complete_todos_and_truncated_write_continue_without_user_messages(self):
        responses = [
            ([("write_todos", '{"todos":["write report"]}'), ("write_file", '{"path":"output.txt","content":"第一段')], None, "length"),
            ("read_file", '{"path":"output.txt"}', "tool_calls"),
            ("write_file", '{"path":"output.txt","append":true,"content":"第二段"}', "tool_calls"),
            (None, "已完成", "stop"),
        ]
        for asynchronous in [False, True]:
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory() as directory:
                graph, server, writes = self.build_graph(Path(directory), responses)
                events = self.collect(graph, asynchronous=asynchronous)
                self.assertEqual((Path(directory) / "output.txt").read_text(), "第一段第二段")
                self.assertEqual(len(writes), 2)
                self.assertEqual(sum(event.type == "tool_result" and event.data.name == "write_todos" for event in events), 1)
                self.assertEqual(sum(event.type == "end" for event in events), 1)
                self.assert_tool_result_continuation(server, events, expected=1)
                self.assert_call_result_order(events)

    def test_content_first_stream_is_saved_then_moved_and_continued(self):
        responses = [
            ("write_file", '{"content":"第一段\\n未完', "length"),
            lambda request: ("read_file", json.dumps({"path": last_write_path(request)}), "tool_calls"),
            lambda request: ("move_file", json.dumps({"source": last_write_path(request), "destination": "final.txt"}), "tool_calls"),
            ("read_file", '{"path":"final.txt"}', "tool_calls"),
            ("write_file", '{"path":"final.txt","append":true,"content":"的句子"}', "tool_calls"),
            (None, "已完成", "stop"),
        ]
        for asynchronous in [False, True]:
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory() as directory:
                graph, server, writes = self.build_graph(Path(directory), responses)
                events = self.collect(graph, asynchronous=asynchronous)
                self.assertEqual((Path(directory) / "final.txt").read_text(), "第一段\n未完的句子")
                self.assertEqual(len(writes), 2)
                path = writes[0][0]
                self.assertTrue(path.startswith(f"{VIRTUAL_PATH_PREFIX}/workspace/recovered_write_"))
                self.assertEqual(writes[0][1:], ("第一段\n未完", False))
                self.assertFalse(mapped_file_path(Path(directory), path).exists())
                self.assertEqual(len(server.requests), 6)
                self.assertEqual(sum(event.type == "end" for event in events), 1)
                self.assertEqual(events[-1].type, "end")
                self.assert_call_result_order(events)
                result = next(event.data for event in events if event.type == "tool_result")
                self.assertEqual(result.content, f"OK\n\n{FALLBACK_CONTINUATION_PROMPT.format(path=path)}")
                metadata = result.additional_kwargs.model_dump()
                self.assertEqual(metadata[RECOVERY_MARKER]["fallback_path"], path)
                self.assertEqual(metadata["deerflow_tool_receipt"]["output_sha256"], hashlib.sha256(result.content.encode()).hexdigest()[:16])
                state = graph.get_state({"configurable": {"thread_id": "thread"}}).values
                stored_result = next(message for message in state["messages"] if isinstance(message, ToolMessage))
                self.assertEqual(stored_result.content, result.content)
                for request in server.requests:
                    self.assertEqual([message["content"] for message in request["messages"] if message["role"] == "user"], ["完整写入文件"])
                    self.assertNotIn(_MODEL_LENGTH_CAPPED_CONTENT, json.dumps(request, ensure_ascii=False))
                self.assertTrue(any(message["role"] == "tool" and message["content"] == result.content for message in server.requests[1]["messages"]))

    def test_denied_workspace_fallback_never_claims_the_fragment_was_saved(self):
        responses = [("write_file", '{"content":"第一段', "length"), (None, "写入被拒绝", "stop")]
        for asynchronous in [False, True]:
            with self.subTest(asynchronous=asynchronous), tempfile.TemporaryDirectory() as directory:
                graph, server, writes = self.build_graph(Path(directory), responses, deny=True)
                events = self.collect(graph, asynchronous=asynchronous)
                self.assertEqual(writes, [])
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertNotIn("已成功暂存", json.dumps(server.requests, ensure_ascii=False))
                self.assertTrue(any(event.type == "tool_result" and event.data.status == "error" for event in events))
                self.assert_call_result_order(events)

    def test_missing_path_without_length_signal_does_not_create_a_recovery_file(self):
        responses = [("write_file", '{"content":"正文"}', "tool_calls"), (None, "路径缺失", "stop")]
        with tempfile.TemporaryDirectory() as directory:
            graph, _, writes = self.build_graph(Path(directory), responses)
            events = self.collect(graph)
            self.assertEqual(writes, [])
            self.assertTrue(any(event.type == "tool_result" and event.data.status == "error" for event in events))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def assert_call_result_order(self, events):
        for position, event in enumerate(events):
            if event.type != "tool_result":
                continue
            matching = [entry for entry in events[:position] if entry.type == "tool_calls" and any(call.tool_call_id == event.data.tool_call_id for call in entry.data)]
            self.assertEqual(len(matching), 1)
            for call in matching[0].data:
                self.assertIsInstance(json.loads(call.function.arguments), dict)

    def test_authorization_failure_does_not_write_or_claim_successful_continuation(self):
        responses = [("write_file", '{"path":"output.txt","content":"第一段', "length"), (None, "写入被拒绝", "stop")]
        with tempfile.TemporaryDirectory() as directory:
            graph, server, writes = self.build_graph(Path(directory), responses, deny=True)
            events = self.collect(graph)
            self.assertEqual(writes, [])
            self.assertFalse((Path(directory) / "output.txt").exists())
            self.assertNotIn(CONTINUATION_PROMPT, json.dumps(server.requests[1], ensure_ascii=False))
            self.assert_call_result_order(events)

    def test_stream_truncated_path_is_not_guessed_by_lenient_json_parser(self):
        responses = [("write_file", '{"content":"text","path":"wrong', "length")]
        with tempfile.TemporaryDirectory() as directory:
            graph, server, writes = self.build_graph(Path(directory), responses)
            events = self.collect(graph)
            self.assertEqual(writes, [])
            self.assertEqual(len(server.requests), 1)
            self.assertFalse(any(event.type == "tool_result" for event in events))

    def test_recovered_append_still_requires_a_current_file_read(self):
        responses = [
            ("write_file", '{"path":"output.txt","content":"第一段', "length"),
            ("write_file", '{"path":"output.txt","content":"第二段', "length"),
            (None, "必须先读取文件", "stop"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            graph, _, writes = self.build_graph(Path(directory), responses)
            events = self.collect(graph)
            self.assertEqual(len(writes), 1)
            self.assertEqual((Path(directory) / "output.txt").read_text(), "第一段")
            self.assertTrue(any(event.type == "tool_result" and event.data.status == "error" for event in events))
            self.assertFalse(any(event.type == "tool_result" and event.data.status == "error" and CONTINUATION_PROMPT in str(event.data.content) for event in events))
            self.assert_call_result_order(events)

    def test_provider_safety_stop_never_recovers_the_file(self):
        responses = [("write_file", '{"path":"output.txt","content":"第一段', "content_filter")]
        with tempfile.TemporaryDirectory() as directory:
            graph, server, writes = self.build_graph(Path(directory), responses)
            events = self.collect(graph)
            self.assertEqual(writes, [])
            self.assertEqual(len(server.requests), 1)
            self.assertFalse(any(event.type == "tool_result" for event in events))

    def test_subagent_values_stream_keeps_repaired_call_before_result(self):
        responses = [
            ("write_file", '{"path":"output.txt","content":"第一段', "length"),
            ("read_file", '{"path":"output.txt"}', "tool_calls"),
            ("write_file", '{"path":"output.txt","append":true,"content":"第二段"}', "tool_calls"),
            (None, "已完成", "stop"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            graph, _, writes = self.build_graph(Path(directory), responses)
            captured, seen, events = [], set(), []
            processed = delivered = 0
            parser = ClientStreamParser("thread", "run")
            for state in graph.stream({"messages": [HumanMessage(content="写文件")]}, config={"configurable": {"thread_id": "child"}}, context={"is_subagent": True}, stream_mode="values"):
                processed = capture_new_step_messages(state["messages"], captured, seen, processed)
                for index in range(delivered, len(captured)):
                    events.extend(parser.parse(("custom", {"type": "task_running", "task_id": "child", "message_index": index, "message": captured[index]})))
                delivered = len(captured)
            self.assertEqual(len(writes), 2)
            self.assertEqual((Path(directory) / "output.txt").read_text(), "第一段第二段")
            self.assert_call_result_order(events)
            self.assertTrue(all(event.agent_id == "child" for event in events))
