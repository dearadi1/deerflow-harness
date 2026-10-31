import ast
import json
import os
import sys
import tempfile
import unittest
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, Mock, patch

from deerflow.config.extensions_config import ExtensionsConfig, SkillStateConfig
from deerflow.config.paths import Paths
from deerflow.config.shared_reset_marker import (
    SharedResetChange,
    SharedResetMarker,
    SharedResetMarkerTracker,
)
from deerflow.config.skills_config import SkillsConfig
from deerflow.skill_client import DeerFlowSkillClient
from deerflow.skills.projection import get_skill_projection_paths
from deerflow.skills.storage import LocalSkillStorage, UserScopedSkillStorage


def load_prompt_cache(module_name, user_factory):
    """直接执行缓存函数，避免离线管理测试加载 Agent、模型及工具依赖。"""
    source = Path(__file__).resolve().parents[1] / (module_name.replace(".", "/") + ".py")
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names = {
        "get_enabled_skills_for_config", "invalidate_user_skill_cache",
        "_check_shared_skills_cache_reset", "_apply_shared_skills_cache_reset",
    }
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ModuleType(module_name)
    module.__dict__.update(
        AppConfig=object,
        Skill=object,
        SharedResetChange=SharedResetChange,
        logger=Mock(),
        _skills_cache_reset_tracker=Mock(poll=Mock(return_value=None)),
        _invalidate_enabled_skills_cache=Mock(),
        _get_enabled_skills=Mock(return_value=[]),
        get_or_new_skill_storage=Mock(),
        _enabled_skills_lock=Lock(),
        _enabled_skills_by_config_cache=OrderedDict(),
        _enabled_skills_refresh_version=0,
        _ENABLED_SKILLS_BY_CONFIG_CACHE_MAXSIZE=256,
        _get_cached_skills_prompt_section=Mock(),
        get_or_new_user_skill_storage=user_factory,
    )
    # 只执行仓库内固定名称的函数，既不接受外部源码，也不复制被测实现。
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), module.__dict__)  # noqa: S102
    return module


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
            "skill_path": "/mnt/skills/public/example", "host_path": path, "enabled": True,
        }])
        self.assertTrue((self.view / "example/SKILL.md").is_file())
        self.user_factory.assert_not_called()

    def use_persisted_skill_states(self):
        """状态测试使用真实文件，覆盖原始配置保留与缓存重载。"""
        self.enterContext(patch.dict(os.environ, {"DEER_FLOW_EXTENSIONS_CONFIG_PATH": str(self.config_path)}))
        self.enterContext(patch.object(
            ExtensionsConfig, "from_file",
            side_effect=lambda *args, **kwargs: ExtensionsConfig._from_path_with_signature(self.config_path)[0],
        ))
        from deerflow.config.extensions_config import reset_extensions_config

        self.addCleanup(reset_extensions_config)

    def test_skill_states_are_separate_and_default_enabled(self):
        self.use_persisted_skill_states()
        self.public.create_skill("example", "公共描述")
        self.client.create_skill("alice", "example", "用户描述")
        public_states = [{"name": "example", "global_enabled": True, "enabled": True}]
        self.assertEqual(self.public.list_skill_states(), public_states)
        self.assertEqual(self.client.list_skill_states("alice"), [dict(public_states[0], user_enabled=True)])
        self.assertEqual(self.client.list_skill_states("bob"), [])
        self.assertIs(self.public.list_skills()[0]["enabled"], True)
        self.assertIs(self.client.list_skills("alice")[0]["enabled"], True)

    def test_custom_toggle_is_user_scoped_and_invalidates_shared_caches(self):
        self.use_persisted_skill_states()
        self.client.create_skill("alice", "example", "用户描述")
        self.client.create_skill("bob", "example", "其他用户")
        modules = self.prompt_cache_modules()
        for module in modules:
            self.assertEqual(len(module.get_enabled_skills_for_config(self.config, "alice")), 1)
            self.assertEqual(len(module.get_enabled_skills_for_config(self.config, "bob")), 1)
        peer = load_prompt_cache("deerflow.agents.lead_agent.prompt_zh", self.user_storage)
        peer._skills_cache_reset_tracker = SharedResetMarkerTracker(
            SharedResetMarker("skills-cache-reset"),
            resolve_config_path=lambda: self.config_path, poll_interval_seconds=0,
        )
        self.assertEqual(len(peer.get_enabled_skills_for_config(self.config, "alice")), 1)
        state = self.client.set_skill_enabled("alice", "example", False)
        self.assertEqual(state, {"name": "example", "global_enabled": True, "user_enabled": False, "enabled": False})
        self.assertTrue(self.client.list_skill_states("bob")[0]["enabled"])
        self.assertIs(self.client.list_skills("alice")[0]["enabled"], False)
        self.assertIs(self.client.list_skills("bob")[0]["enabled"], True)
        self.assertEqual(json.loads(self.config_path.read_text()), {})
        self.assertFalse((get_skill_projection_paths(self.user_storage("alice")).custom / "example").exists())
        for module in modules:
            self.assertEqual(module.get_enabled_skills_for_config(self.config, "alice"), [])
            self.assertEqual(len(module.get_enabled_skills_for_config(self.config, "bob")), 1)
        self.assertEqual(peer.get_enabled_skills_for_config(self.config, "alice"), [])
        self.assertTrue(self.client.set_skill_enabled("alice", "example", True)["enabled"])

    def test_public_toggle_preserves_raw_config_and_global_disable_wins(self):
        from deerflow.config.extensions_config import get_extensions_config

        self.use_persisted_skill_states()
        self.public.create_skill("example", "公共描述")
        self.client.create_skill("alice", "example", "用户描述")
        self.config_path.write_text(json.dumps({"mcp_servers": {}, "skills": {"other": {"enabled": False}}, "placeholder": "$SECRET"}))
        modules = self.prompt_cache_modules()
        for module in modules:
            module.clear_skills_system_prompt_cache = Mock()
            module.get_enabled_skills_for_config(self.config, "alice")
        self.assertTrue(get_extensions_config().is_skill_enabled("example", "public"))
        self.assertFalse(self.public.set_skill_enabled("example", False)["enabled"])
        self.assertFalse(get_extensions_config().is_skill_enabled("example", "public"))
        raw = json.loads(self.config_path.read_text())
        self.assertEqual(raw["placeholder"], "$SECRET")
        self.assertEqual(raw["skills"]["other"], {"enabled": False})
        self.assertFalse((self.view / "example").exists())
        for module in modules:
            module.clear_skills_system_prompt_cache.assert_called_once_with()
        state = self.client.set_skill_enabled("alice", "example", True)
        self.assertEqual(state, {"name": "example", "global_enabled": False, "user_enabled": True, "enabled": False})
        self.assertIs(self.public.list_skills()[0]["enabled"], False)
        self.assertIs(self.client.list_skills("alice")[0]["enabled"], False)
        self.assertTrue(self.public.set_skill_enabled("example", True)["enabled"])
        self.assertIs(self.public.list_skills()[0]["enabled"], True)
        self.assertIs(self.client.list_skills("alice")[0]["enabled"], True)
        self.assertTrue((self.view / "example/SKILL.md").is_file())

    def test_skill_state_validation_and_external_changes(self):
        self.use_persisted_skill_states()
        self.public.create_skill("example", "公共描述")
        with self.assertRaises(ValueError):
            self.client.list_skill_states("")
        with self.assertRaises(ValueError):
            self.client.set_skill_enabled("", "example", False)
        with self.assertRaises(FileNotFoundError):
            self.client.set_skill_enabled("alice", "example", False)
        with self.assertRaises(FileNotFoundError):
            self.public.set_skill_enabled("missing", False)
        with self.assertRaises(TypeError):
            self.public.set_skill_enabled("example", "false")
        self.assertTrue(self.public.list_skill_states()[0]["enabled"])
        self.config_path.write_text('{"skills": {"example": {"enabled": false}}}')
        self.assertFalse(self.public.list_skill_states()[0]["enabled"])
        self.assertIs(self.public.list_skills()[0]["enabled"], False)

    def test_custom_toggle_notification_failure_reports_committed_state(self):
        self.use_persisted_skill_states()
        self.client.create_skill("alice", "example", "用户描述")
        with patch.object(SharedResetMarker, "publish", side_effect=OSError("无法写入")):
            with self.assertRaisesRegex(RuntimeError, "技能已修改"):
                self.client.set_skill_enabled("alice", "example", False)
        self.assertFalse(self.client.list_skill_states("alice")[0]["enabled"])

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

    def test_resource_tree_sorts_folders_first_and_names_by_pinyin(self):
        self.public.create_skill("example", "描述")
        for folder in ("zeta", "中文", "apple"):
            self.public.create_folder("example", folder)
        for file_name in ("中文.txt", "zeta.txt", "apple.txt"):
            self.public.create_file("example", file_name)

        tree = self.public.get_resource_tree("example")

        self.assertEqual(
            [(node["name"], node["type"]) for node in tree],
            [
                ("apple", "folder"),
                ("zeta", "folder"),
                ("中文", "folder"),
                ("apple.txt", "file"),
                ("zeta.txt", "file"),
                ("中文.txt", "file"),
            ],
        )

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

    def prompt_cache_modules(self):
        names = ("deerflow.agents.lead_agent.prompt", "deerflow.agents.lead_agent.prompt_zh")
        modules = {name: load_prompt_cache(name, self.user_storage) for name in names}
        self.enterContext(patch.dict("sys.modules", modules))
        return list(modules.values())

    def test_custom_mutations_refresh_warmed_caches_and_preserve_other_users(self):
        modules = self.prompt_cache_modules()
        for module in modules:
            self.assertEqual(module.get_enabled_skills_for_config(self.config, "alice"), [])
            module.get_enabled_skills_for_config(self.config, "bob")
        other_entries = [module._enabled_skills_by_config_cache[(id(self.config), "bob")] for module in modules]
        self.client.create_skill("alice", "example", "初始描述")
        self.assert_cached_skill_descriptions(modules, ["初始描述"])
        self.client.upload_file("alice", "example", "SKILL.md", self.markdown("新描述"))
        self.assert_cached_skill_descriptions(modules, ["新描述"])
        self.client.delete_skill("alice", "example")
        self.assert_cached_skill_descriptions(modules, [])
        for module, entry in zip(modules, other_entries):
            self.assertIs(module._enabled_skills_by_config_cache[(id(self.config), "bob")], entry)
        marker = SharedResetMarker("skills-cache-reset")
        self.assertEqual(json.loads(marker.path_for(self.config_path).read_text())["user_id"], "alice")

    def assert_cached_skill_descriptions(self, modules, descriptions):
        for module in modules:
            skills = module.get_enabled_skills_for_config(self.config, "alice")
            self.assertEqual([skill.description for skill in skills], descriptions)

    def test_custom_copy_refreshes_only_target_and_publishes_once(self):
        self.client.create_skill("bob", "example", "来源描述")
        modules = self.prompt_cache_modules()
        for module in modules:
            module.get_enabled_skills_for_config(self.config, "alice")
            module.get_enabled_skills_for_config(self.config, "bob")
        source_entries = [module._enabled_skills_by_config_cache[(id(self.config), "bob")] for module in modules]
        with patch.object(SharedResetMarker, "publish", autospec=True, side_effect=SharedResetMarker.publish) as publish:
            self.client.copy_skill("bob", "alice", "example")
            publish.assert_called_once_with(ANY, self.config_path, user_id="alice")
        self.assert_cached_skill_descriptions(modules, ["来源描述"])
        for module, entry in zip(modules, source_entries):
            self.assertIs(module._enabled_skills_by_config_cache[(id(self.config), "bob")], entry)

    def test_public_copy_publishes_target_reset_once(self):
        self.public.create_skill("example", "公共描述")
        with patch.object(SharedResetMarker, "publish", autospec=True, side_effect=SharedResetMarker.publish) as publish:
            self.public.copy_skill("example", "alice")
            publish.assert_called_once_with(ANY, self.config_path, user_id="alice")

    def test_custom_validation_failure_does_not_publish_reset(self):
        self.client.create_skill("alice", "example", "描述")
        with patch.object(SharedResetMarker, "publish") as publish:
            with self.assertRaises(ValueError):
                self.client.upload_file("alice", "example", "SKILL.md", b"invalid")
            publish.assert_not_called()

    def test_user_invalidation_prevents_inflight_stale_cache_writeback(self):
        for module in self.prompt_cache_modules():
            with self.subTest(module=module.__name__):
                started, release = Event(), Event()
                storage = Mock()
                storage.load_skills.side_effect = lambda started=started, release=release, **options: self.block_skill_load(started, release)
                module.get_or_new_user_skill_storage = Mock(return_value=storage)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(module.get_enabled_skills_for_config, self.config, "alice")
                    try:
                        self.assertTrue(started.wait(timeout=5))
                        module.invalidate_user_skill_cache("alice")
                    finally:
                        release.set()
                    self.assertEqual(future.result(timeout=5), ["旧快照"])
                storage.load_skills.return_value = ["新快照"]
                storage.load_skills.side_effect = None
                self.assertEqual(module.get_enabled_skills_for_config(self.config, "alice"), ["新快照"])

    @staticmethod
    def block_skill_load(started, release):
        started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("等待测试释放技能读取超时")
        return ["旧快照"]

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

    def test_custom_marker_failure_reports_applied_change_as_runtime_error(self):
        with (
            patch.object(SharedResetMarker, "publish", side_effect=PermissionError("denied")),
            self.assertRaisesRegex(RuntimeError, "技能已修改"),
        ):
            self.client.create_skill("alice", "example", "描述")
        self.assertTrue((self.user_storage("alice").get_user_custom_root() / "example/SKILL.md").exists())

    def test_custom_parameter_validation_has_no_notification_or_filesystem_effect(self):
        cases = (
            (None, "example", "描述", ValueError),
            (123, "example", "描述", ValueError),
            ([], "example", "描述", ValueError),
            ("", "example", "描述", ValueError),
            (" ", "example", "描述", ValueError),
            ("../bob", "example", "描述", ValueError),
            ("alice", None, "描述", TypeError),
            ("alice", 123, "描述", TypeError),
            ("alice", "", "描述", ValueError),
            ("alice", "../escape", "描述", ValueError),
            ("alice", "a" * 65, "描述", ValueError),
            ("alice", "example", None, ValueError),
            ("alice", "example", 123, ValueError),
            ("alice", "example", " \n", ValueError),
        )
        with patch.object(SharedResetMarker, "publish") as publish:
            for user_id, name, description, error in cases:
                with self.subTest(user_id=user_id, name=name, description=description), self.assertRaises(error):
                    self.client.create_skill(user_id, name, description)
            publish.assert_not_called()
        self.assertFalse(self.paths.base_dir.exists())

    def test_required_and_extra_arguments_follow_public_signatures(self):
        cases = (
            (self.client.create_skill, (), {}),
            (self.client.create_skill, ("alice",), {}),
            (self.client.create_skill, ("alice", "example"), {}),
            (self.client.copy_skill, ("alice", "bob"), {}),
            (self.client.upload_file, ("alice", "example", "SKILL.md"), {}),
            (self.client.delete_skill, ("alice", "example", "extra"), {}),
            (self.client.delete_skill, ("alice", "example"), {"unexpected": True}),
            (self.client.create_skill, ("alice", "example", "描述"), {"custom_field": 123}),
        )
        with patch.object(SharedResetMarker, "publish") as publish:
            for operation, arguments, options in cases:
                with self.subTest(operation=operation.__name__, arguments=arguments), self.assertRaises(TypeError):
                    operation(*arguments, **options)
            publish.assert_not_called()

    def test_name_boundaries_default_enabled_and_extensible_metadata(self):
        for name in ("a", "a" * 64):
            with self.subTest(name=name):
                path = self.client.create_skill("alice", name, "中文描述", custom_field="兼容扩展")
                self.assertIsInstance(path, str)
                self.assertTrue(Path(path).is_absolute())
                self.assertEqual(self.client.get_skill_info("alice", name)["custom_field"], "兼容扩展")
        skills = self.user_storage("alice").load_skills(enabled_only=True)
        self.assertEqual({skill.name for skill in skills}, {"a", "a" * 64})
        items = self.client.list_skills("alice")
        self.assertEqual([item["name"] for item in items], ["a", "a" * 64])
        self.assertEqual(set(items[0]), {"name", "description", "skill_path", "host_path", "enabled"})
        self.assertTrue(all(isinstance(value, str) for item in items for key, value in item.items() if key != "enabled"))
        self.assertTrue(all(item["enabled"] is True for item in items))

    def test_invalid_uploads_preserve_content_cache_and_marker(self):
        path = Path(self.client.create_skill("alice", "example", "保留描述"))
        original = (path / "SKILL.md").read_bytes()
        modules = self.prompt_cache_modules()
        self.assert_cached_skill_descriptions(modules, ["保留描述"])
        cases = (
            ("SKILL.md", None, TypeError),
            ("SKILL.md", "text", TypeError),
            ("SKILL.md", b"\xff", UnicodeDecodeError),
            ("SKILL.md", b"text\x00", ValueError),
            ("SKILL.md", self.markdown(name="other"), ValueError),
            ("SKILL.md", b"---\nname: [\n---\n", ValueError),
            (None, b"text", ValueError),
            ("", b"text", ValueError),
            ("../bob/SKILL.md", b"text", ValueError),
            (str(self.root / "outside"), b"text", ValueError),
        )
        with patch.object(SharedResetMarker, "publish") as publish:
            for relative_path, content, error in cases:
                with self.subTest(path=relative_path, content=content), self.assertRaises(error):
                    self.client.upload_file("alice", "example", relative_path, content)
            publish.assert_not_called()
        self.assertEqual((path / "SKILL.md").read_bytes(), original)
        self.assert_cached_skill_descriptions(modules, ["保留描述"])

    def test_repeated_writes_converge_and_create_copy_delete_reject_replay(self):
        self.client.create_skill("alice", "example", "描述")
        content = bytearray(self.markdown("重复更新"))
        for _ in range(2):
            self.assertIsNone(self.client.upload_file("alice", "example", "SKILL.md", content))
        self.assertEqual(self.client.download_file("alice", "example", "SKILL.md"), bytes(content))
        self.client.copy_skill("alice", "bob", "example")
        with patch.object(SharedResetMarker, "publish") as publish:
            with self.assertRaises(FileExistsError):
                self.client.create_skill("alice", "example", "不覆盖")
            with self.assertRaises(FileExistsError):
                self.client.copy_skill("alice", "bob", "example")
            publish.assert_not_called()
        self.assertIsNone(self.client.delete_skill("alice", "example"))
        with patch.object(SharedResetMarker, "publish") as publish:
            with self.assertRaises(FileNotFoundError):
                self.client.delete_skill("alice", "example")
            publish.assert_not_called()
        self.assertEqual(self.client.get_skill_info("bob", "example")["description"], "重复更新")

    def test_write_failure_rolls_back_creation_without_notification(self):
        with (
            patch.object(type(self.client), "_write_text", side_effect=PermissionError("denied")),
            patch.object(SharedResetMarker, "publish") as publish,
            self.assertRaises(PermissionError),
        ):
            self.client.create_skill("alice", "example", "描述")
        publish.assert_not_called()
        self.assertFalse((self.paths.user_custom_skills_dir("alice") / "example").exists())

    def test_marker_failures_preserve_local_visibility_and_exception_cause(self):
        for error in (FileNotFoundError("gone"), PermissionError("denied"), ValueError("invalid config")):
            with self.subTest(error=type(error).__name__):
                modules = self.prompt_cache_modules()
                name = f"example-{type(error).__name__.lower()}"
                for module in modules:
                    module.get_enabled_skills_for_config(self.config, "alice")
                with (
                    patch.object(SharedResetMarker, "publish", side_effect=error),
                    self.assertRaisesRegex(RuntimeError, "技能已修改") as raised,
                ):
                    self.client.create_skill("alice", name, "描述")
                self.assertIs(raised.exception.__cause__, error)
                for module in modules:
                    self.assertIn(name, [skill.name for skill in module.get_enabled_skills_for_config(self.config, "alice")])

    def test_missing_shared_config_keeps_process_local_invalidation(self):
        modules = self.prompt_cache_modules()
        self.assert_cached_skill_descriptions(modules, [])
        with (
            patch("deerflow.config.shared_reset_marker.resolve_shared_config_path", return_value=None),
            patch.object(SharedResetMarker, "publish") as publish,
        ):
            self.client.create_skill("alice", "example", "描述")
            publish.assert_not_called()
        self.assert_cached_skill_descriptions(modules, ["描述"])

    def test_mutation_does_not_import_unloaded_prompt_modules(self):
        names = ("deerflow.agents.lead_agent.prompt", "deerflow.agents.lead_agent.prompt_zh")
        with patch.dict(sys.modules):
            for name in names:
                sys.modules.pop(name, None)
            self.client.create_skill("alice", "example", "描述")
            for name in names:
                self.assertNotIn(name, sys.modules)

    def test_legacy_and_invalid_shared_markers_retire_all_peer_caches(self):
        marker = SharedResetMarker("skills-cache-reset")
        for content in ('{"generation": "legacy", "extra": true}', '{"generation": 123}', '[]', 'invalid'):
            with self.subTest(content=content):
                marker.publish(self.config_path)
                module = load_prompt_cache("deerflow.agents.lead_agent.prompt", self.user_storage)
                module._skills_cache_reset_tracker = SharedResetMarkerTracker(
                    marker, resolve_config_path=lambda: self.config_path, poll_interval_seconds=0,
                )
                module._check_shared_skills_cache_reset()
                marker.path_for(self.config_path).write_text(content)
                module._check_shared_skills_cache_reset()
                module._invalidate_enabled_skills_cache.assert_called_once_with()

    def test_peer_process_cache_observes_marker_after_poll_interval(self):
        module = load_prompt_cache("deerflow.agents.lead_agent.prompt", self.user_storage)
        now = [0.0]
        module._skills_cache_reset_tracker = SharedResetMarkerTracker(
            SharedResetMarker("skills-cache-reset"), resolve_config_path=lambda: self.config_path,
            clock=lambda: now[0],
        )
        self.assertEqual(module.get_enabled_skills_for_config(self.config, "alice"), [])
        module.get_enabled_skills_for_config(self.config, "bob")
        other = module._enabled_skills_by_config_cache[(id(self.config), "bob")]
        self.client.create_skill("alice", "example", "对端更新")
        self.assertEqual(module.get_enabled_skills_for_config(self.config, "alice"), [])
        now[0] = 1.0
        self.assertEqual([skill.description for skill in module.get_enabled_skills_for_config(self.config, "alice")], ["对端更新"])
        self.assertIs(module._enabled_skills_by_config_cache[(id(self.config), "bob")], other)
        module._invalidate_enabled_skills_cache.assert_not_called()

    def test_multiple_unobserved_publications_use_full_reset(self):
        module = load_prompt_cache("deerflow.agents.lead_agent.prompt", self.user_storage)
        module._skills_cache_reset_tracker = SharedResetMarkerTracker(
            SharedResetMarker("skills-cache-reset"), resolve_config_path=lambda: self.config_path,
            poll_interval_seconds=0,
        )
        module._check_shared_skills_cache_reset()
        self.client.create_skill("alice", "example", "甲")
        self.client.create_skill("bob", "example", "乙")
        module._check_shared_skills_cache_reset()
        module._invalidate_enabled_skills_cache.assert_called_once_with()

    def test_poll_failure_retains_cache_and_retries(self):
        for module in self.prompt_cache_modules():
            module.get_enabled_skills_for_config(self.config, "alice")
            module._skills_cache_reset_tracker.poll.side_effect = [OSError("unavailable"), None]
            self.assertEqual(module.get_enabled_skills_for_config(self.config, "alice"), [])
            self.assertEqual(module.get_enabled_skills_for_config(self.config, "alice"), [])
            self.assertEqual(module._skills_cache_reset_tracker.poll.call_count, 3)

    def test_cache_eviction_and_repeated_user_invalidation(self):
        for module in self.prompt_cache_modules():
            with self.subTest(module=module.__name__):
                storage = Mock(load_skills=Mock(return_value=[]))
                module.get_or_new_user_skill_storage = Mock(return_value=storage)
                for index in range(257):
                    module.get_enabled_skills_for_config(self.config, f"user-{index}")
                self.assertEqual(len(module._enabled_skills_by_config_cache), 256)
                self.assertNotIn((id(self.config), "user-0"), module._enabled_skills_by_config_cache)
                for _ in range(2):
                    module.invalidate_user_skill_cache("user-256")
                self.assertEqual(len(module._enabled_skills_by_config_cache), 255)
                self.assertEqual(module.get_enabled_skills_for_config(None, "user-256"), [])
                module._get_enabled_skills.assert_called_once_with()

    def test_user_invalidation_covers_all_configs_and_preserves_others(self):
        for module in self.prompt_cache_modules():
            for config in (self.config, SimpleNamespace(skills=self.config.skills)):
                module.get_enabled_skills_for_config(config, "alice")
                module.get_enabled_skills_for_config(config, "bob")
            module.invalidate_user_skill_cache("alice")
            self.assertEqual([key[1] for key in module._enabled_skills_by_config_cache], ["bob", "bob"])
            module._get_cached_skills_prompt_section.cache_clear.assert_called_once_with()

    def test_bounded_concurrent_custom_writes_and_repeated_lifecycle(self):
        modules = self.prompt_cache_modules()
        self.assert_cached_skill_descriptions(modules, [])
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(self.client.create_skill, "alice", f"skill-{index}", "并发描述") for index in range(32)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(len(set(results)), 32)
        self.assert_cached_skill_descriptions(modules, ["并发描述"] * 32)
        for _ in range(20):
            self.client.create_skill("bob", "cycle", "循环创建")
            self.client.upload_file("bob", "cycle", "SKILL.md", self.markdown("循环更新", name="cycle"))
            self.client.delete_skill("bob", "cycle")
        self.assertEqual(self.client.list_skills("bob"), [])
        for module in modules:
            self.assertEqual(module.get_enabled_skills_for_config(self.config, "bob"), [])


if __name__ == "__main__":
    unittest.main()
