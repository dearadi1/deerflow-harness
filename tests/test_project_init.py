"""验证初始化命令在无运行时依赖时的复制行为和已有文件保护。"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deerflow.cli import main
from deerflow.project_init import initialize_project

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FILES = {
    ".env": ".env.example",
    "config.yaml": "config.example.yaml",
    ".deer-flow/agents/voris/config.yaml": ".deer-flow/agents/voris/config.yaml",
    ".deer-flow/agents/voris/SOUL.md": ".deer-flow/agents/voris/SOUL.md",
}


class ProjectInitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = self.root / "project"

    def invoke(self, arguments):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = main(arguments)
        return status, output.getvalue(), errors.getvalue()

    def assert_template_contents(self):
        actual_files = {path.relative_to(self.target).as_posix() for path in self.target.rglob("*") if path.is_file()}
        self.assertEqual(actual_files, set(EXPECTED_FILES))
        for relative, source in EXPECTED_FILES.items():
            with self.subTest(relative=relative):
                self.assertEqual((self.target / relative).read_bytes(), (PROJECT_ROOT / source).read_bytes())

    def test_explicit_target_creates_only_templates_with_original_bytes(self):
        status, output, errors = self.invoke(["init", "--target", str(self.target)])
        self.assertEqual(status, 0, errors)
        self.assertIn(str(self.target), output)
        self.assertEqual(output.count("创建："), 4)
        self.assert_template_contents()

    def test_default_target_uses_invocation_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            status, _, errors = self.invoke(["init"])
        finally:
            os.chdir(previous)
        self.assertEqual(status, 0, errors)
        self.target = self.root
        self.assert_template_contents()

    def test_relative_target_uses_invocation_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            status, _, errors = self.invoke(["init", "--target", "nested/project"])
        finally:
            os.chdir(previous)
        self.assertEqual(status, 0, errors)
        self.target = self.root / "nested/project"
        self.assert_template_contents()

    def test_repeat_preserves_modified_files_and_restores_missing_files(self):
        initialize_project(self.target)
        (self.target / ".env").write_text("CUSTOM=1\n", encoding="utf-8")
        (self.target / "config.yaml").unlink()
        status, output, errors = self.invoke(["init", "--target", str(self.target)])
        self.assertEqual(status, 0, errors)
        self.assertEqual((self.target / ".env").read_text(), "CUSTOM=1\n")
        self.assertEqual((self.target / "config.yaml").read_bytes(), (PROJECT_ROOT / "config.example.yaml").read_bytes())
        self.assertEqual(output.count("跳过："), 3)
        self.assertEqual(output.count("创建："), 1)

    def test_force_replaces_templates_and_preserves_unrelated_data(self):
        initialize_project(self.target)
        for relative in EXPECTED_FILES:
            (self.target / relative).write_text("自定义内容", encoding="utf-8")
        extra = self.target / ".deer-flow/users/alice/data.bin"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"user data")
        status, output, errors = self.invoke(["init", "--target", str(self.target), "--force"])
        self.assertEqual(status, 0, errors)
        self.assertEqual(output.count("覆盖："), 4)
        self.assertEqual(extra.read_bytes(), b"user data")
        extra.unlink()
        self.assert_template_contents()

    def test_force_on_empty_target_reports_created_files(self):
        status, output, errors = self.invoke(["init", "--target", str(self.target), "--force"])
        self.assertEqual(status, 0, errors)
        self.assertEqual(output.count("创建："), 4)
        self.assert_template_contents()

    def test_nested_type_conflict_is_reported_before_any_copy(self):
        self.target.mkdir()
        (self.target / ".deer-flow").write_text("保留", encoding="utf-8")
        status, _, errors = self.invoke(["init", "--target", str(self.target)])
        self.assertEqual(status, 1)
        self.assertIn("应为目录", errors)
        self.assertFalse((self.target / ".env").exists())
        self.assertFalse((self.target / "config.yaml").exists())

    def test_existing_directory_at_file_path_is_rejected_even_with_force(self):
        (self.target / "config.yaml").mkdir(parents=True)
        status, _, errors = self.invoke(["init", "--target", str(self.target), "--force"])
        self.assertEqual(status, 1)
        self.assertIn("应为普通文件", errors)
        self.assertFalse((self.target / ".env").exists())

    def test_parent_file_is_rejected(self):
        self.target.write_text("保留", encoding="utf-8")
        status, _, errors = self.invoke(["init", "--target", str(self.target / "nested")])
        self.assertEqual(status, 1)
        self.assertIn("应为目录", errors)
        self.assertEqual(self.target.read_text(), "保留")

    def test_file_symlink_is_rejected_without_changing_referent(self):
        self.target.mkdir()
        outside = self.root / "outside.env"
        outside.write_text("SECRET=1", encoding="utf-8")
        (self.target / ".env").symlink_to(outside)
        for options in ([], ["--force"]):
            with self.subTest(options=options):
                status, _, errors = self.invoke(["init", "--target", str(self.target), *options])
                self.assertEqual(status, 1)
                self.assertIn("符号链接", errors)
                self.assertEqual(outside.read_text(), "SECRET=1")
                self.assertFalse((self.target / "config.yaml").exists())

    def test_directory_symlink_is_rejected_before_any_copy(self):
        self.target.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.target / ".deer-flow").symlink_to(outside, target_is_directory=True)
        status, _, errors = self.invoke(["init", "--target", str(self.target)])
        self.assertEqual(status, 1)
        self.assertIn("符号链接", errors)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.target / ".env").exists())

    def test_missing_template_fails_before_creating_target(self):
        with patch("deerflow.project_init._read_templates", side_effect=FileNotFoundError("缺少模板")):
            status, _, errors = self.invoke(["init", "--target", str(self.target)])
        self.assertEqual(status, 1)
        self.assertIn("缺少模板", errors)
        self.assertFalse(self.target.exists())

    def test_copy_permission_failure_returns_nonzero_status(self):
        with patch("deerflow.project_init._create_file", side_effect=PermissionError("没有写入权限")):
            status, _, errors = self.invoke(["init", "--target", str(self.target)])
        self.assertEqual(status, 1)
        self.assertIn("没有写入权限", errors)

    def test_help_does_not_create_target(self):
        with self.assertRaises(SystemExit) as raised:
            self.invoke(["init", "--target", str(self.target), "--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertFalse(self.target.exists())

    def test_invalid_argument_returns_usage_error(self):
        with self.assertRaises(SystemExit) as raised:
            self.invoke(["init", "--unknown"])
        self.assertEqual(raised.exception.code, 2)

    def test_existing_commands_are_forwarded_unchanged(self):
        terminal_main = Mock(return_value=7)
        with patch.dict(sys.modules, {"deerflow.tui.cli": SimpleNamespace(main=terminal_main)}):
            for arguments in ([], ["--help"], ["--print", "你好"], ["extensions", "--help"]):
                with self.subTest(arguments=arguments):
                    self.assertEqual(main(arguments), 7)
                    terminal_main.assert_called_with(arguments)

    def test_init_runs_without_site_packages_or_runtime_imports(self):
        code = """
import sys
from deerflow.cli import main
assert main(['init', '--target', sys.argv[1]]) == 0
assert not any(name in sys.modules for name in (
    'deerflow.client', 'deerflow.protocol', 'deerflow.tui.cli',
    'deerflow.config', 'langchain', 'textual',
))
"""
        result = subprocess.run(
            [sys.executable, "-S", "-c", code, str(self.target)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_template_contents()


if __name__ == "__main__":
    unittest.main()
