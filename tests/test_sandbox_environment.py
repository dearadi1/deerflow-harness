"""使用真实本地进程验证复用沙盒的变量刷新，并覆盖远程适配器的传输边界。"""

import asyncio
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from deerflow.environment import EnvironmentStore
from deerflow.sandbox import tools
from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.sandbox.middleware import SandboxMiddleware


class SandboxEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {"DEER_FLOW_HOME": str(self.root)}))
        self.store = EnvironmentStore()
        self.sandbox = LocalSandbox("local:alice:thread")
        self.runtime = tools.Runtime(context={"user_id": "alice", "thread_id": "thread"}, state={}, config={}, stream_writer=lambda value: None, tool_call_id="call_1", store=None)
        self.provider = SimpleNamespace(
            acquire=Mock(return_value=self.sandbox.id),
            acquire_async=AsyncMock(return_value=self.sandbox.id),
            get=Mock(return_value=self.sandbox),
            get_scoped=Mock(return_value=self.sandbox),
        )
        self.enterContext(patch.object(tools, "get_sandbox_provider", return_value=self.provider))
        self.enterContext(patch.object(tools, "authorize_sandbox_execution"))
        self.enterContext(patch.object(tools, "authorize_sandbox_execution_async", new_callable=AsyncMock))
        self.enterContext(patch.object(tools, "safe_app_config", return_value=None))
        self.enterContext(patch.object(tools, "safe_app_config_async", new_callable=AsyncMock, return_value=None))

    def write(self, user_id, name, value, operation="create"):
        self.store.mutate(user_id, name, value, operation=operation)

    def prepare_bash_tool(self, *, local=True):
        self.enterContext(patch.object(tools, "ensure_thread_directories_exist"))
        self.enterContext(patch.object(tools, "is_local_sandbox", return_value=local))
        self.enterContext(patch.object(tools, "is_host_bash_allowed", return_value=True))
        self.enterContext(patch.object(tools, "get_thread_data", return_value={"workspace_path": str(self.root)}))
        self.enterContext(patch("deerflow.config.app_config.get_app_config", return_value=SimpleNamespace(sandbox=None)))

    def test_reused_local_sandbox_refreshes_create_update_delete_and_external_writes(self):
        sandbox = tools.ensure_sandbox_initialized(self.runtime)
        self.write(None, "DF_SHARED", "one")
        self.write("alice", "DF_PRIVATE", "alice")
        self.write("bob", "DF_PRIVATE", "bob")
        command = 'printf "%s/%s" "${DF_SHARED-unset}" "${DF_PRIVATE-unset}"'
        self.assertEqual(sandbox.execute_command(command).strip(), "one/alice")
        self.write(None, "DF_SHARED", "two", "update")
        self.write("alice", "DF_PRIVATE", "new", "update")
        self.assertIs(tools.ensure_sandbox_initialized(self.runtime), sandbox)
        self.assertEqual(sandbox.execute_command(command).strip(), "two/new")
        (self.root / "users/alice/.bashrc").write_text("export DF_PRIVATE=external\n")
        self.assertEqual(sandbox.execute_command(command).strip(), "two/external")
        self.write(None, "DF_SHARED", None, "delete")
        self.write("alice", "DF_PRIVATE", None, "delete")
        self.assertEqual(sandbox.execute_command(command).strip(), "unset/unset")
        self.provider.acquire.assert_called_once()

    def test_async_acquire_and_reuse_bind_the_same_identity(self):
        sandbox = asyncio.run(tools.ensure_sandbox_initialized_async(self.runtime))
        self.write("alice", "DF_PRIVATE", "private")
        self.assertEqual(sandbox.execute_command('printf "%s" "$DF_PRIVATE"').strip(), "private")
        self.assertIs(asyncio.run(tools.ensure_sandbox_initialized_async(self.runtime)), sandbox)
        self.provider.acquire_async.assert_awaited_once_with("thread", user_id="alice")
        self.provider.acquire.assert_not_called()

    def test_eager_acquisition_also_binds_environment(self):
        self.write("alice", "DF_PRIVATE", "private")
        with patch("deerflow.sandbox.middleware.get_sandbox_provider", return_value=self.provider):
            middleware = SandboxMiddleware()
            middleware._acquire_sandbox("thread", user_id="alice", owner_id=None)
            asyncio.run(middleware._acquire_sandbox_async("thread", user_id="alice", owner_id=None))
        self.assertEqual(self.sandbox.execute_command('printf "%s" "$DF_PRIVATE"').strip(), "private")

    def test_identity_cannot_be_rebound_to_another_user(self):
        self.sandbox.bind_environment("alice")
        with self.assertRaisesRegex(ValueError, "其他用户"):
            self.sandbox.bind_environment("bob")

    def test_explicit_env_wins_and_does_not_persist_or_change_host(self):
        self.write(None, "DF_SETTING", "managed")
        sandbox = tools.ensure_sandbox_initialized(self.runtime)
        sandbox.environment["DF_SETTING"] = "configured"
        command = 'printf "%s" "$DF_SETTING"'
        self.assertEqual(sandbox.execute_command(command, env={"DF_SETTING": "request"}).strip(), "request")
        self.assertEqual(sandbox.execute_command(command).strip(), "managed")
        self.write(None, "DF_SETTING", None, "delete")
        self.assertEqual(sandbox.execute_command(command).strip(), "configured")
        self.assertNotIn("DF_SETTING", os.environ)

    def test_bash_sync_async_and_subagent_calls_see_updates_and_redact_secrets(self):
        self.prepare_bash_tool()
        self.write(None, "DF_SETTING", "first")
        self.write("alice", "DF_API_TOKEN", "sensitive-example-value")
        command = 'printf "%s/%s" "$DF_SETTING" "$DF_API_TOKEN"'
        result = tools.bash_tool.func(runtime=self.runtime, command=command)
        self.assertIn("first/", result)
        self.assertNotIn("sensitive-example-value", result)
        self.write(None, "DF_SETTING", "second", "update")
        self.runtime.context["sandbox_command_scope_id"] = "child"
        result = asyncio.run(tools.bash_tool.ainvoke({"runtime": self.runtime, "command": command}))
        self.assertIn("second/", result)
        self.assertNotIn("sensitive-example-value", result)

    def test_duck_typed_remote_provider_receives_managed_values(self):
        self.prepare_bash_tool(local=False)
        sandbox = SimpleNamespace(execute_command=Mock(return_value="ordinary sensitive-example-value"), lark_cli_broker=False)
        self.provider.get.return_value = sandbox
        self.provider.get_scoped.return_value = sandbox
        self.write(None, "DF_SETTING", "ordinary")
        self.write("alice", "DF_API_TOKEN", "sensitive-example-value")
        result = tools.bash_tool.func(runtime=self.runtime, command="printenv")
        self.assertIn("ordinary", result)
        self.assertNotIn("sensitive-example-value", result)
        self.assertEqual(sandbox.execute_command.call_args.kwargs["env"], {"DF_SETTING": "ordinary", "DF_API_TOKEN": "sensitive-example-value"})
        self.assertNotIn("sensitive-example-value", sandbox.execute_command.call_args.args[0])

    def test_corrupt_environment_prevents_command_execution(self):
        self.prepare_bash_tool()
        (self.root / ".bashrc").write_text("echo not-an-assignment\n")
        marker = self.root / "not_executed"
        result = tools.bash_tool.func(runtime=self.runtime, command=f"touch {shlex.quote(str(marker))}")
        self.assertIn("Error:", result)
        self.assertFalse(marker.exists())

    def test_update_between_tool_snapshot_and_transport_does_not_inject_unredacted_secret(self):
        self.prepare_bash_tool()
        execute = tools._execute_bash_command

        def update_then_execute(*args, **kwargs):
            self.write("alice", "DF_API_TOKEN", "new-sensitive-value")
            return execute(*args, **kwargs)

        command = 'printf "%s" "${DF_API_TOKEN-unset}"'
        with patch.object(tools, "_execute_bash_command", side_effect=update_then_execute):
            result = tools.bash_tool.func(runtime=self.runtime, command=command)
        self.assertEqual(result.strip(), "unset")
        result = tools.bash_tool.func(runtime=self.runtime, command=command)
        self.assertNotIn("new-sensitive-value", result)
        self.assertNotIn("unset", result)


class RemoteEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {"DEER_FLOW_HOME": str(self.root)}))
        self.store = EnvironmentStore()
        self.calls = []

    def run_shell(self, command, env=None, timeout=None):
        self.calls.append((command, dict(env or {})))
        result = subprocess.run(["bash", "-c", command], env={"PATH": os.defpath, **(env or {})}, capture_output=True, text=True, timeout=timeout or 10)
        return SimpleNamespace(stdout=result.stdout, stderr=result.stderr, stdout_text=result.stdout, stderr_text=result.stderr, exit_code=result.returncode, timed_out=False)

    def aio_sandbox(self):
        from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox

        self.enterContext(patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"))
        self.enterContext(patch("deerflow.community.aio_sandbox.aio_sandbox.sandbox_http_trust_env", return_value=True))
        sandbox = AioSandbox("aio", "https://sandbox.invalid")
        self.enterContext(patch.object(sandbox, "_execute_with_env", side_effect=lambda command, env, timeout: self.run_shell(command, env, timeout).stdout))
        self.enterContext(patch.object(sandbox, "_ensure_default_shell_session_id", return_value=None))
        self.enterContext(patch.object(sandbox, "_exec_shell", side_effect=lambda client, command, **kwargs: (self.run_shell(command).stdout, 0, "completed")))
        return sandbox

    def remote_sandboxes(self):
        from deerflow.community.boxlite.box import BoxliteBox
        from deerflow.community.e2b_sandbox.e2b_sandbox import E2BSandbox
        from deerflow.community.opensandbox.sandbox import OpenSandboxSandbox
        from deerflow.community.tenki.sandbox import TenkiSandbox

        box = BoxliteBox("box", Mock(), Mock())
        self.enterContext(patch.object(box, "_exec", side_effect=lambda *args, **kwargs: self.run_shell(args[-1], **kwargs)))
        tenki = TenkiSandbox("tenki", Mock())
        self.enterContext(patch.object(tenki, "_sh", side_effect=self.run_shell))
        e2b = E2BSandbox("e2b", SimpleNamespace(commands=SimpleNamespace(run=lambda command, envs=None, **kwargs: self.run_shell(command, envs, **kwargs))))
        opensandbox = OpenSandboxSandbox("open", Mock(), run_command_opts_cls=Mock())
        self.enterContext(patch.object(opensandbox, "_run", side_effect=self.run_shell))
        self.enterContext(patch("deerflow.community.opensandbox.sandbox.format_execution", side_effect=lambda execution: execution.stdout))
        return [self.aio_sandbox(), box, tenki, e2b, opensandbox]

    def test_every_remote_adapter_refreshes_environment_on_reused_instance(self):
        sandboxes = self.remote_sandboxes()
        for sandbox in sandboxes:
            sandbox.bind_environment("alice")
        command = 'printf "%s" "${DF_MANAGED-unset}"'
        self.store.mutate(None, "DF_MANAGED", "first", operation="create")
        for sandbox in sandboxes:
            with self.subTest(provider=type(sandbox).__name__, operation="create"):
                self.assertEqual(sandbox.execute_command(command).strip(), "first")
        self.store.mutate(None, "DF_MANAGED", "updated", operation="update")
        for sandbox in sandboxes:
            with self.subTest(provider=type(sandbox).__name__, operation="update"):
                self.assertEqual(sandbox.execute_command(command).strip(), "updated")
        self.store.mutate(None, "DF_MANAGED", None, operation="delete")
        for sandbox in sandboxes:
            with self.subTest(provider=type(sandbox).__name__, operation="delete"):
                self.assertEqual(sandbox.execute_command(command).strip(), "unset")

    def test_aio_subagent_scope_uses_fresh_structured_env_for_each_call(self):
        sandbox = self.aio_sandbox()
        sandbox.bind_environment("alice")
        self.store.mutate("alice", "DF_PRIVATE", "private-first", operation="create")
        command = 'printf "%s" "$DF_PRIVATE"'
        self.assertEqual(sandbox.execute_command_in_scope(command, scope_id="child"), "private-first")
        self.store.mutate("alice", "DF_PRIVATE", "private-new", operation="update")
        self.assertEqual(sandbox.execute_command_in_scope(command, scope_id="child"), "private-new")
        self.assertEqual(sandbox._scoped_shell_sessions, {})
        self.assertEqual(self.calls, [(command, {"DF_PRIVATE": "private-first"}), (command, {"DF_PRIVATE": "private-new"})])


if __name__ == "__main__":
    unittest.main()
