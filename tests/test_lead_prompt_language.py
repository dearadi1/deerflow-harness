"""验证主代理提示词语言配置、渲染及两个入口的一致性。"""

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from deerflow.agents.lead_agent import prompt, prompt_selector, prompt_zh
from deerflow.config.app_config import AppConfig
from deerflow.config.prompt_overlay import PromptOverlay


class LeadPromptLanguageTests(unittest.TestCase):
    def make_config(self, **settings):
        return AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}, **settings})

    def test_missing_language_preserves_english_default(self):
        self.assertEqual(self.make_config().lead_prompt_language, "en")
        self.assertEqual(self.make_config(lead_prompt_language=None).lead_prompt_language, "en")

    def test_invalid_language_is_rejected(self):
        for language in ("fr", "prompt_zh.py", "", True):
            with self.subTest(language=language), self.assertRaises(ValidationError):
                self.make_config(lead_prompt_language=language)

    def test_language_switch_renders_identity_and_preserves_overlay(self):
        config = self.make_config()
        config.lead_prompt_overlay = PromptOverlay(prepend="组织规范", append="补充要求")
        for language, module, identity in (("en", prompt, "You are an AI assistant."), ("zh", prompt_zh, "你是智能助手。")):
            config.lead_prompt_language = language
            with self.subTest(language=language), patch.object(module, "get_skills_prompt_section", return_value=""), patch.object(module, "get_agent_soul", return_value=""):
                rendered = prompt_selector.apply_prompt_template(app_config=config, memory_enabled=False)
            self.assertIn(identity, rendered)
            self.assertTrue(rendered.startswith("组织规范\n\n"))
            self.assertTrue(rendered.endswith("\n\n补充要求"))

    def test_explicit_snapshot_and_render_arguments_are_preserved(self):
        config = self.make_config(lead_prompt_language="zh")
        with patch.object(prompt_selector, "get_app_config") as load_config, patch.object(prompt_zh, "apply_prompt_template", return_value="中文提示词") as render:
            result = prompt_selector.apply_prompt_template(True, 2, app_config=config, agent_name="研究助手", memory_enabled=False)
        self.assertEqual(result, "中文提示词")
        load_config.assert_not_called()
        render.assert_called_once_with(True, 2, app_config=config, agent_name="研究助手", memory_enabled=False)

    def test_missing_snapshot_uses_application_configuration(self):
        config = self.make_config(lead_prompt_language="zh")
        with patch.object(prompt_selector, "get_app_config", return_value=config), patch.object(prompt_zh, "apply_prompt_template", return_value="中文提示词") as render:
            self.assertEqual(prompt_selector.apply_prompt_template(), "中文提示词")
        render.assert_called_once_with(app_config=config)

    def test_gateway_and_client_share_selector(self):
        from deerflow import client
        from deerflow.agents.lead_agent import agent

        self.assertIs(agent.apply_prompt_template, prompt_selector.apply_prompt_template)
        self.assertIs(client.apply_prompt_template, prompt_selector.apply_prompt_template)


if __name__ == "__main__":
    unittest.main()
