"""验证公共预设持久化、私有隔离、运行快照与沙箱视图。"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deerflow.config.app_config import AppConfig
from deerflow.config.paths import Paths
from deerflow.skills.presets import SkillPresetStore, current_public_skill_preset, public_skill_preset_scope
from deerflow.skills.projection import ensure_skill_projections, ensure_thread_skill_projection
from deerflow.skills.slash import resolve_slash_skill
from deerflow.skills.storage import UserScopedSkillStorage


def write_skill(root, name):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 测试技能\n---\n正文 {name}\n", encoding="utf-8")
    return directory


class PresetFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.skills = self.root / "skills"
        self.paths = Paths(self.root / "runtime")
        self.file = self.root / "skill_presets.json"
        self.extension_file = self.root / "extensions.json"
        self.extension_file.write_text("{}")
        self.enterContext(patch.dict(os.environ, {
            "DEER_FLOW_HOME": str(self.paths.base_dir),
            "DEER_FLOW_SKILL_PRESETS_PATH": str(self.file),
            "DEER_FLOW_EXTENSIONS_CONFIG_PATH": str(self.extension_file),
        }))
        self.enterContext(patch("deerflow.config.paths.get_paths", return_value=self.paths))
        self.config = AppConfig.model_validate({
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "skills": {"path": str(self.skills)},
        })
        write_skill(self.skills / "public", "alpha")
        write_skill(self.skills / "public", "beta")
        self.store = SkillPresetStore(self.skills)
        self.user = UserScopedSkillStorage("alice", app_config=self.config)

    def create_choices(self):
        self.preset_one = self.store.create_preset("预设1")["preset_id"]
        self.preset_two = self.store.create_preset("预设2")["preset_id"]
        self.store.update_preset(self.preset_one, skill_states={"alpha": True})
        self.store.update_preset(self.preset_two, skill_states={"beta": True})
        self.store.set_default_preset(self.preset_one)

    def enabled(self):
        return {skill.name for skill in self.user.load_skills(enabled_only=True)}


class SkillPresetStoreTests(PresetFixture):
    def test_create_generates_unique_persisted_ids_and_empty_skill_selection(self):
        created = self.store.create_preset("同名预设")
        another = self.store.create_preset("同名预设")
        preset_id = created["preset_id"]
        self.assertRegex(preset_id, r"^[0-9a-f]{32}$")
        self.assertNotEqual(preset_id, another["preset_id"])
        self.assertEqual(created["skills"], {"alpha": False, "beta": False})
        self.assertEqual(created["enabled_skills"], [])
        self.assertFalse(created["is_default"])
        document = json.loads(self.file.read_text())
        self.assertEqual(document["presets"][preset_id], {"name": "同名预设", "skills": {}})
        self.assertEqual(document["default_preset"], "default")
        self.assertEqual(SkillPresetStore(self.skills).get_preset(preset_id), created)
        created["skills"]["alpha"] = True
        self.assertFalse(self.store.get_preset(preset_id)["skills"]["alpha"])

    def test_generated_id_collision_does_not_overwrite_existing_preset(self):
        existing = self.store.create_preset("原预设")["preset_id"]
        with patch("deerflow.skills.presets.uuid4", side_effect=[SimpleNamespace(hex=existing), SimpleNamespace(hex="generated")]):
            created = self.store.create_preset("新预设")
        self.assertEqual(created["preset_id"], "generated")
        self.assertEqual(self.store.get_preset(existing)["name"], "原预设")

    def test_concurrent_creation_keeps_every_generated_preset(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            created = list(executor.map(self.store.create_preset, ["同名预设"] * 12))
        preset_ids = {preset["preset_id"] for preset in created}
        self.assertEqual(len(preset_ids), 12)
        self.assertEqual(set(self.store.read()["presets"]), {"default"} | preset_ids)

    def test_initial_migration_preserves_legacy_bytes_and_ignores_private_shadow(self):
        self.extension_file.write_text('{"skills":{"alpha":{"enabled":false}},"placeholder":"$SECRET"}')
        original = self.extension_file.read_bytes()
        write_skill(self.paths.user_custom_skills_dir("alice"), "alpha")
        snapshot = self.store.resolve()
        self.assertEqual(snapshot.preset_id, "default")
        self.assertEqual(snapshot.enabled_skills, {"beta"})
        self.assertEqual(self.extension_file.read_bytes(), original)
        self.extension_file.write_text("{}")
        self.assertEqual(self.store.resolve().enabled_skills, {"beta"})

    def test_crud_default_and_return_values_are_independent(self):
        self.create_choices()
        preset_three = self.store.create_preset("预设3")["preset_id"]
        created = self.store.update_preset(preset_three, skill_states={"alpha": True, "beta": True})
        self.assertEqual(created["enabled_skills"], ["alpha", "beta"])
        created["skills"]["alpha"] = False
        self.assertTrue(self.store.get_preset(preset_three)["skills"]["alpha"])
        changed = self.store.update_preset(preset_three, skill_states={"alpha": False}, name="新名称")
        self.assertEqual(changed["enabled_skills"], ["beta"])
        self.assertEqual(changed["name"], "新名称")
        self.store.set_default_preset(preset_three)
        self.store.delete_preset(self.preset_one)
        result = self.store.list_presets()
        self.assertEqual(result["default_preset"], preset_three)
        self.assertEqual([item["preset_id"] for item in result["presets"]], sorted(["default", preset_three, self.preset_two]))
        self.assertEqual(SkillPresetStore(self.skills).resolve().enabled_skills, {"beta"})

    def test_manual_edit_and_restarted_client_load_the_same_file(self):
        self.create_choices()
        data = self.store.read()
        data["default_preset"] = self.preset_two
        data["presets"][self.preset_two]["skills"] = {"alpha": True, "beta": False}
        self.file.write_text("\ufeff" + json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(SkillPresetStore(self.skills).resolve().enabled_skills, {"alpha"})
        self.assertEqual(self.store.resolve().preset_id, self.preset_two)
        config_path = self.root / "config.yaml"
        config_path.write_text(json.dumps({
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "skills": {"path": str(self.skills)},
        }), encoding="utf-8")
        program = "import json, sys; from deerflow import DeerFlowSkillClient; print(json.dumps(DeerFlowSkillClient(config_path=sys.argv[1]).public.get_preset()))"
        restarted = subprocess.run([sys.executable, "-c", program, str(config_path)], capture_output=True, text=True, timeout=30)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(json.loads(restarted.stdout), self.store.get_preset(self.preset_two))

    def test_new_public_skill_is_disabled_and_missing_packages_are_reported(self):
        self.create_choices()
        write_skill(self.skills / "public", "later")
        self.assertFalse(self.store.get_preset(self.preset_one)["skills"]["later"])
        (self.skills / "public/alpha/SKILL.md").unlink()
        detail = self.store.get_preset(self.preset_one)
        self.assertEqual(detail["missing_skills"], ["alpha"])
        self.assertEqual(detail["enabled_skills"], [])

    def test_invalid_operations_leave_file_unchanged(self):
        self.create_choices()
        original = self.file.read_bytes()
        operations = [
            lambda: self.store.create_preset(""),
            lambda: self.store.create_preset(" "),
            lambda: self.store.create_preset("x" * 101),
            lambda: self.store.create_preset(None),
            lambda: self.store.create_preset("名称", preset_id="custom"),
            lambda: self.store.create_preset("名称", copy_from="default"),
            lambda: self.store.create_preset("名称", skill_states={"alpha": True}),
            lambda: self.store.update_preset(self.preset_one, skill_states={"missing": True}),
            lambda: self.store.update_preset(self.preset_one, skill_states={"alpha": "false"}),
            lambda: self.store.update_preset(self.preset_one, skill_states={"alpha": 1}),
            lambda: self.store.update_preset("missing", skill_states={}),
            lambda: self.store.set_default_preset("missing"),
            lambda: self.store.delete_preset(self.preset_one),
            lambda: self.store.delete_preset("missing"),
            lambda: self.store.resolve(""),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises((TypeError, ValueError)):
                operation()
            self.assertEqual(self.file.read_bytes(), original)

    def test_corrupt_configuration_fails_without_overwrite_or_enable_fallback(self):
        self.create_choices()
        original = self.store.read()
        invalid = ["{", '{"version":1,"version":1}', json.dumps(original | {"version": True}),
                   json.dumps(original | {"presets": {}}), json.dumps(original | {"default_preset": "missing"})]
        for content in invalid:
            self.file.write_text(content)
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self.store.read()
                with self.assertRaises(ValueError):
                    self.user.load_skills(enabled_only=True)
                self.assertEqual(self.file.read_text(), content)

    def test_failed_atomic_replace_preserves_previous_configuration(self):
        self.create_choices()
        original = self.file.read_bytes()
        with patch("deerflow.skills.presets.os.replace", side_effect=OSError("磁盘写入失败")):
            with self.assertRaises(OSError):
                self.store.update_preset(self.preset_one, skill_states={"beta": True})
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".skill_presets.json.*.tmp")), [])

    def test_thread_and_process_writers_do_not_lose_updates(self):
        self.create_choices()
        names = [f"skill-{index}" for index in range(12)]
        for name in names:
            write_skill(self.skills / "public", name)
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda name: self.store.update_preset(self.preset_one, skill_states={name: True}), names[:6]))
        program = "from pathlib import Path; from deerflow.skills.presets import SkillPresetStore; import sys; SkillPresetStore(Path(sys.argv[1])).update_preset(sys.argv[3], skill_states={sys.argv[2]: True})"
        processes = [subprocess.Popen([sys.executable, "-c", program, str(self.skills), name, self.preset_one], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for name in names[6:]]
        for process in processes:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, (output, error))
        self.assertTrue(set(names) <= self.store.resolve(self.preset_one).enabled_skills)

    def test_config_yaml_cannot_be_used_as_preset_file(self):
        with self.assertRaises(ValueError):
            SkillPresetStore(self.skills, path=self.root / "config.yaml")

    def test_client_initialization_rejects_invalid_preset_file(self):
        from deerflow.client import DeerFlowClient
        from deerflow.client_config import load_client_config

        self.file.write_text("{")
        with patch("deerflow.client.get_app_config", return_value=self.config):
            with self.assertRaisesRegex(ValueError, "预设文件无效"):
                DeerFlowClient()
        with patch("deerflow.client_config.reload_app_config", return_value=self.config):
            with self.assertRaisesRegex(ValueError, "预设文件无效"):
                load_client_config(str(self.root / "app.yaml"))

    def test_default_file_path_uses_framework_data_directory(self):
        with patch.dict(os.environ):
            os.environ.pop("DEER_FLOW_SKILL_PRESETS_PATH", None)
            self.assertEqual(SkillPresetStore(self.skills).path, self.paths.base_dir / "skill_presets.json")

    def test_preset_path_environment_overrides_default_and_explicit_path_wins(self):
        for path in (str(self.root / "custom" / "presets.json"), "relative_presets.json"):
            with self.subTest(path=path), patch.dict(os.environ, {"DEER_FLOW_SKILL_PRESETS_PATH": path}):
                self.assertEqual(SkillPresetStore(self.skills).path, Path(path).resolve())
                self.assertEqual(SkillPresetStore(self.skills, path=self.file).path, self.file)
        with patch.dict(os.environ, {"DEER_FLOW_SKILL_PRESETS_PATH": ""}):
            self.assertEqual(SkillPresetStore(self.skills).path, self.paths.base_dir / "skill_presets.json")


class SkillPresetRuntimeTests(PresetFixture):
    def setUp(self):
        super().setUp()
        self.create_choices()

    def test_private_states_and_same_name_shadow_are_unchanged(self):
        write_skill(self.paths.user_custom_skills_dir("alice"), "alpha")
        write_skill(self.paths.user_custom_skills_dir("alice"), "personal")
        self.user.set_skill_enabled_state("personal", False)
        private_before = self.user._skill_states_file.read_bytes()
        with public_skill_preset_scope(self.store.resolve(self.preset_two)):
            self.assertEqual(self.enabled(), {"alpha", "beta"})
            by_name = {skill.name: skill for skill in self.user.load_skills()}
            self.assertEqual(by_name["alpha"].category.value, "custom")
        self.extension_file.write_text('{"skills":{"alpha":{"enabled":false}}}')
        with public_skill_preset_scope(self.store.resolve(self.preset_one)):
            self.assertEqual(self.enabled(), set())
        self.assertEqual(self.user._skill_states_file.read_bytes(), private_before)

    def test_snapshot_survives_updates_default_switch_and_deletion(self):
        snapshot = self.store.resolve(self.preset_one)
        with public_skill_preset_scope(snapshot):
            self.store.set_default_preset(self.preset_two)
            self.store.update_preset(self.preset_one, skill_states={"alpha": False})
            self.store.delete_preset(self.preset_one)
            self.assertEqual(self.enabled(), {"alpha"})
        self.assertIsNone(current_public_skill_preset())
        self.assertEqual(self.enabled(), {"beta"})
        with self.assertRaises(ValueError):
            self.store.resolve(self.preset_one)

    def test_concurrent_contexts_and_thread_hops_keep_own_preset(self):
        barrier = threading.Barrier(2)

        def run(preset_id):
            with public_skill_preset_scope(self.store.resolve(preset_id)):
                barrier.wait(timeout=5)
                return asyncio.run(asyncio.to_thread(self.enabled))

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(run, (self.preset_one, self.preset_two))), [{"alpha"}, {"beta"}])
        self.assertIsNone(current_public_skill_preset())

    def test_prompt_caches_are_preset_and_revision_scoped(self):
        from test_skill_client_public import load_prompt_cache

        for module_name in ("deerflow.agents.lead_agent.prompt", "deerflow.agents.lead_agent.prompt_zh"):
            module = load_prompt_cache(module_name, lambda *args, **kwargs: self.user)
            for preset_id, expected in ((self.preset_one, ["alpha"]), (self.preset_two, ["beta"]), (self.preset_one, ["alpha"])):
                with public_skill_preset_scope(self.store.resolve(preset_id)):
                    self.assertEqual([skill.name for skill in module.get_enabled_skills_for_config(self.config, "alice")], expected)
            self.store.update_preset(self.preset_one, skill_states={"beta": True})
            self.assertEqual({skill.name for skill in module.get_enabled_skills_for_config(self.config, "alice")}, {"alpha", "beta"})
            self.store.update_preset(self.preset_one, skill_states={"beta": False})

    def test_thread_views_are_isolated_and_default_view_is_not_overwritten(self):
        write_skill(self.paths.user_custom_skills_dir("alice"), "personal")
        views = {}
        for thread_id, preset_id in (("thread_a", self.preset_one), ("thread_b", self.preset_two)):
            with public_skill_preset_scope(self.store.resolve(preset_id)):
                views[thread_id] = ensure_thread_skill_projection(self.user, thread_id, None)
                shared = ensure_skill_projections(self.user)
        self.assertTrue((views["thread_a"].public / "alpha/SKILL.md").is_file())
        self.assertFalse((views["thread_a"].public / "beta").exists())
        self.assertTrue((views["thread_b"].public / "beta/SKILL.md").is_file())
        self.assertFalse((views["thread_b"].public / "alpha").exists())
        self.assertTrue((views["thread_b"].custom / "personal/SKILL.md").is_file())
        self.assertTrue((shared.public / "alpha/SKILL.md").is_file())
        with public_skill_preset_scope(self.store.resolve(self.preset_two)):
            ensure_thread_skill_projection(self.user, "thread_a", None)
        self.assertFalse((views["thread_a"].public / "alpha").exists())
        self.assertTrue((views["thread_a"].public / "beta/SKILL.md").is_file())

    def test_whitelist_intersects_preset_without_reenabling_public_skill(self):
        with public_skill_preset_scope(self.store.resolve(self.preset_one)):
            view = ensure_thread_skill_projection(self.user, "thread_a", {"beta"})
        self.assertEqual(list(view.public.iterdir()), [])

    def test_slash_registry_and_file_guard_use_same_snapshot(self):
        from deerflow.sandbox.tools import _is_disabled_skill_path
        from deerflow.skills.container_registry import build_container_path_registry

        with patch("deerflow.sandbox.tools.get_app_config", return_value=self.config):
            with public_skill_preset_scope(self.store.resolve(self.preset_two)):
                self.assertIsNone(resolve_slash_skill("/alpha 内容", self.user.load_skills()))
                self.assertIsNotNone(resolve_slash_skill("/beta 内容", self.user.load_skills()))
                self.assertTrue(_is_disabled_skill_path("/mnt/skills/public/alpha/SKILL.md", user_id="alice"))
                self.assertFalse(_is_disabled_skill_path("/mnt/skills/public/beta/SKILL.md", user_id="alice"))
                registry = build_container_path_registry(self.user)
                self.assertFalse(registry["/mnt/skills/public/alpha/SKILL.md"].enabled)
                self.assertTrue(registry["/mnt/skills/public/beta/SKILL.md"].enabled)

    def test_public_file_filters_resolve_declared_names_and_nested_packages(self):
        from deerflow.sandbox.tools import _drop_disabled_skill_paths, _is_disabled_skill_path

        directory = write_skill(self.skills / "public/team", "declared-name")
        directory.rename(directory.with_name("folder-alias"))
        write_skill(self.skills / "public/team", "disabled-sibling")
        self.store.update_preset(self.preset_two, skill_states={"declared-name": True})
        allowed = "/mnt/skills/public/team/folder-alias/SKILL.md"
        blocked = "/mnt/skills/public/team/disabled-sibling/SKILL.md"
        with patch("deerflow.sandbox.tools.get_app_config", return_value=self.config):
            with public_skill_preset_scope(self.store.resolve(self.preset_two)):
                self.assertFalse(_is_disabled_skill_path(allowed))
                self.assertTrue(_is_disabled_skill_path(blocked))
                self.assertFalse(_is_disabled_skill_path("/mnt/skills/public/team"))
                self.assertEqual(_drop_disabled_skill_paths([allowed, blocked]), [allowed])

    def test_subagent_isolated_loop_inherits_public_preset(self):
        from deerflow.subagents.executor import _copy_isolated_subagent_context

        with public_skill_preset_scope(self.store.resolve(self.preset_two)):
            inherited = _copy_isolated_subagent_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            self.assertEqual(executor.submit(inherited.run, self.enabled).result(timeout=5), {"beta"})

    def test_persisted_public_reminders_follow_preset_without_changing_private(self):
        from deerflow.agents.middlewares.durable_context_middleware import DurableContextMiddleware
        from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
        from deerflow.skills.container_registry import build_container_path_registry

        write_skill(self.paths.user_custom_skills_dir("alice"), "personal")
        self.user.set_skill_enabled_state("personal", False)
        activation = SkillActivationMiddleware(app_config=self.config, user_id="alice", slash_source_owner_token="preset_test")
        renderer = DurableContextMiddleware(entry_decisions_owner_token="preset_test")
        entries = [{"path": f"/mnt/skills/{category}/{name}/SKILL.md", "name": name} for category, name in (("public", "alpha"), ("public", "beta"), ("custom", "personal"))]
        request = SimpleNamespace(runtime=SimpleNamespace(context={}))
        with public_skill_preset_scope(self.store.resolve(self.preset_two)):
            registry = build_container_path_registry(self.user)
            with patch.object(activation, "_storage", return_value=self.user):
                activation._publish_entry_decisions(request, [entry["path"] for entry in entries], registry, None)
            rendered = renderer._renderable_skills(request, entries)
        self.assertEqual([entry["name"] for entry in rendered], ["beta", "personal"])
        self.assertEqual(len(entries), 3)

    def test_sandbox_middleware_requires_isolation_and_creates_preset_view(self):
        from deerflow.sandbox.middleware import SandboxMiddleware, SandboxRuntimeError

        middleware = SandboxMiddleware()
        provider = SimpleNamespace(supports_agent_skill_isolation=True)
        with patch("deerflow.sandbox.middleware.get_sandbox_provider", return_value=provider), patch("deerflow.config.get_app_config", return_value=self.config):
            with public_skill_preset_scope(self.store.resolve(self.preset_two)):
                view = middleware._prepare_agent_skill_projection("thread_1", user_id="alice")
                self.assertTrue((view.public / "beta/SKILL.md").is_file())
                self.assertFalse((view.public / "alpha").exists())
                provider.supports_agent_skill_isolation = False
                with self.assertRaises(SandboxRuntimeError):
                    middleware._prepare_agent_skill_projection("thread_2", user_id="alice")


if __name__ == "__main__":
    unittest.main()
