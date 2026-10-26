"""独立的用户 custom skill 管理器，复用 DeerFlow 配置和存储隔离。"""

from __future__ import annotations

import errno
import mimetypes
import os
import shutil
import stat
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any, Callable, Iterator, ParamSpec, TypeVar

import yaml

from deerflow.client_config import load_client_config
from deerflow.config.paths import get_paths
from deerflow.skills.frontmatter import split_skill_markdown
from deerflow.skills.parser import parse_allowed_tools, parse_required_secrets
from deerflow.skills.permissions import make_skill_tree_sandbox_readable, make_skill_written_path_sandbox_readable
from deerflow.skills.projection import _projection_lock, get_skill_projection_paths
from deerflow.skills.storage import UserScopedSkillStorage, get_or_new_user_skill_storage
from deerflow.skills.types import SKILL_MD_FILE, SkillCategory


_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")
_METADATA_CACHE_LOCK = threading.RLock()
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


class _SkillMetadataChangedError(RuntimeError):
    """读取期间文件变化时重新读取，避免把不同版本的内容放入同一缓存条目。"""


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

    config_path 未传或为 None 时使用包所在项目根目录的 config.yaml。
    create_skill 和 copy_skill 返回实际存储目录的绝对路径，不包含 SKILL.md；列表中的
    skill_path 使用容器路径，host_path 使用实际目录的绝对路径。
    所有文件参数均相对于技能根目录，父目录
    必须已存在。delete_folder 递归删除目录，upload_file 仅接受 UTF-8 文本。
    根目录的 SKILL.md 由 create_skill 创建，只能通过 upload_file 更新，
    不能单独创建、删除或移动；删除整个技能请使用 delete_skill。
    """

    @_chinese_errors
    def __init__(self, config_path: str | None = None) -> None:
        self._app_config = load_client_config(config_path)

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
    def _validate_markdown(name: str, content: str) -> dict[str, Any]:
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
        return parts.metadata

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

    @_chinese_errors
    def copy_skill(self, source_user_id: str, target_user_id: str, skill_name: str) -> str:
        """将来源用户的 custom 技能完整复制到目标用户，返回目标技能的绝对路径。

        自动创建目标用户缺失的 custom 目录，保留技能名称和包内全部资源，
        不复制来源用户的历史记录和启用状态。目标同名路径已存在时直接报错，
        不覆盖或合并。源技能必须有效，包内不能包含符号链接或特殊文件。
        """
        name = self._validate_skill_name(skill_name)
        source_storage = self._storage(source_user_id)
        source = self._skill_dir(source_storage, name)
        target_storage = self._storage(target_user_id)
        target = self._skill_dir(target_storage, name, exists=False)
        self._require_absent(target)
        self._validate_copy_package(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 在目标文件系统暂存，避免跨磁盘发布失败或暴露尚未复制完整的技能。
        with tempfile.TemporaryDirectory(prefix=".copy-skill-", dir=target.parent) as temporary:
            staged = Path(temporary) / name
            self._stage_skill_copy(source_storage, name, staged)
            self._publish_skill_copy(target_user_id, name, staged)
        return str(target.resolve())

    def _validate_copy_package(self, directory: Path) -> None:
        for path in directory.rglob("*"):
            self._check_owned_path(directory, path)
            mode = path.stat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"技能复制只支持普通文件和文件夹: {path}")
        skill_file = self._relative_path(directory, SKILL_MD_FILE)
        self._require_file(skill_file)
        self._validate_markdown(directory.name, skill_file.read_text(encoding="utf-8"))

    def _stage_skill_copy(self, storage: UserScopedSkillStorage, name: str, staged: Path) -> None:
        # 复制快照时阻止源技能被管理接口修改；发布前释放源锁，避免双向复制死锁。
        with _projection_lock(get_skill_projection_paths(storage).custom.parent):
            source = self._skill_dir(storage, name)
            self._validate_copy_package(source)
            try:
                shutil.copytree(source, staged, symlinks=True)
            except shutil.Error as error:
                raise shutil.Error("复制技能资源失败，请检查源文件权限和目标磁盘空间") from error
        self._validate_copy_package(staged)
        make_skill_tree_sandbox_readable(staged)

    def _publish_skill_copy(self, user_id: str, name: str, staged: Path) -> None:
        conflict = None
        with self._mutation(user_id, name, exists=False) as (_, target):
            # 先独占预留目标，确保并发复制也不会覆盖其他请求创建的同名技能。
            try:
                target.mkdir()
            except FileExistsError as error:
                # 冲突不是内容损坏，正常退出更新流程，保留胜出请求的技能视图。
                conflict = error
            else:
                self._replace_reserved_copy(staged, target)
        if conflict is not None:
            raise conflict

    @staticmethod
    def _replace_reserved_copy(staged: Path, target: Path) -> None:
        try:
            staged.replace(target)
        except Exception:
            target.rmdir()
            raise

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

    @staticmethod
    def _metadata_signature(skill_file: Path) -> tuple[int, ...]:
        details = skill_file.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise ValueError(f"不允许通过符号链接读取技能信息: {skill_file}")
        if stat.S_ISDIR(details.st_mode):
            raise IsADirectoryError(f"SKILL.md 必须是普通文件: {skill_file}")
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"SKILL.md 必须是普通文件: {skill_file}")
        return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns)

    @staticmethod
    @lru_cache(maxsize=1024)
    def _load_metadata_snapshot(skill_file: Path, signature: tuple[int, ...]) -> tuple[dict[str, Any] | None, str | None]:
        """按绝对路径及文件版本缓存完整 YAML，跨客户端复用且限制内存条目数量。"""
        content = skill_file.read_bytes()
        if DeerFlowSkillClient._metadata_signature(skill_file) != signature:
            raise _SkillMetadataChangedError("读取技能信息期间文件发生变化")
        try:
            return DeerFlowSkillClient._validate_markdown(skill_file.parent.name, content.decode("utf-8")), None
        except UnicodeDecodeError:
            return None, "SKILL.md 必须使用有效的 UTF-8 编码"
        except ValueError as error:
            # 无效文档也按版本缓存，避免列表请求不断重复解析同一份损坏的 YAML。
            return None, str(error)

    def _read_skill_metadata(self, directory: Path) -> dict[str, Any]:
        skill_file = self._check_owned_path(directory, directory / SKILL_MD_FILE)
        with _METADATA_CACHE_LOCK:
            for _attempt in range(3):
                signature = self._metadata_signature(skill_file)
                try:
                    metadata, error = self._load_metadata_snapshot(skill_file, signature)
                except _SkillMetadataChangedError:
                    continue
                if metadata is None:
                    raise ValueError(error or "无法解析 SKILL.md 的 YAML 头")
                # 调用方可以修改返回的嵌套字段，不能让这些修改污染进程内共享缓存。
                return deepcopy(metadata)
        raise RuntimeError("技能文件持续发生变化，暂时无法读取技能信息，请稍后重试")

    @_chinese_errors
    def get_skill_info(self, user_id: str, skill_name: str) -> dict[str, Any]:
        """返回该用户 custom 技能的完整 YAML 头，保留各字段原始类型。

        文件未变化时直接复用进程内解析缓存；每次仅检查文件属性，不重复读取
        文件内容。文件被更新、替换或删除后，下次查询立即识别新状态。
        """
        directory = self._skill_dir(self._storage(user_id), skill_name)
        return self._read_skill_metadata(directory)

    @staticmethod
    def _raise_walk_error(error: OSError) -> None:
        raise error

    def _iter_custom_skill_directories(self, root: Path) -> Iterator[Path]:
        if not root.exists():
            return
        self._require_directory(root)
        for current, directories, files in os.walk(root, followlinks=False, onerror=self._raise_walk_error):
            directory = self._check_owned_path(root, Path(current))
            directories[:] = sorted(name for name in directories if not name.startswith(".") and not (directory / name).is_symlink())
            if SKILL_MD_FILE in files:
                # 与运行时发现规则一致，技能包内的示例文档不作为独立技能列出。
                directories.clear()
                yield directory

    def _skill_info(self, root: Path, directory: Path, metadata: dict[str, Any]) -> dict[str, str]:
        return {
            "name": metadata["name"],
            "description": metadata["description"].strip(),
            "skill_path": self._container_path(directory.relative_to(root).as_posix()),
            "host_path": str(directory),
        }

    @_chinese_errors
    def list_skills(self, user_id: str) -> list[dict[str, str]]:
        """仅扫描当前用户的 custom 目录，复用详情缓存，包含禁用项并跳过无效文档。"""
        storage = self._storage(user_id)
        root = storage.get_user_custom_root()
        skills_by_name: dict[str, dict[str, str]] = {}
        for directory in self._iter_custom_skill_directories(root):
            try:
                metadata = self._read_skill_metadata(directory)
            except (ValueError, FileNotFoundError):
                continue
            skills_by_name[metadata["name"]] = self._skill_info(root, directory, metadata)
        return [skills_by_name[name] for name in sorted(skills_by_name)]

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
