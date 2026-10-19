import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deerflow.agents.middlewares.uploads_middleware import UploadsMiddleware
from deerflow.client import DeerFlowClient
from deerflow.config.paths import Paths
from deerflow.tools.builtins.list_uploaded_files_tool import _list_uploaded_files_impl


class ClientUploadsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = Paths(self.temporary.name)
        self.middleware = UploadsMiddleware(base_dir=self.temporary.name)
        self.client = object.__new__(DeerFlowClient)
        self.prepare_client()
        self.stub_external_runtime()

    def prepare_client(self):
        self.client._model_name = None
        self.client._thinking_enabled = False
        self.client._plan_mode = False
        self.client._subagent_enabled = False
        self.client._agent_name = None
        self.client._environment = None
        self.client._checkpointer = object()
        self.client._checkpoint_channel_mode = "full"
        self.client._ensure_agent = Mock()
        self.client._agent = SimpleNamespace(stream=Mock(side_effect=self.run_middlewares))

    def stub_external_runtime(self):
        # 保留真实参数传递与文件校验，只替换模型、持久化和沙箱外部边界。
        replacements = {
            "build_tracing_callbacks": Mock(return_value=[]),
            "inject_langfuse_metadata": Mock(),
            "ensure_checkpoint_mode_compatible": Mock(),
            "_stream_with_sandbox_lease_cleanup": lambda items, context: items,
        }
        for name, replacement in replacements.items():
            self.enterContext(patch(f"deerflow.client.{name}", replacement))
        self.enterContext(patch("deerflow.runtime.client_replay.ClientTurnReplay.prepare"))

    def run_middlewares(self, state, *, config, context, stream_mode):
        self.input_message = state["messages"][-1]
        self.runtime = SimpleNamespace(context=context, state=state)
        self.middleware_result = self.middleware.before_agent(state, self.runtime)
        state.update(self.middleware_result)
        return iter(())

    def create_upload(self, filename, *, user_id="user_001"):
        uploads_dir = self.paths.sandbox_uploads_dir("thread_001", user_id=user_id)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        (uploads_dir / filename).write_text("测试资料", encoding="utf-8")

    def stream_turn(self, **kwargs):
        events = list(self.client.stream("分析资料", thread_id="thread_001", user_id="user_001", **kwargs))
        self.assertEqual(events[-1].type, "end")

    def historical_filenames(self):
        result = _list_uploaded_files_impl(runtime=self.runtime, _paths=self.paths)
        return {entry["filename"] for entry in result["files"]}

    def test_files_reach_middleware_and_missing_entries_are_skipped(self):
        self.create_upload("report.txt")
        files = [{"filename": "report.txt", "size": 12}, {"filename": "missing.txt", "size": 99}]
        original = deepcopy(files)
        self.stream_turn(files=files)
        self.assertEqual(self.input_message.additional_kwargs["files"], original)
        self.assertTrue(self.input_message.additional_kwargs["run_id"])
        self.assertEqual(files, original)
        self.assertEqual(self.runtime.context["user_id"], "user_001")
        self.assertEqual([entry["filename"] for entry in self.middleware_result["uploaded_files"]], ["report.txt"])
        content = self.middleware_result["messages"][-1].content
        self.assertIn("<current_uploads>", content)
        self.assertIn("/mnt/user-data/uploads/report.txt", content)
        self.assertNotIn("missing.txt", content)

    def test_all_missing_files_do_not_create_files_or_fail_turn(self):
        self.stream_turn(files=[{"filename": "missing.txt", "size": 12}])
        self.assertEqual(self.middleware_result, {"uploaded_files": []})
        self.assertEqual(self.input_message.content, "分析资料")
        self.assertFalse(self.paths.thread_dir("thread_001", user_id="user_001").exists())

    def test_current_files_become_historical_on_next_turn(self):
        self.create_upload("old.txt")
        self.create_upload("new.txt")
        self.create_upload("other_user.txt", user_id="user_002")
        self.stream_turn(files=[{"filename": "new.txt", "size": 12}])
        self.assertEqual(self.historical_filenames(), {"old.txt"})
        self.stream_turn()
        self.assertEqual(self.historical_filenames(), {"old.txt", "new.txt"})

    def test_omitted_and_empty_files_keep_plain_message_behavior(self):
        for kwargs in ({}, {"files": []}):
            with self.subTest(kwargs=kwargs):
                self.stream_turn(**kwargs)
                self.assertEqual(self.middleware_result, {"uploaded_files": []})
                self.assertEqual(self.input_message.content, "分析资料")
                self.assertEqual("files" in self.input_message.additional_kwargs, "files" in kwargs)


if __name__ == "__main__":
    unittest.main()
