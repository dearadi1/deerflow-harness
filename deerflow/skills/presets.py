"""公共技能预设的文件存储与每轮不可变快照；私有技能沿用原有状态。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from deerflow.config.extensions_config import extensions_config_file_lock
from deerflow.config.skills_config import SkillsConfig
from deerflow.skills.parser import parse_skill_file
from deerflow.skills.types import SKILL_MD_FILE, Skill, SkillCategory

_PRESET_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_WRITE_LOCK = threading.RLock()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicSkillPreset:
    preset_id: str
    name: str
    enabled_skills: frozenset[str]
    signature: str

    def is_enabled(self, skill_name: str) -> bool:
        return skill_name in self.enabled_skills


_CURRENT_PRESET: ContextVar[PublicSkillPreset | None] = ContextVar("public_skill_preset", default=None)


def current_public_skill_preset() -> PublicSkillPreset | None:
    return _CURRENT_PRESET.get()


@contextmanager
def public_skill_preset_scope(preset: PublicSkillPreset | None) -> Iterator[None]:
    """仅绑定当前执行片段；同步生成器必须在 yield 前退出此作用域。"""
    token = _CURRENT_PRESET.set(preset)
    try:
        yield
    finally:
        _CURRENT_PRESET.reset(token)


def validate_preset_id(preset_id: str) -> str:
    if not isinstance(preset_id, str):
        raise TypeError("预设 ID 必须为字符串")
    if not _PRESET_ID.fullmatch(preset_id):
        raise ValueError("预设 ID 必须为 1～64 个小写字母、数字、下划线或连字符，且以字母或数字开头")
    return preset_id


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError("预设名称必须为 1～100 个字符的非空字符串")
    return name.strip()


def _validate_skill_states(states: Any) -> dict[str, bool]:
    if not isinstance(states, dict):
        raise TypeError("skills 必须为技能名称到布尔值的字典")
    for name, enabled in states.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError("技能名称必须为非空且不含首尾空白的字符串")
        if type(enabled) is not bool:
            raise TypeError(f"技能 {name} 的启用状态必须为布尔值")
    return dict(states)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"预设文件存在重复字段：{key}")
        result[key] = value
    return result


def _validate_document(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"version", "default_preset", "presets"}:
        raise ValueError("预设文件必须且只能包含 version、default_preset、presets")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("不支持的预设文件版本，version 必须为 1")
    presets = data["presets"]
    if not isinstance(presets, dict) or not presets:
        raise ValueError("至少需要保留一个公共技能预设")
    for preset_id, preset in presets.items():
        validate_preset_id(preset_id)
        if not isinstance(preset, dict) or set(preset) != {"name", "skills"}:
            raise ValueError(f"预设 {preset_id} 必须且只能包含 name、skills")
        preset["name"] = _validate_name(preset["name"])
        preset["skills"] = _validate_skill_states(preset["skills"])
    if validate_preset_id(data["default_preset"]) not in presets:
        raise ValueError("default_preset 指向不存在的预设")
    return data


def load_public_skill_catalog(skills_root: Path) -> list[Skill]:
    """独立扫描公共目录，避免预设中的技能被用户同名私有技能遮蔽。"""
    from deerflow.skills.storage.skill_storage import walk_skill_directories

    root = skills_root / SkillCategory.PUBLIC.value
    if not root.is_dir():
        return []
    by_name = {}
    for directory, directories, files in walk_skill_directories(root):
        directories[:] = sorted(name for name in directories if not name.startswith("."))
        if SKILL_MD_FILE not in files:
            continue
        directories.clear()
        skill = parse_skill_file(Path(directory) / SKILL_MD_FILE, category=SkillCategory.PUBLIC, relative_path=Path(directory).relative_to(root))
        if skill is not None:
            by_name[skill.name] = skill
    return [by_name[name] for name in sorted(by_name)]


def _write_document(path: Path, data: dict[str, Any]) -> None:
    """原子替换且保留权限；不对单文件挂载退化为可能截断配置的覆盖写。"""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            temporary.chmod(mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _default_document() -> dict[str, Any]:
    return {"version": 1, "default_preset": "default", "presets": {"default": {"name": "默认预设", "skills": {}}}}


def _effective_skill_states(preset_id: str, configured_states: dict[str, bool], names: set[str]) -> dict[str, bool]:
    # 内置预设自动接纳新增公共技能；显式关闭和自定义预设的选择仍由文件决定。
    return {name: configured_states.get(name, preset_id == "default") for name in sorted(names | configured_states.keys())}


class SkillPresetStore:
    """文件是唯一事实来源；每次操作重读，跨进程锁覆盖整个读改写事务。"""

    def __init__(self, skills_root: Path, *, path: Path | None = None) -> None:
        from deerflow.config.paths import get_paths

        self.skills_root = Path(skills_root)
        configured_path = os.environ.get("DEER_FLOW_SKILL_PRESETS_PATH")
        self.path = Path(path or configured_path or get_paths().base_dir / "skill_presets.json").resolve()
        if self.path.name.lower() in {"config.yaml", "config.yml"}:
            raise ValueError("公共技能预设必须使用独立文件，不能使用 config.yaml")

    def _parse_document(self, content: bytes) -> dict[str, Any]:
        try:
            data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object)
            return _validate_document(data)
        except (ValueError, TypeError, RecursionError) as error:
            raise ValueError(f"公共技能预设文件无效：{self.path}；{error}") from error

    def _read_existing(self) -> dict[str, Any]:
        return self._parse_document(self.path.read_bytes())

    def _backup_invalid_document(self, content: bytes) -> Path:
        """独占创建备份，完整写入后才允许调用方替换损坏文件。"""
        descriptor, backup_name = tempfile.mkstemp(dir=self.path.parent, prefix=f"{self.path.name}.invalid-", suffix=".bak")
        backup = Path(backup_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        return backup

    def initialize(self) -> dict[str, Any]:
        """仅在启动边界恢复格式错误；锁内校验和备份同一份原始字节。"""
        with _WRITE_LOCK, extensions_config_file_lock(self.path):
            if not self.path.exists():
                _write_document(self.path, _default_document())
                return self._read_existing()
            content = self.path.read_bytes()
            try:
                return self._parse_document(content)
            except ValueError as error:
                backup = self._backup_invalid_document(content)
                data = _default_document()
                _write_document(self.path, data)
                logger.warning("公共技能预设格式错误，已恢复全部启用的默认预设；原文件备份：%s；原因：%s", backup, error)
                return data

    def read(self) -> dict[str, Any]:
        if self.path.exists():
            return self._read_existing()
        with _WRITE_LOCK, extensions_config_file_lock(self.path):
            if not self.path.exists():
                _write_document(self.path, _default_document())
            return self._read_existing()

    def _mutate(self, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with _WRITE_LOCK, extensions_config_file_lock(self.path):
            data = self._read_existing() if self.path.exists() else _default_document()
            change(data)
            _write_document(self.path, _validate_document(data))
            return data

    @staticmethod
    def _entry(data: dict[str, Any], preset_id: str) -> dict[str, Any]:
        validate_preset_id(preset_id)
        if preset_id not in data["presets"]:
            raise ValueError(f"公共技能预设不存在：{preset_id}")
        return data["presets"][preset_id]

    def _details(self, data: dict[str, Any], preset_id: str, names: set[str]) -> dict[str, Any]:
        entry = self._entry(data, preset_id)
        states = _effective_skill_states(preset_id, entry["skills"], names)
        return {
            "preset_id": preset_id, "name": entry["name"], "is_default": preset_id == data["default_preset"],
            "skills": states, "enabled_skills": sorted(name for name in names if states[name]),
            "missing_skills": sorted(entry["skills"].keys() - names),
        }

    def list_presets(self) -> dict[str, Any]:
        data = self.read()
        names = {skill.name for skill in load_public_skill_catalog(self.skills_root)}
        return {"default_preset": data["default_preset"], "presets": [self._details(data, preset_id, names) for preset_id in sorted(data["presets"])]}

    def get_preset(self, preset_id: str | None = None) -> dict[str, Any]:
        data = self.read()
        names = {skill.name for skill in load_public_skill_catalog(self.skills_root)}
        return self._details(data, data["default_preset"] if preset_id is None else preset_id, names)

    def resolve(self, preset_id: str | None = None) -> PublicSkillPreset:
        data = self.read()
        selected = data["default_preset"] if preset_id is None else preset_id
        entry = self._entry(data, selected)
        names = {skill.name for skill in load_public_skill_catalog(self.skills_root)} if selected == "default" else set()
        states = _effective_skill_states(selected, entry["skills"], names)
        signature = hashlib.sha256(json.dumps([str(self.path), selected, entry, states], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return PublicSkillPreset(selected, entry["name"], frozenset(name for name, enabled in states.items() if enabled), signature)

    def _checked_states(self, states: dict[str, bool]) -> dict[str, bool]:
        states = _validate_skill_states(states)
        names = {skill.name for skill in load_public_skill_catalog(self.skills_root)}
        missing = states.keys() - names
        if missing:
            raise ValueError(f"公共技能不存在：{', '.join(sorted(missing))}")
        return states

    def create_preset(self, name: str) -> dict[str, Any]:
        name = _validate_name(name)
        preset_id = uuid4().hex

        def change(data):
            nonlocal preset_id
            # 在文件事务内检查冲突，不能覆盖手工配置或其他进程刚创建的预设。
            while preset_id in data["presets"]:
                preset_id = uuid4().hex
            data["presets"][preset_id] = {"name": name, "skills": {}}

        data = self._mutate(change)
        return self._details(data, preset_id, {skill.name for skill in load_public_skill_catalog(self.skills_root)})

    def update_preset(self, preset_id: str, *, skill_states: dict[str, bool], name: str | None = None) -> dict[str, Any]:
        states = self._checked_states(skill_states)
        if name is not None:
            name = _validate_name(name)

        def change(data):
            entry = self._entry(data, preset_id)
            entry["skills"].update(states)
            if name is not None:
                entry["name"] = name

        data = self._mutate(change)
        return self._details(data, preset_id, {skill.name for skill in load_public_skill_catalog(self.skills_root)})

    def set_default_preset(self, preset_id: str) -> dict[str, Any]:
        def change(data):
            self._entry(data, preset_id)
            data["default_preset"] = preset_id

        data = self._mutate(change)
        return self._details(data, preset_id, {skill.name for skill in load_public_skill_catalog(self.skills_root)})

    def delete_preset(self, preset_id: str) -> None:
        def change(data):
            self._entry(data, preset_id)
            if data["default_preset"] == preset_id:
                raise ValueError("不能删除默认预设，请先设置其他默认预设")
            del data["presets"][preset_id]

        self._mutate(change)


def preset_store_for_config(app_config=None) -> SkillPresetStore:
    skills_config = getattr(app_config, "skills", None) or SkillsConfig()
    return SkillPresetStore(skills_config.get_skills_path())


def resolve_public_skill_preset(*, skills_root: Path | None = None, app_config=None) -> PublicSkillPreset:
    preset = current_public_skill_preset()
    if preset is not None:
        return preset
    store = SkillPresetStore(skills_root) if skills_root is not None else preset_store_for_config(app_config)
    return store.resolve()
