"""验证沙箱工具异步执行时说明字段与操作参数保持独立。"""

import inspect
import unittest
from tempfile import TemporaryDirectory
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from deerflow.sandbox import tools
from deerflow.sandbox.local.local_sandbox import LocalSandbox


TOOL_CASES = [
    (tools.bash_tool, {"command": "printf 'actual_stdout'"}, {}),
    (tools.ls_tool, {"path": "/mnt/user-data/workspace"}, {}),
    (tools.glob_tool, {"pattern": "**/*.py", "path": "/mnt/user-data/workspace"}, {"include_dirs": True, "max_results": 17}),
    (tools.grep_tool, {"pattern": "needle", "path": "/mnt/user-data/workspace"}, {"glob": "*.py", "literal": True, "case_sensitive": True, "max_results": 19}),
    (tools.read_file_tool, {"path": "/mnt/user-data/workspace/input.txt"}, {"start_line": 2, "end_line": 5}),
    (tools.write_file_tool, {"path": "/mnt/user-data/workspace/output.txt", "content": "写入正文"}, {"append": True}),
    (tools.str_replace_tool, {"path": "/mnt/user-data/workspace/output.txt", "old_str": "旧内容", "new_str": "新内容"}, {"replace_all": True}),
]


def capture_arguments(signature, captured):
    def capture(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        captured.update(bound.arguments)
        return "工具执行成功"

    return capture


class SandboxAsyncArgumentsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = tools.Runtime(context={}, state={}, config={}, stream_writer=lambda value: None, tool_call_id="call_1", store=None)
        self.enterContext(patch.object(tools, "sandbox_authorization_scope_async", side_effect=lambda runtime: nullcontext()))
        self.initialize = self.enterContext(patch.object(tools, "ensure_sandbox_initialized_async", new_callable=AsyncMock))

    async def check_tool_arguments(self, tool, arguments):
        signature = inspect.signature(tool.func)
        captured = {}
        expected = signature.bind(runtime=self.runtime, **arguments)
        expected.apply_defaults()
        with patch.object(tool, "func", side_effect=capture_arguments(signature, captured)):
            result = await tool.ainvoke({"runtime": self.runtime, **arguments})
        self.assertEqual(result, "工具执行成功")
        self.assertEqual(captured, expected.arguments)
        self.initialize.assert_awaited_with(self.runtime)

    async def test_operational_arguments_and_defaults_reach_sync_body(self):
        for tool, required, _ in TOOL_CASES:
            with self.subTest(tool=tool.name):
                await self.check_tool_arguments(tool, {"description": "说明文字不得当作命令或路径", **required})

    async def test_optional_arguments_reach_sync_body(self):
        for tool, required, optional in TOOL_CASES:
            with self.subTest(tool=tool.name):
                await self.check_tool_arguments(tool, {"description": "验证非默认参数", **required, **optional})

    async def test_bash_returns_real_shell_stdout(self):
        sandbox = LocalSandbox("local")
        self.enterContext(patch.object(tools, "ensure_sandbox_initialized", return_value=sandbox))
        self.enterContext(patch.object(tools, "ensure_thread_directories_exist"))
        self.enterContext(patch.object(tools, "is_local_sandbox", return_value=True))
        self.enterContext(patch.object(tools, "is_host_bash_allowed", return_value=True))
        workspace = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(tools, "get_thread_data", return_value={"workspace_path": workspace}))
        self.enterContext(patch("deerflow.config.app_config.get_app_config", return_value=SimpleNamespace(sandbox=None)))
        output = await tools.bash_tool.ainvoke({"runtime": self.runtime, "description": "这是一段不能执行的说明文字", "command": "printf 'actual_stdout'"})
        self.assertEqual(output.strip(), "actual_stdout")


if __name__ == "__main__":
    unittest.main()
