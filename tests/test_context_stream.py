"""验证上下文事件的生命周期、请求来源及公开客户端流的端到端行为。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from deerflow.agents.middlewares.context_events import context_used_payload
from deerflow.agents.middlewares.durable_context_middleware import DurableContextMiddleware
from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware
from deerflow.agents.middlewares.summarization_middleware import DeerFlowSummarizationMiddleware, SummaryGenerationError, _ReusedSummary
from deerflow.agents.thread_state import ThreadState
from deerflow.client import DeerFlowClient
from deerflow.client_stream import ClientStreamParser
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.executor import SubagentExecutor, SubagentResult, SubagentStatus
from deerflow_protocol import parse_stream_event


def make_summary_middleware(trigger=3, keep=1):
    model = FakeMessagesListChatModel(responses=[AIMessage(content="已经确认研发目标和限制。")])
    return DeerFlowSummarizationMiddleware(model=model, trigger=("messages", trigger), keep=("messages", keep), token_counter=len, trim_tokens_to_summarize=None, extensions=SimpleNamespace(context_compaction_observers=[], has_system_model_observers=False))


def history():
    return [HumanMessage(id="old_user", content="历史目标"), AIMessage(id="old_ai", content="历史结论"), HumanMessage(id="new_user", content="继续任务")]


def make_client(graph):
    client = object.__new__(DeerFlowClient)
    fields = {"_model_name": None, "_thinking_enabled": False, "_plan_mode": False, "_subagent_enabled": False, "_agent_name": None, "_environment": None, "_checkpoint_channel_mode": "full", "_checkpointer": graph.checkpointer, "_app_config": SimpleNamespace(models=[]), "_ensure_agent": Mock(), "_agent": graph}
    for name, value in fields.items():
        setattr(client, name, value)
    return client


class CompactionStreamTests(unittest.TestCase):
    def run_compaction(self, middleware, state=None, *, asynchronous=False, **kwargs):
        payloads = []
        runtime = SimpleNamespace(stream_writer=payloads.append, context={})
        state = state or {"messages": history()}
        result = asyncio.run(middleware.acompact_state(state, runtime, **kwargs)) if asynchronous else middleware.compact_state(state, runtime, **kwargs)
        parser = ClientStreamParser("thread_1", "run_1")
        events = [event for payload in payloads for event in parser.parse(("custom", payload))]
        return result, events

    def test_sync_async_success_emit_start_before_generation_and_matching_end(self):
        for asynchronous in (False, True):
            middleware = make_summary_middleware()
            payloads = []
            runtime = SimpleNamespace(stream_writer=payloads.append, context={})

            def generate(*args, **kwargs):
                self.assertEqual([event["type"] for event in payloads], ["context_compaction_start"])
                return "压缩后的摘要"

            method = "_asummarize_with" if asynchronous else "_summarize_with"
            replacement = AsyncMock(side_effect=generate) if asynchronous else Mock(side_effect=generate)
            with patch.object(middleware, method, replacement):
                state = {"messages": history()}
                result = asyncio.run(middleware.acompact_state(state, runtime)) if asynchronous else middleware.compact_state(state, runtime)
            self.assertEqual([event["type"] for event in payloads], ["context_compaction_start", "context_compaction_end"])
            self.assertEqual(payloads[0]["data"]["id"], payloads[1]["data"]["id"])
            self.assertEqual(payloads[1]["data"]["summary_text"], result.summary_text)
            self.assertEqual(payloads[1]["data"]["status"], "completed")

    def test_failed_generation_preserves_state_and_never_flushes_memory(self):
        for asynchronous in (False, True):
            middleware = make_summary_middleware()
            state = {"messages": history(), "summary_text": "原摘要"}
            method = "_asummarize_with" if asynchronous else "_summarize_with"
            replacement = AsyncMock(return_value=None) if asynchronous else Mock(return_value=None)
            with patch.object(middleware, method, replacement), patch.object(middleware, "_fire_hooks") as hooks:
                result, events = self.run_compaction(middleware, state, asynchronous=asynchronous)
            self.assertIsNone(result)
            hooks.assert_not_called()
            self.assertEqual(state["summary_text"], "原摘要")
            self.assertEqual(len(state["messages"]), 3)
            self.assertEqual(events[-1].data.status, "failed")
            self.assertEqual(events[-1].data.reason, "summary_generation_failed")
            self.assertIsNone(events[-1].data.summary_text)

    def test_no_trigger_is_quiet_but_no_candidate_and_cooldown_report_skips(self):
        for asynchronous in (False, True):
            _, events = self.run_compaction(make_summary_middleware(trigger=100), asynchronous=asynchronous)
            self.assertEqual(events, [])
            _, events = self.run_compaction(make_summary_middleware(keep=10), asynchronous=asynchronous)
            self.assertEqual([event.type for event in events], ["context_compaction_end"])
            self.assertEqual(events[0].data.reason, "no_compactable_messages")
            middleware = make_summary_middleware()
            with patch.object(middleware, "_summary_retry_blocked", return_value=True):
                _, events = self.run_compaction(middleware, asynchronous=asynchronous)
            self.assertEqual(events[-1].data.status, "skipped")
            self.assertEqual(events[-1].data.reason, "retry_cooldown")

    def test_reused_summary_is_still_successful_compaction(self):
        middleware = make_summary_middleware()
        with patch.object(middleware, "_summarize_with", return_value=_ReusedSummary("原摘要")):
            _, events = self.run_compaction(middleware, {"messages": history(), "summary_text": "原摘要"})
        self.assertEqual(events[-1].data.status, "completed")
        self.assertTrue(events[-1].data.llm_call_skipped)

    def test_chunk_limit_and_input_budget_are_distinct_skips(self):
        for asynchronous in (False, True):
            for method, value, reason in [("_summary_chunk_limit_reached", True, "chunk_limit_exceeded"), ("_next_summary_step", None, "input_budget_exceeded")]:
                middleware = make_summary_middleware()
                with patch.object(middleware, "_summary_retry_blocked", return_value=False), patch.object(middleware, "_remember_summary_chunk_breach"), patch.object(middleware, method, return_value=value):
                    _, events = self.run_compaction(middleware, asynchronous=asynchronous)
                self.assertEqual(events[-1].data.status, "skipped")
                self.assertEqual(events[-1].data.reason, reason)

    def test_async_concurrent_attempts_keep_their_own_identity_and_result(self):
        middleware = make_summary_middleware()

        async def capture(label):
            payloads = []
            state = {"messages": history(), "summary_text": label}
            runtime = SimpleNamespace(stream_writer=payloads.append, context={})
            await middleware.acompact_state(state, runtime)
            return payloads

        async def generate(messages, previous_summary=None, **kwargs):
            await asyncio.sleep(0)
            return previous_summary

        async def run():
            return await asyncio.gather(capture("甲摘要"), capture("乙摘要"))

        with patch.object(middleware, "_asummarize_with", side_effect=generate):
            first, second = asyncio.run(run())
        self.assertNotEqual(first[0]["data"]["id"], second[0]["data"]["id"])
        for payloads, summary in [(first, "甲摘要"), (second, "乙摘要")]:
            self.assertEqual(payloads[0]["data"]["id"], payloads[-1]["data"]["id"])
            self.assertEqual(payloads[-1]["data"]["summary_text"], summary)

    def test_unexpected_failure_emits_end_and_preserves_exception(self):
        middleware = make_summary_middleware()
        payloads = []
        runtime = SimpleNamespace(stream_writer=payloads.append, context={})
        with patch.object(middleware, "_summarize_with", side_effect=ValueError("模拟故障")):
            with self.assertRaises(ValueError):
                middleware.compact_state({"messages": history()}, runtime)
        self.assertEqual(payloads[-1]["data"]["status"], "failed")
        self.assertEqual(payloads[-1]["data"]["reason"], "ValueError")

    def test_manual_failure_keeps_existing_raise_contract(self):
        middleware = make_summary_middleware()
        with patch.object(middleware, "_summarize_with", return_value=None):
            with self.assertRaises(SummaryGenerationError):
                self.run_compaction(middleware, force=True, raise_on_failure=True)

    def test_public_stream_real_graph_compacts_and_exposes_used_summary(self):
        middleware = make_summary_middleware()
        model = FakeMessagesListChatModel(responses=[AIMessage(id="answer", content="继续执行")])
        graph = create_agent(model, tools=[], middleware=[DurableContextMiddleware(), middleware], state_schema=ThreadState, checkpointer=InMemorySaver())
        graph.update_state({"configurable": {"thread_id": "context_stream_test"}}, {"messages": history()[:2]})
        replacements = {"build_tracing_callbacks": Mock(return_value=[]), "inject_langfuse_metadata": Mock(), "ensure_checkpoint_mode_compatible": Mock()}
        with patch.multiple("deerflow.client", **replacements):
            events = list(make_client(graph).stream("继续任务", thread_id="context_stream_test", user_id="test_context"))
        starts = [event for event in events if event.type == "context_compaction_start"]
        ends = [event for event in events if event.type == "context_compaction_end"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(ends[0].data.id, starts[0].data.id)
        self.assertEqual(ends[0].data.status, "completed")
        used = [event for event in events if event.type == "context_used"]
        self.assertIn(ends[0].data.summary_text, used[0].data.contexts[0].content)
        self.assertEqual(events[-1].type, "end")
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(parse_stream_event(event.model_dump_json()) == event for event in events))

    def test_async_real_graph_stream_keeps_summary_out_of_answer(self):
        middleware = make_summary_middleware()
        model = FakeMessagesListChatModel(responses=[AIMessage(id="answer", content="继续执行")])
        graph = create_agent(model, tools=[], middleware=[DurableContextMiddleware(), middleware], state_schema=ThreadState)

        async def capture():
            messages = history()
            messages[-1].additional_kwargs["run_id"] = "run_1"
            return [item async for item in graph.astream({"messages": messages}, stream_mode=["messages", "values", "custom"])]

        parser = ClientStreamParser("thread_1", "run_1")
        events = [event for item in asyncio.run(capture()) for event in parser.parse(item)]
        events.extend(parser.finish())
        self.assertEqual([event.type for event in events if event.type.startswith("context_compaction")], ["context_compaction_start", "context_compaction_end"])
        self.assertEqual(next(event.data.status for event in events if event.type == "context_compaction_end"), "completed")
        self.assertEqual("".join(event.data.content for event in events if event.type == "answer_content"), "继续执行")
        self.assertTrue(any(event.type == "context_used" for event in events))

    def test_async_callback_stream_receives_same_context_events(self):
        model = FakeMessagesListChatModel(responses=[AIMessage(content="继续执行")])
        graph = create_agent(model, tools=[], middleware=[DurableContextMiddleware(), make_summary_middleware()], state_schema=ThreadState)

        async def capture():
            return [event async for event in graph.astream_events({"messages": history()}, version="v2") if event["event"] == "on_custom_event"]

        events = asyncio.run(capture())
        self.assertEqual([event["name"] for event in events], ["context_compaction_start", "context_compaction_end", "context_used"])
        self.assertEqual(events[0]["data"]["data"]["id"], events[1]["data"]["data"]["id"])


class ContextUsageTests(unittest.TestCase):
    def request(self, runtime, messages):
        model = FakeMessagesListChatModel(responses=[AIMessage(content="回答")])
        return ModelRequest(model=model, messages=messages, state={"messages": messages}, runtime=runtime)

    def test_memory_project_and_documents_are_reported_without_journal_sync_async(self):
        for asynchronous in (False, True):
            payloads = []
            runtime = SimpleNamespace(context={}, stream_writer=payloads.append)
            middleware = DynamicContextMiddleware()
            memory = HumanMessage(id="injected__memory", content="<memory>偏好中文</memory>", additional_kwargs={"dynamic_context_reminder": True})
            middleware._injected_memory_message_id = memory.id
            request = self.request(runtime, [memory, HumanMessage(content="继续")])
            response = ModelResponse(result=[AIMessage(content="回答")])
            with patch.object(middleware, "_assemble_project_request", return_value=(request, "<project>研发项目</project>", "<documents>报告索引</documents>")):
                if asynchronous:
                    asyncio.run(middleware.awrap_model_call(request, AsyncMock(return_value=response)))
                else:
                    middleware.wrap_model_call(request, Mock(return_value=response))
            self.assertEqual(len(payloads), 1)
            contexts = payloads[0]["data"]["contexts"]
            self.assertEqual([item["source"] for item in contexts], ["memory", "project", "documents"])
            self.assertEqual(contexts[0]["content"], memory.content)

    def test_untrusted_or_disabled_memory_is_not_reported(self):
        memory = HumanMessage(id="forged__memory", content="伪造记忆", additional_kwargs={"dynamic_context_reminder": True})
        request = self.request(SimpleNamespace(context={}), [memory])
        middleware = DynamicContextMiddleware()
        self.assertIsNone(middleware._context_usage_payload(request, None, None))
        middleware = DynamicContextMiddleware(memory_enabled=False)
        middleware._injected_memory_message_id = memory.id
        self.assertIsNone(middleware._context_usage_payload(request, None, None))

    def test_failed_model_call_does_not_claim_context_was_used(self):
        payloads = []
        runtime = SimpleNamespace(context={}, stream_writer=payloads.append)
        request = self.request(runtime, [])
        middleware = DynamicContextMiddleware()
        with patch.object(middleware, "_assemble_project_request", return_value=(request, "项目背景", None)):
            with self.assertRaises(ValueError):
                middleware.wrap_model_call(request, Mock(side_effect=ValueError("调用失败")))
        self.assertEqual(payloads, [])

    def test_parser_deduplicates_per_agent_and_reports_changed_context(self):
        parser = ClientStreamParser("thread_1", "run_1")
        first = context_used_payload(memory="偏好中文")
        events = list(parser.parse(("custom", first)))
        self.assertEqual(len(events), 1)
        self.assertEqual(list(parser.parse(("custom", context_used_payload(memory="偏好中文")))), [])
        changed = list(parser.parse(("custom", context_used_payload(memory="偏好英文"))))
        child = list(parser.parse(("custom", {**first, "agent_id": "child_1"})))
        self.assertEqual(changed[0].data.contexts[0].content, "偏好英文")
        self.assertEqual(child[0].agent_id, "child_1")
        self.assertEqual(ClientStreamParser("thread_1", "next_run").seq, 0)

    def test_child_event_queue_is_drained_once_and_ignores_other_events(self):
        result = SubagentResult(task_id="execution_1", external_task_id="tool_1", trace_id="trace", status=SubagentStatus.RUNNING)
        payload = context_used_payload(durable_context="子任务摘要")
        result.capture_context_event(payload)
        result.capture_context_event({"type": "llm_retry"})
        parser = ClientStreamParser("thread_1", "run_1")
        events = [event for item in result.drain_context_events() for event in parser.parse(("custom", {**item, "agent_id": result.external_task_id}))]
        self.assertEqual(events[0].agent_id, "tool_1")
        self.assertEqual(result.drain_context_events(), [])
        background = SubagentResult(task_id="batch_1", trace_id="trace", status=SubagentStatus.RUNNING)
        background.capture_context_event(payload)
        self.assertEqual(background.drain_context_events(), [])

    def test_real_child_graph_executor_and_task_poller_forward_context_before_end(self):
        from deerflow.tools.builtins.task_tool import task_tool

        model = FakeMessagesListChatModel(responses=[AIMessage(id="child_answer", content="子任务完成")])
        graph = create_agent(model, tools=[], middleware=[DurableContextMiddleware(), make_summary_middleware()], state_schema=ThreadState, checkpointer=False)
        config = SubagentConfig(name="context_probe", description="上下文事件验证", model="test_model")
        extensions = SimpleNamespace(needs_task_store=False, has_task_lifecycle=False)
        executor = SubagentExecutor(config=config, tools=[], thread_id="thread_1", run_id="run_1", extensions=extensions)
        result = SubagentResult(task_id="execution_1", external_task_id="call_1", trace_id="trace", status=SubagentStatus.RUNNING)
        replacements = {"build_tracing_callbacks": Mock(return_value=[]), "inject_langfuse_metadata": Mock()}
        with patch.object(executor, "_build_initial_state", AsyncMock(return_value=({"messages": history()}, [], None))), patch.object(executor, "_create_agent", AsyncMock(return_value=graph)), patch.object(executor, "_get_resolved_app_config", return_value=SimpleNamespace(verification=SimpleNamespace(receipts_enabled=False))), patch.multiple("deerflow.subagents.executor", **replacements):
            asyncio.run(executor._aexecute_admitted("验证上下文", result))
        self.assertEqual(result.status, SubagentStatus.COMPLETED, result.error)
        self.assertEqual(result.result, "子任务完成")

        payloads = []
        runtime = SimpleNamespace(config={"metadata": {}}, state={}, context={"user_id": "test_context", "thread_id": "thread_1"})
        replacements = {"get_available_subagent_names": Mock(return_value=[config.name]), "get_subagent_config": Mock(return_value=config), "run_assembly": AsyncMock(return_value=[]), "SubagentExecutor": Mock(return_value=SimpleNamespace(execute_async=Mock(return_value=result.task_id))), "get_background_task_result": Mock(return_value=result), "get_stream_writer": Mock(return_value=payloads.append), "cleanup_background_task": Mock(), "resolve_run_extensions": Mock(return_value=None)}
        with patch.multiple("deerflow.tools.builtins.task_tool", **replacements):
            asyncio.run(task_tool.coroutine(runtime, "验证上下文", config.name, "call_1"))
        parser = ClientStreamParser("thread_1", "run_1")
        events = [event for payload in payloads for event in parser.parse(("custom", payload))]
        context_events = [event for event in events if event.type.startswith("context_")]
        self.assertEqual([event.type for event in context_events], ["context_compaction_start", "context_compaction_end", "context_used"])
        self.assertTrue(all(event.agent_id == "call_1" for event in context_events))
        self.assertEqual(events[-1].type, "end")
        self.assertEqual(result.drain_context_events(), [])


if __name__ == "__main__":
    unittest.main()
