"""验证托管 AIO 使用单次原生异步请求，保留原请求参数和输出语义。"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.runtime.turn_cancellation import TurnCancellation, turn_cancellation_scope


class AioManagedRequestsTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = AioSandbox("request-test", "http://127.0.0.1:1", home_dir="/home/test", request_headers={"X-Relay": "test-control"})
        self.addCleanup(self.sandbox.close)
        self.client = self.enterContext(patch.object(self.sandbox, "_client", Mock()))
        self.enterContext(patch.object(self.sandbox, "_create_shell_session", return_value="shell_1"))
        self.enterContext(patch.object(self.sandbox, "_create_bash_session", return_value="bash_1"))
        self.requests = []
        self.transports = []
        self.client_type = httpx.AsyncClient
        self.enterContext(patch("deerflow.community.aio_sandbox.aio_sandbox.httpx.AsyncClient", side_effect=self.transport))
        self.addCleanup(setattr, self.sandbox, "_recovery_session_id", None)

    def transport(self, **kwargs):
        client = self.client_type(transport=httpx.MockTransport(self.respond), **kwargs)
        self.transports.append(client)
        return client

    def respond(self, request):
        self.requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/v1/shell/exec":
            data = {"session_id": body["id"], "command": body["command"], "status": "completed", "output": "current", "exit_code": 0}
        elif request.url.path == "/v1/bash/exec":
            data = {"session_id": body["session_id"], "command_id": "cmd_1", "command": body["command"], "status": "completed", "stdout": "result", "stderr": "error", "exit_code": 2}
        else:
            self.fail(f"不应轮询：{request.url.path}")
        return httpx.Response(200, json={"success": True, "data": data})

    def test_shell_uses_one_request_with_original_command_timeout_and_headers(self):
        with turn_cancellation_scope(TurnCancellation()):
            self.assertEqual(self.sandbox.execute_command("script", timeout=120), "current")
        self.assertEqual(len(self.requests), 1)
        body = json.loads(self.requests[0].content)
        self.assertNotIn("async_mode", body)
        self.assertEqual(body["hard_timeout"], 120)
        self.assertEqual(body["id"], "shell_1")
        self.assertEqual(self.requests[0].headers["X-Relay"], "test-control")
        self.assertEqual(self.transports[0].timeout.read, 125)
        self.assertTrue(self.transports[0].is_closed)
        self.client.shell.exec_command.assert_not_called()

    def test_env_bash_keeps_stdin_env_stdout_stderr_and_exit_code(self):
        with turn_cancellation_scope(TurnCancellation()):
            output = self.sandbox.execute_command("script", env={"KEY": "value"})
        self.assertEqual(output, "result\nStd Error:\nerror\nExit Code: 2")
        self.assertEqual(len(self.requests), 1)
        body = json.loads(self.requests[0].content)
        self.assertNotIn("async_mode", body)
        self.assertEqual(body["command"], "exec < /dev/null\nscript")
        self.assertEqual(body["env"], {"KEY": "value"})
        self.client.bash.close_session.assert_called_once()
        self.assertTrue(self.transports[0].is_closed)

    def test_sync_shell_keeps_original_request_path(self):
        self.client.shell.exec_command.return_value = SimpleNamespace(data=SimpleNamespace(status="completed", output="sync", exit_code=0))
        self.assertEqual(self.sandbox.execute_command("script"), "sync")
        self.assertNotIn("async_mode", self.client.shell.exec_command.call_args.kwargs)
        self.assertNotIn("id", self.client.shell.exec_command.call_args.kwargs)
        self.assertEqual(self.requests, [])

    def test_cancel_before_submission_does_not_create_session(self):
        cancellation = TurnCancellation()
        cancellation.set()
        with turn_cancellation_scope(cancellation), self.assertRaises(asyncio.CancelledError):
            self.sandbox.execute_command("script")
        self.sandbox._create_shell_session.assert_not_called()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
