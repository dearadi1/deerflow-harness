"""显式指定隔离 AIO 地址，验证真实命令终止、输出与会话复用，不调用模型。"""

import json
import os
import time
import unittest
from uuid import uuid4

from langchain.tools import tool

import test_agent_managed_stream as managed_support
from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.runtime.turn_cancellation import TurnCancellation, turn_cancellation_scope


@unittest.skipUnless(
    os.environ.get("DEER_FLOW_RUN_LIVE_TESTS") == "1" and os.environ.get("DEER_FLOW_TEST_AIO_URL"),
    "需显式启用真实测试并指定隔离的 DEER_FLOW_TEST_AIO_URL",
)
class AioManagedStreamLiveTests(unittest.TestCase):
    def setUp(self):
        base_url = os.environ["DEER_FLOW_TEST_AIO_URL"]
        self.sandbox = AioSandbox("managed-cancel-test", base_url, home_dir="/home/gem")
        self.observer = AioSandbox("managed-cancel-observer", base_url, home_dir="/home/gem")
        self.addCleanup(self.observer.close)
        self.addCleanup(self.sandbox.close)
        self.harness = managed_support.ManagedStreamTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def observe(self, command):
        # 使用显式观察会话，测试退出时也能清理所有服务端会话。
        with turn_cancellation_scope(TurnCancellation()):
            return self.observer.execute_command(command, timeout=5)

    def assert_child_stopped(self, marker):
        status = self.observe(
            f"pid=$(cat {marker}); if kill -0 $pid 2>/dev/null; then ps -p $pid -o stat=; else printf stopped; fi"
        ).strip()
        self.assertTrue("stopped" in status or status.startswith("Z"), status)
        self.observe(f"rm -f {marker}")

    def test_close_stops_remote_children_and_releases_checkpoint(self):
        for mode in ("full", "delta"):
            for variant in ("shell", "bash_env", "scoped_shell"):
                with self.subTest(mode=mode, variant=variant):
                    marker = f"/tmp/deerflow-managed-cancel-{uuid4().hex}.pid"
                    command = f"sleep 60 & child=$!; printf '%s' $child > {marker}; wait $child"
                    env = {"MANAGED_CANCEL_TEST": "1"} if variant == "bash_env" else None
                    scope = uuid4().hex if variant == "scoped_shell" else None

                    @tool("slow_script")
                    def slow_script() -> str:
                        """执行用于取消验收的长脚本。"""
                        return self.sandbox.execute_command_in_scope(command, env=env, timeout=120, scope_id=scope)

                    graph, pool = self.harness.tool_harness(slow_script, {}, mode, backend="sqlite")
                    stream = self.harness.start(pool)
                    deadline = time.monotonic() + 15
                    while "started" not in self.observe(f"test -s {marker} && printf started"):
                        self.assertLess(time.monotonic(), deadline, "远端命令未启动")
                        time.sleep(0.1)
                    started = time.monotonic()
                    stream.close(timeout=5)
                    elapsed = time.monotonic() - started
                    self.assert_child_stopped(marker)
                    self.assertFalse(graph.snapshot().next)
                    self.assertEqual(pool.stats["available"], 1)
                    command = "printf reentered"
                    events = list(self.harness.start(pool, "停止后立即重入", acquire_timeout=0))
                    self.assertEqual(events[-1].type, "end")
                    self.assertEqual(graph.snapshot().values["messages"][-1].content, "reentered")
                    with turn_cancellation_scope(TurnCancellation()):
                        self.assertIn("reusable", self.sandbox.execute_command("printf reusable", timeout=5))
                    print(json.dumps({"mode": mode, "variant": variant, "close_seconds": round(elapsed, 3)}))

    def test_single_request_preserves_output_shell_state_and_deadlines(self):
        with turn_cancellation_scope(TurnCancellation()):
            output = self.sandbox.execute_command("export MANAGED_POLL_STATE=kept; printf alpha; sleep 0.2; printf omega", timeout=5)
            self.assertEqual(output.strip(), "alphaomega")
            self.assertEqual(self.sandbox.execute_command('printf "$MANAGED_POLL_STATE"', timeout=5).strip(), "kept")
            output = self.sandbox.execute_command('printf "$TEST_VALUE"; sleep 0.2; printf "$TEST_VALUE"', env={"TEST_VALUE": "chunk"}, timeout=5)
            self.assertEqual(output.strip(), "chunkchunk")
            for env in (None, {"TEST_VALUE": "unused"}):
                with self.subTest(env=env):
                    marker = f"/tmp/deerflow-managed-deadline-{uuid4().hex}.pid"
                    started = time.monotonic()
                    output = self.sandbox.execute_command(
                        f"sleep 60 & child=$!; printf '%s' $child > {marker}; wait $child", env=env, timeout=0.5,
                    )
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertIn("Exit Code: 124", output)
                    self.assert_child_stopped(marker)


if __name__ == "__main__":
    unittest.main()
