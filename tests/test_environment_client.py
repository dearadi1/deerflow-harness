"""验证变量持久化、用户隔离、并发冲突和 bash 字面量往返。"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from deerflow.environment import EnvironmentStore
from deerflow.environment_client import DeerFlowEnvironmentClient


class EnvironmentClientTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {"DEER_FLOW_HOME": str(self.root)}))
        self.enterContext(patch("deerflow.environment_client.load_client_config"))
        self.client = DeerFlowEnvironmentClient()

    def test_public_and_private_crud_and_isolation(self):
        public = self.client.public
        public.create_variable("PUBLIC", "shared")
        self.client.create_variable("alice", "PRIVATE", "alice value")
        self.client.create_variable("bob", "PRIVATE", "bob value")
        self.assertEqual(public.list_variables(), {"PUBLIC": "shared"})
        self.assertEqual(self.client.list_variables("alice"), {"PRIVATE": "alice value"})
        self.assertEqual(self.client.get_variable("bob", "PRIVATE"), "bob value")
        self.assertEqual(self.client.get_effective_variables("alice"), {"PUBLIC": "shared", "PRIVATE": "alice value"})
        public.update_variable("PUBLIC", "new")
        self.client.update_variable("alice", "PRIVATE", "updated")
        self.assertEqual(public.get_variable("PUBLIC"), "new")
        self.assertEqual(self.client.get_variable("alice", "PRIVATE"), "updated")
        public.delete_variable("PUBLIC")
        self.client.delete_variable("alice", "PRIVATE")
        self.assertEqual(self.client.get_effective_variables("alice"), {})
        self.assertEqual(self.client.list_variables("bob"), {"PRIVATE": "bob value"})
        self.assertTrue((self.root / ".bashrc").is_file())
        self.assertTrue((self.root / "users/alice/.bashrc").is_file())

    def test_conflicts_in_both_directions_do_not_change_files(self):
        self.client.public.create_variable("SHARED", "public")
        with self.assertRaisesRegex(ValueError, "重名"):
            self.client.create_variable("alice", "SHARED", "private")
        self.client.create_variable("alice", "PERSONAL", "private")
        public_before = (self.root / ".bashrc").read_bytes()
        with self.assertRaisesRegex(ValueError, "重名"):
            self.client.public.create_variable("PERSONAL", "public")
        self.assertEqual((self.root / ".bashrc").read_bytes(), public_before)
        self.assertEqual(self.client.list_variables("alice"), {"PERSONAL": "private"})

    def test_duplicate_create_and_missing_update_delete_are_explicit(self):
        self.client.public.create_variable("NAME", "original")
        with self.assertRaises(ValueError):
            self.client.public.create_variable("NAME", "replacement")
        for operation in (lambda: self.client.public.update_variable("MISSING", "value"), lambda: self.client.public.delete_variable("MISSING")):
            with self.assertRaises(KeyError):
                operation()
        self.assertEqual(self.client.public.get_variable("NAME"), "original")

    def test_private_operations_require_valid_user_id(self):
        for user_id in (None, "", " ", "../alice", "alice/bob", "alice\\bob", 1):
            with self.subTest(user_id=user_id):
                with self.assertRaises(ValueError):
                    self.client.create_variable(user_id, "NAME", "value")
                with self.assertRaises(ValueError):
                    self.client.get_effective_variables(user_id)
        self.assertFalse((self.root / ".bashrc").exists())

    def test_invalid_variable_names_and_values_are_rejected(self):
        for name in ("", "1NAME", "A=B", "A;id", "A\n", "中文", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.client.public.create_variable(name, "value")
        for value in (None, 1, {}, "before\0after"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.client.public.create_variable("NAME", value)

    def test_file_round_trip_matches_real_bash_without_expansion(self):
        marker = self.root / "must_not_exist"
        values = {
            "TEXT": "中文 and spaces",
            "QUOTES": "single' double\" slash\\ hash#",
            "MULTILINE": "first\nsecond\r\nthird",
            "SHELL": f"$(touch {marker}) `touch {marker}` $HOME ; && |",
            "EMPTY": "",
        }
        for name, value in values.items():
            self.client.public.create_variable(name, value)
        self.assertEqual(DeerFlowEnvironmentClient().public.list_variables(), values)
        script = "import json,os; print(json.dumps({name: os.environ[name] for name in " + repr(list(values)) + "}))"
        command = f"source {shlex.quote(str(self.root / '.bashrc'))}; {shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), values)
        self.assertFalse(marker.exists())
        self.assertEqual((self.root / ".bashrc").stat().st_mode & 0o777, 0o600)

    def test_invalid_existing_file_is_not_executed_or_overwritten(self):
        marker = self.root / "must_not_exist"
        content = f"export GOOD='value'\ntouch {marker}\n"
        (self.root / ".bashrc").write_text(content)
        with self.assertRaisesRegex(ValueError, "格式无效"):
            self.client.public.create_variable("NEW", "value")
        self.assertEqual((self.root / ".bashrc").read_text(), content)
        self.assertFalse(marker.exists())

    def test_duplicate_or_manually_conflicting_files_fail_closed(self):
        path = self.root / ".bashrc"
        path.write_text("export NAME=one\nexport NAME=two\n")
        with self.assertRaises(ValueError):
            self.client.public.list_variables()
        path.write_text("export NAME=public\n")
        user = self.root / "users/alice"
        user.mkdir(parents=True)
        (user / ".bashrc").write_text("export NAME=private\n")
        with self.assertRaisesRegex(ValueError, "重名"):
            self.client.get_effective_variables("alice")
        self.client.delete_variable("alice", "NAME")
        self.assertEqual(self.client.get_effective_variables("alice"), {"NAME": "public"})

    def test_symlinked_file_user_directory_and_lock_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        target = outside / "target"
        target.write_text("export NAME=external\n")
        users = self.root / "users"
        users.mkdir()
        (users / "alice").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.client.list_variables("alice")
        (self.root / ".bashrc").symlink_to(target)
        with self.assertRaises(ValueError):
            self.client.public.update_variable("NAME", "changed")
        (self.root / ".bashrc").unlink()
        (self.root / "..bashrc.lock").symlink_to(target)
        with self.assertRaises(ValueError):
            self.client.public.create_variable("NAME", "changed")
        self.assertEqual(target.read_text(), "export NAME=external\n")

    def test_atomic_write_failure_keeps_old_values(self):
        self.client.public.create_variable("NAME", "original")
        with patch("deerflow.environment.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.client.public.update_variable("NAME", "replacement")
        self.assertEqual(self.client.public.get_variable("NAME"), "original")
        self.assertEqual(list(self.root.glob(".bashrc-*.tmp")), [])

    def test_parallel_clients_do_not_lose_updates(self):
        def create(index):
            EnvironmentStore().mutate(None, f"NAME_{index}", str(index), operation="create")

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(create, range(24)))
        self.assertEqual(len(self.client.public.list_variables()), 24)

    def test_cross_process_public_private_conflict_has_one_winner(self):
        script = """
import sys, time
from deerflow.environment import EnvironmentStore
while not __import__('pathlib').Path(sys.argv[1]).exists():
    time.sleep(0.01)
try:
    EnvironmentStore().mutate(None if sys.argv[2] == 'public' else 'alice', 'RACE', sys.argv[2], operation='create')
except ValueError:
    sys.exit(2)
"""
        start = self.root / "start"
        processes = [subprocess.Popen([sys.executable, "-c", script, str(start), scope], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for scope in ("public", "private")]
        try:
            start.touch()
            for process in processes:
                _, stderr = process.communicate(timeout=30)
                self.assertIn(process.returncode, (0, 2), stderr)
            self.assertEqual(sorted(process.returncode for process in processes), [0, 2])
            self.assertEqual(len(self.client.get_effective_variables("alice")), 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
