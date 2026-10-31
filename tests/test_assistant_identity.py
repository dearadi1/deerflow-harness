"""验证默认身份中性化、自定义身份保留及托管技能文案升级。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deerflow.agents.lead_agent import prompt, prompt_zh
from deerflow.config.app_config import AppConfig
from deerflow.config.prompt_overlay import PromptOverlay
from deerflow.integrations.lark_cli import (
    _DEERFLOW_LARK_SHARED_GUIDANCE_LEGACY_MARKERS,
    _DEERFLOW_LARK_SHARED_GUIDANCE_MARKER,
    _append_deerflow_lark_shared_guidance,
)


class AssistantIdentityTests(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})

    def render_prompt(self, module, **kwargs):
        with patch.object(module, "get_skills_prompt_section", return_value=""), patch.object(module, "get_agent_soul", return_value=""):
            return module.apply_prompt_template(app_config=self.config, memory_enabled=False, **kwargs)

    def test_default_identity_and_citation_example_are_neutral(self):
        for module, role in ((prompt, "You are an AI assistant."), (prompt_zh, "你是智能助手。")):
            with self.subTest(module=module.__name__):
                rendered = self.render_prompt(module)
                self.assertIn(role, rendered)
                self.assertNotIn("deerflow", rendered.lower())
                self.assertNotIn("deer-flow", rendered.lower())
                self.assertIn("https://docs.python.org/3/", rendered)

    def test_custom_name_and_operator_overlay_are_preserved(self):
        self.config.lead_prompt_overlay = PromptOverlay(prepend="组织写作规则", append="报告补充规则")
        for module in (prompt, prompt_zh):
            with self.subTest(module=module.__name__):
                rendered = self.render_prompt(module, agent_name="研究助手")
                self.assertIn("研究助手", rendered)
                self.assertTrue(rendered.startswith("组织写作规则\n\n"))
                self.assertTrue(rendered.endswith("\n\n报告补充规则"))

    def test_model_visible_tool_schemas_and_invocation_errors_are_neutral(self):
        from deerflow.tools.builtins.background_tasks_tool import cancel_background_task
        from deerflow.tools.builtins.batch_task_tool import batch_task
        from deerflow.tools.builtins.invoke_acp_agent_tool import _format_invocation_error
        from deerflow.tools.builtins.setup_agent_tool import setup_agent

        for tool in (setup_agent, batch_task, cancel_background_task):
            with self.subTest(tool=tool.name):
                visible = tool.description + json.dumps(tool.args, ensure_ascii=False)
                self.assertNotIn("deerflow", visible.lower())
        error = _format_invocation_error("mcode", "mcode", FileNotFoundError())
        self.assertNotIn("deerflow", error.lower())
        self.assertIn("npm install --global @minimax-ai/code", error)

    def test_lark_guidance_replaces_every_legacy_version_and_is_idempotent(self):
        for marker in (None, *_DEERFLOW_LARK_SHARED_GUIDANCE_LEGACY_MARKERS):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                skill_file = root / "lark-shared" / "SKILL.md"
                skill_file.parent.mkdir()
                original = "# 飞书共享技能\n"
                legacy = marker + "\n## DeerFlow 授权入口\n旧版授权说明\n" if marker else ""
                skill_file.write_text(original + legacy, encoding="utf-8")
                _append_deerflow_lark_shared_guidance(root)
                updated = skill_file.read_text(encoding="utf-8")
                self.assertTrue(updated.startswith(original))
                self.assertNotIn("deerflow", updated.lower())
                self.assertNotIn("旧版授权说明", updated)
                self.assertIn("/workspace/capabilities?tab=plugins&plugin=lark", updated)
                self.assertEqual(updated.count(_DEERFLOW_LARK_SHARED_GUIDANCE_MARKER), 1)
                _append_deerflow_lark_shared_guidance(root)
                self.assertEqual(skill_file.read_text(encoding="utf-8"), updated)


if __name__ == "__main__":
    unittest.main()
