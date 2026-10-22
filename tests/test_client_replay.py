"""用真实图和持久化验证最近一轮替换，避免仅验证参数被透传。"""

import operator
import tempfile
import unittest
from contextlib import ExitStack
from contextvars import Context
from copy import deepcopy
from types import SimpleNamespace
from typing import Annotated, NotRequired
from unittest.mock import Mock, patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

from deerflow.agents.middlewares.message_utils import is_genuine_user_message
from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware
from deerflow.agents.thread_state import get_thread_state_schema, normalize_middleware_state_schemas
from deerflow.client import DeerFlowClient
from deerflow.runtime.checkpoint_state import CheckpointStateAccessor
from deerflow.runtime.checkpoint_mode import CheckpointModeMismatchError
from deerflow.runtime.client_replay import ClientTurnReplay, ReplayConflictError, ReplayUnavailableError
from deerflow.trace_context import get_current_trace_id


class ReplayGraph:
    def __init__(self, checkpointer, mode, *, inject=False, compact=False):
        self.checkpointer = checkpointer
        self.mode = mode
        self.inject = inject
        self.compact = compact
        self.observed = []
        self.fail = False
        self.wrote_failure = False
        self.thread_ids = set()
        self.build_graph()

    def build_graph(self):
        schema = get_thread_state_schema(self.mode, 10)

        class ExtendedState(schema):
            counter: Annotated[int, operator.add]
            scratch: NotRequired[str]

        builder = StateGraph(ExtendedState)
        builder.add_node("inject", self.inject_context)
        builder.add_node("tools", self.call_tools)
        builder.add_node("answer", self.answer)
        builder.add_edge(START, "inject")
        builder.add_edge("inject", "tools")
        builder.add_edge("tools", "answer")
        builder.add_edge("answer", END)
        self.graph = builder.compile(checkpointer=self.checkpointer)

    def inject_context(self, state):
        if not self.inject:
            return {}
        message = self.last_user(state)
        if message.id.endswith("__user"):
            return {}
        return {"messages": [
            RemoveMessage(id=message.id),
            SystemMessage(content="框架日期", additional_kwargs={"dynamic_context_reminder": True}),
            HumanMessage(content="用户记忆", additional_kwargs={"hide_from_ui": True}),
            HumanMessage(content=message.content, id=f"{message.id}__user", additional_kwargs=message.additional_kwargs),
        ]}

    @staticmethod
    def last_user(state):
        return next(message for message in reversed(state["messages"]) if is_genuine_user_message(message))

    def call_tools(self, state, runtime: Runtime):
        message = self.last_user(state)
        self.observed.append(deepcopy(state))
        self.thread_ids.add(runtime.context["thread_id"])
        tool_id = runtime.context["run_id"]
        return {
            "messages": [
                AIMessage(content="", tool_calls=[{"name": "lookup", "args": {"query": message.content}, "id": tool_id}]),
                ToolMessage(content=f"资料：{message.content}", tool_call_id=tool_id),
            ],
            "counter": 1,
            "scratch": f"临时结果：{message.content}",
        }

    def answer(self, state):
        if self.fail:
            self.wrote_failure = True
            raise RuntimeError("模拟模型失败")
        message = self.last_user(state)
        answer = AIMessage(content=f"答复：{message.content}")
        update = {"messages": [answer], "summary_text": f"摘要：{message.content}", "todos": [{"content": message.content, "status": "completed"}]}
        if self.compact:
            update["messages"] = Overwrite([message, answer])
        return update

    def client(self, *, agent_name=None):
        client = object.__new__(DeerFlowClient)
        client._model_name = None
        client._thinking_enabled = False
        client._plan_mode = False
        client._subagent_enabled = False
        client._agent_name = agent_name
        client._environment = None
        client._checkpointer = self.checkpointer
        client._checkpoint_channel_mode = self.mode
        client._agent = self.graph
        client._ensure_agent = Mock()
        return client

    def snapshot(self, thread_id="thread_001"):
        accessor = CheckpointStateAccessor.bind(self.graph, self.checkpointer, mode=self.mode)
        return accessor.get({"configurable": {"thread_id": thread_id}})


class ClientReplayTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.sqlite_count = 0
        replacements = {
            "build_tracing_callbacks": Mock(return_value=[]),
            "inject_langfuse_metadata": Mock(),
            "_stream_with_sandbox_lease_cleanup": lambda items, context: items,
        }
        for name, replacement in replacements.items():
            self.enterContext(patch(f"deerflow.client.{name}", replacement))

    def harness(self, mode, backend="memory", **kwargs):
        if backend == "sqlite":
            self.sqlite_count += 1
            saver = self.stack.enter_context(SqliteSaver.from_conn_string(f"{self.directory}/{self.sqlite_count}.db"))
            saver.setup()
        else:
            saver = InMemorySaver()
        harness = ReplayGraph(saver, mode, **kwargs)
        return harness, harness.client()

    @staticmethod
    def stream(client, text, **kwargs):
        return list(client.stream(text, thread_id="thread_001", user_id="user_001", **kwargs))

    @staticmethod
    def users(snapshot):
        return [message.content for message in snapshot.values["messages"] if is_genuine_user_message(message)]

    def test_last_turn_replace_and_continuation_with_real_savers(self):
        for mode in ("full", "delta"):
            for backend in ("memory", "sqlite"):
                with self.subTest(mode=mode, backend=backend):
                    harness, client = self.harness(mode, backend)
                    self.stream(client, "问题一")
                    self.stream(client, "问题二", files=[{"filename": "report.txt", "size": 12}])
                    events = self.stream(client, "修改后的问题二", replay_last_turn=True)
                    snapshot = harness.snapshot()
                    self.assertEqual(self.users(snapshot), ["问题一", "修改后的问题二"])
                    self.assertEqual(snapshot.values["counter"], 2)
                    self.assertEqual(snapshot.values["scratch"], "临时结果：修改后的问题二")
                    self.assertEqual(harness.observed[-1]["summary_text"], "摘要：问题一")
                    self.assertEqual(harness.last_user(harness.observed[-1]).additional_kwargs["files"], [{"filename": "report.txt", "size": 12}])
                    self.assertEqual(events[-1].type, "end")
                    self.assertFalse(any("答复：问题二" in str(event.data) for event in events))
                    self.stream(client, "修改后的问题二", replay_last_turn=True)
                    self.assertEqual(self.users(harness.snapshot()), ["问题一", "修改后的问题二"])
                    self.stream(client, "问题三")
                    self.assertEqual(self.users(harness.snapshot()), ["问题一", "修改后的问题二", "问题三"])

    def test_child_end_cannot_commit_an_unfinished_replay(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                harness, client = self.harness(mode)
                self.stream(client, "原问题")
                original_values = deepcopy(harness.snapshot().values)
                original_stream = harness.graph.stream

                def child_finishes_first(*args, **kwargs):
                    for index, item in enumerate(original_stream(*args, **kwargs)):
                        yield item
                        if index == 0:
                            yield ("custom", {"type": "task_completed", "task_id": "child_task"})

                with patch.object(harness.graph, "stream", side_effect=child_finishes_first):
                    stream = client.stream("修改问题", thread_id="thread_001", user_id="user_001", replay_last_turn=True)
                    for event in stream:
                        if event.type == "end" and event.agent_id == "child_task":
                            break
                    else:
                        self.fail("未收到子智能体结束事件")
                    stream.close()
                self.assertEqual(harness.snapshot().values, original_values)
                self.stream(client, "重试问题", replay_last_turn=True)
                self.assertEqual(self.users(harness.snapshot()), ["重试问题"])

    def test_first_turn_can_be_replayed_repeatedly(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                harness, client = self.harness(mode)
                self.stream(client, "首轮", files=[{"filename": "old.txt"}])
                self.stream(client, "首轮修改", replay_last_turn=True, files=[])
                self.assertEqual(self.users(harness.snapshot()), ["首轮修改"])
                self.assertEqual(harness.snapshot().values["counter"], 1)
                self.assertIsNone(harness.observed[-1].get("scratch"))
                self.assertEqual(harness.last_user(harness.observed[-1]).additional_kwargs["files"], [])
                self.stream(client, "首轮再修改", replay_last_turn=True)
                self.assertEqual(self.users(harness.snapshot()), ["首轮再修改"])

    def test_compacted_and_injected_turns_keep_the_pre_turn_context(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                harness, client = self.harness(mode, inject=True, compact=True)
                self.stream(client, "早期问题")
                self.stream(client, "被压缩的旧问题")
                self.stream(client, "修正问题", replay_last_turn=True)
                self.assertEqual(self.users(harness.snapshot()), ["修正问题"])
                self.assertEqual(harness.observed[-1]["summary_text"], "摘要：早期问题")
                self.assertEqual(self.users(SimpleNamespace(values=harness.observed[-1])), ["早期问题", "修正问题"])

    def test_failure_restores_every_channel_and_allows_retry(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                harness, client = self.harness(mode)
                self.stream(client, "保留问题")
                original = deepcopy(harness.snapshot().values)
                harness.fail = True
                with self.assertRaisesRegex(RuntimeError, "模拟模型失败"):
                    self.stream(client, "失败修改", replay_last_turn=True)
                self.assertTrue(harness.wrote_failure)
                self.assertEqual(harness.snapshot().values, original)
                self.assertFalse(harness.snapshot().next)
                harness.fail = False
                self.stream(client, "成功修改", replay_last_turn=True)
                self.assertEqual(self.users(harness.snapshot()), ["成功修改"])

    def test_early_close_restores_original_state_and_releases_locks(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                harness, client = self.harness(mode)
                self.stream(client, "原问题")
                original = deepcopy(harness.snapshot().values)
                stream = client.stream("提前中断修改", thread_id="thread_001", user_id="user_001", replay_last_turn=True)
                for event in stream:
                    if event.type == "tool_result":
                        break
                stream.close()
                self.assertEqual(harness.snapshot().values, original)
                self.stream(client, "重试问题", replay_last_turn=True)
                self.assertEqual(self.users(harness.snapshot()), ["重试问题"])

    def test_same_thread_conflicts_across_clients_and_normal_turns(self):
        harness, client = self.harness("full")
        self.stream(client, "原问题")
        stream = client.stream("未结束问题", thread_id="thread_001", user_id="user_001")
        next(stream)
        try:
            for contender in (client, harness.client()):
                with self.assertRaises(ReplayConflictError):
                    self.stream(contender, "重生成", replay_last_turn=True)
        finally:
            stream.close()

    def test_one_client_cannot_change_cached_agent_during_another_stream(self):
        harness, client = self.harness("full")
        stream = client.stream("问题", thread_id="thread_001", user_id="user_001")
        next(stream)
        try:
            with self.assertRaises(ReplayConflictError):
                list(client.stream("其他线程", thread_id="thread_002", user_id="user_001"))
        finally:
            stream.close()

    def test_user_and_agent_mismatch_do_not_mutate_checkpoints(self):
        harness, client = self.harness("full")
        self.stream(client, "原问题")
        original = deepcopy(harness.snapshot().values)
        with self.assertRaises(PermissionError):
            list(client.stream("越权", thread_id="thread_001", user_id="user_002", replay_last_turn=True))
        with self.assertRaises(ReplayUnavailableError):
            self.stream(harness.client(agent_name="other"), "其他智能体", replay_last_turn=True)
        self.assertEqual(harness.snapshot().values, original)

    def test_missing_thread_and_invalid_flags_fail_before_generation(self):
        harness, client = self.harness("full")
        with self.assertRaises(ReplayUnavailableError):
            list(client.stream("问题", replay_last_turn=True))
        with self.assertRaises(ReplayUnavailableError):
            self.stream(client, "问题", replay_last_turn=True)
        with self.assertRaises(TypeError):
            self.stream(client, "问题", replay_last_turn="true")
        self.assertEqual(harness.observed, [])

    def test_checkpoint_without_user_input_is_not_replayable(self):
        harness, client = self.harness("full")
        harness.graph.update_state({"configurable": {"thread_id": "thread_001"}}, {"messages": []}, as_node="answer")
        with self.assertRaises(ReplayUnavailableError):
            self.stream(client, "问题", replay_last_turn=True)

    def test_legacy_thread_uses_parent_lineage(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                harness, client = self.harness(mode)
                with patch.object(ClientTurnReplay, "prepare"):
                    self.stream(client, "旧问题一")
                    self.stream(client, "旧问题二")
                self.stream(client, "旧问题修改", replay_last_turn=True)
                self.assertEqual(self.users(harness.snapshot()), ["旧问题一", "旧问题修改"])

    def test_reopen_sqlite_and_replay_without_client_local_state(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                self.sqlite_count += 1
                path = f"{self.directory}/{self.sqlite_count}.db"
                with SqliteSaver.from_conn_string(path) as saver:
                    harness = ReplayGraph(saver, mode)
                    self.stream(harness.client(), "持久化问题")
                with SqliteSaver.from_conn_string(path) as saver:
                    harness = ReplayGraph(saver, mode)
                    self.stream(harness.client(), "重启后修改", replay_last_turn=True)
                    self.assertEqual(self.users(harness.snapshot()), ["重启后修改"])

    def test_new_run_ids_and_trace_scope_do_not_leak(self):
        harness, client = self.harness("full")
        original_trace = get_current_trace_id()
        self.stream(client, "问题")
        original_id = harness.last_user(harness.snapshot().values).additional_kwargs["run_id"]
        self.stream(client, "问题", replay_last_turn=True)
        replay_id = harness.last_user(harness.snapshot().values).additional_kwargs["run_id"]
        self.assertNotEqual(original_id, replay_id)
        self.assertEqual(get_current_trace_id(), original_trace)

    def test_real_agent_and_dynamic_context_keep_streaming_deltas(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                harness, client = self.harness(mode)
                middleware = DynamicContextMiddleware(memory_enabled=False)
                harness.graph = create_agent(
                    model=FakeListChatModel(responses=["原回答", "新回答"]),
                    checkpointer=harness.checkpointer,
                    state_schema=get_thread_state_schema(mode, 10),
                    middleware=normalize_middleware_state_schemas([middleware], mode, 10),
                )
                client._agent = harness.graph
                self.stream(client, "原始问题")
                events = self.stream(client, "修改问题", replay_last_turn=True)
                deltas = [event.data.content for event in events if event.type == "answer_content"]
                self.assertEqual("".join(deltas), "新回答")
                self.assertGreater(len(deltas), 1)
                self.assertEqual(self.users(harness.snapshot()), ["修改问题"])

    def test_preserves_current_goal_but_clears_old_completion(self):
        for mode in ("full", "delta"):
            with self.subTest(mode=mode):
                harness, client = self.harness(mode)
                self.stream(client, "原问题")
                config = {"configurable": {"thread_id": "thread_001"}}
                harness.graph.update_state(
                    config,
                    {"goal": {"objective": "当前目标", "continuation_count": 2}, "goal_outcome": {"objective": "旧目标"}},
                    as_node="answer",
                )
                goal = deepcopy(harness.snapshot().values["goal"])
                self.stream(client, "修改问题", replay_last_turn=True)
                self.assertEqual(harness.snapshot().values["goal"], goal)
                self.assertIsNone(harness.snapshot().values["goal_outcome"])

    def test_missing_base_fails_without_changing_live_state(self):
        harness, client = self.harness("full")
        self.stream(client, "原问题")
        original = deepcopy(harness.snapshot().values)
        accessor = CheckpointStateAccessor.bind(harness.graph, harness.checkpointer)
        snapshot = harness.snapshot()
        snapshot = snapshot._replace(metadata={**snapshot.metadata, "deerflow_embedded_turn_base": "missing"})
        replay = ClientTurnReplay(enabled=True, accessor=accessor, read_config={"configurable": {"thread_id": "thread_001", "checkpoint_ns": ""}})
        with self.assertRaises(ReplayUnavailableError):
            replay._find_base(snapshot, harness.last_user(original))
        self.assertEqual(harness.snapshot().values, original)

    def test_mode_mismatch_cannot_overwrite_delta_thread(self):
        harness, client = self.harness("delta")
        self.stream(client, "原问题")
        original = deepcopy(harness.snapshot().values)
        incompatible = ReplayGraph(harness.checkpointer, "full").client()
        with self.assertRaises(CheckpointModeMismatchError):
            self.stream(incompatible, "错误模式", replay_last_turn=True)
        self.assertEqual(harness.snapshot().values, original)

    def test_stream_can_be_closed_from_another_context(self):
        harness, client = self.harness("full")
        self.stream(client, "原问题")
        original = deepcopy(harness.snapshot().values)
        stream = client.stream("修改问题", thread_id="thread_001", user_id="user_001", replay_last_turn=True)
        Context().run(next, stream)
        Context().run(stream.close)
        self.assertEqual(harness.snapshot().values, original)

    def test_replay_requires_a_checkpointer(self):
        replay = ClientTurnReplay(enabled=True)
        with self.assertRaises(ReplayUnavailableError):
            replay.prepare(None, None, {}, {}, {}, "full")

    def test_incomplete_normal_turn_cannot_be_replayed(self):
        harness, client = self.harness("full")
        harness.fail = True
        with self.assertRaises(RuntimeError):
            self.stream(client, "未完成问题")
        original = deepcopy(harness.snapshot().values)
        with self.assertRaises(ReplayConflictError):
            self.stream(client, "不能重放", replay_last_turn=True)
        self.assertEqual(harness.snapshot().values, original)


if __name__ == "__main__":
    unittest.main()
