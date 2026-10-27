import os
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deerflow.agent_client import DeerFlowClientPool
from deerflow.client_config import load_client_config
from deerflow.config.app_config import AppConfig, get_app_config, reload_app_config, reset_app_config
from deerflow.skill_client import DeerFlowSkillClient
from deerflow.thread_client import DeerFlowThreadClient


class ManagementClientConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary).resolve()
        self.package_root = self.root / "installed_harness"
        self.package_root.mkdir()
        self.caller_root = self.root / "caller"
        self.caller_root.mkdir()
        self.default_path = self.package_root / "config.yaml"
        self.write_config(self.default_path, 41)
        self.write_config(self.caller_root / "config.yaml", 82)
        self.enterContext(chdir(self.caller_root))
        self.enterContext(patch.dict(os.environ, {
            "DEER_FLOW_CONFIG_PATH": str(self.caller_root / "config.yaml"),
            "DEER_FLOW_PROJECT_ROOT": str(self.caller_root),
            "DEER_FLOW_HOME": str(self.root / "runtime"),
        }))
        self.enterContext(patch("deerflow.client_config.__file__", str(self.package_root / "deerflow" / "client_config.py")))
        # 保留真实文件加载和缓存重解析，隔离无关运行时单例的初始化副作用。
        self.enterContext(patch.object(AppConfig, "_apply_singleton_configs"))
        self.enterContext(patch("deerflow.agent_client.DeerFlowClient", side_effect=self.create_client))
        reset_app_config()
        self.addCleanup(reset_app_config)

    @staticmethod
    def write_config(path, recursion_limit):
        path.write_text(f"sandbox:\n  use: deerflow.sandbox.local:LocalSandboxProvider\nrecursion_limit: {recursion_limit}\n", encoding="utf-8")

    @staticmethod
    def create_client(**options):
        return SimpleNamespace(config=get_app_config(), options=options)

    def constructors(self):
        return (DeerFlowClientPool, DeerFlowSkillClient, DeerFlowThreadClient)

    def assert_config(self, constructor, options, expected_limit):
        client = constructor(**options)
        self.assertEqual(get_app_config().recursion_limit, expected_limit)
        if isinstance(client, DeerFlowClientPool):
            self.addCleanup(client.close)
            self.assertTrue(all(slot.client.config.recursion_limit == expected_limit for slot in client._available))
        if isinstance(client, DeerFlowSkillClient):
            self.assertEqual(client._app_config.recursion_limit, expected_limit)

    def test_default_uses_package_root_from_other_working_directory(self):
        for constructor in self.constructors():
            for options in ({}, {"config_path": None}):
                with self.subTest(constructor=constructor.__name__, options=options):
                    self.assert_config(constructor, options, 41)
                    self.assertEqual(os.environ["DEER_FLOW_CONFIG_PATH"], str(self.default_path))

    def test_explicit_paths_override_package_default_and_survive_chdir(self):
        for constructor in self.constructors():
            for path in ("config.yaml", str(self.caller_root / "config.yaml")):
                with self.subTest(constructor=constructor.__name__, path=path):
                    self.assert_config(constructor, {"config_path": path}, 82)
                    with chdir(self.package_root):
                        self.assertEqual(get_app_config().recursion_limit, 82)

    def test_pool_loads_once_before_constructing_all_clients(self):
        with patch("deerflow.client_config.reload_app_config", wraps=reload_app_config) as reload_config:
            pool = DeerFlowClientPool(size=3, model_name="example")
        self.addCleanup(pool.close)
        reload_config.assert_called_once_with(str(self.default_path))
        self.assertEqual([slot.client.options for slot in pool._available], [{"model_name": "example"}] * 3)

    def test_custom_factory_keeps_ownership_of_configuration(self):
        self.default_path.unlink()
        with patch("deerflow.agent_client.load_client_config") as load_config:
            pool = DeerFlowClientPool(size=2, client_factory=object)
        self.addCleanup(pool.close)
        load_config.assert_not_called()

    def test_missing_default_does_not_fall_back_to_caller_config(self):
        self.default_path.unlink()
        for constructor in self.constructors():
            with self.subTest(constructor=constructor.__name__):
                with self.assertRaises(FileNotFoundError):
                    constructor()
        self.assertEqual(os.environ["DEER_FLOW_CONFIG_PATH"], str(self.caller_root / "config.yaml"))

    def test_selected_config_retains_hot_reload(self):
        load_client_config()
        self.write_config(self.default_path, 123)
        self.assertEqual(get_app_config().recursion_limit, 123)


if __name__ == "__main__":
    unittest.main()
