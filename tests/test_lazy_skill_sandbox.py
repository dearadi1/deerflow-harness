"""验证首块前无沙盒 I/O，首次工具访问仍落实预设、复用、并发和环境隔离。"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from deerflow.agent_client import DeerFlowClientPool
from deerflow.client import DeerFlowClient
from deerflow.environment import EnvironmentStore, temporary_environment_scope
from deerflow.sandbox import tools
from deerflow.sandbox.exceptions import SandboxAuthorizationError, SandboxRuntimeError
from deerflow.sandbox.lazy_init import get_lazy_sandbox_initialization
from deerflow.sandbox.lease import discard_sandbox_lease_manager, release_sandbox_execution_lease, release_sandbox_execution_lease_async
from deerflow.sandbox.local.local_sandbox import LocalSandbox, PathMapping
from deerflow.sandbox.middleware import SandboxMiddleware
from deerflow.sandbox.overwrite import unwrap_sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider
from deerflow.skills.presets import public_skill_preset_scope
from test_skill_presets import PresetFixture


class ProjectionProvider(SandboxProvider):
    """用独立文件映射验证同步顺序；不把本地 shell 当作容器安全边界。"""

    supports_agent_skill_isolation = True

    def __init__(self):
        self.sandboxes = {}
        self.acquisitions = []
        self.syncs = []
        self.releases = []
        self.sync_hook = None

    def acquire(self, thread_id=None, *, user_id=None):
        sandbox_id = f"probe:{user_id}:{thread_id}"
        self.acquisitions.append((user_id, thread_id))
        self.sandboxes.setdefault(sandbox_id, LocalSandbox(sandbox_id))
        return sandbox_id

    def get(self, sandbox_id):
        return self.sandboxes.get(sandbox_id)

    def get_scoped(self, sandbox_id, *, thread_id, user_id):
        if sandbox_id == f"probe:{user_id}:{thread_id}":
            return self.get(sandbox_id)
        return None

    def sync_agent_skills(self, sandbox_id, *, thread_id, user_id, projection):
        if self.sync_hook is not None:
            self.sync_hook()
        self.syncs.append((sandbox_id, frozenset(path.name for path in projection.public.iterdir())))
        self.sandboxes[sandbox_id] = LocalSandbox(sandbox_id, path_mappings=[PathMapping("/mnt/skills", str(projection.public.parent), read_only=True)])

    def release(self, sandbox_id):
        self.releases.append(sandbox_id)


class LazySkillSandboxTests(PresetFixture):
    def setUp(self):
        super().setUp()
        self.create_choices()
        self.provider = ProjectionProvider()
        self.addCleanup(discard_sandbox_lease_manager, self.provider)
        self.enterContext(patch("deerflow.sandbox.sandbox_provider._default_sandbox_provider", self.provider))
        self.enterContext(patch("deerflow.config.get_app_config", return_value=self.config))
        self.enterContext(patch("deerflow.client.get_app_config", return_value=self.config))
        self.enterContext(patch("deerflow.client.build_tracing_callbacks", return_value=[]))
        self.enterContext(patch("deerflow.client.inject_langfuse_metadata"))
        for module in ("deerflow.sandbox.middleware", "deerflow.sandbox.tools"):
            self.enterContext(patch(f"{module}.get_sandbox_provider", return_value=self.provider))
            self.enterContext(patch(f"{module}.safe_app_config", return_value=self.config))
            self.enterContext(patch(f"{module}.safe_app_config_async", return_value=self.config))
        self.enterContext(patch("deerflow.sandbox.middleware.get_initialized_sandbox_provider", return_value=self.provider))
        self.middleware = SandboxMiddleware()

    def runtime(self, thread="thread", user="alice", sandbox_id=None):
        state = {"messages": []}
        if sandbox_id is not None:
            state["sandbox"] = {"sandbox_id": sandbox_id}
        runtime = tools.Runtime(context={"thread_id": thread, "user_id": user}, state=state, config={}, stream_writer=lambda value: None, tool_call_id="call", store=None)
        self.addCleanup(release_sandbox_execution_lease, runtime.context)
        return runtime

    def prepare(self, runtime, preset_id=None, *, middleware=None):
        with public_skill_preset_scope(self.store.resolve(preset_id or self.preset_one)):
            result = (middleware or self.middleware).before_agent(runtime.state, runtime)
        self.assertIsNone(result)
        return runtime

    def client(self):
        saver = InMemorySaver()
        client = DeerFlowClient(checkpointer=saver)
        client._agent = create_agent(FakeMessagesListChatModel(responses=[AIMessage(content="你好")]), middleware=[SandboxMiddleware()], checkpointer=saver)
        client._ensure_agent = Mock()
        return client

    def test_chat_entry_emits_first_block_without_projection_or_sandbox_io(self):
        with (
            patch.object(SandboxMiddleware, "_prepare_agent_skill_projection", side_effect=AssertionError("首块前准备技能视图")),
            patch("deerflow.environment.EnvironmentStore.effective", side_effect=AssertionError("纯聊天读取沙盒环境变量")),
        ):
            for managed in (False, True):
                with self.subTest(managed=managed):
                    client = self.client()
                    client._agent.update_state({"configurable": {"thread_id": "old"}}, {"sandbox": {"sandbox_id": "old-shared-sandbox"}})
                    pool = DeerFlowClientPool(size=1, client_factory=lambda: client)
                    try:
                        for thread in ("new", "old"):
                            stream = pool.start_stream("你好", thread_id=thread, user_id="alice") if managed else client.stream("你好", thread_id=thread, user_id="alice")
                            try:
                                first = next(stream)
                                self.assertEqual(first.type, "answer_content")
                                self.assertEqual(self.provider.acquisitions, [])
                                list(stream)
                            finally:
                                stream.close()
                    finally:
                        pool.close()
        self.assertEqual(self.provider.releases, [])

    def test_first_access_uses_pinned_snapshot_and_replaces_old_shared_sandbox(self):
        runtime = self.prepare(self.runtime(sandbox_id="old-shared-sandbox"))
        self.store.update_preset(self.preset_one, skill_states={"alpha": False, "beta": True})
        sandbox = tools.ensure_sandbox_initialized(runtime)
        self.assertEqual(self.provider.syncs[-1][1], {"alpha"})
        self.assertIn("alpha", sandbox.read_file("/mnt/skills/public/alpha/SKILL.md"))
        self.assertNotEqual(runtime.state["sandbox"]["sandbox_id"], "old-shared-sandbox")
        following = self.prepare(self.runtime(sandbox_id=sandbox.id))
        tools.ensure_sandbox_initialized(following)
        self.assertEqual(self.provider.syncs[-1][1], {"beta"})

    def test_reused_sandbox_keeps_fresh_projection_without_copying_again(self):
        first_runtime = self.prepare(self.runtime())
        first_sandbox = tools.ensure_sandbox_initialized(first_runtime)
        release_sandbox_execution_lease(first_runtime.context)
        second_runtime = self.prepare(self.runtime(sandbox_id=first_sandbox.id))
        with patch("deerflow.skills.projection._rebuild_thread_locked", side_effect=AssertionError("复用未变化的视图不应再次复制")):
            second_sandbox = tools.ensure_sandbox_initialized(second_runtime)
        self.assertEqual(first_sandbox.id, second_sandbox.id)
        self.assertEqual(self.provider.syncs[-1][1], {"alpha"})

    def test_parallel_tools_initialize_and_sync_once(self):
        runtime = self.prepare(self.runtime())
        with ThreadPoolExecutor(max_workers=8) as executor:
            sandboxes = list(executor.map(lambda _: tools.ensure_sandbox_initialized(runtime), range(16)))
        self.assertEqual(len({id(sandbox) for sandbox in sandboxes}), 1)
        self.assertEqual(len(self.provider.syncs), 1)
        self.assertEqual(len(self.provider.acquisitions), 1)

    def test_parallel_async_tools_initialize_and_sync_once(self):
        runtime = self.prepare(self.runtime())
        async def run():
            return await asyncio.gather(*(tools.ensure_sandbox_initialized_async(runtime) for _ in range(16)))
        sandboxes = asyncio.run(run())
        self.assertEqual(len({id(sandbox) for sandbox in sandboxes}), 1)
        self.assertEqual(len(self.provider.syncs), 1)
        self.assertEqual(len(self.provider.acquisitions), 1)

    def test_first_access_in_subagent_preserves_lead_whitelist(self):
        lead = SandboxMiddleware(available_skills={"alpha"})
        parent = self.prepare(self.runtime(sandbox_id="old-shared"), "default", middleware=lead)
        child = self.runtime()
        request = SimpleNamespace(runtime=parent, tool_call={"name": "task"})
        def delegated():
            subagent = SandboxMiddleware(available_skills={"beta"}, owns_agent_skill_projection=False)
            subagent.before_agent(child.state, child)
            return tools.ensure_sandbox_initialized(child)
        def handler(_request):
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(copy_context().run, delegated).result(timeout=5)
            return ToolMessage(content="完成", tool_call_id="call")
        result = lead.wrap_tool_call(request, handler)
        self.assertEqual(self.provider.syncs[-1][1], {"alpha"})
        self.assertEqual(len(self.provider.acquisitions), 1)
        self.assertEqual(parent.context["sandbox_id"], child.context["sandbox_id"])
        updated, overwritten = unwrap_sandbox(result.update["sandbox"])
        self.assertTrue(overwritten)
        self.assertEqual(updated["sandbox_id"], child.context["sandbox_id"])
        self.assertEqual(parent.state["sandbox"]["sandbox_id"], child.context["sandbox_id"])
        release_sandbox_execution_lease(parent.context)
        self.assertEqual(self.provider.releases, [])
        release_sandbox_execution_lease(child.context)
        self.assertEqual(len(self.provider.releases), 1)

    def test_async_subagent_first_access_inherits_initialization(self):
        parent = self.prepare(self.runtime())
        child = self.runtime()
        request = SimpleNamespace(runtime=parent, tool_call={"name": "task"})
        async def handler(_request):
            subagent = SandboxMiddleware(owns_agent_skill_projection=False)
            await subagent.abefore_agent(child.state, child)
            await tools.ensure_sandbox_initialized_async(child)
            return ToolMessage(content="完成", tool_call_id="call")
        result = asyncio.run(self.middleware.awrap_tool_call(request, handler))
        self.assertEqual(self.provider.syncs[-1][1], {"alpha"})
        self.assertEqual(parent.context["sandbox_id"], child.context["sandbox_id"])
        self.assertEqual(result.update["sandbox"]["sandbox_id"], child.context["sandbox_id"])

    def test_concurrent_runs_keep_different_presets_and_users(self):
        alice = self.prepare(self.runtime("alice_thread"))
        bob = self.prepare(self.runtime("bob_thread", "bob"), self.preset_two)
        async def run():
            await asyncio.gather(tools.ensure_sandbox_initialized_async(alice), tools.ensure_sandbox_initialized_async(bob))
        asyncio.run(run())
        self.assertCountEqual(self.provider.syncs, [("probe:alice:alice_thread", {"alpha"}), ("probe:bob:bob_thread", {"beta"})])

    def test_initialization_rejects_context_from_another_user(self):
        parent = self.prepare(self.runtime())
        child = self.runtime(user="bob")
        initialization = get_lazy_sandbox_initialization(parent.context)
        with self.assertRaises(SandboxRuntimeError):
            initialization.ensure(child)
        self.assertEqual(self.provider.acquisitions, [])

    def test_explicit_eager_mode_still_initializes_before_model(self):
        for asynchronous in (False, True):
            runtime = self.runtime(f"eager_{asynchronous}")
            middleware = SandboxMiddleware(lazy_init=False)
            with public_skill_preset_scope(self.store.resolve(self.preset_one)):
                result = asyncio.run(middleware.abefore_agent(runtime.state, runtime)) if asynchronous else middleware.before_agent(runtime.state, runtime)
            self.assertIn("sandbox", result)
            self.assertIsNone(get_lazy_sandbox_initialization(runtime.context))
        self.assertEqual(len(self.provider.syncs), 2)

    def test_environment_refresh_and_temporary_values_survive_lazy_initialization(self):
        store = EnvironmentStore(self.paths.base_dir)
        store.mutate(None, "DF_PUBLIC", "public", operation="create")
        store.mutate("alice", "DF_PRIVATE", "private", operation="create")
        runtime = self.prepare(self.runtime())
        with temporary_environment_scope({"DF_PUBLIC": "temporary"}):
            sandbox = asyncio.run(tools.ensure_sandbox_initialized_async(runtime))
            self.assertEqual(sandbox.execute_command('printf "%s/%s" "$DF_PUBLIC" "$DF_PRIVATE"').strip(), "temporary/private")
        store.mutate("alice", "DF_PRIVATE", "updated", operation="update")
        self.assertEqual(sandbox.execute_command('printf "%s/%s" "$DF_PUBLIC" "$DF_PRIVATE"').strip(), "public/updated")
        store.mutate("alice", "DF_PRIVATE", None, operation="delete")
        self.assertEqual(sandbox.execute_command('printf "%s" "${DF_PRIVATE-unset}"').strip(), "unset")

    def test_denied_access_never_prepares_view_or_reuses_old_sandbox(self):
        runtime = self.prepare(self.runtime(sandbox_id="old"))
        with patch.object(tools, "authorize_sandbox_execution", side_effect=SandboxAuthorizationError("denied")):
            with self.assertRaises(SandboxAuthorizationError):
                tools.ensure_sandbox_initialized(runtime)
        self.assertEqual(self.provider.acquisitions, [])
        self.assertFalse(self.paths.thread_skills_view_dir("thread", user_id="alice").exists())

    def test_sync_failure_does_not_fall_back_to_old_sandbox(self):
        runtime = self.prepare(self.runtime(sandbox_id="old"))
        self.provider.sync_hook = Mock(side_effect=RuntimeError("同步失败"))
        with self.assertRaisesRegex(RuntimeError, "同步失败"):
            tools.ensure_sandbox_initialized(runtime)
        self.assertFalse(get_lazy_sandbox_initialization(runtime.context).completed)
        self.assertNotIn("sandbox_id", runtime.context)
        self.assertEqual(len(self.provider.releases), 1)

    def test_unsupported_provider_fails_on_first_access(self):
        self.provider.supports_agent_skill_isolation = False
        runtime = self.prepare(self.runtime(sandbox_id="old"))
        with self.assertRaises(SandboxRuntimeError):
            tools.ensure_sandbox_initialized(runtime)
        self.assertEqual(self.provider.acquisitions, [])

    def test_cancelled_initialization_drains_sync_before_releasing(self):
        runtime = self.prepare(self.runtime())
        entered, finish = threading.Event(), threading.Event()
        def blocking_sync():
            entered.set()
            self.assertTrue(finish.wait(5))
        self.provider.sync_hook = blocking_sync
        async def run():
            task = asyncio.create_task(tools.ensure_sandbox_initialized_async(runtime))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(self.provider.releases, [])
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await release_sandbox_execution_lease_async(runtime.context)
        try:
            asyncio.run(run())
        finally:
            finish.set()
        self.assertFalse(get_lazy_sandbox_initialization(runtime.context).completed)
        self.assertEqual(len(self.provider.releases), 1)
