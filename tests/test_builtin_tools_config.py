"""验证内置开关与宿主调用限制共同约束最终工具集合。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from deerflow.config.app_config import AppConfig
from deerflow.tools.tools import get_available_tools


def make_config(settings=""):
    data = yaml.safe_load(settings) or {}
    return AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}, **data})


def tool_names(config, **kwargs):
    with patch("deerflow.tools.tools.is_mcp_task_runtime_available", return_value=False):
        tools = get_available_tools(
            app_config=config, include_mcp=False, extensions=SimpleNamespace(plugins=[]), **kwargs
        )
    return {tool.name for tool in tools}


class BuiltinToolsConfigTests(unittest.TestCase):
    def test_omitted_config_preserves_defaults(self):
        names = tool_names(make_config())
        self.assertTrue({"list_uploaded_files", "review_skill_package"} <= names)

    def test_yaml_switches_are_independent(self):
        for disabled, retained in (
            ("list_uploaded_files", "review_skill_package"),
            ("review_skill_package", "list_uploaded_files"),
        ):
            with self.subTest(disabled=disabled):
                names = tool_names(make_config(f"builtin_tools:\n  {disabled}: false\n"))
                self.assertNotIn(disabled, names)
                self.assertIn(retained, names)

    def test_both_can_be_disabled_without_removing_other_builtins(self):
        names = tool_names(make_config("builtin_tools:\n  list_uploaded_files: false\n  review_skill_package: false\n"))
        self.assertFalse({"list_uploaded_files", "review_skill_package"} & names)
        self.assertIn("ask_clarification", names)

    def test_host_upload_restriction_still_applies(self):
        self.assertNotIn("list_uploaded_files", tool_names(make_config(), include_upload_tool=False))

    def test_configured_alias_cannot_restore_disabled_builtin(self):
        config = make_config("""
builtin_tools:
  review_skill_package: false
tools:
  - name: review_alias
    group: review
    use: deerflow.tools.builtins.review_skill_package_tool:review_skill_package
""")
        self.assertNotIn("review_skill_package", tool_names(config))
