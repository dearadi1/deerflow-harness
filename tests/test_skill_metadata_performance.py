"""验证解析复用不会掩盖文件修改、访问失败或不同用户的技能开关。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deerflow.skills import parser
from deerflow.skills.types import SkillCategory
from test_skill_presets import PresetFixture, write_skill


class SkillMetadataCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.file = write_skill(self.root, "cache-probe") / "SKILL.md"
        parser._cached_frontmatter.cache_clear()
        self.addCleanup(parser._cached_frontmatter.cache_clear)

    def parse(self, category=SkillCategory.PUBLIC, relative_path=None, **kwargs):
        return parser.parse_skill_file(self.file, category, relative_path, **kwargs)

    def test_repeated_reads_reuse_yaml_without_sharing_path_or_enabled_state(self):
        with patch.object(parser.yaml, "safe_load", wraps=parser.yaml.safe_load) as load:
            public = self.parse()
            custom = self.parse(SkillCategory.CUSTOM, Path("namespace/custom"))
        self.assertEqual(load.call_count, 1)
        self.assertEqual(public.category, SkillCategory.PUBLIC)
        self.assertEqual(custom.category, SkillCategory.CUSTOM)
        self.assertEqual(custom.relative_path, Path("namespace/custom"))
        self.assertTrue(custom.enabled)
        self.assertIsNot(public, custom)

    def test_same_size_same_timestamp_edit_is_visible_immediately(self):
        original = self.parse()
        metadata = self.file.stat()
        self.file.write_text(self.file.read_text().replace("测试技能", "即时更新"))
        os.utime(self.file, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        updated = self.parse()
        self.assertEqual(self.file.stat().st_size, metadata.st_size)
        self.assertNotEqual(updated.description, original.description)
        self.assertEqual(updated.description, "即时更新")

    def test_warm_cache_does_not_bypass_read_failures_or_deletion(self):
        self.assertIsNotNone(self.parse())
        with patch.object(Path, "read_text", side_effect=PermissionError("测试拒绝读取")):
            with self.assertLogs(parser.logger, level="ERROR"):
                self.assertIsNone(self.parse())
        self.file.unlink()
        self.assertIsNone(self.parse())

    def test_malformed_yaml_keeps_per_call_management_diagnostics(self):
        self.file.write_text("---\nname: probe\ndescription: invalid: yaml\n---\n")
        for _ in range(2):
            diagnostics = []
            with self.assertLogs(parser.logger, level="ERROR"):
                self.assertIsNone(self.parse(diagnostics=diagnostics))
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0].hint, "quote_colon_value")
        self.assertEqual(parser._cached_frontmatter.cache_info().currsize, 0)

    def test_large_frontmatter_is_not_retained(self):
        self.file.write_text("---\nname: probe\ndescription: " + "x" * parser._FRONTMATTER_CACHE_MAX_CHARS + "\n---\n")
        self.assertIsNotNone(self.parse())
        self.assertEqual(parser._cached_frontmatter.cache_info().currsize, 0)


class UserSkillDiscoveryReadTests(PresetFixture):
    def test_public_only_discovery_does_not_read_private_switches(self):
        with patch.object(self.user, "_read_skill_states", side_effect=AssertionError("无私有技能时读取用户开关")):
            self.assertEqual(self.enabled(), {"alpha", "beta"})

    def test_private_switch_file_is_read_once_and_next_call_sees_external_edit(self):
        for name in ("personal-one", "personal-two", "personal-three"):
            write_skill(self.paths.user_custom_skills_dir("alice"), name)
        state_file = self.paths.user_skills_dir("alice") / "_skill_states.json"
        state_file.write_text(json.dumps({"personal-one": {"enabled": False}, "personal-three": None}))
        with patch.object(self.user, "_read_skill_states", wraps=self.user._read_skill_states) as read:
            before = self.enabled()
            self.assertEqual(read.call_count, 1)
        state_file.write_text(json.dumps({"personal-two": {"enabled": False}}))
        after = self.enabled()
        self.assertNotIn("personal-one", before)
        self.assertIn("personal-two", before)
        self.assertIn("personal-three", before)
        self.assertIn("personal-one", after)
        self.assertNotIn("personal-two", after)


if __name__ == "__main__":
    unittest.main()
