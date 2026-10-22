"""验证推荐追问在回合末尾生成、配置统一启停及独立用量归属。"""

import asyncio
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from deerflow.agents.lead_agent.agent import build_middlewares
from deerflow.agents.middlewares.follow_up_suggestions_middleware import FollowUpSuggestionsMiddleware
from deerflow.client import DeerFlowClient
from deerflow.client_stream import ClientStreamParser
from deerflow.client_stream_pricing import ClientStreamPricing
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.suggestions_config import SuggestionsConfig
from deerflow_protocol import SuggestionsData, parse_stream_event


def usage(inputs=10, outputs=5):
    return {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs}


def profile(name, price):
    return ModelConfig(name=name, model=name, use="test.Model", price={"price_input_normal": price, "price_input_cache_hit": price, "price_output_normal": price})


class FollowUpSuggestionsTests(unittest.TestCase):
    def setUp(self):
        self.questions = ["如何制定下一阶段的实验计划？", "哪些指标可以验证实验效果？", "如何识别和控制实验风险？"]
        self.profiles = [profile("main_model", 1), profile("suggestions_model", 2)]
        self.app = AppConfig(models=self.profiles, sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"}, suggestions=SuggestionsConfig(model_name="suggestions_model"))
        self.middleware = FollowUpSuggestionsMiddleware(app_config=self.app, model_name="main_model")
        self.response = AIMessage(id="suggestions_message", content=json.dumps({"questions": self.questions}), usage_metadata=usage())
        self.model = Mock(invoke=Mock(return_value=self.response), ainvoke=AsyncMock(return_value=self.response))

    def graph(self):
        responses = [AIMessage(id=identity, content="建议先建立实验基线，再逐项验证。", usage_metadata=usage(20, 10)) for identity in ("answer_message", "answer_message_2")]
        model = FakeMessagesListChatModel(responses=responses)
        return create_agent(model, tools=[], middleware=[self.middleware], checkpointer=InMemorySaver())

    def parse_items(self, items):
        parser = ClientStreamParser("thread_001", "run_001", pricing=ClientStreamPricing(self.profiles, default_model="main_model"))
        events = [event for item in items for event in parser.parse(item)]
        return events + list(parser.finish())

    def assert_suggestions(self, events):
        self.assertEqual([event.type for event in events[-3:]], ["suggestions", "token_usage", "end"])
        self.assertEqual(events[-3].data.questions, self.questions)
        self.assertEqual(events[-3].data.id, "suggestions_message")
        self.assertEqual(events[-3].agent_id, "main")
        related = [event for event in events if event.type == "token_usage" and event.data.id == "suggestions_message"]
        self.assertEqual(len(related), 1)
        self.assertIs(related[0], events[-2])
        self.assertEqual(related[0].data.model_name, "suggestions_model")
        self.assertEqual(related[0].data.cost.total_cost, Decimal("0.000030"))
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 45)
        self.assertEqual(events[-1].data.cost.total_cost, Decimal("0.000060"))
        self.assertEqual("".join(event.data.content for event in events if event.type == "answer_content"), "建议先建立实验基线，再逐项验证。")
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(parse_stream_event(event.model_dump_json(exclude_unset=True)) == event for event in events))

    def test_public_sync_stream_finishes_with_suggestions_and_total_usage(self):
        client = object.__new__(DeerFlowClient)
        graph = self.graph()
        fields = {"_model_name": "main_model", "_thinking_enabled": False, "_plan_mode": False, "_subagent_enabled": False, "_agent_name": None, "_environment": None, "_checkpoint_channel_mode": "full", "_checkpointer": graph.checkpointer, "_app_config": self.app, "_ensure_agent": Mock(), "_agent": graph}
        for name, value in fields.items():
            setattr(client, name, value)
        with patch("deerflow.agents.middlewares.follow_up_suggestions_middleware.create_chat_model", return_value=self.model), patch("deerflow.client.build_tracing_callbacks", return_value=[]), patch("deerflow.client.ensure_checkpoint_mode_compatible"):
            events = list(client.stream("如何安排电池研发实验？", thread_id="thread_001", user_id="test_suggestions"))
        self.assert_suggestions(events)
        self.assertNotIn("suggestions_enabled", client._ensure_agent.call_args.args[0]["configurable"])
        self.assertEqual(len(graph.get_state({"configurable": {"thread_id": "thread_001"}}).values["messages"]), 2)
        self.model.invoke.assert_called_once()

    def test_async_graph_uses_identical_protocol_and_accounting(self):
        async def capture():
            return [item async for item in self.graph().astream({"messages": [HumanMessage(content="实验如何推进？")]}, config={"configurable": {"thread_id": "thread_001"}}, stream_mode=["messages", "values", "custom"])]

        with patch("deerflow.agents.middlewares.follow_up_suggestions_middleware.create_chat_model", return_value=self.model):
            events = self.parse_items(asyncio.run(capture()))
        self.assert_suggestions(events)
        self.model.ainvoke.assert_awaited_once()
        self.model.invoke.assert_not_called()

    def test_assembly_uses_only_config_switch(self):
        for config_enabled in (False, True):
            for run_config in ({}, {"suggestions_enabled": False}, {"suggestions_enabled": True}):
                with self.subTest(config_enabled=config_enabled, run_config=run_config):
                    self.app.suggestions.enabled = config_enabled
                    with patch("deerflow.agents.lead_agent.agent.build_lead_runtime_middlewares", return_value=[]), patch("deerflow.agents.lead_agent.agent._create_summarization_middleware", return_value=None):
                        chain = build_middlewares({"configurable": run_config}, model_name="main_model", app_config=self.app, memory_enabled=False)
                    self.assertEqual(any(isinstance(middleware, FollowUpSuggestionsMiddleware) for middleware in chain), config_enabled)

    def test_disabled_config_skips_sync_and_async_model_calls(self):
        self.app.suggestions.enabled = False
        state = {"messages": [HumanMessage(content="问题"), AIMessage(content="回答")]}
        writer = Mock()
        with patch("deerflow.agents.middlewares.follow_up_suggestions_middleware.create_chat_model") as factory:
            self.middleware.after_agent(state, SimpleNamespace(stream_writer=writer))
            asyncio.run(self.middleware.aafter_agent(state, SimpleNamespace(stream_writer=writer)))
        factory.assert_not_called()
        writer.assert_not_called()

    def test_final_exchange_ignores_old_turns_tool_steps_and_dynamic_context(self):
        messages = [HumanMessage(content="旧问题"), AIMessage(content="旧回答"), HumanMessage(content="被包装的问题", additional_kwargs={"original_user_content": "新问题"}), AIMessage(content="", tool_calls=[{"id": "call", "name": "lookup", "args": {}}]), ToolMessage(content="工具资料", tool_call_id="call"), HumanMessage(content="隐藏上下文", additional_kwargs={"dynamic_context_reminder": True}), AIMessage(content="最终回答")]
        self.assertEqual(self.middleware._exchange({"messages": messages}), ("新问题", "最终回答"))
        prompt = self.middleware._messages(self.middleware._exchange({"messages": messages}))
        self.assertIsInstance(prompt[0], SystemMessage)
        self.assertEqual(json.loads(prompt[1].content), {"user": "新问题", "assistant": "最终回答"})

    def test_each_turn_uses_its_latest_answer(self):
        graph = self.graph()
        with patch("deerflow.agents.middlewares.follow_up_suggestions_middleware.create_chat_model", return_value=self.model):
            for question in ("第一轮问题", "第二轮问题"):
                list(graph.stream({"messages": [HumanMessage(content=question)]}, config={"configurable": {"thread_id": "thread_001"}}, stream_mode=["messages", "values", "custom"]))
        self.assertEqual(self.model.invoke.call_count, 2)
        self.assertEqual(json.loads(self.model.invoke.call_args.args[0][1].content)["user"], "第二轮问题")

    def test_incomplete_or_error_answers_do_not_trigger_a_call(self):
        states = [{"messages": []}, {"messages": [HumanMessage(content="问题")]}, {"messages": [HumanMessage(content="问题"), AIMessage(content="失败", additional_kwargs={"deerflow_error_fallback": True})]}, {"messages": [HumanMessage(content="问题"), AIMessage(content="", tool_calls=[{"id": "call", "name": "lookup", "args": {}}])]}]
        with patch("deerflow.agents.middlewares.follow_up_suggestions_middleware.create_chat_model") as factory:
            for state in states:
                self.middleware.after_agent(state, SimpleNamespace(stream_writer=Mock()))
                asyncio.run(self.middleware.aafter_agent(state, SimpleNamespace(stream_writer=Mock())))
        factory.assert_not_called()

    def test_invalid_generated_questions_keep_usage_without_suggestions(self):
        for content in ("不是 JSON", '{"questions":["重复","重复","另一个"]}', '{"questions":["只有一个"]}', '{"questions":[1,2,3]}'):
            with self.assertLogs("deerflow.agents.middlewares.follow_up_suggestions_middleware", level="WARNING"):
                payload = self.middleware._result(self.response.model_copy(update={"content": content}))
            events = self.parse_items([("custom", payload)])
            self.assertEqual([event.type for event in events], ["token_usage", "end"])
            self.assertEqual(events[-1].data.usage_metadata.total_tokens, 15)

    def test_nostream_tag_and_provider_failure_preserve_main_answer(self):
        self.assertIn("nostream", self.middleware._invoke_config()["tags"])
        self.model.invoke.side_effect = RuntimeError("模拟模型不可用")
        with patch("deerflow.agents.middlewares.follow_up_suggestions_middleware.create_chat_model", return_value=self.model), self.assertLogs("deerflow.agents.middlewares.follow_up_suggestions_middleware", level="WARNING"):
            events = self.parse_items(self.graph().stream({"messages": [HumanMessage(content="问题")]}, config={"configurable": {"thread_id": "thread_001"}}, stream_mode=["messages", "values", "custom"]))
        self.assertEqual(events[-1].type, "end")
        self.assertFalse(any(event.type == "suggestions" for event in events))
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 30)

    def test_child_end_and_pending_usage_precede_suggestions(self):
        payload = self.middleware._result(self.response)
        events = self.parse_items([("custom", payload), ("custom", {"type": "task_running", "task_id": "child", "message": AIMessage(id="child_message", content="子任务回答", usage_metadata=usage(20, 10)).model_dump()}), ("custom", payload)])
        self.assertEqual([event.type for event in events[-4:]], ["end", "suggestions", "token_usage", "end"])
        self.assertEqual(events[-4].agent_id, "child")
        self.assertEqual(events[-3].data.id, events[-2].data.id)
        self.assertEqual(len([event for event in events if event.type == "token_usage" and event.data.id == "suggestions_message"]), 1)
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 45)
        self.assertEqual(len([event for event in events if event.type == "suggestions"]), 1)

    def test_suggestions_without_usage_still_precede_end(self):
        payload = self.middleware._result(self.response.model_copy(update={"usage_metadata": None}))
        events = self.parse_items([("custom", payload)])
        self.assertEqual([event.type for event in events], ["suggestions", "end"])
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 0)

    def test_suggestions_and_usage_are_deferred_together(self):
        parser = ClientStreamParser("thread_001", "run_001")
        payload = self.middleware._result(self.response)
        self.assertEqual(list(parser.parse(("custom", payload))), [])
        self.assertEqual(list(parser.parse(("custom", payload))), [])
        events = list(parser.finish())
        self.assertEqual([event.type for event in events], ["suggestions", "token_usage", "end"])
        self.assertEqual(events[0].data.id, events[1].data.id)
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 15)

    def test_configuration_and_protocol_limits_are_enforced(self):
        for field in ("max_suggestions", "max_chars"):
            with self.assertRaises(ValidationError):
                SuggestionsConfig(**{field: True})
        for questions in ([], [" "], ["重复", "重复"], ["问题"] * 6):
            with self.assertRaises(ValidationError):
                SuggestionsData(id="test", questions=questions)
        content = "<think>思考</think>\n```json\n" + self.response.content + "\n```"
        self.assertEqual(self.middleware._parse_questions(content), self.questions)
        self.app.suggestions.max_chars = 10
        with self.assertRaises(ValueError):
            self.middleware._parse_questions(self.response.content)

    def test_unset_model_uses_effective_main_model_and_missing_id_is_generated(self):
        self.app.suggestions.model_name = None
        middleware = FollowUpSuggestionsMiddleware(app_config=self.app, model_name="main_model")
        payload = middleware._result(self.response.model_copy(update={"id": None}))
        self.assertEqual(payload["model_name"], "main_model")
        self.assertTrue(payload["id"].startswith("suggestions_"))


if __name__ == "__main__":
    unittest.main()
