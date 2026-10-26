"""独立的用户 custom skill 管理器，复用 DeerFlow 配置和存储隔离。"""

from __future__ import annotations

import errno
import mimetypes
import shutil
import stat
import tempfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, ParamSpec, TypeVar

import yaml

from deerflow.config.app_config import get_app_config, reload_app_config
from deerflow.config.paths import get_paths
from deerflow.skills.frontmatter import split_skill_markdown
from deerflow.skills.parser import parse_allowed_tools, parse_required_secrets
from deerflow.skills.permissions import make_skill_written_path_sandbox_readable
from deerflow.skills.storage import UserScopedSkillStorage, get_or_new_user_skill_storage
from deerflow.skills.types import SKILL_MD_FILE, Skill, SkillCategory


_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")
_FILESYSTEM_ERRORS = {
    errno.ENOENT: "文件或目录不存在",
    errno.EEXIST: "目标文件或目录已存在",
    errno.ENOTDIR: "指定路径不是文件夹",
    errno.EISDIR: "指定路径是文件夹，不能作为文件操作",
    errno.EACCES: "没有访问该文件或目录的权限",
    errno.EPERM: "系统不允许执行此操作",
    errno.ENOTEMPTY: "目标文件夹非空",
    errno.ENOSPC: "磁盘空间不足",
    errno.EROFS: "文件系统为只读，无法修改",
    errno.EXDEV: "无法跨文件系统移动文件或文件夹",
    errno.EINVAL: "文件操作参数无效",
    errno.ENAMETOOLONG: "文件名或路径过长",
    errno.ELOOP: "路径中存在循环或过多的符号链接",
    errno.EBUSY: "文件或目录正被占用",
    errno.EMFILE: "当前进程打开的文件数量已达上限",
    errno.ENFILE: "系统打开的文件数量已达上限",
    errno.EIO: "读取或写入文件时发生输入输出错误",
}


def _chinese_errors(method: Callable[_Parameters, _Result]) -> Callable[_Parameters, _Result]:
    """在公开接口处翻译底层提示，保留异常类型、错误码、路径及原始调用栈。"""
    @wraps(method)
    def wrapped(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Result:
        try:
            return method(*args, **kwargs)
        except UnicodeDecodeError as error:
            error.reason = "内容不是有效的 UTF-8 文本，请检查文件编码"
            raise
        except UnicodeEncodeError as error:
            error.reason = "文本包含无法使用 UTF-8 编码的字符"
            raise
        except OSError as error:
            if error.errno is not None:
                message = _FILESYSTEM_ERRORS.get(error.errno, "文件系统操作失败")
                error.strerror = message
                error.args = (error.errno, message, *error.args[2:])
            raise

    return wrapped


class DeerFlowSkillClient:
    """管理指定用户拥有的技能，不需要初始化 Agent。

    create_skill 返回实际存储目录的绝对路径，不包含 SKILL.md；列表中的
    skill_path 使用容器路径，host_path 使用实际目录的绝对路径。
    所有文件参数均相对于技能根目录，父目录
    必须已存在。delete_folder 递归删除目录，upload_file 仅接受 UTF-8 文本。
    根目录的 SKILL.md 由 create_skill 创建，只能通过 upload_file 更新，
    不能单独创建、删除或移动；删除整个技能请使用 delete_skill。
    """

    @_chinese_errors
    def __init__(self, config_path: str | None = None) -> None:
        if config_path is not None:
            reload_app_config(config_path)
        self._app_config = get_app_config()

    @staticmethod
    def _check_owned_path(base: Path, target: Path) -> Path:
        """管理操作拒绝符号链接，避免修改运行时允许挂载的外部技能。"""
        current = base.resolve()
        try:
            relative = target.relative_to(base)
        except ValueError as error:
            raise ValueError(f"目标路径必须位于当前用户的技能目录内: {target}") from error
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"不允许通过符号链接操作技能: {current}")
        return current

    def _storage(self, user_id: str) -> UserScopedSkillStorage:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id 必须是非空字符串")
        storage = get_or_new_user_skill_storage(user_id, app_config=self._app_config)
        if not isinstance(storage, UserScopedSkillStorage):
            raise TypeError("技能管理器需要用户隔离的本地技能存储")
        self._check_owned_path(get_paths().base_dir, storage.get_user_custom_root())
        return storage

    def _skill_dir(self, storage: UserScopedSkillStorage, name: str, *, exists: bool = True) -> Path:
        name = self._validate_skill_name(name)
        directory = self._check_owned_path(storage.get_user_custom_root(), storage.get_custom_skill_dir(name))
        if exists:
            self._require_directory(directory)
        return directory

    @staticmethod
    def _require_directory(path: Path) -> None:
        if not stat.S_ISDIR(path.stat().st_mode):
            raise NotADirectoryError(f"指定路径不是文件夹: {path}")

    @staticmethod
    def _require_file(path: Path) -> None:
        mode = path.stat().st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(f"指定路径是文件夹，不能作为文件操作: {path}")
        if not stat.S_ISREG(mode):
            raise ValueError(f"只支持普通文件: {path}")

    @staticmethod
    def _require_absent(path: Path) -> None:
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"目标文件或目录已存在: {path}")

    @staticmethod
    def _validate_skill_name(name: str) -> str:
        if not isinstance(name, str):
            raise TypeError("技能名称必须是字符串")
        try:
            return UserScopedSkillStorage.validate_skill_name(name)
        except ValueError as error:
            raise ValueError("技能名称必须为 1 至 64 个小写字母、数字或连字符（-），不能以连字符开头或结尾，也不能包含连续连字符") from error

    def _relative_path(self, directory: Path, path: str) -> Path:
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            raise ValueError("path 必须是非空的相对路径，使用 / 分隔")
        relative = Path(path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("path 不得指向技能根目录、绝对路径或包含 ..")
        return self._check_owned_path(directory, directory / relative)

    @contextmanager
    def _mutation(
        self,
        user_id: str,
        skill_name: str,
        *,
        exists: bool = True,
        protected_paths: tuple[str, ...] = (),
    ) -> Iterator[tuple[UserScopedSkillStorage, Path]]:
        storage = self._storage(user_id)
        name = self._validate_skill_name(skill_name)
        directory = self._skill_dir(storage, name, exists=exists)
        # 校验失败不能进入视图更新流程，否则底层异常清理会移除仍有效的技能视图。
        for path in protected_paths:
            self._protect_skill_markdown(directory, self._relative_path(directory, path))
        # 与现有编辑入口共用锁及视图刷新，避免沙箱继续使用过期的技能副本。
        with storage._skill_projection_mutation():
            yield storage, self._skill_dir(storage, name, exists=exists)

    def _container_path(self, name: str) -> str:
        return self._app_config.skills.get_skill_container_path(name, category=SkillCategory.CUSTOM.value)

    @staticmethod
    def _validate_markdown(name: str, content: str) -> None:
        parts, error = split_skill_markdown(content)
        if parts is None:
            messages = {
                "No YAML frontmatter found": "SKILL.md 缺少有效的 YAML 头，请使用独占一行的 --- 包围 YAML 内容",
                "Frontmatter must be a YAML dictionary": "SKILL.md 的 YAML 头必须是字段与值组成的映射",
            }
            message = messages.get(error, "SKILL.md 的 YAML 头语法无效，请检查缩进、引号和字段格式")
            raise ValueError(message) from ValueError(error)
        if parts.metadata.get("name") != name:
            raise ValueError(f"SKILL.md 的 name 必须与目录名 {name!r} 一致")
        description = parts.metadata.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("SKILL.md 的 description 必须是非空字符串")
        DeerFlowSkillClient._validate_runtime_fields(parts.metadata)

    @staticmethod
    def _validate_runtime_fields(metadata: dict[str, object]) -> None:
        """复用运行时解析规则，避免文档写入成功后却无法被技能列表发现。"""
        validators = (
            ("allowed-tools", parse_allowed_tools, "必须是格式正确的工具名称字符串或非空字符串元素组成的列表"),
            ("required-secrets", parse_required_secrets, "必须是列表"),
        )
        for field, validator, message in validators:
            try:
                validator(metadata.get(field), Path(SKILL_MD_FILE))
            except ValueError as error:
                raise ValueError(f"SKILL.md 的 {field} {message}") from error

    def _build_markdown(self, name: str, description: str, extra_fields: dict[str, str]) -> str:
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description 必须是非空字符串")
        if any(not isinstance(value, str) for value in extra_fields.values()):
            raise TypeError("extra_fields 的值必须是字符串")
        metadata = {"name": name, "description": description, **extra_fields}
        header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
        content = f"---\n{header}---\n\n# {name}\n\n{description}\n"
        self._validate_markdown(name, content)
        return content

    @_chinese_errors
    def create_skill(self, user_id: str, name: str, description: str, **extra_fields: str) -> str:
        name = self._validate_skill_name(name)
        content = self._build_markdown(name, description, extra_fields)
        with self._mutation(user_id, name, exists=False) as (storage, directory):
            storage.get_user_custom_root().mkdir(parents=True, exist_ok=True)
            directory.mkdir()
            try:
                self._write_text(directory, directory / SKILL_MD_FILE, content)
            except Exception:
                shutil.rmtree(directory)
                raise
        return str(directory.resolve())

    def _delete_skill_metadata(self, storage: UserScopedSkillStorage, name: str) -> None:
        root = storage.get_user_custom_root()
        history = self._check_owned_path(root, storage.get_skill_history_file(name))
        states_file = self._check_owned_path(root.parent, root.parent / "_skill_states.json")
        history.unlink(missing_ok=True)
        if states_file.exists():
            states = storage._read_skill_states()
            if name in states:
                del states[name]
                storage._write_skill_states(states)

    @_chinese_errors
    def delete_skill(self, user_id: str, skill_name: str) -> None:
        """删除技能目录、历史记录和该用户的启用状态。"""
        with self._mutation(user_id, skill_name) as (storage, directory):
            self._delete_skill_metadata(storage, directory.name)
            shutil.rmtree(directory)

    def _skill_info(self, storage: UserScopedSkillStorage, skill: Skill) -> dict[str, str]:
        directory = self._check_owned_path(storage.get_user_custom_root(), skill.skill_dir)
        return {
            "name": skill.name,
            "description": skill.description,
            "skill_path": self._container_path(skill.skill_path),
            "host_path": str(directory),
        }

    @_chinese_errors
    def list_skills(self, user_id: str) -> list[dict[str, str]]:
        """返回可解析的用户技能，包括禁用项，不包含 public、legacy 和集成技能。"""
        storage = self._storage(user_id)
        return [
            self._skill_info(storage, skill)
            for skill in storage.load_skills(enabled_only=False)
            if skill.category == SkillCategory.CUSTOM
        ]

    @_chinese_errors
    def get_resource_tree(self, user_id: str, skill_name: str) -> list[dict[str, Any]]:
        """返回指定用户技能的资源树，路径相对技能根目录且使用 / 分隔。

        保留旧版节点结构及忽略大小写的名称排序；排除点号开头的目录及其
        子树，但保留隐藏文件。文件和空目录的 children 为 None；符号链接
        仅作为文件节点展示，不读取链接目标内容或递归遍历目标目录。
        """
        storage = self._storage(user_id)
        directory = self._skill_dir(storage, skill_name)
        return self._build_resource_children(directory, directory)

    def _build_resource_children(self, root: Path, directory: Path) -> list[dict[str, Any]]:
        directory = self._check_owned_path(root, directory)
        return [
            self._build_resource_node(root, child)
            for child in sorted(directory.iterdir(), key=lambda item: item.name.lower())
            if not (child.name.startswith(".") and child.is_dir())
        ]

    def _build_resource_node(self, root: Path, path: Path) -> dict[str, Any]:
        is_directory = not path.is_symlink() and path.is_dir()
        children = self._build_resource_children(root, path) if is_directory else []
        return {
            "name": path.name,
            "path": path.relative_to(root).as_posix(),
            "type": "folder" if is_directory else "file",
            "content_type": None if is_directory else (mimetypes.guess_type(path.name)[0] or "application/octet-stream"),
            "children": children or None,
        }

    @_chinese_errors
    def create_file(self, user_id: str, skill_name: str, path: str) -> None:
        with self._mutation(user_id, skill_name, protected_paths=(path,)) as (_, directory):
            target = self._relative_path(directory, path)
            with target.open("xb"):
                pass
            make_skill_written_path_sandbox_readable(directory, target)

    @_chinese_errors
    def create_folder(self, user_id: str, skill_name: str, path: str) -> None:
        with self._mutation(user_id, skill_name, protected_paths=(path,)) as (_, directory):
            target = self._relative_path(directory, path)
            target.mkdir()
            make_skill_written_path_sandbox_readable(directory, target)

    @_chinese_errors
    def delete_file(self, user_id: str, skill_name: str, path: str) -> None:
        with self._mutation(user_id, skill_name, protected_paths=(path,)) as (_, directory):
            target = self._relative_path(directory, path)
            self._require_file(target)
            target.unlink()

    @_chinese_errors
    def delete_folder(self, user_id: str, skill_name: str, path: str) -> None:
        with self._mutation(user_id, skill_name, protected_paths=(path,)) as (_, directory):
            target = self._relative_path(directory, path)
            self._require_directory(target)
            shutil.rmtree(target)

    def _move(self, user_id: str, skill_name: str, path: str, new_path: str, *, folder: bool) -> None:
        with self._mutation(user_id, skill_name, protected_paths=(path, new_path)) as (_, directory):
            source = self._relative_path(directory, path)
            target = self._relative_path(directory, new_path)
            self._require_directory(source) if folder else self._require_file(source)
            self._require_absent(target)
            self._require_directory(target.parent)
            if folder and target.is_relative_to(source):
                raise ValueError("不能将文件夹移入自身的子目录")
            source.rename(target)
            make_skill_written_path_sandbox_readable(directory, target)

    @_chinese_errors
    def move_file(self, user_id: str, skill_name: str, path: str, new_path: str) -> None:
        self._move(user_id, skill_name, path, new_path, folder=False)

    @_chinese_errors
    def move_folder(self, user_id: str, skill_name: str, path: str, new_path: str) -> None:
        self._move(user_id, skill_name, path, new_path, folder=True)

    @staticmethod
    def _write_text(directory: Path, target: Path, content: str) -> None:
        """在外层已持锁时原子替换文件，避免重复获取存储层的跨进程锁。"""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            temporary.replace(target)
            make_skill_written_path_sandbox_readable(directory, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _is_skill_markdown(directory: Path, target: Path) -> bool:
        skill_file = directory / SKILL_MD_FILE
        relative = target.relative_to(directory)
        reserved_name = bool(relative.parts) and relative.parts[0].casefold() == SKILL_MD_FILE.casefold()
        return reserved_name or (skill_file.exists() and target.exists() and target.samefile(skill_file))

    def _protect_skill_markdown(self, directory: Path, target: Path) -> None:
        """大小写别名和硬链接也不能绕过入口文件保护。"""
        if self._is_skill_markdown(directory, target):
            raise ValueError("SKILL.md 是技能入口文件，不能单独创建、删除或移动；请使用 upload_file 更新内容，或使用 delete_skill 删除整个技能")

    def _validate_upload(self, directory: Path, path: str, content: str) -> Path:
        target = self._relative_path(directory, path)
        self._require_file(target)
        if self._is_skill_markdown(directory, target):
            self._validate_markdown(directory.name, content)
        return target

    @_chinese_errors
    def upload_file(self, user_id: str, skill_name: str, path: str, file_bytes: bytes) -> None:
        if not isinstance(file_bytes, (bytes, bytearray)):
            raise TypeError("上传内容必须是字节数据（bytes 或 bytearray）")
        content = file_bytes.decode("utf-8")
        if "\x00" in content:
            raise ValueError("上传内容必须是 UTF-8 文本，不能包含 NUL 字符")
        directory = self._skill_dir(self._storage(user_id), skill_name)
        self._validate_upload(directory, path, content)
        with self._mutation(user_id, skill_name) as (_, directory):
            target = self._validate_upload(directory, path, content)
            self._write_text(directory, target, content)

    @_chinese_errors
    def download_file(self, user_id: str, skill_name: str, path: str) -> bytes:
        storage = self._storage(user_id)
        directory = self._skill_dir(storage, skill_name)
        target = self._relative_path(directory, path)
        self._require_file(target)
        return target.read_bytes()
