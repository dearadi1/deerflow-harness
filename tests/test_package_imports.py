"""验证协议轻量导入后，顶层客户端和配置函数仍保持原有公共入口。"""

import importlib
import subprocess
import sys
import unittest


class PackageImportTests(unittest.TestCase):
    def test_top_level_exports_keep_their_implementation_identity(self):
        package = importlib.import_module("deerflow")
        exports = {
            "DeerFlowClient": "deerflow.agent_client",
            "DeerFlowClientPool": "deerflow.agent_client",
            "DeerFlowThreadClient": "deerflow.thread_client",
            "DeerFlowSkillClient": "deerflow.skill_client",
            "configure_upload_file": "deerflow.callable",
            "configure_knowledge_search": "deerflow.callable",
        }
        self.assertEqual(set(package.__all__), set(exports))
        for name, module_name in exports.items():
            with self.subTest(name=name):
                implementation = importlib.import_module(module_name)
                self.assertIs(getattr(package, name), getattr(implementation, name))
                self.assertIs(package.__dict__[name], getattr(implementation, name))

    def test_star_import_resolves_all_public_exports(self):
        namespace = {}
        exec("from deerflow import *", namespace)
        package = importlib.import_module("deerflow")
        for name in package.__all__:
            with self.subTest(name=name):
                self.assertIs(namespace[name], getattr(package, name))

    def test_exports_are_discoverable_before_runtime_loading(self):
        code = """
import sys
import deerflow
assert set(deerflow.__all__) <= set(dir(deerflow))
assert 'deerflow.client' not in sys.modules
assert 'deerflow.agent_client' not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_attribute_raises_attribute_error(self):
        package = importlib.import_module("deerflow")
        with self.assertRaisesRegex(AttributeError, "missing_export"):
            getattr(package, "missing_export")


if __name__ == "__main__":
    unittest.main()
