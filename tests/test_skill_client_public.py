import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier
from unittest.mock import Mock, patch

from deerflow.config.extensions_config import ExtensionsConfig, SkillStateConfig
from deerflow.config.paths import Paths
from deerflow.config.shared_reset_marker import SharedResetMarker
from deerflow.config.skills_config import SkillsConfig
from deerflow.skill_client import DeerFlowSkillClient
from deerflow.skills.projection import get_skill_projection_paths
from deerflow.skills.storage import LocalSkillStorage, UserScopedSkillStorage


class SkillClientPublicTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.paths = Paths(str(self.root / "runtime"))
        self.config = SimpleNamespace(skills=SkillsConfig(path=str(self.root / "configured-skills")))
        self.extensions = ExtensionsConfig()
        self.config_path = self.root / "extensions.json"
        self.config_path.write_text("{}")
        self.enterContext(patch("deerflow.skill_client.load_client_config", return_value=self.config))
        self.enterContext(patch("deerflow.skill_client.get_paths", return_value=self.paths))
        self.enterContext(patch("deerflow.config.paths.get_paths", return_value=self.paths))
        self.enterContext(patch.object(ExtensionsConfig, "from_file", return_value=self.extensions))
        self.enterContext(patch("deerflow.config.shared_reset_marker.resolve_shared_config_path", return_value=self.config_path))
        self.storage = LocalSkillStorage(app_config=self.config)
        self.users = {}
        self.public_factory = self.enterContext(patch("deerflow.skill_client.get_or_new_skill_storage", return_value=self.storage))
        self.user_factory = self.enterContext(patch("deerflow.skill_client.get_or_new_user_skill_storage", side_effect=self.user_storage))
        self.client = DeerFlowSkillClient()
        self.public = self.client.public
        self.public_root = self.root / "configured-skills/public"
        self.view = get_skill_projection_paths(self.storage).public

    def user_storage(self, user_id, **options):
        if user_id not in self.users:
            self.users[user_id] = UserScopedSkillStorage(user_id, app_config=self.config)
        return self.users[user_id]

    @staticmethod
    def markdown(description="更新描述", name="example"):
        return f"---\nname: {name}\ndescription: {description}\ntags: [one, two]\n---\n正文\n".encode()

    def test_public_creation_query_and_projection_without_user(self):
        self.assertEqual(self.public.list_skills(), [])
        path = self.public.create_skill("example", "描述", license="MIT")
        self.assertEqual(path, str(self.public_root / "example"))
        self.assertEqual(self.public.get_skill_info("example")["license"], "MIT")
        self.assertEqual(self.public.list_skills(), [{
            "name": "example", "description": "描述",
            "skill_path": "/mnt/skills/public/example", "host_path": path,
        }])
        self.assertTrue((self.view / "example/SKILL.md").is_file())
        self.user_factory.assert_not_called()

    def test_all_public_resource_operations(self):
        self.public.create_skill("example", "描述")
        self.public.create_folder("example", "scripts")
        self.public.create_file("example", "scripts/main.py")
        self.public.upload_file("example", "scripts/main.py", bytearray(b"print(1)\n"))
        self.assertEqual(self.public.download_file("example", "scripts/main.py"), b"print(1)\n")
        self.assertEqual((self.view / "example/scripts/main.py").read_bytes(), b"print(1)\n")
        self.public.move_file("example", "scripts/main.py", "scripts/run.py")
        self.public.move_folder("example", "scripts", "code")
        tree = self.public.get_resource_tree("example")
        node = next(node for node in tree if node["name"] == "code")
        self.assertEqual(node["children"][0]["path"], "code/run.py")
        self.public.delete_file("example", "code/run.py")
        self.public.delete_folder("example", "code")
        self.assertFalse((self.view / "example/code").exists())
        self.public.delete_skill("example")
        self.assertEqual(self.public.list_skills(), [])
        self.assertFalse((self.view / "example").exists())

    def test_public_metadata_cache_updates_and_returns_independent_values(self):
        self.public.create_skill("example", "描述")
        self.public.upload_file("example", "SKILL.md", self.markdown())
        info = self.public.get_skill_info("example")
        info["tags"].append("changed")
        self.assertEqual(self.public.get_skill_info("example")["tags"], ["one", "two"])
        self.public.upload_file("example", "SKILL.md", self.markdown("再次更新"))
        self.assertEqual(self.public.list_skills()[0]["description"], "再次更新")

    def test_same_name_public_and_users_remain_isolated(self):
        public_path = Path(self.public.create_skill("example", "公共描述"))
        first = Path(self.client.create_skill("alice", "example", "甲"))
        second = Path(self.client.create_skill("bob", "example", "乙"))
        self.public.upload_file("example", "SKILL.md", self.markdown())
        self.assertEqual(self.client.get_skill_info("alice", "example")["description"], "甲")
        self.public.delete_skill("example")
        self.assertFalse(public_path.exists())
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.client.list_skills("alice")[0]["skill_path"], "/mnt/skills/custom/example")

    def test_copy_public_to_custom_preserves_all_resources_and_is_independent(self):
        source = Path(self.public.create_skill("example", "公共描述"))
        (source / ".hidden").write_bytes(b"hidden")
        (source / "empty").mkdir()
        (source / "image.bin").write_bytes(b"\x00\xff")
        result = Path(self.public.copy_skill(skill_name="example", target_user_id="alice"))
        self.assertEqual(result, self.paths.user_custom_skills_dir("alice") / "example")
        self.assertTrue((result / "empty").is_dir())
        self.assertEqual((result / "image.bin").read_bytes(), b"\x00\xff")
        self.assertEqual((result / ".hidden").read_bytes(), b"hidden")
        self.client.upload_file("alice", "example", "SKILL.md", self.markdown())
        self.assertEqual(self.public.get_skill_info("example")["description"], "公共描述")
        self.public.delete_skill("example")
        self.assertEqual(self.client.get_skill_info("alice", "example")["description"], "更新描述")
        self.assertTrue((get_skill_projection_paths(self.user_storage("alice")).custom / "example/SKILL.md").exists())

    def test_copy_does_not_overwrite_existing_custom_or_copy_state(self):
        self.public.create_skill("example", "公共描述")
        self.public.copy_skill("example", "alice")
        self.assertFalse(self.paths.user_skills_dir("alice").joinpath("_skill_states.json").exists())
        with self.assertRaises(FileExistsError):
            self.public.copy_skill("example", "alice")
        self.assertEqual(self.client.get_skill_info("alice", "example")["description"], "公共描述")

    def test_copy_rejects_invalid_source_package_and_invalid_user(self):
        source = Path(self.public.create_skill("example", "公共描述"))
        for user_id in (None, "", " "):
            with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                self.public.copy_skill("example", user_id)
        (source / "link").symlink_to(source / "SKILL.md")
        with self.assertRaises(ValueError):
            self.public.copy_skill("example", "alice")
        self.assertFalse((self.paths.user_custom_skills_dir("alice") / "example").exists())
        (source / "link").unlink()
        (source / "SKILL.md").write_bytes(self.markdown(name="wrong"))
        with self.assertRaises(ValueError):
            self.public.copy_skill("example", "alice")

    def test_concurrent_public_copies_only_one_wins(self):
        self.public.create_skill("example", "公共描述")
        def copy():
            try:
                return self.public.copy_skill("example", "alice")
            except FileExistsError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: copy(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.client.get_skill_info("alice", "example")["description"], "公共描述")

    def test_existing_custom_copy_and_deletion_metadata(self):
        self.client.create_skill("alice", "example", "自定义")
        self.client.copy_skill("alice", "bob", "example")
        storage = self.user_storage("bob")
        storage.set_skill_enabled_state("example", False)
        storage.append_history("example", {"action": "test"})
        self.client.delete_skill("bob", "example")
        self.assertFalse(storage.get_skill_history_file("example").exists())
        self.assertNotIn("example", storage._read_skill_states())
        self.assertEqual(self.client.get_skill_info("alice", "example")["description"], "自定义")

    def test_root_markdown_protection_preserves_source_and_view(self):
        self.public.create_skill("example", "描述")
        for operation, arguments in (
            (self.public.create_file, ("SKILL.md",)),
            (self.public.delete_file, ("SKILL.md",)),
            (self.public.move_file, ("SKILL.md", "other.md")),
            (self.public.create_folder, ("skill.MD",)),
        ):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                operation("example", *arguments)
        with self.assertRaises(ValueError):
            self.public.upload_file("example", "SKILL.md", self.markdown(name="wrong"))
        self.assertEqual(self.public.get_skill_info("example")["description"], "描述")
        self.assertTrue((self.view / "example/SKILL.md").exists())

    def test_paths_links_and_upload_rules(self):
        source = Path(self.public.create_skill("example", "描述"))
        for path in ("../outside", "/tmp/outside", "a\\b", ".", "", "a\x00b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.public.create_file("example", path)
        (source / "link").symlink_to(self.config_path)
        with self.assertRaises(ValueError):
            self.public.download_file("example", "link")
        self.assertEqual(next(node for node in self.public.get_resource_tree("example") if node["name"] == "link")["type"], "file")
        with self.assertRaises(FileNotFoundError):
            self.public.upload_file("example", "missing", b"text")
        self.public.create_file("example", "test.txt")
        for content, error in ((b"\xff", UnicodeDecodeError), (b"\x00", ValueError), ("text", TypeError)):
            with self.subTest(content=content), self.assertRaises(error):
                self.public.upload_file("example", "test.txt", content)

    def test_public_root_symlink_is_rejected(self):
        self.public_root.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        self.public_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.public.create_skill("example", "描述")
        self.assertEqual(list(outside.iterdir()), [])

    def test_public_list_includes_disabled_and_skips_invalid_and_nested_packages(self):
        source = Path(self.public.create_skill("example", "描述"))
        self.extensions.skills["example"] = SkillStateConfig(enabled=False)
        self.public.create_file("example", "refresh.txt")
        self.assertEqual(len(self.public.list_skills()), 1)
        self.assertFalse((self.view / "example").exists())
        nested = source / "nested"
        nested.mkdir()
        (nested / "SKILL.md").write_bytes(self.markdown(name="nested"))
        bad = self.public_root / "bad"
        bad.mkdir()
        (bad / "SKILL.md").write_text("invalid")
        self.assertEqual([item["name"] for item in self.public.list_skills()], ["example"])

    def test_deleting_public_preserves_shared_and_user_state(self):
        self.public.create_skill("example", "描述")
        self.client.create_skill("alice", "example", "自定义")
        storage = self.user_storage("alice")
        storage.set_skill_enabled_state("example", False)
        self.extensions.skills["example"] = SkillStateConfig(enabled=False)
        self.public.delete_skill("example")
        self.assertFalse(self.extensions.skills["example"].enabled)
        self.assertFalse(storage.get_skill_enabled_state("example"))

    def test_cache_notifications_are_global_for_public_and_user_scoped_for_copy(self):
        modules = {name: Mock() for name in ("deerflow.agents.lead_agent.prompt", "deerflow.agents.lead_agent.prompt_zh")}
        marker = SharedResetMarker("skills-cache-reset")
        with patch.dict("sys.modules", modules):
            self.public.create_skill("example", "描述")
            for module in modules.values():
                module.clear_skills_system_prompt_cache.assert_called_once_with()
            self.assertIsNone(json.loads(marker.path_for(self.config_path).read_text()).get("user_id"))
            self.public.copy_skill("example", "alice")
            for module in modules.values():
                module.invalidate_user_skill_cache.assert_called_once_with("alice")
            self.assertEqual(json.loads(marker.path_for(self.config_path).read_text())["user_id"], "alice")

    def test_public_copy_failure_does_not_publish_partial_target(self):
        self.public.create_skill("example", "描述")
        with patch("deerflow.skill_client.shutil.copytree", side_effect=OSError("复制失败")):
            with self.assertRaises(OSError):
                self.public.copy_skill("example", "alice")
        target_root = self.paths.user_custom_skills_dir("alice")
        self.assertEqual(list(target_root.iterdir()), [])
        self.assertTrue((self.public_root / "example/SKILL.md").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "当前平台不支持 FIFO")
    def test_public_copy_rejects_special_files(self):
        source = Path(self.public.create_skill("example", "描述"))
        os.mkfifo(source / "pipe")
        with self.assertRaises(ValueError):
            self.public.copy_skill("example", "alice")
        self.assertFalse((self.paths.user_custom_skills_dir("alice") / "example").exists())

    def test_missing_skill_is_rejected_by_every_public_operation(self):
        operations = (
            (self.public.get_skill_info, ()),
            (self.public.get_resource_tree, ()),
            (self.public.delete_skill, ()),
            (self.public.copy_skill, ("alice",)),
            (self.public.create_file, ("file.txt",)),
            (self.public.create_folder, ("folder",)),
            (self.public.delete_file, ("file.txt",)),
            (self.public.delete_folder, ("folder",)),
            (self.public.move_file, ("file.txt", "other.txt")),
            (self.public.move_folder, ("folder", "other")),
            (self.public.upload_file, ("file.txt", b"text")),
            (self.public.download_file, ("file.txt",)),
        )
        for operation, arguments in operations:
            with self.subTest(operation=operation.__name__), self.assertRaises(FileNotFoundError):
                operation("missing", *arguments)
        self.assertFalse(self.paths.user_custom_skills_dir("alice").exists())
        self.assertFalse(self.public_root.exists())

    def test_copy_to_custom_with_other_skills_preserves_existing_content_and_state(self):
        self.public.create_skill("example", "公共描述")
        existing = Path(self.client.create_skill("alice", "personal", "原有技能"))
        self.client.create_file("alice", "personal", "notes.txt")
        self.client.upload_file("alice", "personal", "notes.txt", b"private notes")
        storage = self.user_storage("alice")
        storage.set_skill_enabled_state("personal", False)
        storage.append_history("personal", {"action": "existing"})
        states = storage._skill_states_file.read_bytes()
        history = storage.get_skill_history_file("personal").read_bytes()
        self.public.copy_skill("example", "alice")
        self.assertEqual((existing / "notes.txt").read_bytes(), b"private notes")
        self.assertEqual(storage._skill_states_file.read_bytes(), states)
        self.assertEqual(storage.get_skill_history_file("personal").read_bytes(), history)
        self.assertEqual([item["name"] for item in self.client.list_skills("alice")], ["example", "personal"])

    def test_copy_conflict_preserves_distinct_existing_custom_contents_and_view(self):
        self.public.create_skill("example", "公共描述")
        existing = Path(self.client.create_skill("alice", "example", "用户修改版"))
        self.client.create_file("alice", "example", "private.txt")
        self.client.upload_file("alice", "example", "private.txt", b"keep me")
        before = (existing / "SKILL.md").read_bytes()
        with self.assertRaises(FileExistsError):
            self.public.copy_skill("example", "alice")
        self.assertEqual((existing / "SKILL.md").read_bytes(), before)
        self.assertEqual((existing / "private.txt").read_bytes(), b"keep me")
        view = get_skill_projection_paths(self.user_storage("alice")).custom
        self.assertEqual((view / "example/private.txt").read_bytes(), b"keep me")

    def test_copy_rejects_existing_file_empty_directory_and_symlink_targets(self):
        self.public.create_skill("example", "公共描述")
        for user_id, kind in (("file_user", "file"), ("directory_user", "directory"), ("link_user", "link")):
            target = self.paths.user_custom_skills_dir(user_id) / "example"
            target.parent.mkdir(parents=True)
            if kind == "file":
                target.write_bytes(b"existing")
            elif kind == "directory":
                target.mkdir()
            else:
                target.symlink_to(self.root / "nonexistent")
            with self.subTest(kind=kind), self.assertRaises((FileExistsError, ValueError)):
                self.public.copy_skill("example", user_id)
            if kind == "file":
                self.assertEqual(target.read_bytes(), b"existing")
            elif kind == "directory":
                self.assertEqual(list(target.iterdir()), [])
            else:
                self.assertTrue(target.is_symlink())

    def test_copy_rejects_missing_skill_markdown(self):
        source = Path(self.public.create_skill("example", "公共描述"))
        (source / "SKILL.md").unlink()
        with self.assertRaises(FileNotFoundError):
            self.public.copy_skill("example", "alice")
        self.assertFalse(self.paths.user_custom_skills_dir("alice").exists())

    def test_copy_rejects_corrupt_source_markdown_without_creating_target(self):
        source = Path(self.public.create_skill("example", "公共描述"))
        for content in (b"plain text", b"---\nname: [\n---\n", b"\xff", self.markdown(name="wrong")):
            (source / "SKILL.md").write_bytes(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.public.copy_skill("example", "alice")
            self.assertFalse(self.paths.user_custom_skills_dir("alice").exists())

    def test_invalid_names_are_rejected_without_creating_directories(self):
        for name in ("", " ", "../escape", "/absolute", "Upper", "bad_name", "-bad", "bad-", "bad--name", "a" * 65, None, 123):
            expected = TypeError if not isinstance(name, str) else ValueError
            for operation, arguments in ((self.public.create_skill, ("描述",)), (self.public.copy_skill, ("alice",)), (self.public.get_skill_info, ())):
                with self.subTest(name=name, operation=operation.__name__), self.assertRaises(expected):
                    operation(name, *arguments)
        self.assertFalse(self.public_root.exists())
        self.user_factory.assert_not_called()

    def test_duplicate_public_creation_preserves_existing_contents_and_view(self):
        self.public.create_skill("example", "原有描述")
        before = (self.view / "example/SKILL.md").read_bytes()
        with self.assertRaises(FileExistsError):
            self.public.create_skill("example", "不能覆盖")
        self.assertEqual(self.public.get_skill_info("example")["description"], "原有描述")
        self.assertEqual((self.view / "example/SKILL.md").read_bytes(), before)

    def test_concurrent_public_creation_preserves_winner_and_other_views(self):
        self.public.create_skill("other", "其他技能")
        operations = self.public._operations
        barrier = Barrier(2)
        check_absent = operations._require_absent

        def check_before_race(path):
            check_absent(path)
            barrier.wait(timeout=5)

        def create(description):
            try:
                return self.public.create_skill("example", description)
            except FileExistsError:
                return None

        with patch.object(type(operations), "_require_absent", side_effect=check_before_race):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(create, ("甲", "乙")))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual((self.view / "example/SKILL.md").read_bytes(), (self.public_root / "example/SKILL.md").read_bytes())
        self.assertTrue((self.view / "other/SKILL.md").exists())

    def test_invalid_create_metadata_leaves_no_package(self):
        for description, fields, expected in (("", {}, ValueError), (None, {}, ValueError), ("描述", {"tags": ["tag"]}, TypeError), ("描述", {"allowed-tools": "("}, ValueError)):
            with self.subTest(description=description, fields=fields), self.assertRaises(expected):
                self.public.create_skill("example", description, **fields)
            self.assertFalse((self.public_root / "example").exists())

    def test_missing_resources_parents_and_wrong_resource_types(self):
        self.public.create_skill("example", "描述")
        self.public.create_folder("example", "folder")
        self.public.create_file("example", "file.txt")
        cases = (
            (self.public.create_file, ("missing/file.txt",), FileNotFoundError),
            (self.public.create_folder, ("missing/sub",), FileNotFoundError),
            (self.public.download_file, ("missing.txt",), FileNotFoundError),
            (self.public.delete_file, ("missing.txt",), FileNotFoundError),
            (self.public.delete_folder, ("missing",), FileNotFoundError),
            (self.public.download_file, ("folder",), IsADirectoryError),
            (self.public.upload_file, ("folder", b"text"), IsADirectoryError),
            (self.public.delete_file, ("folder",), IsADirectoryError),
            (self.public.delete_folder, ("file.txt",), NotADirectoryError),
        )
        for operation, arguments, expected in cases:
            with self.subTest(operation=operation.__name__, arguments=arguments), self.assertRaises(expected):
                operation("example", *arguments)
        self.assertTrue((self.public_root / "example/folder").is_dir())
        self.assertTrue((self.public_root / "example/file.txt").is_file())

    def test_moves_reject_overwrite_missing_parent_and_self_descendant(self):
        source = Path(self.public.create_skill("example", "描述"))
        self.public.create_file("example", "first.txt")
        self.public.upload_file("example", "first.txt", b"first")
        self.public.create_file("example", "second.txt")
        self.public.upload_file("example", "second.txt", b"second")
        self.public.create_folder("example", "folder")
        cases = (
            (self.public.move_file, ("first.txt", "second.txt"), FileExistsError),
            (self.public.move_file, ("first.txt", "missing/new.txt"), FileNotFoundError),
            (self.public.move_file, ("missing.txt", "new.txt"), FileNotFoundError),
            (self.public.move_folder, ("folder", "folder/child"), ValueError),
            (self.public.move_file, ("first.txt", "SKILL.md"), ValueError),
        )
        for operation, arguments, expected in cases:
            with self.subTest(arguments=arguments), self.assertRaises(expected):
                operation("example", *arguments)
        self.assertEqual((source / "first.txt").read_bytes(), b"first")
        self.assertEqual((source / "second.txt").read_bytes(), b"second")
        self.assertTrue((source / "folder").is_dir())

    def test_marker_failure_reports_applied_change_as_runtime_error(self):
        with patch.object(SharedResetMarker, "publish", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(RuntimeError, "技能已修改"):
                self.public.create_skill("example", "描述")
        self.assertTrue((self.public_root / "example/SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
