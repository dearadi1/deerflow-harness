"""从 agent_client 公共入口验证路由、真实图组装、存储与取消生命周期。"""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from collections import deque
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from deerflow.agent_client import DeerFlowClientPool
from deerflow.agents.lead_agent.agent import _create_todo_list_middleware
from deerflow.agents.routing_context import request_size
from deerflow.client import DeerFlowClient
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelPriceConfig
from deerflow.runtime.checkpoint_state import CheckpointStateAccessor
from deerflow.runtime.client_replay import ReplayAccessError


def decision(mode="thinking", **overrides):
    return {"mode": mode, "needs_vision": False, "context_sufficient": True, "continuation": False, "missing_references": [], "summary": "当前任务摘要", **overrides}


class MainModel(BaseChatModel):
    bound_tools: list[str] = Field(default_factory=list)
    usage_payload: dict = Field(default_factory=lambda: {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    @property
    def _llm_type(self):
        return "auto-routing-test"

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        question = next(message.content for message in reversed(messages) if isinstance(message, HumanMessage))
        if question == "模拟主模型失败":
            raise RuntimeError("模拟主模型失败")
        answer = AIMessage(id=uuid.uuid4().hex, content=f"答复：{question}", usage_metadata=self.usage_payload)
        return ChatResult(generations=[ChatGeneration(message=answer)])


class AnalysisModel:
    def __init__(self):
        self.responses = deque()
        self.calls = []
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self.block = False
        self.usage = {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}

    async def ainvoke(self, messages, config):
        self.calls.append(deepcopy(messages))
        self.entered.set()
        try:
            while self.block:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        response = self.responses.popleft() if self.responses else decision()
        content = json.dumps(response, ensure_ascii=False) if isinstance(response, dict) else response
        return AIMessage(id=uuid.uuid4().hex, content=content, usage_metadata=self.usage)


class AgentClientAutoRoutingTests(unittest.TestCase):
    def setUp(self):
        self.mode = os.environ.get("DEERFLOW_ROUTING_TEST_MODE", "full")
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"DEER_FLOW_HOME": self.directory}))
        self.app = AppConfig.model_validate({
            "models": [
                {"name": "quick", "model": "quick", "use": "test.Model"},
                {"name": "deep", "model": "deep", "use": "test.Model", "supports_thinking": True},
                {"name": "vision", "model": "vision", "use": "test.Model", "supports_thinking": True, "supports_vision": True},
                {"name": "required", "model": "required", "use": "test.Model", "reasoning": {"thinking": "required", "on_disable_request": "reject"}},
            ],
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "database": {"checkpoint_channel_mode": self.mode},
            "auto_routing": {"enabled": True, "model_name": "quick", "routes": {
                "fast": {"text": "quick", "vision": "vision"},
                "thinking": {"text": "deep", "vision": "vision"},
                "plan": {"text": "deep", "vision": "vision"},
                "subagent": {"text": "deep", "vision": "vision"},
            }},
        })
        self.saver = InMemorySaver()
        self.analysis = AnalysisModel()
        self.clients = []
        self.models = []
        self.assemblies = []
        self.main_usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        self.enterContext(patch("deerflow.client.get_app_config", return_value=self.app))
        self.enterContext(patch("deerflow.client.build_tracing_callbacks", return_value=[]))
        self.enterContext(patch("deerflow.client.inject_langfuse_metadata"))
        self.enterContext(patch("deerflow.sandbox.lease.release_sandbox_execution_lease"))
        self.enterContext(patch("deerflow.client.get_enabled_skills_for_config", return_value=[]))
        self.enterContext(patch("deerflow.client.build_skill_search_setup", return_value=SimpleNamespace(describe_skill_tool=None, skill_names=[])))
        self.enterContext(patch("deerflow.agents.task_continuity.tools.append_task_continuity_tools"))
        self.enterContext(patch("deerflow.client.apply_prompt_template", return_value="测试系统提示词"))
        self.enterContext(patch("deerflow.client.build_middlewares", side_effect=self.middlewares))
        self.enterContext(patch.object(DeerFlowClient, "_get_tools", side_effect=self.tools))
        self.enterContext(patch("deerflow.client.create_chat_model", side_effect=self.main_model))
        self.enterContext(patch("deerflow.agents.auto_routing.create_chat_model", return_value=self.analysis))
        self.pool = DeerFlowClientPool(size=1, client_factory=self.client)
        self.addCleanup(self.pool.close)

    def client(self):
        client = DeerFlowClient(checkpointer=self.saver)
        self.clients.append(client)
        return client

    def main_model(self, **kwargs):
        model = MainModel(usage_payload=self.main_usage)
        self.models.append((kwargs, model))
        return model

    def middlewares(self, config, **kwargs):
        self.assemblies.append(deepcopy(config["configurable"]))
        middleware = _create_todo_list_middleware(config["configurable"].get("is_plan_mode", False))
        return [middleware] if middleware else []

    @staticmethod
    def tools(*, subagent_enabled, **kwargs):
        return [StructuredTool.from_function(lambda: "完成", name="task", description="测试委派工具")] if subagent_enabled else []

    def run_turn(self, message="测试问题", **kwargs):
        options = {"thread_id": "thread_001", "user_id": "user_001", "auto_routing_enabled": True, **kwargs}
        with self.pool.start_stream(message, **options) as stream:
            return list(stream)

    def snapshot(self):
        client = self.clients[-1]
        accessor = CheckpointStateAccessor.bind(client._agent, self.saver, mode=self.mode)
        return accessor, accessor.get({"configurable": {"thread_id": "thread_001"}})

    def seed_history(self, messages, **fields):
        self.run_turn("初始化", auto_routing_enabled=False)
        accessor, _ = self.snapshot()
        accessor.update({"configurable": {"thread_id": "thread_001"}}, {"messages": messages, **fields})

    def assert_setting(self, events, model, flags):
        self.assertTrue(events)
        expected = {"model": model, "thinking_enabled": flags[0], "is_plan_mode": flags[1], "subagent_enabled": flags[2]}
        self.assertTrue(all(event.agent_setting.model_dump() == expected for event in events))
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(self.models[-1][0]["name"], model)
        self.assertEqual(self.models[-1][0]["thinking_enabled"], flags[0])

    def test_each_mode_overrides_manual_inputs_and_changes_actual_tools(self):
        for index, mode in enumerate(("fast", "thinking", "plan", "subagent")):
            with self.subTest(mode=mode):
                self.analysis.responses.append(decision(mode))
                events = self.run_turn(model_name="nonexistent", thinking_enabled=False, plan_mode=False, subagent_enabled=False)
                self.assert_setting(events, "quick" if index == 0 else "deep", (index >= 1, index >= 2, index >= 3))
                bound = self.models[-1][1].bound_tools
                self.assertEqual("write_todos" in bound, index >= 2)
                self.assertEqual("task" in bound, index >= 3)
                self.assertNotIn('"mode"', "".join(event.data.content for event in events if event.type == "answer_content"))

    def test_both_switches_are_required(self):
        for enabled, requested in ((False, False), (False, True), (True, False)):
            with self.subTest(enabled=enabled, requested=requested):
                self.app.auto_routing.enabled = enabled
                events = self.run_turn(auto_routing_enabled=requested, model_name="deep", plan_mode=True)
                self.assert_setting(events, "deep", (True, True, False))
        self.assertEqual(self.analysis.calls, [])

    def test_session_stream_and_chat_use_public_parameters_and_reset_next_turn(self):
        self.analysis.responses.append(decision("plan"))
        with self.pool.session(user_id="user_001") as session:
            events = list(session.stream("规划", thread_id="thread_001", auto_routing_enabled=True))
            self.assert_setting(events, "deep", (True, True, False))
            self.analysis.responses.append(decision("fast"))
            self.assertEqual(session.chat("你好", thread_id="thread_001", auto_routing_enabled=True), "答复：你好")
            events = list(session.stream("手动", thread_id="thread_001", model_name="deep", thinking_enabled=False))
            self.assert_setting(events, "deep", (False, False, False))
        self.assertEqual(len(self.analysis.calls), 2)

    def test_manual_unsupported_thinking_downgrades_all_modes_without_error(self):
        events = self.run_turn(auto_routing_enabled=False, model_name="quick", subagent_enabled=True)
        self.assert_setting(events, "quick", (False, False, False))

    def test_required_thinking_uses_compatible_parameters_even_when_disabled(self):
        events = self.run_turn(auto_routing_enabled=False, model_name="required", thinking_enabled=False)
        self.assert_setting(events, "required", (True, False, False))

    def test_route_with_only_non_thinking_models_downgrades_without_error(self):
        self.app.models = self.app.models[:1]
        self.analysis.responses.append(decision("subagent"))
        events = self.run_turn()
        self.assert_setting(events, "quick", (False, False, False))

    def test_disabled_delegation_tools_remain_disabled_and_cache_reports_effective_setting(self):
        for _ in range(2):
            self.analysis.responses.append(decision("subagent"))
            events = self.run_turn(disabled_tools=["task", "batch_task"])
            self.assert_setting(events, "deep", (True, True, False))
        self.assertEqual(len(self.models), 1)

    def test_current_image_selects_vision_without_sending_image_bytes_to_analysis(self):
        self.analysis.responses.append(decision("thinking"))
        events = self.run_turn("分析图片", files=[{"filename": "image.png", "size": 30, "data": "secret_base64"}])
        self.assert_setting(events, "vision", (True, False, False))
        self.assertNotIn("secret_base64", self.analysis.calls[0][1].content)

    def test_image_continuation_retains_vision_and_new_topic_can_drop_it(self):
        self.run_turn("看图片", files=[{"filename": "figure.png"}])
        self.analysis.responses.append(decision(continuation=True))
        self.assert_setting(self.run_turn("继续分析上图"), "vision", (True, False, False))
        self.analysis.responses.append(decision("fast", continuation=False))
        self.assert_setting(self.run_turn("换个话题，问好"), "quick", (False, False, False))

    def test_authorization_fallback_recomputes_model_capabilities(self):
        self.analysis.responses.append(decision("subagent"))
        with patch("deerflow.client._authorize_model_name", return_value="quick"):
            self.assert_setting(self.run_turn(), "quick", (False, False, False))

    def test_unresolved_continuation_keeps_previous_plan_without_more_than_two_calls(self):
        self.analysis.responses.append(decision("plan", summary="已有复杂计划"))
        self.run_turn()
        self.analysis.responses.extend([decision("fast", continuation=True, context_sufficient=False, missing_references=["第二步"])] * 2)
        self.assert_setting(self.run_turn("继续第二步"), "deep", (True, True, False))
        self.assertEqual(len(self.analysis.calls), 3)

    def test_replay_explicit_empty_files_clears_inherited_vision(self):
        self.run_turn("原图", files=[{"filename": "figure.png"}])
        self.analysis.responses.append(decision("fast"))
        self.assert_setting(self.run_turn("仅问好", replay_last_turn=True, files=[]), "quick", (False, False, False))
        self.assertNotIn("figure.png", self.analysis.calls[-1][1].content)

    def test_long_history_and_large_question_stay_within_budget(self):
        messages = []
        for index in range(140):
            messages.extend([HumanMessage(content=f"历史{index}" + "数据" * 2000), AIMessage(content="旧答复" * 2000)])
        self.seed_history(messages, summary_text="已有会话摘要")
        self.run_turn("处理长材料" + "信息" * 10000)
        self.assertLessEqual(request_size(self.analysis.calls[0]), self.app.auto_routing.context.input_token_budget)
        self.assertNotIn("历史0数据", self.analysis.calls[0][1].content)
        self.assertIn("已截断", self.analysis.calls[0][1].content)

    def test_missing_reference_expands_once_and_accounts_both_analysis_calls(self):
        messages = [HumanMessage(content="历史蓝图约束：必须离线运行"), AIMessage(content="已记录蓝图")]
        for index in range(8):
            messages.extend([HumanMessage(content=f"后续事项{index}"), AIMessage(content="后续答复")])
        self.seed_history(messages)
        self.analysis.responses.extend([decision(context_sufficient=False, continuation=True, missing_references=["历史蓝图"]), decision("plan", continuation=True)])
        events = self.run_turn("按历史蓝图继续")
        self.assertEqual(len(self.analysis.calls), 2)
        self.assertIn("必须离线运行", json.loads(self.analysis.calls[1][1].content)["related_history"])
        self.assertLessEqual(request_size(self.analysis.calls[1]), self.app.auto_routing.context.expanded_input_token_budget)
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 21)

    def test_summary_is_persisted_and_new_topic_replaces_it(self):
        self.analysis.responses.append(decision("plan", summary="项目甲：离线运行，尚待验证"))
        self.run_turn("项目甲")
        self.analysis.responses.append(decision("fast", summary="新任务：翻译标题"))
        self.run_turn("换个话题，翻译标题")
        self.assertIn("项目甲", json.loads(self.analysis.calls[1][1].content)["task_summary"])
        _, snapshot = self.snapshot()
        self.assertEqual(snapshot.values["auto_routing"]["summary"], "新任务：翻译标题")

    def test_malformed_analysis_falls_back_and_keeps_consumed_usage(self):
        self.analysis.responses.append("invalid json")
        events = self.run_turn()
        self.assert_setting(events, "deep", (True, False, False))
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 18)

    def configure_prices(self):
        self.app.models[0].price = ModelPriceConfig(price_input_normal=10, price_input_cache_hit=5, price_output_normal=20)
        self.app.models[1].price = ModelPriceConfig(price_input_normal=40, price_input_cache_hit=10, price_output_normal=60)
        self.analysis.usage["input_token_details"] = {"cache_read": 1}
        self.main_usage["input_token_details"] = {"cache_read": 2}

    def test_analysis_and_main_model_use_their_own_prices_and_sum_two_calls(self):
        self.configure_prices()
        self.analysis.responses.extend([decision(context_sufficient=False, missing_references=["前一步"]), decision("plan")])
        events = self.run_turn()
        usage = [event.data for event in events if event.type == "token_usage"]
        self.assertEqual([entry.model_name for entry in usage], ["quick", "quick", "deep"])
        self.assertEqual([entry.cost.total_cost for entry in usage], [Decimal("0.000035"), Decimal("0.000035"), Decimal("0.000640")])
        self.assertEqual(len({entry.id for entry in usage}), 3)
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 21)
        self.assertEqual(events[-1].data.cost.total_cost, Decimal("0.000710"))

    def test_invalid_analysis_output_is_still_priced_for_the_analysis_model(self):
        self.configure_prices()
        self.analysis.responses.append("invalid json")
        events = self.run_turn()
        self.assertEqual(events[0].data.model_name, "quick")
        self.assertEqual(events[0].data.cost.total_cost, Decimal("0.000035"))
        self.assertEqual(events[-1].data.cost.total_cost, Decimal("0.000675"))

    def test_missing_analysis_usage_does_not_fabricate_tokens_or_complete_cost(self):
        self.configure_prices()
        self.analysis.usage = None
        events = self.run_turn()
        usage = [event.data for event in events if event.type == "token_usage"]
        self.assertEqual([entry.model_name for entry in usage], ["deep"])
        self.assertEqual(events[-1].data.usage_metadata.total_tokens, 15)
        self.assertEqual(events[-1].data.cost.status, "unpriced")
        self.assertIsNone(events[-1].data.cost.total_cost)

    def test_timeout_cancels_analysis_then_runs_fallback(self):
        self.analysis.block = True
        self.app.auto_routing.timeout_seconds = 0.03
        started = time.monotonic()
        events = self.run_turn()
        self.assertTrue(self.analysis.cancelled.is_set())
        self.assertLess(time.monotonic() - started, 3)
        self.assert_setting(events, "deep", (True, False, False))

    def test_cancel_during_analysis_prevents_agent_creation_and_returns_capacity(self):
        self.analysis.block = True
        stream = self.pool.start_stream("等待分析", user_id="user_001", thread_id="thread_001", auto_routing_enabled=True)
        self.assertTrue(self.analysis.entered.wait(3))
        stream.close(timeout=3)
        self.assertTrue(self.analysis.cancelled.is_set())
        self.assertEqual(self.models, [])
        self.analysis.block = False
        self.assert_setting(self.run_turn("新的问题", auto_routing_enabled=False), "quick", (False, False, False))

    def test_replay_analyzes_baseline_and_inherits_files(self):
        self.run_turn("旧问题标记", files=[{"filename": "photo.png"}])
        events = self.run_turn("替换问题", replay_last_turn=True)
        self.assert_setting(events, "vision", (True, False, False))
        prompt = self.analysis.calls[1][1].content
        self.assertNotIn("旧问题标记", prompt)
        self.assertIn("photo.png", prompt)
        _, snapshot = self.snapshot()
        users = [message for message in snapshot.values["messages"] if isinstance(message, HumanMessage)]
        self.assertEqual([message.content for message in users], ["替换问题"])

    def test_cancelled_replay_analysis_preserves_original_checkpoint(self):
        self.run_turn("原始问题")
        _, original = self.snapshot()
        self.analysis.entered.clear()
        self.analysis.block = True
        stream = self.pool.start_stream("被取消的替换", user_id="user_001", thread_id="thread_001", replay_last_turn=True, auto_routing_enabled=True)
        self.assertTrue(self.analysis.entered.wait(3))
        stream.close(timeout=3)
        _, current = self.snapshot()
        self.assertEqual(current.config, original.config)
        self.assertEqual(current.values, original.values)

    def test_thread_ownership_is_checked_before_sending_history_to_analysis(self):
        self.analysis.responses.append(decision(summary="用户甲私有任务"))
        self.run_turn("用户甲私有输入")
        with self.assertRaises(ReplayAccessError):
            self.run_turn("用户乙", user_id="user_002")
        self.assertEqual(len(self.analysis.calls), 1)
        self.run_turn("用户乙新会话", user_id="user_002", thread_id="thread_002")
        self.assertNotIn("用户甲私有", self.analysis.calls[-1][1].content)

    def test_invalid_enable_parameter_is_rejected_at_pool_entry(self):
        with self.assertRaises(TypeError):
            self.pool.start_stream("问题", user_id="user_001", auto_routing_enabled="true")
        self.assertEqual(self.analysis.calls, [])

    def test_error_event_preserves_resolved_setting_and_consumer_mutation_is_isolated(self):
        with self.pool.session(user_id="user_001") as session:
            events = list(session.stream("模拟主模型失败", thread_id="thread_001", auto_routing_enabled=True))
        self.assertEqual(events[-1].type, "error")
        self.assertEqual(events[-1].agent_setting.model, "deep")
        events[0].agent_setting.model = "consumer_changed"
        self.assertEqual(events[-1].agent_setting.model, "deep")


if __name__ == "__main__":
    unittest.main()
