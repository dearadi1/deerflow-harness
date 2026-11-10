"""以受限的 bashrc 格式保存变量，文件始终是跨进程共享的事实来源。"""

from __future__ import annotations

import os
import re
import shlex
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from deerflow.config.extensions_config import extensions_config_file_lock
from deerflow.config.paths import Paths, get_paths

_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_STORE_LOCK = threading.RLock()
_TEMPORARY_ENVIRONMENT: ContextVar[MappingProxyType | None] = ContextVar("deerflow_temporary_environment", default=None)


class EnvironmentSnapshot(dict[str, str]):
    """标记一次命令已解析的环境，确保传输和输出脱敏使用同一份值。"""


def validate_temporary_environment(variables: dict[str, str] | None) -> dict[str, str]:
    """入口即复制，避免排队或延迟消费期间调用方修改本轮参数。"""
    if variables is None:
        return {}
    if not isinstance(variables, dict):
        raise TypeError("temporary_env 必须是字符串字典或 None")
    snapshot = dict(variables)
    for name, value in snapshot.items():
        if not isinstance(value, str):
            raise TypeError("temporary_env 的变量值必须是字符串")
        _validate_variable(name, value)
    return snapshot


@contextmanager
def temporary_environment_scope(variables: dict[str, str]) -> Iterator[None]:
    """显式空作用域隔离相邻轮次；不通过共享沙盒对象或 checkpoint 传递临时值。"""
    token = _TEMPORARY_ENVIRONMENT.set(MappingProxyType(dict(variables)))
    try:
        yield
    finally:
        _TEMPORARY_ENVIRONMENT.reset(token)


def get_temporary_environment() -> dict[str, str]:
    return dict(_TEMPORARY_ENVIRONMENT.get() or {})


def _validate_variable(name: str, value: str | None = None) -> None:
    if not isinstance(name, str) or not _VARIABLE_NAME.fullmatch(name):
        raise ValueError("变量名必须以字母或下划线开头，且仅包含字母、数字和下划线")
    if value is not None and (not isinstance(value, str) or "\0" in value):
        raise ValueError("变量值必须是不含 NUL 的字符串")


def _parse_bashrc(content: str) -> dict[str, str]:
    """只接受 export 赋值；绝不通过 source 或子进程解析宿主上的文件。"""
    tokens = shlex.split(content, comments=True, posix=True)
    if len(tokens) % 2:
        raise ValueError("变量文件只能包含 export NAME=VALUE 赋值")
    variables: dict[str, str] = {}
    for offset in range(0, len(tokens), 2):
        directive, assignment = tokens[offset : offset + 2]
        name, separator, value = assignment.partition("=")
        if directive != "export" or not separator:
            raise ValueError("变量文件只能包含 export NAME=VALUE 赋值")
        _validate_variable(name, value)
        if name in variables:
            raise ValueError(f"变量文件包含重复名称：{name}")
        variables[name] = value
    return variables


def _write_bashrc(path: Path, variables: dict[str, str]) -> None:
    """原子发布，避免并发工具读取半份文件；变量文件始终仅允许属主访问。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".bashrc-", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for name, value in sorted(variables.items()):
                stream.write(f"export {name}={shlex.quote(value)}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class EnvironmentStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        root = Path(base_dir or get_paths().base_dir).absolute()
        if root.is_symlink():
            raise ValueError("环境变量根目录不能是符号链接")
        self.base_dir = root.resolve()
        self._paths = Paths(self.base_dir)

    def _checked_path(self, path: Path) -> Path:
        current = self.base_dir
        for part in ("", *path.relative_to(self.base_dir).parts):
            current = current / part
            if current.is_symlink():
                raise ValueError("环境变量路径不能包含符号链接")
        return path

    def _path(self, user_id: str | None) -> Path:
        if user_id is None:
            return self._checked_path(self.base_dir / ".bashrc")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id 必须是非空字符串")
        return self._checked_path(self._paths.user_dir(user_id) / ".bashrc")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        # 公共和所有私有写入共用同一把锁，使跨文件冲突检查与写入不可交错。
        with _STORE_LOCK:
            self._checked_path(self.base_dir / "..bashrc.lock")
            with extensions_config_file_lock(self._path(None)):
                yield

    def _read(self, path: Path) -> dict[str, str]:
        self._checked_path(path)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return {}
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("环境变量文件必须是普通文件")
            try:
                return _parse_bashrc(stream.read())
            except (ValueError, UnicodeError) as error:
                raise ValueError(f"环境变量文件格式无效：{path}") from error

    def read(self, user_id: str | None = None) -> dict[str, str]:
        path = self._path(user_id)
        if not self.base_dir.exists():
            return {}
        with self._transaction():
            return self._read(path)

    def effective(self, user_id: str) -> dict[str, str]:
        private_path = self._path(user_id)
        if not self.base_dir.exists():
            return {}
        with self._transaction():
            public = self._read(self._path(None))
            private = self._read(private_path)
            if public.keys() & private.keys():
                raise ValueError("私有变量与公共变量重名，请先修正变量文件")
            return {**public, **private}

    def _check_conflict(self, user_id: str | None, name: str) -> None:
        if user_id is not None:
            if name in self._read(self._path(None)):
                raise ValueError(f"私有变量不能与公共变量重名：{name}")
            return
        users = self._checked_path(self.base_dir / "users")
        if not users.exists():
            return
        for user in users.iterdir():
            self._checked_path(user)
            if user.is_dir() and name in self._read(user / ".bashrc"):
                raise ValueError(f"公共变量不能与已有私有变量重名：{name}")

    def mutate(self, user_id: str | None, name: str, value: str | None, *, operation: Literal["create", "update", "delete"]) -> None:
        _validate_variable(name, value)
        if operation != "delete" and not isinstance(value, str):
            raise ValueError("变量值必须是字符串")
        path = self._path(user_id)
        with self._transaction():
            variables = self._read(path)
            if operation == "create" and name in variables:
                raise ValueError(f"变量已存在：{name}")
            if operation != "create" and name not in variables:
                raise KeyError(f"变量不存在：{name}")
            if operation == "delete":
                del variables[name]
            else:
                self._check_conflict(user_id, name)
                variables[name] = value
            _write_bashrc(path, variables)


def get_environment_variables(user_id: str) -> dict[str, str]:
    return EnvironmentStore().effective(user_id)
