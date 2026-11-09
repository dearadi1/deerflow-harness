"""验证可选说明的模型契约，避免遗漏说明阻断工具或误放宽操作参数。"""

import inspect
import unittest
from unittest.mock import Mock, patch

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from deerflow.community.hours_knowledge.tools import knowledge_base_search_tool
from deerflow.config.agents_config import AgentConfig
from deerflow.sandbox import tools
from deerflow.tools.builtins.setup_agent_tool import setup_agent
from deerflow.tools.builtins.task_tool import task_tool
from deerflow.tools.builtins.update_agent_tool import update_agent
from deerflow.tools.types import Runtime


TOOL_CASES = [
    (tools.bash_tool, {"command": "pwd"}),
    (tools.ls_tool, {"path": "/mnt/user-data/workspace"}),
    (tools.glob_tool, {"pattern": "*.py", "path": "/mnt/user-data/workspace"}),
    (tools.grep_tool, {"pattern": "needle", "path": "/mnt/user-data/workspace"}),
    (tools.read_file_tool, {"path": "/mnt/user-data/workspace/input.txt"}),
    (tools.write_file_tool, {"path": "/mnt/user-data/workspace/output.txt", "content": "正文"}),
    (tools.str_replace_tool, {"path": "/mnt/user-data/workspace/output.txt", "old_str": "旧", "new_str": "新"}),
    (knowledge_base_search_tool, {"query": "检索依据"}),
    (task_tool, {"prompt": "整理检索依据", "subagent_type": "general-purpose"}),
    (setup_agent, {"soul": "使用中文回答"}),
    (update_agent, {}),
]


class ToolDescriptionTests(unittest.TestCase):
    def test_model_schema_keeps_description_optional_and_first(self):
        for tool, required in TOOL_CASES:
            with self.subTest(tool=tool.name):
                schema = convert_to_openai_tool(tool)["function"]["parameters"]
                self.assertEqual(next(iter(schema["properties"])), "description")
                self.assertEqual(set(schema.get("required", [])), set(required))
                self.assertNotIn("runtime", schema["properties"])
                self.assertEqual(schema["properties"]["description"]["default"], "")
                hint = schema["properties"]["description"]["description"]
                # 保留模型提示中的可选性与顺序要求，允许不改变含义的文案调整。
                self.assertRegex(hint, r"(?i)\boptional\b")
                self.assertRegex(hint, r"(?i)\bfirst\s+(?:tool\s+)?argument\b")

    def test_omission_is_valid_without_relaxing_required_arguments(self):
        for tool, required in TOOL_CASES:
            with self.subTest(tool=tool.name):
                schema = tool.tool_call_schema
                value = schema.model_validate(required)
                self.assertEqual(value.description, "")
                for name in required:
                    with self.assertRaises(ValidationError):
                        schema.model_validate({key: value for key, value in required.items() if key != name})

    def test_entry_points_keep_description_after_runtime_and_first_in_args(self):
        for tool, _ in TOOL_CASES:
            with self.subTest(tool=tool.name):
                entry_points = [entry for entry in (tool.func, tool.coroutine) if entry is not None]
                for entry in entry_points:
                    parameters = inspect.signature(entry).parameters
                    self.assertEqual(list(parameters)[:2], ["runtime", "description"])
                    self.assertEqual(parameters["description"].default, "")
                if tool.func is not None and tool.coroutine is not None:
                    self.assertEqual(inspect.signature(tool.func), inspect.signature(tool.coroutine))
                docstring = inspect.getdoc(entry_points[0])
                self.assertTrue(docstring.split("Args:\n", 1)[1].lstrip().startswith("description:"))


class UpdateAgentDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime(state={}, context={"agent_name": "test-agent", "user_id": "test-user"}, config={}, stream_writer=lambda value: None, tool_call_id="update_1", store=None)
        self.store = Mock()
        self.enterContext(patch("deerflow.tools.builtins.update_agent_tool.get_agent_store", return_value=self.store))
        self.enterContext(patch("deerflow.tools.builtins.update_agent_tool.load_agent_config", return_value=AgentConfig(name="test-agent", description="原有描述")))

    def test_empty_default_does_not_turn_an_empty_request_into_a_write(self):
        result = update_agent.invoke({"runtime": self.runtime})
        self.assertEqual(result.update["messages"][0].status, "error")
        self.assertIn("No fields provided", result.update["messages"][0].content)
        self.store.update.assert_not_called()

    def test_description_is_preserved_unless_a_nonempty_value_is_provided(self):
        for arguments, expected in (({}, "原有描述"), ({"description": ""}, "原有描述"), ({"description": None}, "原有描述"), ({"description": "新的描述"}, "新的描述")):
            with self.subTest(arguments=arguments):
                self.store.update.reset_mock()
                result = update_agent.invoke({"runtime": self.runtime, "skills": [], **arguments})
                self.assertEqual(result.update["messages"][0].status, "success")
                self.store.update.assert_called_once()
                self.assertEqual(self.store.update.call_args.args[1]["description"], expected)


if __name__ == "__main__":
    unittest.main()
