"""显式启用真实 Docker/AIO 注入与热复用验收；模型决策使用确定性测试图。"""

import json
import os
import platform
import shlex
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import yaml

from deerflow.skills.presets import SkillPresetStore
from temporary_environment_support import EnvironmentChatHarness, PROBE_NAMES, digest
from test_skill_presets import write_skill

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("DEER_FLOW_RUN_DOCKER_ENV_TESTS") == "1", "需显式启用独立的 Docker 环境变量测试")
class TemporaryEnvironmentDockerLiveTests(EnvironmentChatHarness):
    def docker(self, *arguments):
        result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=30, check=True)
        return result.stdout.strip()

    def temporary_parent(self):
        # Colima 通常共享项目所在的 /Users；macOS 的 /var/folders 临时目录不一定可挂载。
        return REPOSITORY_ROOT

    def sandbox_settings(self):
        return {
            "use": "deerflow.community.aio_sandbox:AioSandboxProvider",
            "image": self.image,
            "port": 18080,
            "container_prefix": self.prefix,
            "idle_timeout": 600,
            "replicas": 2,
            "ownership": {"type": "memory"},
            "bash_command_timeout": 30,
        }

    def setUp(self):
        config_path = Path(os.environ.get("DEER_FLOW_TEST_CONFIG_PATH", REPOSITORY_ROOT / "config.yaml"))
        sandbox = yaml.safe_load(config_path.read_text())["sandbox"]
        self.assertTrue(sandbox["use"].endswith(":AioSandboxProvider"))
        self.image = sandbox["image"]
        self.prefix = "deerflow-env-test-" + uuid4().hex[:10]
        self.docker("image", "inspect", self.image, "--format", "{{.Id}}")
        self.server = json.loads(self.docker("version", "--format", "{{json .Server}}"))
        self.addCleanup(self.verify_container_cleanup)
        super().setUp()
        self.assertEqual(self.provider._backend._runtime, "docker", "本测试必须使用真实 Docker 后端")

    def verify_container_cleanup(self):
        remaining = self.docker("ps", "-aq", "--filter", f"name={self.prefix}").split()
        # 即使验收失败也只清理本测试的独立命名前缀，不接触用户容器。
        for container in remaining:
            self.docker("rm", "-f", container)
        self.assertEqual(remaining, [], "提供方关闭后仍遗留测试容器")

    def warm_container(self, *, user="alice", thread="env_thread"):
        with self.provider._lock:
            matches = [entry[0] for sandbox_id, entry in self.provider._warm_pool.items() if self.provider._warm_pool_identity.get(sandbox_id) == (user, thread)]
        self.assertEqual(len(matches), 1, "对话完成后沙盒必须进入十分钟复用池")
        info = matches[0]
        container_id = self.docker("inspect", info.container_name, "--format", "{{.Id}}")
        self.assertEqual(container_id, info.container_id)
        self.assertEqual(self.docker("inspect", container_id, "--format", "{{.State.Running}}"), "true")
        return container_id

    def assert_real_container(self, result, expected_id=None):
        self.assertEqual(result["system"], "Linux")
        container_id = expected_id or self.warm_container()
        self.assertTrue(container_id.startswith(result["hostname"]))
        return container_id

    def test_real_chat_entrypoints_reuse_one_container_without_stale_values(self):
        special = "中文 ' \" \\ \n $HOME $(touch should_not_exist) `echo no` ; |"
        first_values = {"DF_ENV_TEMP": "first-turn", "DF_ENV_PUBLIC": "public-override", "DF_ENV_PRIVATE": "private-override", "DF_ENV_OLD_ONLY": "only-first", "DF_ENV_EMPTY": "", "DF_ENV_SPECIAL": special}
        first = self.managed(temporary_env=first_values)
        self.assert_environment(first, **first_values)
        container_id = self.assert_real_container(first)
        for variables in ({"DF_ENV_TEMP": "second-turn"}, {}, None):
            result = self.managed(temporary_env=variables)
            self.assert_environment(result, **(variables or {}))
            self.assertEqual(self.warm_container(), container_id)
            self.assert_real_container(result, container_id)
        with self.pool.session(user_id="alice") as client:
            self.assert_environment(json.loads(client.chat("probe", thread_id="env_thread", temporary_env={"DF_ENV_TEMP": "session-chat"})), DF_ENV_TEMP="session-chat")
            self.assertEqual(self.warm_container(), container_id)
            list(client.stream("probe", thread_id="env_thread", temporary_env={"DF_ENV_TEMP": "session-stream"}))
            message = self.saver.get_tuple({"configurable": {"thread_id": "env_thread"}}).checkpoint["channel_values"]["messages"][-1]
            self.assert_environment(json.loads(message.content), DF_ENV_TEMP="session-stream")
        self.assertEqual(self.warm_container(), container_id)
        self.store.mutate(None, "DF_ENV_PUBLIC", "updated-public", operation="update")
        self.assert_environment(self.managed(), DF_ENV_PUBLIC="updated-public")
        self.store.mutate(None, "DF_ENV_PUBLIC", None, operation="delete")
        self.store.mutate("alice", "DF_ENV_PRIVATE", None, operation="delete")
        self.assert_environment(self.managed(temporary_env={"DF_ENV_TEMP": "temporary-only"}), DF_ENV_PUBLIC=None, DF_ENV_PRIVATE=None, DF_ENV_TEMP="temporary-only")
        self.assert_environment(self.managed(), DF_ENV_PUBLIC=None, DF_ENV_PRIVATE=None)
        self.assertEqual(self.warm_container(), container_id)
        script = f"import json,os; print(json.dumps({{name: name in os.environ for name in {PROBE_NAMES!r}}}))"
        outside = json.loads(self.docker("exec", container_id, "python3", "-c", script))
        self.assertFalse(any(outside.values()), "变量不能写入容器全局环境")
        self.assertFalse(any(self.root.rglob("should_not_exist")))
        print(json.dumps({"case": "reuse", "host": platform.system(), "docker_arch": self.server["Arch"], "container_arch": first["machine"], "image": self.image, "container_id": container_id, "turns": 9}))

    def test_real_concurrent_users_and_parallel_tool_calls_are_isolated(self):
        pool = self.new_pool(size=2)
        barrier = threading.Barrier(2)
        self.before_choose = lambda: barrier.wait(timeout=30)
        with ThreadPoolExecutor(max_workers=2) as executor:
            alice = executor.submit(self.managed, "parallel", pool=pool, thread_id="alice_thread", temporary_env={"DF_ENV_TEMP": "alice-turn"})
            bob = executor.submit(self.managed, "parallel", pool=pool, user_id="bob", thread_id="bob_thread", temporary_env={"DF_ENV_TEMP": "bob-turn"})
            self.assert_environment(alice.result(timeout=120), DF_ENV_TEMP="alice-turn")
            self.assert_environment(bob.result(timeout=120), user="bob", DF_ENV_TEMP="bob-turn")
        self.before_choose = None
        alice_id = self.warm_container(thread="alice_thread")
        bob_id = self.warm_container(user="bob", thread="bob_thread")
        self.assertNotEqual(alice_id, bob_id)
        self.assert_environment(self.managed(pool=pool, thread_id="alice_thread"))
        self.assert_environment(self.managed(pool=pool, thread_id="bob_thread", user_id="bob", temporary_env={"DF_ENV_TEMP": "bob-next"}), user="bob", DF_ENV_TEMP="bob-next")
        self.assertEqual(self.warm_container(thread="alice_thread"), alice_id)
        self.assertEqual(self.warm_container(user="bob", thread="bob_thread"), bob_id)

    def test_real_cancel_failure_and_replay_keep_reused_container_clean(self):
        self.managed(temporary_env={"DF_ENV_OLD_ONLY": "previous"})
        container_id = self.warm_container()
        self.assert_environment(self.managed(replay_last_turn=True, temporary_env={"DF_ENV_TEMP": "new-replay"}), DF_ENV_TEMP="new-replay")
        self.assert_environment(self.managed(replay_last_turn=True))
        with self.assertRaisesRegex(RuntimeError, "合成测试失败"):
            self.managed("fail", temporary_env={"DF_ENV_TEMP": "failed"})
        self.assert_environment(self.managed())
        stream = self.pool.start_stream("wait", user_id="alice", thread_id="env_thread", temporary_env={"DF_ENV_TEMP": "cancelled"})
        try:
            deadline = time.monotonic() + 30
            while not self.started_marker().exists():
                self.assertLess(time.monotonic(), deadline, "真实容器中的命令尚未开始或宿主挂载不可见")
                time.sleep(0.05)
        finally:
            stream.close(timeout=15)
        self.assert_environment(self.managed())
        self.assertEqual(self.warm_container(), container_id)
        checkpoints = repr(list(self.saver.list(None)))
        for value in ("previous", "new-replay", "cancelled"):
            self.assertNotIn(value, checkpoints)

    def test_real_preset_snapshot_and_switch_preserve_container_and_environment(self):
        skills = self.root / "skills"
        for name in ("alpha", "beta"):
            write_skill(skills / "public", name)
        presets = SkillPresetStore(skills)
        selected = presets.create_preset("测试预设")["preset_id"]
        presets.update_preset(selected, skill_states={"alpha": True})
        script = (
            "import pathlib,json,os,hashlib; "
            "value=os.environ.get('DF_ENV_TEMP'); "
            "print(json.dumps({'skills':sorted(p.name for p in pathlib.Path('/mnt/skills/public').iterdir()), "
            "'temporary':hashlib.sha256(value.encode()).hexdigest() if value else None}))"
        )
        self.command = "python3 -c " + shlex.quote(script)

        def change_after_model_entry():
            self.assertEqual(self.docker("ps", "-aq", "--filter", f"name={self.prefix}"), "")
            presets.update_preset(selected, skill_states={"alpha": False, "beta": True})

        self.before_choose = change_after_model_entry
        first = self.managed(skill_preset=selected, temporary_env={"DF_ENV_TEMP": "first-turn"})
        self.assertEqual(first, {"skills": ["alpha"], "temporary": digest("first-turn")})
        container_id = self.warm_container()
        self.before_choose = None
        self.assertEqual(self.managed(skill_preset=selected), {"skills": ["beta"], "temporary": None})
        self.assertEqual(self.warm_container(), container_id)
        self.assertEqual(self.managed(), {"skills": ["alpha", "beta"], "temporary": None})
        self.assertEqual(self.warm_container(), container_id)


if __name__ == "__main__":
    unittest.main()
