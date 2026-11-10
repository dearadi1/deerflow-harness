"""共享真实聊天入口、LangGraph 工具节点和环境探针，不依赖付费模型。"""

import hashlib
import json
import os
import shlex
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import yaml
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from deerflow.agent_client import DeerFlowClientPool
from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
from deerflow.agents.thread_state import get_thread_state_schema
from deerflow.config.app_config import reset_app_config
from deerflow.config.paths import get_paths
from deerflow.environment import EnvironmentStore
from deerflow.sandbox.middleware import SandboxMiddleware
from deerflow.sandbox.sandbox_provider import get_sandbox_provider, shutdown_sandbox_provider
from deerflow.sandbox.tools import bash_tool

PROBE_NAMES = (
    "DF_ENV_PUBLIC", "DF_ENV_PRIVATE", "DF_ENV_TEMP", "DF_ENV_OLD_ONLY",
    "DF_ENV_EMPTY", "DF_ENV_SPECIAL", "DF_ENV_TOKEN",
)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest() if value is not None else None


def probe_command():
    # 只返回摘要；仍由容器中的 Python 及其子进程实际读取环境。
    child = "import os,json; print(json.dumps({name:os.environ.get(name) for name in " + repr(PROBE_NAMES) + "}))"
    script = (
        "import hashlib,json,os,platform,socket,subprocess,sys; "
        "encode=lambda values:{name:hashlib.sha256(value.encode()).hexdigest() if value is not None else None for name,value in values.items()}; "
        f"direct={{name:os.environ.get(name) for name in {PROBE_NAMES!r}}}; "
        f"child=json.loads(subprocess.check_output([sys.executable,'-c',{child!r}],text=True)); "
        "print(json.dumps({'values':encode(direct),'child':encode(child),'system':platform.system(),'machine':platform.machine(),'hostname':socket.gethostname()}))"
    )
    return "python3 -c " + shlex.quote(script)


class EnvironmentChatHarness(unittest.TestCase):
    def temporary_parent(self):
        return None

    def sandbox_settings(self):
        return {"use": "deerflow.sandbox.local:LocalSandboxProvider", "allow_host_bash": True}

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix=".temporary-env-test-", dir=self.temporary_parent()))).resolve()
        self.data_root = self.root / "runtime"
        self.config_path = self.root / "config.yaml"
        extensions = self.root / "extensions.json"
        extensions.write_text("{}")
        self.enterContext(patch.dict(os.environ, {
            "DEER_FLOW_HOME": str(self.data_root),
            "DEER_FLOW_HOST_BASE_DIR": str(self.data_root),
            "DEER_FLOW_PROJECT_ROOT": str(self.root),
            "DEER_FLOW_SKILL_PRESETS_PATH": str(self.root / "skill_presets.json"),
            "DEER_FLOW_SKILLS_PATH": str(self.root / "skills"),
            "DEER_FLOW_EXTENSIONS_CONFIG_PATH": str(extensions),
            "DEER_FLOW_CONFIG_PATH": str(self.config_path),
            "DEERFLOW_CONFIG_PATH": str(self.config_path),
        }))
        self.config_path.write_text(yaml.safe_dump({
            "sandbox": self.sandbox_settings(),
            "skills": {"path": str(self.root / "skills")},
            "authorization": {"enabled": False},
        }))
        self.enterContext(patch("deerflow.sandbox.sandbox_provider._default_sandbox_provider", None))
        self.enterContext(patch("deerflow.client.build_tracing_callbacks", return_value=[]))
        self.enterContext(patch("deerflow.client.inject_langfuse_metadata"))
        reset_app_config()
        self.addCleanup(reset_app_config)
        self.addCleanup(shutdown_sandbox_provider)
        self.saver = InMemorySaver()
        self.observations = []
        self.choose_entered = threading.Event()
        self.before_choose = None
        self.store = EnvironmentStore()
        self.store.mutate(None, "DF_ENV_PUBLIC", "public-default", operation="create")
        self.store.mutate("alice", "DF_ENV_PRIVATE", "alice-default", operation="create")
        self.store.mutate("bob", "DF_ENV_PRIVATE", "bob-default", operation="create")
        self.command = probe_command()
        self.pool = self.new_pool()
        self.provider = get_sandbox_provider()

    def choose(self, state, runtime):
        self.observations.append(dict(runtime.context))
        self.choose_entered.set()
        if self.before_choose is not None:
            self.before_choose()
        message = next(message.content for message in reversed(state["messages"]) if isinstance(message, HumanMessage))
        if message == "fail":
            raise RuntimeError("合成测试失败")
        command = self.command
        if message == "wait":
            command = "printf started > .temporary_env_started; sleep 60; " + command
        if message == "raw":
            command = 'printf "%s" "$DF_ENV_TOKEN"'
        calls = [{"name": "bash", "args": {"command": command}, "id": uuid4().hex, "type": "tool_call"} for _ in range(2 if message == "parallel" else 1)]
        return {"messages": [AIMessage(content="", tool_calls=calls)]}

    def prepare(self, state, runtime):
        updates = ThreadDataMiddleware().before_agent(state, runtime) or {}
        middleware = SandboxMiddleware(owns_agent_skill_projection=self.provider.supports_agent_skill_isolation)
        updates.update(middleware.before_agent({**state, **updates}, runtime) or {})
        return updates

    def answer(self, state):
        request = next(message for message in reversed(state["messages"]) if isinstance(message, AIMessage) and message.tool_calls)
        call_ids = {call["id"] for call in request.tool_calls}
        results = [message for message in state["messages"] if isinstance(message, ToolMessage) and message.tool_call_id in call_ids]
        self.assertEqual(len(results), len(call_ids))
        # 同时核验每个并行工具结果，避免仅最后一个工具正确时出现假通过。
        for result in results:
            self.assertEqual(result.content, results[0].content)
        return {"messages": [AIMessage(content=results[0].content)]}

    def new_pool(self, size=1):
        pool = DeerFlowClientPool(config_path=str(self.config_path), size=size, checkpointer=self.saver)
        self.addCleanup(pool.close)
        for slot in pool._available:
            builder = StateGraph(get_thread_state_schema("full", 10), context_schema=dict)
            builder.add_node("prepare", self.prepare)
            builder.add_node("choose", self.choose)
            builder.add_node("tools", ToolNode([bash_tool]))
            builder.add_node("answer", self.answer)
            builder.add_edge(START, "prepare")
            builder.add_edge("prepare", "choose")
            builder.add_edge("choose", "tools")
            builder.add_edge("tools", "answer")
            builder.add_edge("answer", END)
            slot.client._agent = builder.compile(checkpointer=self.saver)
            # 仅替换模型决策，保留公开入口、工具执行、检查点和沙盒租约完整链路。
            slot.client._ensure_agent = Mock()
        return pool

    def managed(self, message="probe", *, thread_id="env_thread", user_id="alice", pool=None, **kwargs):
        with (pool or self.pool).start_stream(message, user_id=user_id, thread_id=thread_id, **kwargs) as stream:
            events = list(stream)
        self.assertEqual(events[-1].type, "end")
        errors = [event for event in events if event.type == "error"]
        self.assertEqual(errors, [])
        snapshot = self.saver.get_tuple({"configurable": {"thread_id": thread_id}})
        answer = snapshot.checkpoint["channel_values"]["messages"][-1].content
        self.assertNotIn("Error:", answer)
        return json.loads(answer)

    def assert_environment(self, result, *, user="alice", **overrides):
        expected = {name: None for name in PROBE_NAMES}
        expected.update(DF_ENV_PUBLIC="public-default", DF_ENV_PRIVATE=f"{user}-default")
        expected.update(overrides)
        hashed = {name: digest(value) for name, value in expected.items()}
        self.assertEqual(result["values"], hashed)
        self.assertEqual(result["child"], hashed)

    def started_marker(self, thread_id="env_thread", user="alice"):
        return get_paths().sandbox_work_dir(thread_id, user_id=user) / ".temporary_env_started"
