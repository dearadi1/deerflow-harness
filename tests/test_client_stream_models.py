"""验证下游独立导入、事件识别和 JSON 往返，覆盖协议模型迁移的公共边界。"""

import json
import subprocess
import sys
import unittest
from importlib.resources import files

from pydantic import ValidationError

from deerflow.protocol import (
    AnswerContentEvent,
    ContentData,
    ContextCompactionStartEvent,
    ContextCompactionEndEvent,
    ContextUsedEvent,
    EndEvent,
    InputTokenDetails,
    MessageMetadata,
    ReasoningContentEvent,
    StreamEvent,
    SuggestionsEvent,
    TitleEvent,
    TokenUsage,
    TokenUsageEvent,
    ToolCallData,
    ToolCallsEvent,
    ToolChunkData,
    ToolChunkEvent,
    ToolFunction,
    ToolFunctionChunk,
    ToolResultEvent,
    parse_stream_event,
    stream_event_json_schema,
)


class StreamModelTests(unittest.TestCase):
    def envelope(self, kind, data):
        return {"type": kind, "agent_id": "main", "thread_id": "thread_001", "seq": 1, "data": data}

    @staticmethod
    def tool(arguments="{}", name="lookup"):
        return {"index": 0, "id": "model_message", "tool_call_id": "call_a", "type": "function", "function": {"name": name, "arguments": arguments}}

    def cases(self):
        usage = {"id": "model_message", "usage_metadata": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}}
        return [
            ("answer_content", {"id": "model_message", "content": "回答"}, AnswerContentEvent),
            ("reasoning_content", {"id": "model_message", "content": "思考"}, ReasoningContentEvent),
            ("title", {"id": "thread_001", "content": "标题"}, TitleEvent),
            ("suggestions", {"id": "suggestions_message", "questions": ["如何开始？", "有哪些风险？", "如何验证？"]}, SuggestionsEvent),
            ("context_compaction_start", {"id": "compact_1", "input_tokens": 100, "compacted_message_count": 2, "kept_message_count": 1}, ContextCompactionStartEvent),
            ("context_compaction_end", {"id": "compact_1", "status": "completed", "summary_text": "历史摘要"}, ContextCompactionEndEvent),
            ("context_used", {"id": "context_1", "contexts": [{"source": "memory", "content": "偏好中文", "content_sha256": "a" * 64}]}, ContextUsedEvent),
            ("tool_chunk", [self.tool(arguments="")], ToolChunkEvent),
            ("tool_calls", [self.tool()], ToolCallsEvent),
            ("tool_result", {"id": "result_message", "tool_call_id": "call_a", "content": "结果", "name": None, "status": "success"}, ToolResultEvent),
            ("token_usage", usage, TokenUsageEvent),
            ("end", usage, EndEvent),
        ]

    def test_every_event_recovers_its_concrete_model_and_preserves_wire_payload(self):
        for kind, data, event_class in self.cases():
            with self.subTest(kind=kind):
                payload = self.envelope(kind, data)
                event = parse_stream_event(payload)
                self.assertIsInstance(event, StreamEvent)
                self.assertIsInstance(event, event_class)
                self.assertEqual(event.model_dump(exclude_unset=True), payload)
                encoded = event.model_dump_json(exclude_unset=True)
                recovered = parse_stream_event(encoded)
                self.assertIsInstance(recovered, event_class)
                self.assertEqual(recovered, event)

    def test_nested_protocol_fields_are_models(self):
        answer = parse_stream_event(self.envelope("answer_content", {"id": "m", "content": "回答", "additional_kwargs": {"deerflow_error_fallback": True, "error_reason": "错误", "supplier": {"signature": "原值"}}}))
        self.assertIsInstance(answer.data, ContentData)
        self.assertIsInstance(answer.data.additional_kwargs, MessageMetadata)
        self.assertTrue(answer.data.additional_kwargs.deerflow_error_fallback)
        self.assertEqual(answer.data.additional_kwargs.model_dump()["supplier"], {"signature": "原值"})
        chunk = parse_stream_event(self.envelope("tool_chunk", [self.tool(arguments="")]))
        call = parse_stream_event(self.envelope("tool_calls", [self.tool()]))
        self.assertIsInstance(chunk.data[0], ToolChunkData)
        self.assertIsInstance(chunk.data[0].function, ToolFunctionChunk)
        self.assertIsInstance(call.data[0], ToolCallData)
        self.assertIsInstance(call.data[0].function, ToolFunction)
        self.assertEqual(call.data[0].id, "model_message")
        self.assertEqual(call.data[0].tool_call_id, "call_a")

    def test_usage_details_and_unknown_supplier_counts_survive_roundtrip(self):
        payload = self.envelope("token_usage", {"id": "m", "usage_metadata": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3, "input_token_details": {"cache_read": 1, "future_counter": 7}, "output_token_details": {"reasoning": 1}}})
        event = parse_stream_event(payload)
        self.assertIsInstance(event.data.usage_metadata, TokenUsage)
        self.assertIsInstance(event.data.usage_metadata.input_token_details, InputTokenDetails)
        self.assertEqual(event.data.usage_metadata.input_token_details.cache_read, 1)
        self.assertEqual(parse_stream_event(event.model_dump_json(exclude_unset=True)).model_dump(exclude_unset=True), payload)

    def test_opaque_tool_content_and_artifact_remain_lossless(self):
        data = {"id": "result", "tool_call_id": "call_a", "content": [{"type": "text", "text": "结果"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}], "artifact": {"assets": ["report.png"], "native": {"x": 1}}}
        event = parse_stream_event(self.envelope("tool_result", data))
        self.assertEqual(event.data.artifact, data["artifact"])
        self.assertEqual(json.loads(event.model_dump_json(exclude_unset=True))["data"], data)

    def test_wrong_type_missing_identity_and_wrong_payload_are_rejected(self):
        bad_call = self.tool()
        bad_call.pop("tool_call_id")
        payloads = [
            self.envelope("unknown", {"id": "m"}),
            self.envelope("answer_content", [{"id": "m", "content": "错误形态"}]),
            self.envelope("tool_calls", [bad_call]),
            self.envelope("tool_calls", [self.tool(arguments="未完成 JSON")]),
            self.envelope("tool_chunk", [self.tool(arguments="{}")]),
            self.envelope("context_compaction_end", {"id": "compact_1", "status": "completed"}),
            self.envelope("context_compaction_end", {"id": "compact_1", "status": "skipped"}),
            self.envelope("context_compaction_end", {"id": "compact_1", "status": "failed", "reason": "error", "summary_text": "未生效摘要"}),
            self.envelope("context_used", {"id": "context_1", "contexts": []}),
            {**self.envelope("title", {"id": "t", "content": "标题"}), "seq": "1"},
            {**self.envelope("title", {"id": "t", "content": "标题"}), "seq": 0},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    parse_stream_event(payload)

    def test_importing_protocol_does_not_load_runtime_or_langchain(self):
        code = """
import sys
import deerflow.protocol
unexpected = [
    name for name in sys.modules
    if (name.startswith('deerflow.') and name != 'deerflow.protocol' and not name.startswith('deerflow.protocol.'))
    or name.startswith(('langchain', 'langgraph', 'deerflow_extension_api'))
]
assert not unexpected, unexpected
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_model_mutation_cannot_change_parser_usage_state(self):
        from langchain_core.messages import AIMessage
        from deerflow.client_stream import ClientStreamParser

        parser = ClientStreamParser("thread_001", "run_001")
        message = AIMessage(id="m", content="", usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})
        usage = next(event for event in parser.parse(("messages", (message, {}))) if event.type == "token_usage")
        usage.data.usage_metadata.total_tokens = 999
        final = list(parser.finish())[-1]
        self.assertEqual(final.data.usage_metadata.total_tokens, 3)

    def test_cli_json_serializes_models_as_protocol_objects(self):
        import io
        from unittest.mock import Mock, patch
        from deerflow.tui.cli import LaunchPlan, _run_json

        events = [parse_stream_event(self.envelope(kind, data)) for kind, data, _ in self.cases() if kind in {"answer_content", "end"}]
        session = Mock()
        session.resolve_thread.return_value = "thread_001"
        session.client.stream.return_value = iter(events)
        output = io.StringIO()
        with patch("deerflow.tui.cli._make_session", return_value=session), patch("sys.stdout", output):
            status = _run_json(LaunchPlan(mode="json", message="测试"))
        self.assertEqual(status, 0)
        recovered = [parse_stream_event(line) for line in output.getvalue().splitlines()]
        self.assertEqual(recovered, events)

    def test_exported_schema_matches_models_and_has_all_event_discriminators(self):
        from jsonschema import validate

        schema = stream_event_json_schema()
        resource = files("deerflow.protocol").joinpath("stream_event.schema.json")
        self.assertEqual(json.loads(resource.read_text(encoding="utf-8")), schema)
        self.assertEqual(set(schema["discriminator"]["mapping"]), {kind for kind, _, _ in self.cases()})
        for kind, data, _ in self.cases():
            validate(self.envelope(kind, data), schema)


if __name__ == "__main__":
    unittest.main()
