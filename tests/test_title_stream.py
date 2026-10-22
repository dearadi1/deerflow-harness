"""验证标题独立身份、真实模型用量及同步和异步图的完整输出。"""

import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from deerflow.agents.middlewares.title_middleware import TitleMiddleware
from deerflow.client import DeerFlowClient
from deerflow.client_stream import ClientStreamParser
from deerflow.client_stream_pricing import ClientStreamPricing
from deerflow.config.model_config import ModelConfig
from deerflow.config.title_config import TitleConfig
from deerflow.extensions.notify import observe_system_model_call_sync
from deerflow_protocol import parse_stream_event


def usage(inputs=10, outputs=2):
    return {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs, "input_token_details": {"cache_read": 0}}


def model_profile(name, price):
    return ModelConfig(name=name, model=name, use="test.Model", price={"price_input_normal": price, "price_input_cache_hit": price, "price_output_normal": price})


class TitleStreamTests(unittest.TestCase):
    def setUp(self):
        self.middleware = TitleMiddleware(title_config=TitleConfig(model_name="title_model"))
        self.state = {"messages": [HumanMessage(content="请整理季度研发工作进展"), AIMessage(content="可以分为成果和计划。")]}
        self.response = AIMessage(id="title_message", content="季度研发进展", usage_metadata=usage())
        self.title_model = Mock(invoke=Mock(return_value=self.response), ainvoke=AsyncMock(return_value=self.response))
        self.profiles = [model_profile("main_model", 1), model_profile("title_model", 2)]

    def parser(self):
        return ClientStreamParser("thread_001", "run_001", pricing=ClientStreamPricing(self.profiles, default_model="main_model"))

    def graph(self, middleware=None):
        main_model = FakeMessagesListChatModel(responses=[AIMessage(id="answer_message", content="这是回答", usage_metadata=usage(20, 4))])
        return create_agent(main_model, tools=[], middleware=[middleware or self.middleware], checkpointer=InMemorySaver())

    def parse_items(self, items):
        parser = self.parser()
        events = [event for item in items for event in parser.parse(item)]
        return events + list(parser.finish())

    def assert_title_usage(self, events):
        titles = [event for event in events if event.type == "title"]
        self.assertEqual([(event.data.id, event.data.content) for event in titles], [("title_message", "季度研发进展")])
        counts = {event.data.id: event for event in events if event.type == "token_usage"}
        self.assertEqual(set(counts), {"title_message", "answer_message"})
        self.assertEqual(counts["title_message"].data.model_name, "title_model")
        self.assertEqual(counts["title_message"].data.usage_metadata.total_tokens, 12)
        self.assertEqual(counts["title_message"].data.cost.total_cost, Decimal("0.000024"))
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 36)
        self.assertEqual(events[-1].data.cost.total_cost, Decimal("0.000048"))
        self.assertEqual("".join(event.data.content for event in events if event.type == "answer_content"), "这是回答")
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(parse_stream_event(event.model_dump_json(exclude_unset=True)) == event for event in events))

    def test_public_sync_stream_binds_title_usage_without_polluting_history(self):
        client = object.__new__(DeerFlowClient)
        graph = self.graph()
        fields = {"_model_name": "main_model", "_thinking_enabled": False, "_plan_mode": False, "_subagent_enabled": False, "_agent_name": None, "_environment": None, "_checkpoint_channel_mode": "full", "_checkpointer": graph.checkpointer, "_app_config": SimpleNamespace(models=self.profiles), "_ensure_agent": Mock(), "_agent": graph}
        for name, value in fields.items():
            setattr(client, name, value)
        replacements = {"build_tracing_callbacks": Mock(return_value=[]), "inject_langfuse_metadata": Mock(), "ensure_checkpoint_mode_compatible": Mock()}
        with patch("deerflow.agents.middlewares.title_middleware.create_chat_model", return_value=self.title_model), patch.multiple("deerflow.client", **replacements):
            events = list(client.stream("请整理季度研发工作进展", thread_id="thread_001", user_id="test_title"))
        self.assert_title_usage(events)
        self.title_model.invoke.assert_called_once()
        self.title_model.ainvoke.assert_not_called()
        state = graph.get_state({"configurable": {"thread_id": "thread_001"}}).values
        self.assertEqual(state["title"], "季度研发进展")
        self.assertEqual(len(state["messages"]), 2)

    def test_async_graph_has_the_same_title_identity_and_accounting(self):
        async def capture():
            state = {"messages": [self.state["messages"][0]]}
            return [item async for item in self.graph().astream(state, config={"configurable": {"thread_id": "thread_001"}}, stream_mode=["messages", "values", "custom"])]

        with patch("deerflow.agents.middlewares.title_middleware.create_chat_model", return_value=self.title_model):
            items = asyncio.run(capture())
        self.assert_title_usage(self.parse_items(items))
        self.title_model.ainvoke.assert_awaited_once()
        self.title_model.invoke.assert_not_called()

    def test_existing_title_or_non_first_turn_never_calls_model(self):
        cases = [{**self.state, "title": "已有标题"}, {"messages": [*self.state["messages"], HumanMessage(content="继续")]}, {"messages": [HumanMessage(content="首轮尚未回答")]}]
        with patch("deerflow.agents.middlewares.title_middleware.create_chat_model") as factory:
            for state in cases:
                self.assertIsNone(self.middleware.after_model(state, None))
                self.assertIsNone(asyncio.run(self.middleware.aafter_model(state, None)))
        factory.assert_not_called()

    def test_disabled_model_local_fallback_and_attachments_have_no_usage(self):
        cases = [(TitleConfig(enabled=False, model_name="title_model"), self.state, None), (TitleConfig(), self.state, "请整理季度研发工作进展"), (TitleConfig(model_name="title_model"), {"messages": [HumanMessage(content=""), AIMessage(content="附件摘要")], "uploaded_files": [{"filename": "季度报告.pdf"}]}, "季度报告.pdf")]
        with patch("deerflow.agents.middlewares.title_middleware.create_chat_model") as factory:
            for config, state, title in cases:
                middleware = TitleMiddleware(title_config=config)
                expected = {"title": title} if title else None
                self.assertEqual(middleware.after_model(state, None), expected)
                self.assertEqual(asyncio.run(middleware.aafter_model(state, None)), expected)
        factory.assert_not_called()

    def test_sync_and_async_errors_fall_back_without_usage(self):
        self.title_model.invoke.side_effect = RuntimeError("模拟服务不可用")
        self.title_model.ainvoke.side_effect = RuntimeError("模拟服务不可用")
        with patch("deerflow.agents.middlewares.title_middleware.create_chat_model", return_value=self.title_model), patch("deerflow.agents.middlewares.title_middleware.emit_custom_event") as emit, patch("deerflow.agents.middlewares.title_middleware.aemit_custom_event") as aemit, self.assertLogs("deerflow.agents.middlewares.title_middleware", level="WARNING"):
            self.assertEqual(self.middleware.after_model(self.state, None), {"title": "请整理季度研发工作进展"})
            self.assertEqual(asyncio.run(self.middleware.aafter_model(self.state, None)), {"title": "请整理季度研发工作进展"})
        emit.assert_not_called()
        aemit.assert_not_called()

    def test_missing_response_id_gets_stable_title_usage_identity(self):
        self.title_model.invoke.return_value = self.response.model_copy(update={"id": None})
        with patch("deerflow.agents.middlewares.title_middleware.create_chat_model", return_value=self.title_model), patch("deerflow.agents.middlewares.title_middleware.emit_custom_event") as emit:
            result = self.middleware.after_model(self.state, None)
        payload = emit.call_args.args[0]
        self.assertTrue(payload["id"].startswith("title_"))
        events = self.parse_items([("custom", payload), ("values", result), ("custom", payload)])
        self.assertEqual([event.data.id for event in events if event.type in {"title", "token_usage"}], [payload["id"], payload["id"]])

    def test_empty_model_output_still_reports_tokens_spent(self):
        self.title_model.invoke.return_value = self.response.model_copy(update={"content": ""})
        with patch("deerflow.agents.middlewares.title_middleware.create_chat_model", return_value=self.title_model), patch("deerflow.agents.middlewares.title_middleware.emit_custom_event") as emit:
            result = self.middleware.after_model(self.state, None)
        self.assertEqual(result["title"], "请整理季度研发工作进展")
        events = self.parse_items([("custom", emit.call_args.args[0]), ("values", result)])
        self.assertEqual([event.data.id for event in events if event.type in {"title", "token_usage"}], ["title_message", "title_message"])

    def test_local_snapshot_id_is_independent_and_has_no_usage(self):
        events = self.parse_items([("values", {"title": "本地标题"}), ("values", {"title": "本地标题"})])
        titles = [event for event in events if event.type == "title"]
        self.assertEqual(len(titles), 1)
        self.assertNotEqual(titles[0].data.id, "thread_001")
        self.assertFalse(any(event.type == "token_usage" for event in events))

    def test_missing_provider_usage_is_not_invented_or_priced(self):
        events = self.parse_items([("custom", {"type": "title_generated", "id": "title_message", "title": "标题", "model_name": "title_model"})])
        self.assertFalse(any(event.type == "token_usage" for event in events))
        self.assertEqual(events[-1].data.cost.status, "unpriced")

    def test_sync_observer_receives_success_and_failure(self):
        from deerflow_extension_api import SystemOperationKind

        def dispatch(coro, what):
            asyncio.run(coro)

        extensions = SimpleNamespace(has_system_model_observers=True)
        for error in (None, RuntimeError("模拟异常")):
            invoke = Mock(return_value=self.response, side_effect=error)
            with patch("deerflow.extensions.notify.dispatch_system_model_observation", side_effect=dispatch), patch("deerflow.extensions.notify.notify_system_model_call", new_callable=AsyncMock) as notify:
                arguments = dict(messages="标题提示词", model_name="title_model", invoke_config={}, invoke=invoke)
                if error:
                    with self.assertRaises(RuntimeError):
                        observe_system_model_call_sync(extensions, SystemOperationKind.TITLE, **arguments)
                else:
                    self.assertIs(observe_system_model_call_sync(extensions, SystemOperationKind.TITLE, **arguments), self.response)
                observation = notify.call_args.args[4]
                self.assertIs(observation.error, error)
                self.assertIs(observation.response, None if error else self.response)


if __name__ == "__main__":
    unittest.main()
