import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_upstream.py"
PREFIX = "backend/packages/harness"


def git(directory, *arguments):
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def initialize(directory, branch):
    directory.mkdir()
    git(directory, "init", "-b", branch)
    git(directory, "config", "user.name", "同步测试")
    git(directory, "config", "user.email", "sync-test@example.invalid")
    git(directory, "config", "commit.gpgsign", "false")
    git(directory, "config", "core.hooksPath", str(directory / ".no-hooks"))


def write_file(directory, relative, content):
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def commit(directory):
    git(directory, "add", ".")
    git(directory, "commit", "-m", "test: 构造同步场景")
    return git(directory, "rev-parse", "HEAD")


class SyncUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.source = self.workspace / "source"
        self.target = self.workspace / "target"
        self.cache = self.workspace / "cache"
        self.prepare_source()
        self.prepare_target()

    def prepare_source(self):
        initialize(self.source, "chore/source")
        write_file(self.source, f"{PREFIX}/deerflow/core.py", "value = 1\n")
        write_file(self.source, f"{PREFIX}/pyproject.toml", '[project]\nname = "fixture"\n')
        write_file(self.source, "LICENSE", "测试许可证\n")
        self.source_commit = commit(self.source)
        self.snapshot = git(self.source, "subtree", "split", f"--prefix={PREFIX}", "HEAD")

    def prepare_target(self):
        initialize(self.target, "feat/customization")
        git(self.target, "fetch", str(self.source), self.snapshot)
        git(self.target, "reset", "--hard", "FETCH_HEAD")
        git(self.target, "branch", "chore/upstream_module")
        (self.target / "scripts").mkdir()
        shutil.copyfile(SCRIPT, self.target / "scripts/sync_upstream.py")
        shutil.copyfile(self.source / "LICENSE", self.target / "LICENSE")
        settings = {
            "repository": str(self.source), "branch": "chore/source", "prefix": PREFIX,
            "initial_source_commit": self.source_commit, "initial_split_commit": self.snapshot,
            "license_sha256": hashlib.sha256((self.source / "LICENSE").read_bytes()).hexdigest(),
        }
        write_file(self.target, "UPSTREAM.json", json.dumps(settings) + "\n")
        commit(self.target)
        git(self.target, "branch", "main")

    def sync(self):
        return subprocess.run(
            [sys.executable, str(self.target / "scripts/sync_upstream.py"),
             "--cache", str(self.cache)],
            capture_output=True, text=True,
        )

    def update_source(self, relative="deerflow/new.py", content="new_value = 2\n"):
        write_file(self.source, f"{PREFIX}/{relative}", content)
        return commit(self.source)

    def test_update_preserves_customization_and_main(self):
        write_file(self.target, "deerflow/custom.py", "custom_value = 3\n")
        commit(self.target)
        original_main = git(self.target, "rev-parse", "main")
        source_commit = self.update_source()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.target / "deerflow/custom.py").exists())
        self.assertTrue((self.target / "deerflow/new.py").exists())
        self.assertEqual(git(self.target, "rev-parse", "main"), original_main)
        self.assertEqual(git(self.target, "branch", "--show-current"),
                         f"chore/sync_upstream_{source_commit[:12]}")
        self.assertIn(source_commit, git(self.target, "log", "-1", "--format=%B"))
        self.assertEqual(len(git(self.target, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        self.assertEqual(git(self.target, "status", "--porcelain"), "")

    def test_repeated_sync_creates_no_commit(self):
        self.update_source()
        self.assertEqual(self.sync().returncode, 0)
        previous = git(self.target, "rev-parse", "HEAD")
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), previous)
        self.assertIn("无需创建提交", result.stdout)

    def test_unrelated_update_creates_no_commit(self):
        previous = git(self.target, "rev-parse", "HEAD")
        write_file(self.source, "frontend/index.js", "export default {};\n")
        commit(self.source)
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), previous)
        self.assertFalse((self.target / "frontend").exists())

    def test_conflict_preserves_merge_state(self):
        write_file(self.target, "deerflow/core.py", "value = 100\n")
        original_head = commit(self.target)
        self.update_source("deerflow/core.py", "value = 200\n")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("解决冲突", result.stderr)
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), original_head)
        self.assertTrue((self.target / ".git/MERGE_HEAD").exists())
        self.assertIn("UU deerflow/core.py", git(self.target, "status", "--porcelain"))

    def test_dirty_worktree_is_untouched(self):
        write_file(self.target, "deerflow/core.py", "value = 123\n")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未提交修改", result.stderr)
        self.assertFalse(self.cache.exists())
        self.assertEqual((self.target / "deerflow/core.py").read_text(), "value = 123\n")

    def test_rewritten_history_is_rejected(self):
        git(self.source, "switch", "--orphan", "chore/rewrite")
        write_file(self.source, f"{PREFIX}/deerflow/core.py", "value = 999\n")
        write_file(self.source, "LICENSE", "测试许可证\n")
        commit(self.source)
        git(self.source, "branch", "-f", "chore/source", "HEAD")
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无法快进", result.stderr)
        self.assertEqual(git(self.target, "rev-parse", "chore/upstream_module"), self.snapshot)

    def test_license_change_is_rejected(self):
        write_file(self.source, "LICENSE", "修改后的许可证\n")
        commit(self.source)
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("许可证已变化", result.stderr)
        self.assertEqual((self.target / "LICENSE").read_text(), "测试许可证\n")


if __name__ == "__main__":
    unittest.main()
