"""通过同步池入口和真实 LangGraph 托管执行验证预设生命周期。"""

import asyncio
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from deerflow.agent_client import DeerFlowClientPool
from deerflow.agents.thread_state import get_thread_state_schema
from deerflow.client import DeerFlowClient
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.skills.presets import current_public_skill_preset, public_skill_preset_scope
from deerflow.skills.storage import UserScopedSkillStorage
from test_skill_presets import PresetFixture, write_skill


class ClientSkillPresetTests(PresetFixture):
    def setUp(self):
        super().setUp()
        self.create_choices()
        self.observations = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.addCleanup(self.release.set)
        self.enterContext(patch("deerflow.client.get_app_config", return_value=self.config))
        self.enterContext(patch("deerflow.client.build_tracing_callbacks", return_value=[]))
        self.enterContext(patch("deerflow.client.inject_langfuse_metadata"))
        self.enterContext(patch("deerflow.sandbox.lease.release_sandbox_execution_lease"))
        self.saver = InMemorySaver()

    def observation(self):
        preset = current_public_skill_preset()
        user = get_effective_user_id()
        skills = UserScopedSkillStorage(user, app_config=self.config).load_skills(enabled_only=True)
        result = (user, preset.preset_id, frozenset(skill.name for skill in skills))
        self.observations.append(result)
        return {"messages": [AIMessage(content=preset.preset_id)]}

    async def answer(self, state):
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return self.observation()

    def client(self, *, synchronous=False):
        client = DeerFlowClient(checkpointer=self.saver)
        graph = StateGraph(get_thread_state_schema("full", 10))
        graph.add_node("answer", (lambda state: self.observation()) if synchronous else self.answer)
        graph.add_edge(START, "answer")
        graph.add_edge("answer", END)
        client._agent = graph.compile(checkpointer=self.saver)
        client._ensure_agent = Mock()
        return client

    def pool(self, *, synchronous=False, size=2):
        pool = DeerFlowClientPool(size=size, client_factory=lambda: self.client(synchronous=synchronous))
        self.addCleanup(pool.close)
        return pool

    def test_recovered_empty_default_supports_repeated_and_concurrent_streams(self):
        write_skill(self.paths.user_custom_skills_dir("alice"), "personal")
        self.file.write_bytes(b"{")
        synchronous = self.pool(synchronous=True)
        with synchronous.session(user_id="alice") as client:
            for _ in range(2):
                list(client.stream("默认空预设", thread_id="recovered_sync"))
        managed = self.pool()
        with managed.start_stream("甲", user_id="alice", thread_id="recovered_a") as first:
            with managed.start_stream("乙", user_id="bob", thread_id="recovered_b") as second:
                list(first)
                list(second)
        self.assertEqual(self.observations[:2], [("alice", "default", {"personal"})] * 2)
        self.assertCountEqual(self.observations[2:], [("alice", "default", {"personal"}), ("bob", "default", set())])
        for pool in (synchronous, managed):
            self.assertEqual(pool.stats["borrowed"], 0)
            self.assertEqual(pool.stats["available"], 2)
        self.assertEqual(len(list(self.root.glob("skill_presets.json.invalid-*.bak"))), 1)

    def test_session_stream_selects_preset_and_omission_uses_current_default(self):
        pool = self.pool(synchronous=True, size=1)
        with pool.session(user_id="alice") as client:
            list(client.stream("第一轮", thread_id="session_1", skill_preset=self.preset_two))
            self.assertIsNone(current_public_skill_preset())
            list(client.stream("第二轮", thread_id="session_1"))
            self.store.set_default_preset(self.preset_two)
            list(client.stream("第三轮", thread_id="session_1"))
        self.assertEqual(self.observations, [("alice", self.preset_two, {"beta"}), ("alice", self.preset_one, {"alpha"}), ("alice", self.preset_two, {"beta"})])

    def test_sync_generators_do_not_leak_preset_between_yields_or_close(self):
        first = self.client(synchronous=True).stream("甲", thread_id="sync_1", skill_preset=self.preset_one)
        second = self.client(synchronous=True).stream("乙", thread_id="sync_2", skill_preset=self.preset_two)
        try:
            next(first)
            self.assertIsNone(current_public_skill_preset())
            list(second)
            list(first)
        finally:
            first.close()
            second.close()
        self.assertIsNone(current_public_skill_preset())
        self.assertEqual({item[1] for item in self.observations}, {self.preset_one, self.preset_two})

    def test_managed_concurrent_runs_keep_snapshot_while_file_changes(self):
        pool = self.pool()
        self.release.clear()
        first = pool.start_stream("甲", user_id="alice", thread_id="managed_1", skill_preset=self.preset_one)
        second = pool.start_stream("乙", user_id="bob", thread_id="managed_2", skill_preset=self.preset_two)
        try:
            self.assertTrue(self.entered.wait(5))
            self.store.update_preset(self.preset_one, skill_states={"alpha": False, "beta": True})
            self.release.set()
            list(first)
            list(second)
        finally:
            self.release.set()
            first.close(timeout=5)
            second.close(timeout=5)
        self.assertIn(("alice", self.preset_one, {"alpha"}), self.observations)
        self.assertIn(("bob", self.preset_two, {"beta"}), self.observations)
        with pool.start_stream("新轮次", user_id="alice", thread_id="managed_1") as stream:
            list(stream)
        self.assertEqual(self.observations[-1], ("alice", self.preset_one, {"beta"}))

    def test_cancelled_run_releases_snapshot_before_same_thread_reentry(self):
        pool = self.pool(size=1)
        self.release.clear()
        stream = pool.start_stream("等待", user_id="alice", thread_id="cancel_1", skill_preset=self.preset_two)
        try:
            self.assertTrue(self.entered.wait(5))
            stream.close(timeout=5)
        finally:
            self.release.set()
        with pool.start_stream("继续", user_id="alice", thread_id="cancel_1") as following:
            list(following)
        self.assertEqual(self.observations, [("alice", self.preset_one, {"alpha"})])

    def test_unknown_preset_fails_and_pool_remains_usable(self):
        pool = self.pool(size=1)
        with pool.start_stream("无效", user_id="alice", skill_preset="missing") as stream:
            with self.assertRaises(ValueError):
                list(stream)
        with pool.start_stream("有效", user_id="alice") as stream:
            list(stream)
        self.assertEqual(self.observations[-1][1], self.preset_one)

    def test_invalid_preset_arguments_do_not_leak_pool_slots_or_context(self):
        pool = self.pool(synchronous=True, size=1)
        for preset_id in ("", "../invalid", "无效", "x" * 65, 1):
            with self.subTest(preset_id=preset_id):
                with self.assertRaises((TypeError, ValueError)):
                    pool.start_stream("无效", user_id="alice", skill_preset=preset_id)
                with pool.session(user_id="alice") as client:
                    with self.assertRaises((TypeError, ValueError)):
                        list(client.stream("无效", skill_preset=preset_id))
                self.assertEqual(pool.stats["borrowed"], 0)
                self.assertEqual(pool.stats["available"], 1)
                self.assertIsNone(current_public_skill_preset())
        with pool.session(user_id="alice") as client:
            list(client.stream("有效", skill_preset=self.preset_two))
        self.assertEqual(self.observations[-1][1], self.preset_two)

    def test_chat_uses_requested_preset_then_returns_to_default(self):
        pool = self.pool(synchronous=True, size=1)
        with pool.session(user_id="alice") as client:
            self.assertEqual(client.chat("显式选择", skill_preset=self.preset_two), self.preset_two)
            self.assertEqual(client.chat("默认选择"), self.preset_one)
        self.assertEqual([item[1] for item in self.observations], [self.preset_two, self.preset_one])

    def test_replay_selects_requested_preset_without_inheriting_previous_choice(self):
        pool = self.pool(synchronous=True, size=1)
        with pool.session(user_id="alice") as client:
            list(client.stream("原始输入", thread_id="preset_replay", skill_preset=self.preset_two))
            list(client.stream("替换输入", thread_id="preset_replay", replay_last_turn=True, skill_preset=self.preset_one))
            self.store.set_default_preset(self.preset_two)
            list(client.stream("再次替换", thread_id="preset_replay", replay_last_turn=True))
        self.assertEqual([item[1] for item in self.observations], [self.preset_two, self.preset_one, self.preset_two])

    def test_agent_cache_rebuilds_for_preset_and_content_revision(self):
        client = DeerFlowClient(checkpointer=self.saver)
        self.enterContext(patch.object(client, "_get_tools", return_value=[]))
        self.enterContext(patch("deerflow.client.get_enabled_skills_for_config", return_value=[]))
        self.enterContext(patch("deerflow.client.build_skill_search_setup", return_value=SimpleNamespace(describe_skill_tool=None, skill_names=[])))
        self.enterContext(patch("deerflow.agents.task_continuity.tools.append_task_continuity_tools"))
        self.enterContext(patch("deerflow.client.build_middlewares", return_value=[]))
        self.enterContext(patch("deerflow.client.create_chat_model"))
        self.enterContext(patch("deerflow.client.apply_prompt_template", return_value="提示词"))
        create = self.enterContext(patch("deerflow.client.create_agent"))
        for preset_id in (self.preset_one, self.preset_one, self.preset_two):
            config = client._get_runnable_config("cache_1")
            with public_skill_preset_scope(self.store.resolve(preset_id)):
                client._ensure_agent(config)
            self.assertEqual(config["metadata"]["skill_preset"], preset_id)
        self.assertEqual(create.call_count, 2)
        self.store.update_preset(self.preset_two, skill_states={"alpha": True})
        with public_skill_preset_scope(self.store.resolve(self.preset_two)):
            client._ensure_agent(client._get_runnable_config("cache_1"))
        self.assertEqual(create.call_count, 3)

    def test_legacy_public_toggle_rejected_but_private_entry_still_works(self):
        client = DeerFlowClient(checkpointer=self.saver)
        write_skill(self.paths.user_custom_skills_dir("alice"), "personal")
        with patch("deerflow.client.get_or_new_user_skill_storage", return_value=self.user):
            with self.assertRaisesRegex(ValueError, "预设"):
                client.update_skill("alpha", enabled=False)
            self.assertFalse(client.update_skill("personal", enabled=False)["enabled"])
        self.assertEqual(self.store.resolve(self.preset_one).enabled_skills, {"alpha"})

    def test_agent_prompt_and_describe_tool_share_effective_preset(self):
        self.config.skills.deferred_discovery = True
        client = DeerFlowClient(checkpointer=self.saver)
        self.enterContext(patch.object(client, "_get_tools", return_value=[]))
        self.enterContext(patch("deerflow.agents.task_continuity.tools.append_task_continuity_tools"))
        self.enterContext(patch("deerflow.client.build_middlewares", return_value=[]))
        self.enterContext(patch("deerflow.client.create_chat_model"))
        prompt = self.enterContext(patch("deerflow.client.apply_prompt_template", return_value="提示词"))
        create = self.enterContext(patch("deerflow.client.create_agent"))
        for preset_id, selected, absent in ((self.preset_one, "alpha", "beta"), (self.preset_two, "beta", "alpha")):
            with public_skill_preset_scope(self.store.resolve(preset_id)):
                client._ensure_agent(client._get_runnable_config("discovery_1"))
            self.assertEqual(prompt.call_args.kwargs["skill_names"], {selected})
            describe = next(tool for tool in create.call_args.kwargs["tools"] if tool.name == "describe_skill")
            command = describe.invoke({"type": "tool_call", "id": "describe_1", "name": "describe_skill", "args": {"name": "select:alpha,beta"}})
            content = command.update["messages"][0].content
            self.assertIn(f"## Skill: {selected}\n", content)
            self.assertNotIn(f"## Skill: {absent}\n", content)

    def cached_skill_client(self):
        self.config.skills.deferred_discovery = True
        client = DeerFlowClient(checkpointer=self.saver)
        self.enterContext(patch.object(client, "_get_tools", return_value=[]))
        self.enterContext(patch("deerflow.agents.task_continuity.tools.append_task_continuity_tools"))
        self.enterContext(patch("deerflow.client.build_middlewares", return_value=[]))
        self.enterContext(patch("deerflow.client.create_chat_model"))
        prompt = self.enterContext(patch("deerflow.client.apply_prompt_template", return_value="提示词"))
        create = self.enterContext(patch("deerflow.client.create_agent"))
        return client, prompt, create

    def assemble_skills(self, client, prompt, *, user="alice"):
        config = client._get_runnable_config("private_cache")
        config["configurable"]["user_id"] = user
        with public_skill_preset_scope(self.store.resolve(self.preset_two)):
            client._ensure_agent(config)
        return prompt.call_args.kwargs["skill_names"]

    def test_cached_agent_observes_same_size_private_state_edit_without_invalidation(self):
        write_skill(self.paths.user_custom_skills_dir("alice"), "alpha")
        self.user.set_skill_enabled_state("alpha", True)
        states = self.user._skill_states_file
        states.write_text('{"alpha":{"enabled":true }}')
        client, prompt, create = self.cached_skill_client()
        self.assertEqual(self.assemble_skills(client, prompt), {"alpha", "beta"})
        self.assemble_skills(client, prompt)
        self.assertEqual(create.call_count, 1)
        original_stat = states.stat()
        states.write_text('{"alpha":{"enabled":false}}')
        os.utime(states, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertEqual(self.assemble_skills(client, prompt), {"beta"})
        self.assertEqual(create.call_count, 2)
        self.user.set_skill_enabled_state("alpha", True)
        self.assertEqual(self.assemble_skills(client, prompt), {"alpha", "beta"})
        self.assertEqual(create.call_count, 3)

    def test_all_cached_clients_observe_peer_process_private_toggle(self):
        write_skill(self.paths.user_custom_skills_dir("alice"), "personal")
        write_skill(self.paths.user_custom_skills_dir("bob"), "personal")
        self.user.set_skill_enabled_state("personal", True)
        client, prompt, create = self.cached_skill_client()
        another = DeerFlowClient(checkpointer=self.saver)
        self.enterContext(patch.object(another, "_get_tools", return_value=[]))
        for selected in (client, another):
            self.assertEqual(self.assemble_skills(selected, prompt), {"beta", "personal"})
        process = subprocess.run([
            sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('{\"personal\":{\"enabled\":false}}')",
            str(self.user._skill_states_file),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        for selected in (client, another):
            self.assertEqual(self.assemble_skills(selected, prompt), {"beta"})
        self.assertEqual(create.call_count, 4)
        self.assertEqual(self.assemble_skills(client, prompt, user="bob"), {"beta", "personal"})

    def test_management_mutations_rebuild_cached_catalog_without_manual_invalidation(self):
        from deerflow.skill_client import DeerFlowSkillClient

        with patch("deerflow.skill_client.load_client_config", return_value=self.config):
            manager = DeerFlowSkillClient()
        client, prompt, create = self.cached_skill_client()
        self.assertEqual(self.assemble_skills(client, prompt), {"beta"})
        manager.create_skill("alice", "personal", "私有测试")
        self.assertEqual(self.assemble_skills(client, prompt), {"beta", "personal"})
        manager.set_skill_enabled("alice", "personal", False)
        self.assertEqual(self.assemble_skills(client, prompt), {"beta"})
        manager.set_skill_enabled("alice", "personal", True)
        self.assertEqual(self.assemble_skills(client, prompt), {"beta", "personal"})
        manager.delete_skill("alice", "personal")
        self.assertEqual(self.assemble_skills(client, prompt), {"beta"})
        self.assertEqual(create.call_count, 5)
