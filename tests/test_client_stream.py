"""验证协议的身份绑定、增量归并和结束边界，避免只检查返回字段。"""

import json
import unittest
from unittest.mock import Mock

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from deerflow_protocol import StreamEvent, parse_stream_event

from deerflow.client import _AIMessageAccumulator
from deerflow.client_stream import ClientStreamParser
from deerflow.tui.cli import _RunOutcome
from deerflow.tui.runtime import stream_actions, translate
from deerflow.tui.view_state import AssistantDelta, RunEnded, ToolStarted


class EventSnapshots(list):
    """回归断言比较序列化载荷，同时确保解析器每次真正返回协议模型。"""

    def extend(self, events):
        for event in events:
            if not isinstance(event, StreamEvent):
                raise AssertionError("流中出现未建模的事件")
            self.append(event.model_dump(exclude_unset=True))


class ClientStreamTests(unittest.TestCase):
    def setUp(self):
        self.parser = ClientStreamParser("thread_001", "run_001")
        self.events = EventSnapshots()

    def message(self, message, metadata=None):
        self.events.extend(self.parser.parse(("messages", (message, metadata or {}))))

    def values(self, *messages, **fields):
        self.events.extend(self.parser.parse(("values", {"messages": messages, **fields})))

    def custom(self, kind, task_id="task_a", **fields):
        self.events.extend(self.parser.parse(("custom", {"type": kind, "task_id": task_id, **fields})))

    def events_of(self, kind):
        return [event for event in self.events if event["type"] == kind]

    def test_fast_answer_and_thinking_blocks_reconcile_snapshots(self):
        self.message(AIMessageChunk(id="answer", content="甲", additional_kwargs={"reasoning_content": "先想"}))
        self.message(AIMessageChunk(id="answer", content="乙"))
        self.values(AIMessage(id="answer", content="甲乙", additional_kwargs={"reasoning_content": "先想"}), title="标题")
        self.values(AIMessage(id="answer", content="甲乙，补充"), title="标题")
        self.values(AIMessage(id="answer", content="替换正文"))
        self.assertEqual([event["data"]["content"] for event in self.events_of("answer_content")], ["甲", "乙", "，补充"])
        self.assertEqual([event["data"]["content"] for event in self.events_of("reasoning_content")], ["先想"])
        self.assertEqual(len(self.events_of("title")), 1)

    def test_standard_reasoning_blocks_do_not_duplicate_private_field(self):
        self.message(AIMessageChunk(id="a", content=[{"type": "reasoning", "reasoning": "思考"}, {"type": "text", "text": "结论"}], additional_kwargs={"reasoning_content": "思考"}))
        self.assertEqual(self.events_of("reasoning_content")[0]["data"]["content"], "思考")
        self.assertEqual(self.events_of("answer_content")[0]["data"]["content"], "结论")

    def test_parallel_fragments_and_sequential_index_reuse_keep_call_ids(self):
        self.message(AIMessageChunk(id="round_1", content="", tool_call_chunks=[{"index": 0, "id": "call_a", "name": "lookup", "args": '{"q":'}, {"index": 1, "id": "call_b", "name": "lookup", "args": '{"q":'}]))
        self.message(AIMessageChunk(id="round_1", content="", tool_call_chunks=[{"index": 1, "id": None, "name": None, "args": '"乙"}'}, {"index": 0, "id": None, "name": None, "args": '"甲"}'}], chunk_position="last"))
        complete = AIMessage(id="round_1", content="", tool_calls=[{"id": "call_a", "name": "lookup", "args": {"q": "甲"}}, {"id": "call_b", "name": "lookup", "args": {"q": "乙"}}])
        self.values(complete)
        self.message(ToolMessage(id="result_b", content="乙结果", tool_call_id="call_b"))
        self.message(ToolMessage(id="result_a", content="甲结果", tool_call_id="call_a"))
        self.values(complete, ToolMessage(id="rebuilt_result", content="甲结果", tool_call_id="call_a"))
        self.message(AIMessageChunk(id="round_2", content="", tool_call_chunks=[{"index": 0, "id": "call_c", "name": "write_todos", "args": '{"todos":[]}'}], chunk_position="last"))
        summaries = self.events_of("tool_calls")
        self.assertEqual([[call["tool_call_id"] for call in event["data"]] for event in summaries], [["call_a", "call_b"], ["call_c"]])
        self.assertEqual([event["data"]["tool_call_id"] for event in self.events_of("tool_result")], ["call_b", "call_a"])
        fragments = self.events_of("tool_chunk")
        for event in fragments:
            for call in event["data"]:
                self.assertFalse(call["function"]["name"] and call["function"]["arguments"])
        arguments = {}
        for event in fragments:
            for call in event["data"]:
                arguments[call["tool_call_id"]] = arguments.get(call["tool_call_id"], "") + call["function"]["arguments"]
        self.assertEqual(json.loads(arguments["call_a"]), {"q": "甲"})
        self.assertEqual(json.loads(arguments["call_b"]), {"q": "乙"})
        self.assertEqual(json.loads(arguments["call_c"]), {"todos": []})

    def test_parallel_tool_chunks_bind_one_model_usage_and_separate_tool_results(self):
        self.message(AIMessageChunk(id="model_message", content="", tool_call_chunks=[
            {"index": 0, "id": "call_a", "name": "lookup", "args": '{"q":"甲"}'},
            {"index": 1, "id": "call_b", "name": "lookup", "args": '{"q":"乙"}'},
        ]))
        self.message(AIMessageChunk(id="model_message", content="", usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}))
        self.values(AIMessage(id="model_message", content="", tool_calls=[
            {"id": "call_b", "name": "lookup", "args": {"q": "乙"}},
            {"id": "call_a", "name": "lookup", "args": {"q": "甲"}},
        ], usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}))
        self.message(ToolMessage(id="result_b", content="乙", tool_call_id="call_b"))
        self.message(ToolMessage(id="result_a", content="甲", tool_call_id="call_a"))
        usage = self.events_of("token_usage")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["data"]["id"], "model_message")
        for event in self.events_of("tool_chunk") + self.events_of("tool_calls"):
            for call in event["data"]:
                self.assertEqual(call["id"], usage[0]["data"]["id"])
                self.assertIn(call["tool_call_id"], {"call_a", "call_b"})
        self.assertEqual(len(self.events_of("tool_calls")), 1)
        self.assertEqual([event["data"]["id"] for event in self.events_of("tool_result")], ["result_b", "result_a"])
        self.events.extend(self.parser.finish())
        self.assertEqual(self.events[-1]["data"]["usage_metadata"]["total_tokens"], 14)

    def test_sequential_tools_from_different_ai_messages_keep_separate_usage(self):
        for position, amount in enumerate((3, 7), 1):
            message_id, call_id = f"model_{position}", f"call_{position}"
            usage = {"input_tokens": amount, "output_tokens": 1, "total_tokens": amount + 1}
            self.message(AIMessageChunk(id=message_id, content="", tool_call_chunks=[{"index": 0, "id": call_id, "name": "lookup", "args": "{}"}]))
            self.message(AIMessageChunk(id=message_id, content="", usage_metadata=usage))
            complete = AIMessage(id=message_id, content="", tool_calls=[{"id": call_id, "name": "lookup", "args": {}}], usage_metadata=usage)
            self.values(complete)
            self.message(ToolMessage(id=f"result_{position}", content="结果", tool_call_id=call_id))
            self.values(complete)
        self.events.extend(self.parser.finish())
        usage_events = self.events_of("token_usage")
        self.assertEqual([event["data"]["id"] for event in usage_events], ["model_1", "model_2"])
        self.assertEqual([event["data"]["usage_metadata"]["total_tokens"] for event in usage_events], [4, 8])
        self.assertEqual([event["data"][0]["id"] for event in self.events_of("tool_calls")], ["model_1", "model_2"])
        self.assertEqual([event["data"][0]["tool_call_id"] for event in self.events_of("tool_calls")], ["call_1", "call_2"])
        self.assertEqual(self.events[-1]["data"]["usage_metadata"]["total_tokens"], 12)

    def test_usage_only_chunk_without_id_reuses_the_model_invocation_identity(self):
        metadata = {"langgraph_checkpoint_ns": "model:invocation", "langgraph_step": 2}
        self.message(AIMessageChunk(id="model_message", content="", tool_call_chunks=[{"index": 0, "id": "call_a", "name": "lookup", "args": "{}"}]), metadata)
        self.message(AIMessageChunk(content="", usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}), metadata)
        self.values(AIMessage(id="model_message", content="", tool_calls=[{"id": "call_a", "name": "lookup", "args": {}}], usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}))
        self.assertEqual(len(self.events_of("token_usage")), 1)
        self.assertEqual(self.events_of("token_usage")[0]["data"]["id"], "model_message")
        self.assertTrue(all(event["data"][0]["id"] == "model_message" for event in self.events_of("tool_chunk") + self.events_of("tool_calls")))

    def test_complete_child_tool_calls_without_chunks_bind_model_usage(self):
        message = AIMessage(id="child_message", content="", tool_calls=[{"id": "child_call", "name": "lookup", "args": {}}], usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})
        self.custom("task_running", message=message.model_dump(), message_index=1)
        self.assertFalse(self.events_of("tool_chunk"))
        call = self.events_of("tool_calls")[0]["data"][0]
        usage = self.events_of("token_usage")[0]
        self.assertEqual(call["id"], usage["data"]["id"])
        self.assertEqual(call["tool_call_id"], "child_call")
        self.assertEqual(usage["agent_id"], "task_a")

    def test_late_tool_id_is_bound_before_publishing_buffered_fragments(self):
        self.message(AIMessageChunk(id="m", content="", tool_call_chunks=[{"index": 0, "id": None, "name": "lookup", "args": '{"q":'}]))
        self.assertFalse(self.events)
        self.message(AIMessageChunk(id="m", content="", tool_call_chunks=[{"index": 0, "id": "late", "name": None, "args": '"甲"}'}], chunk_position="last"))
        self.assertTrue(self.events_of("tool_calls"))
        self.assertTrue(all(event["data"][0]["tool_call_id"] == "late" for event in self.events_of("tool_chunk")))

    def test_unbound_fragment_cannot_corrupt_an_existing_pending_call(self):
        self.message(AIMessageChunk(id="m", content="", tool_call_chunks=[{"index": 0, "id": None, "name": "lookup", "args": '{"q":'}]))
        self.message(AIMessageChunk(id="m", content="", tool_call_chunks=[{"index": None, "id": None, "name": None, "args": "错误归属"}]))
        self.message(AIMessageChunk(id="m", content="", tool_call_chunks=[{"index": 0, "id": "late", "name": None, "args": '"甲"}'}], chunk_position="last"))
        self.assertEqual(json.loads(self.events_of("tool_calls")[0]["data"][0]["function"]["arguments"]), {"q": "甲"})

    def test_invalid_partial_arguments_are_not_published_as_complete_calls(self):
        self.message(AIMessageChunk(id="m", content="", tool_call_chunks=[{"index": 0, "id": "c", "name": "lookup", "args": '{"q":'}]))
        self.events.extend(self.parser.finish())
        self.assertFalse(self.events_of("tool_calls"))
        self.assertEqual(self.events_of("end")[-1]["agent_id"], "main")

    def test_usage_signed_chunks_full_remainder_and_details(self):
        def usage(input_tokens, output_tokens, cache_read):
            return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens, "input_token_details": {"cache_read": cache_read}, "output_token_details": {"reasoning": 1}}
        self.message(AIMessageChunk(id="m", content="", usage_metadata=usage(10, 4, 8)))
        self.message(AIMessageChunk(id="m", content="", usage_metadata=usage(-2, 1, -1)))
        self.assertFalse(self.events_of("token_usage"))
        self.values(AIMessage(id="m", content="", usage_metadata=usage(12, 6, 9)))
        self.values(AIMessage(id="m", content="", usage_metadata=usage(12, 6, 9)))
        self.events.extend(self.parser.finish())
        total = self.events[-1]["data"]["usage_metadata"]
        self.assertEqual((total["input_tokens"], total["output_tokens"], total["total_tokens"]), (12, 6, 18))
        self.assertEqual(total["input_token_details"], {"cache_read": 9})
        self.assertEqual(total["output_token_details"], {"reasoning": 2})
        self.assertEqual(len(self.events_of("token_usage")), 1)

    def test_usage_without_complete_snapshot_flushes_once_before_end(self):
        self.message(AIMessageChunk(id="m", content="回答", usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}))
        self.message(AIMessageChunk(id="m", content="", usage_metadata={"input_tokens": 0, "output_tokens": 1, "total_tokens": 1}))
        self.assertFalse(self.events_of("token_usage"))
        self.events.extend(self.parser.finish())
        usage = self.events_of("token_usage")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["data"]["usage_metadata"]["total_tokens"], 4)
        self.assertLess(usage[0]["seq"], self.events_of("end")[0]["seq"])

    def test_usage_chunks_without_full_child_snapshot_flush_before_child_end(self):
        self.message(AIMessageChunk(id="m", content="子回答", usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}), {"agent_id": "task_a"})
        self.custom("task_completed", usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})
        self.events.extend(self.parser.finish())
        self.assertEqual(len(self.events_of("token_usage")), 1)
        usage = self.events_of("token_usage")[0]
        self.assertLess(usage["seq"], self.events_of("end")[0]["seq"])
        self.assertEqual(usage["agent_id"], "task_a")

    def test_full_snapshot_without_usage_publishes_accumulated_usage(self):
        self.message(AIMessageChunk(id="m", content="回答", usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}))
        self.values(AIMessage(id="m", content="回答"))
        self.values(AIMessage(id="m", content="回答"))
        self.events.extend(self.parser.finish())
        self.assertEqual(len(self.events_of("token_usage")), 1)
        self.assertEqual(self.events_of("token_usage")[0]["data"]["id"], "m")

    def test_historical_messages_never_become_current_output_or_usage(self):
        old = AIMessage(id="old", content="旧回答", usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
        current = HumanMessage(id="user", content="新问题", additional_kwargs={"run_id": "run_001"})
        self.values(old, current)
        self.message(old)
        self.values(old, current, AIMessage(id="new", content="新回答"))
        self.events.extend(self.parser.finish())
        self.assertEqual([event["data"]["id"] for event in self.events_of("answer_content")], ["new"])
        self.assertEqual(self.events[-1]["data"]["usage_metadata"]["total_tokens"], 0)

    def test_parallel_children_with_same_message_id_and_tool_index_are_isolated(self):
        self.message(AIMessage(id="main", content="主回答", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}))
        for task_id in ("task_a", "task_b"):
            self.custom("task_started", task_id)
            child = AIMessage(id="shared", content=task_id, tool_calls=[{"id": "child_call", "name": "lookup", "args": {}}], usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})
            self.custom("task_running", task_id, message=child.model_dump(), message_index=1)
            self.custom("task_running", task_id, message=child.model_dump(), message_index=1)
            self.custom("task_running", task_id, message=ToolMessage(id="same_result", content=task_id, tool_call_id="child_call").model_dump(), message_index=2)
        self.custom("task_completed", "task_b", usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        self.custom("task_failed", "task_a", usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})
        self.custom("task_failed", "task_a", usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})
        self.events.extend(self.parser.finish())
        self.assertEqual([event["agent_id"] for event in self.events_of("tool_result")], ["task_a", "task_b"])
        self.assertEqual([event["agent_id"] for event in self.events_of("end")], ["task_b", "task_a", "main"])
        self.assertEqual(self.events[-1]["data"]["usage_metadata"]["total_tokens"], 21)
        answer = _AIMessageAccumulator()
        for event in self.events:
            answer.observe(parse_stream_event(event))
        self.assertEqual(answer.answer(), "主回答")
        self.assertEqual([event["seq"] for event in self.events], list(range(1, len(self.events) + 1)))
        json.dumps(self.events, ensure_ascii=False)

    def test_subagent_result_fallback_preserves_artifact_and_ends_child(self):
        self.message(ToolMessage(id="result", content="完成", tool_call_id="task_a", artifact={"path": "report"}, additional_kwargs={"subagent_status": "failed", "subagent_token_usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}}))
        self.assertEqual(self.events_of("tool_result")[0]["data"]["artifact"], {"path": "report"})
        self.assertEqual(self.events_of("end")[0]["agent_id"], "task_a")
        self.assertEqual(self.events_of("end")[0]["data"]["status"], "failed")

    def test_unknown_sources_are_ignored_and_extension_can_use_shared_sequence(self):
        self.events.extend(self.parser.parse(("future", {"messages": [AIMessage(id="bad", content="误解析")]})))
        self.assertFalse(self.events)
        def handler(payload, *, namespace):
            yield self.parser.event("answer_content", "main", {"id": "extension", "content": payload})
        self.parser.register_source("future", handler)
        self.events.extend(self.parser.parse(("future", "扩展")))
        self.assertEqual(self.events[0]["seq"], 1)
        self.assertEqual(self.events[0]["data"]["content"], "扩展")

    def test_missing_message_id_then_native_id_keeps_the_same_chain(self):
        metadata = {"langgraph_checkpoint_ns": "model:invocation", "langgraph_step": 2}
        self.message(AIMessageChunk(content="首片"), metadata)
        self.message(AIMessageChunk(id="native", content="后片"), metadata)
        self.values(AIMessage(id="native", content="首片后片"))
        answers = self.events_of("answer_content")
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[0]["data"]["id"], answers[1]["data"]["id"])

    def test_namespaced_source_uses_parent_tool_call_identity(self):
        self.events.extend(self.parser.parse((("worker:instance",), "messages", (AIMessageChunk(id="a", content="子回答"), {"parent_tool_call_id": "task_a"}))))
        self.assertEqual(self.events[0]["agent_id"], "task_a")

    def test_cli_final_error_metadata_and_tui_main_end_boundary(self):
        self.message(AIMessageChunk(id="m", content="失败说明"))
        self.values(AIMessage(id="m", content="失败说明", additional_kwargs={"deerflow_error_fallback": True}))
        self.custom("task_completed", usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        self.events.extend(self.parser.finish())
        outcome = _RunOutcome()
        for event in self.events:
            outcome.observe(parse_stream_event(event))
        self.assertEqual(outcome.answer(), "失败说明")
        self.assertIsNotNone(outcome.error_text())
        actions = list(stream_actions(Mock(stream=Mock(return_value=map(parse_stream_event, self.events))), "问题"))
        self.assertEqual(sum(isinstance(action, RunEnded) for action in actions), 1)
        self.assertTrue(any(isinstance(action, AssistantDelta) for action in actions))
        event = self.parser.event("tool_calls", "main", [{"id": "message", "tool_call_id": "tool", "index": 0, "function": {"name": "lookup", "arguments": '{"q":"甲"}'}}])
        self.assertIsInstance(translate(event)[0], ToolStarted)
        self.assertEqual(translate(event)[0].args, {"q": "甲"})
        self.assertEqual(translate(event)[0].tool_call_id, "tool")


if __name__ == "__main__":
    unittest.main()
