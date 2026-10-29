"""验证本轮工具禁用覆盖延迟目录、中间件声明及客户端缓存。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.tools import StructuredTool

from deerflow.client import DeerFlowClient
from deerflow.config.app_config import AppConfig
from deerflow.tools.mcp_metadata import tag_mcp_tool


def make_tool(name):
    return StructuredTool.from_function(lambda: "结果", name=name, description="测试工具")


class ClientDisabledToolsTests(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})
        self.enterContext(patch("deerflow.client.get_app_config", return_value=self.config))
        self.client = DeerFlowClient(checkpointer=object())
        self.tools = [make_tool("allowed"), make_tool("blocked"), make_tool("remote_blocked")]
        tag_mcp_tool(self.tools[-1])
        self.middleware = SimpleNamespace(tools=[make_tool("middleware_blocked"), make_tool("middleware_allowed")])
        self.enterContext(patch.object(self.client, "_get_tools", return_value=self.tools))
        self.enterContext(patch("deerflow.client.get_enabled_skills_for_config", return_value=[]))
        self.enterContext(patch("deerflow.client.build_skill_search_setup", return_value=SimpleNamespace(describe_skill_tool=make_tool("describe_skill"), skill_names=[])))
        self.enterContext(patch("deerflow.agents.task_continuity.tools.append_task_continuity_tools"))
        self.enterContext(patch("deerflow.client.build_middlewares", return_value=[self.middleware]))
        self.enterContext(patch("deerflow.client.create_chat_model"))
        self.enterContext(patch("deerflow.client.apply_prompt_template", return_value="提示词"))
        self.create_agent = self.enterContext(patch("deerflow.client.create_agent", side_effect=lambda **kwargs: Mock()))

    def assemble(self, disabled_tools=None):
        config = self.client._get_runnable_config("thread_001")
        config["configurable"]["disabled_tools"] = disabled_tools
        self.client._ensure_agent(config)
        return self.create_agent.call_args.kwargs

    def test_filters_ordinary_deferred_late_and_middleware_tools_without_mutation(self):
        build = self.assemble(["blocked", "remote_blocked", "describe_skill", "middleware_blocked"])
        self.assertEqual([tool.name for tool in build["tools"]], ["allowed"])
        self.assertEqual([tool.name for tool in build["middleware"][0].tools], ["middleware_allowed"])
        self.assertEqual([tool.name for tool in self.tools], ["allowed", "blocked", "remote_blocked"])
        self.assertEqual(len(self.middleware.tools), 2)

    def test_cache_tracks_names_as_a_set_and_resets_next_turn(self):
        self.assemble(["blocked", "remote_blocked"])
        self.assemble(["remote_blocked", "blocked", "blocked"])
        self.assertEqual(self.create_agent.call_count, 1)
        build = self.assemble([])
        self.assertEqual(self.create_agent.call_count, 2)
        self.assertIn("blocked", {tool.name for tool in build["tools"]})
        self.assemble(None)
        self.assertEqual(self.create_agent.call_count, 2)

    def test_synthetic_search_tool_can_be_disabled(self):
        build = self.assemble(["tool_search"])
        self.assertNotIn("tool_search", {tool.name for tool in build["tools"]})

    def test_validation_rejects_invalid_lists(self):
        for invalid in ("blocked", ("blocked",), [None], [1]):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                list(self.client.stream("问题", disabled_tools=invalid))
        for invalid in ([""], ["  "]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                list(self.client.stream("问题", disabled_tools=invalid))

    def test_stream_replay_and_chat_forward_explicit_parameter(self):
        with patch("deerflow.client.stream_client_turn", return_value=(event for event in ())) as stream_turn:
            disabled = ["blocked", "blocked"]
            list(self.client.stream("问题", thread_id="thread_001", replay_last_turn=True, disabled_tools=disabled))
            self.assertEqual(stream_turn.call_args.kwargs["disabled_tools"], ["blocked"])
            self.assertTrue(stream_turn.call_args.kwargs["replay_last_turn"])
            self.assertEqual(disabled, ["blocked", "blocked"])
        with patch.object(self.client, "stream", return_value=iter(())) as stream:
            self.client.chat("问题", disabled_tools=["blocked"])
            self.assertEqual(stream.call_args.kwargs["disabled_tools"], ["blocked"])

    def test_stream_turn_reaches_assembly_and_does_not_reuse_previous_names(self):
        graph = SimpleNamespace(stream=Mock(return_value=iter(())))
        replacements = {
            "build_tracing_callbacks": Mock(return_value=[]),
            "inject_langfuse_metadata": Mock(),
            "ensure_checkpoint_mode_compatible": Mock(),
            "_stream_with_sandbox_lease_cleanup": lambda items, context: items,
        }
        for name, replacement in replacements.items():
            self.enterContext(patch(f"deerflow.client.{name}", replacement))
        self.enterContext(patch("deerflow.runtime.client_replay.ClientTurnReplay.prepare"))
        with patch.object(self.client, "_ensure_agent") as ensure_agent:
            self.client._agent = graph
            list(self.client.stream("问题", thread_id="thread_001", disabled_tools=["blocked"]))
            self.assertEqual(ensure_agent.call_args.args[0]["configurable"]["disabled_tools"], ["blocked"])
            list(self.client.stream("追问", thread_id="thread_001"))
            self.assertEqual(ensure_agent.call_args.args[0]["configurable"]["disabled_tools"], [])

    def test_tui_adapter_forwards_names(self):
        from deerflow.tui.runtime import stream_actions

        client = SimpleNamespace(stream=Mock(return_value=iter(())))
        list(stream_actions(client, "问题", thread_id="thread_001", disabled_tools=["blocked"]))
        self.assertEqual(client.stream.call_args.kwargs["disabled_tools"], ["blocked"])
