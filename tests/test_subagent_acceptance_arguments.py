"""验证子代理工具兼容数组字符串，同时保持公开类型和错误输入边界。"""

import asyncio
import json
import unittest

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from deerflow.tools.builtins.batch_task_tool import batch_task
from deerflow.tools.builtins.task_tool import task_tool
from deerflow.tools.types import Runtime


_MISSING = object()
_CRITERIA = [
    "file:/mnt/user-data/outputs/deepseek-harness-调研报告.md exists",
    "file:/mnt/user-data/outputs/deepseek-harness-调研报告.md non-empty",
    'file:../outputs/数据.csv csv-columns:["id","name"]',
]


def _arguments(tool, criteria=_MISSING):
    task = {"prompt": "调研并保存报告"}
    if criteria is not _MISSING:
        task["acceptance_criteria"] = criteria
    if tool.name == "task":
        return {**task, "subagent_type": "general-purpose"}
    return {"title": "调研报告", "items": [{"key": "report", **task}], "subagent_type": "general-purpose"}


def _criteria_from_arguments(arguments):
    if "items" in arguments:
        return arguments["items"][0].acceptance_criteria
    return arguments["acceptance_criteria"]


def _echo_criteria(**arguments):
    return json.dumps(_criteria_from_arguments(arguments), ensure_ascii=False)


async def _async_echo_criteria(**arguments):
    return _echo_criteria(**arguments)


class SubagentAcceptanceArgumentsTests(unittest.TestCase):
    def test_arrays_and_serialized_arrays_reach_the_same_criteria(self):
        for tool in (task_tool, batch_task):
            for criteria in (_CRITERIA, json.dumps(_CRITERIA, ensure_ascii=False), " \n" + json.dumps(_CRITERIA) + "\n "):
                with self.subTest(tool=tool.name, criteria=criteria):
                    arguments = tool.tool_call_schema.model_validate(_arguments(tool, criteria))
                    self.assertEqual(_criteria_from_arguments(dict(arguments)), _CRITERIA)
                    dumped = arguments.model_dump()
                    persisted = dumped["acceptance_criteria"] if tool.name == "task" else dumped["items"][0]["acceptance_criteria"]
                    self.assertEqual(persisted, _CRITERIA)

    def test_optional_and_empty_values_remain_supported(self):
        for tool in (task_tool, batch_task):
            for criteria, expected in ((_MISSING, None), (None, None), ([], []), ("[]", [])):
                with self.subTest(tool=tool.name, criteria=criteria):
                    arguments = tool.tool_call_schema.model_validate(_arguments(tool, criteria))
                    self.assertEqual(_criteria_from_arguments(dict(arguments)), expected)

    def test_invalid_values_are_not_repaired_or_silently_dropped(self):
        invalid_values = [
            "file:../outputs/report.md exists",
            "",
            "null",
            '"file:../outputs/report.md exists"',
            json.dumps(json.dumps(_CRITERIA)),
            {"criterion": "file:../outputs/report.md exists"},
            '{"criterion": "file:../outputs/report.md exists"}',
            '["file:../outputs/report.md exists",]',
            123,
            True,
            [None],
            ["file:../outputs/report.md exists", 123],
            '["file:../outputs/report.md exists", 123]',
            '[{"criterion": "file:../outputs/report.md exists"}]',
            "[" * 1500 + "0" + "]" * 1500,
        ]
        for tool in (task_tool, batch_task):
            for criteria in invalid_values:
                with self.subTest(tool=tool.name, criteria=criteria):
                    with self.assertRaises(ValidationError) as raised:
                        tool.tool_call_schema.model_validate(_arguments(tool, criteria))
                    self.assertIn("acceptance_criteria", raised.exception.errors()[0]["loc"])

    def test_model_schema_still_requests_only_a_string_array_or_null(self):
        for tool in (task_tool, batch_task):
            with self.subTest(tool=tool.name):
                schema = convert_to_openai_tool(tool)["function"]["parameters"]
                owner = schema if tool.name == "task" else schema["properties"]["items"]["items"]
                field = owner["properties"]["acceptance_criteria"]
                self.assertEqual(field["anyOf"], [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}])
                self.assertIsNone(field["default"])
                self.assertNotIn("acceptance_criteria", owner.get("required", []))

    def test_sync_and_async_tool_invocation_receive_decoded_arrays(self):
        runtime = Runtime(state={}, context={}, config={}, stream_writer=lambda value: None, tool_call_id="acceptance_1", store=None)
        for tool in (task_tool, batch_task):
            instrumented = tool.model_copy(update={"func": _echo_criteria, "coroutine": _async_echo_criteria})
            call = {
                "type": "tool_call",
                "id": "acceptance_1",
                "name": tool.name,
                "args": {"runtime": runtime, **_arguments(tool, json.dumps(_CRITERIA, ensure_ascii=False))},
            }
            with self.subTest(tool=tool.name, mode="sync"):
                self.assertEqual(json.loads(instrumented.invoke(call).content), _CRITERIA)
            with self.subTest(tool=tool.name, mode="async"):
                self.assertEqual(json.loads(asyncio.run(instrumented.ainvoke(call)).content), _CRITERIA)


if __name__ == "__main__":
    unittest.main()
