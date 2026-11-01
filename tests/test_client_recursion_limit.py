"""验证部署环境的步数预算优先于客户端透传参数。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from deerflow.agent_client import DeerFlowClientPool
from deerflow.client import DeerFlowClient
from deerflow.config.app_config import AppConfig
from deerflow.env import load_selected_env_file


class ClientRecursionLimitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("DEER_FLOW_RECURSION_LIMIT", None)
        config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})
        self.enterContext(patch("deerflow.client.get_app_config", return_value=config))
        self.client = DeerFlowClient(checkpointer=object())

    def assert_limit(self, expected, **kwargs):
        config = self.client._get_runnable_config("thread_001", **kwargs)
        self.assertEqual(config["recursion_limit"], expected)

    def test_missing_environment_preserves_default_and_call_override(self):
        self.assert_limit(100)
        self.assert_limit(250, recursion_limit=250)
        self.assert_limit(None, recursion_limit=None)

    def test_empty_environment_preserves_call_override_and_default(self):
        for value in ("", " \t "):
            with self.subTest(value=value):
                os.environ["DEER_FLOW_RECURSION_LIMIT"] = value
                self.assert_limit(100)
                self.assert_limit(250, recursion_limit=250)

    def test_environment_overrides_omitted_lower_higher_and_none_arguments(self):
        for value in ("1", " 300 "):
            os.environ["DEER_FLOW_RECURSION_LIMIT"] = value
            for kwargs in ({}, {"recursion_limit": 20}, {"recursion_limit": 500}, {"recursion_limit": None}):
                with self.subTest(value=value, kwargs=kwargs):
                    self.assert_limit(int(value), **kwargs)

    def test_invalid_environment_does_not_fall_back_to_call_argument(self):
        for value in ("0", "-1", "1.5", "true", "none", "invalid"):
            with self.subTest(value=value):
                os.environ["DEER_FLOW_RECURSION_LIMIT"] = value
                with self.assertRaisesRegex(ValueError, "DEER_FLOW_RECURSION_LIMIT 必须是正整数"):
                    self.client._get_runnable_config("thread_001", recursion_limit=250)

    def test_environment_is_read_again_for_each_configuration(self):
        os.environ["DEER_FLOW_RECURSION_LIMIT"] = "300"
        self.assert_limit(300, recursion_limit=250)
        os.environ["DEER_FLOW_RECURSION_LIMIT"] = "400"
        self.assert_limit(400, recursion_limit=250)
        os.environ.pop("DEER_FLOW_RECURSION_LIMIT")
        self.assert_limit(250, recursion_limit=250)

    def prepare_graph(self):
        graph = Mock()
        graph.stream.side_effect = lambda *args, **kwargs: iter(())
        self.client._agent = graph
        self.enterContext(patch.object(self.client, "_ensure_agent"))
        self.enterContext(patch("deerflow.client.build_tracing_callbacks", return_value=[]))
        self.enterContext(patch("deerflow.client.inject_langfuse_metadata"))
        self.enterContext(patch("deerflow.client.ensure_checkpoint_mode_compatible"))
        self.enterContext(patch("deerflow.client._stream_with_sandbox_lease_cleanup", side_effect=lambda items, context: items))
        self.enterContext(patch("deerflow.runtime.client_replay.ClientTurnReplay.prepare"))
        return graph

    def test_session_stream_uses_env_file_and_retains_process_environment_priority(self):
        graph = self.prepare_graph()
        directory = self.enterContext(tempfile.TemporaryDirectory())
        env_file = Path(directory) / ".env"
        env_file.write_text("DEER_FLOW_RECURSION_LIMIT=300\n", encoding="utf-8")
        os.environ.update(DEER_FLOW_ENV_FILE=str(env_file), PYTHON_DOTENV_DISABLED="0")
        pool = self.enterContext(DeerFlowClientPool(size=1, client_factory=lambda: self.client))
        for process_value, expected in ((None, 300), ("450", 450)):
            with self.subTest(process_value=process_value):
                if process_value is not None:
                    os.environ["DEER_FLOW_RECURSION_LIMIT"] = process_value
                self.assertTrue(load_selected_env_file())
                with pool.session(user_id="user_001") as session:
                    events = list(session.stream("问题", thread_id="thread_001", recursion_limit=200))
                self.assertEqual([event.type for event in events], ["end"])
                self.assertEqual(graph.stream.call_args.kwargs["config"]["recursion_limit"], expected)


if __name__ == "__main__":
    unittest.main()
