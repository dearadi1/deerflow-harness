"""独立管理嵌入式客户端的线程、上传文件及转换产物。

路径沿用 ``get_paths()``，不创建 Agent 或调用模型。默认使用配置中的同步
checkpointer；若 DeerFlowClient 使用自定义实例，此处必须传入同一个实例。
本模块管理嵌入式线程的 checkpoint 和本地目录，不管理 Gateway 的运行记录
或远程沙箱生命周期；调用方应在会话停止运行后删除线程。
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import stat
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock

from langgraph.checkpoint.base import BaseCheckpointSaver, empty_checkpoint

from deerflow.client_config import load_client_config
from deerflow.config.paths import get_paths
from deerflow.uploads.companions import invalidate_overwritten_upload, register_companion, resolve_companion
from deerflow.uploads.manager import (
    UnsafeUploadPathError,
    copy_upload_file_no_symlink,
    delete_file_safe,
    list_files_in_dir,
    normalize_filename,
    publish_upload_copy,
)

logger = logging.getLogger(__name__)

# 固定数量的锁避免线程数量增长导致锁表泄漏，并协调同进程的多个管理器实例。
_THREAD_LOCKS = tuple(RLock() for _ in range(64))


def _thread_lock(user_id: str, thread_id: str):
    return _THREAD_LOCKS[hash((user_id, thread_id)) % len(_THREAD_LOCKS)]


def _validate_filename(file_name: str) -> str:
    if not isinstance(file_name, str) or normalize_filename(file_name) != file_name:
        raise ValueError("file_name 必须是单个文件名，不能包含目录")
    return file_name


def _read_upload_file(path: Path) -> bytes:
    original = path.lstat()
    if not stat.S_ISREG(original.st_mode):
        raise UnsafeUploadPathError(f"下载目标必须是普通文件: {path.name}")
    # 沙箱可修改上传目录，打开时拒绝链接，并核对文件身份后再读取内容。
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise UnsafeUploadPathError(f"下载目标不能是符号链接: {path.name}") from error
        raise
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode) or not os.path.samestat(original, opened) or not os.path.samestat(current, opened):
            raise UnsafeUploadPathError(f"下载目标在打开时发生变化: {path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def _check_directory(path: Path) -> None:
    """拒绝存储根目录下的目录链接，防止跨用户读写或递归删除。"""
    base = get_paths().base_dir
    relative = path.relative_to(base)
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeUploadPathError(f"线程目录不能是符号链接: {current}")
        if current.exists() and not current.is_dir():
            raise NotADirectoryError(str(current))


def _convert_document(source: Path, output: Path) -> Path | None:
    """同步接口在已有事件循环中运行时，将转换放入独立工作线程。"""
    from deerflow.utils.file_conversion import convert_file_to_markdown

    def convert() -> Path | None:
        return asyncio.run(convert_file_to_markdown(source, output_path=output))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return convert()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(convert).result()


class DeerFlowThreadClient:
    """按显式 user_id 隔离线程，不依赖或修改当前用户上下文。

    config_path 未传或为 None 时使用包所在项目根目录的 config.yaml。
    user_id 使用 DeerFlow 的存储用户 ID。线程 ID 在共享 checkpointer 中必须
    全局唯一，本管理器创建时使用 UUID。上传、下载和删除仅接受单个文件名。
    文件列表仅返回 uploads 直属文件的文件名，不递归列出子目录，
    排除有有效归属记录的转码文件；用户直接上传的 Markdown 仍正常返回。
    """

    def __init__(self, config_path: str | None = None, checkpointer: BaseCheckpointSaver | None = None) -> None:
        load_client_config(config_path)
        self._checkpointer = checkpointer

    def _get_checkpointer(self) -> BaseCheckpointSaver:
        if self._checkpointer is not None:
            return self._checkpointer
        from deerflow.runtime.checkpointer.provider import get_checkpointer

        return get_checkpointer()

    def _thread_dir(self, user_id: str, thread_id: str) -> Path:
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id 必须是非空字符串")
        directory = get_paths().thread_dir(thread_id, user_id=user_id)
        _check_directory(directory)
        return directory

    def _uploads_dir(self, user_id: str, thread_id: str) -> Path:
        directory = self._thread_dir(user_id, thread_id)
        if not directory.is_dir():
            raise FileNotFoundError(f"用户线程不存在: {user_id}/{thread_id}")
        uploads = get_paths().sandbox_uploads_dir(thread_id, user_id=user_id)
        _check_directory(uploads)
        _check_directory(directory / "upload-companions")
        return uploads

    def create_thread(self, user_id: str) -> str:
        thread_id = str(uuid.uuid4())
        directory = self._thread_dir(user_id, thread_id)
        with _thread_lock(user_id, thread_id):
            directory.mkdir(parents=True, exist_ok=False)
            self._initialize_thread(user_id, thread_id)
        return thread_id

    def _initialize_thread(self, user_id: str, thread_id: str) -> None:
        try:
            get_paths().ensure_thread_dirs(thread_id, user_id=user_id)
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
            metadata = {"source": "input", "step": -1, "parents": {}, "user_id": user_id}
            self._get_checkpointer().put(config, empty_checkpoint(), metadata, {})
        except Exception:
            get_paths().delete_thread_dir(thread_id, user_id=user_id)
            raise

    def delete_thread(self, user_id: str, thread_id: str) -> None:
        """删除所有 checkpoint 命名空间及整个线程目录；不存在时不执行删除。"""
        with _thread_lock(user_id, thread_id):
            directory = self._thread_dir(user_id, thread_id)
            if not directory.exists():
                return
            checkpointer = self._get_checkpointer()
            self._check_checkpoint_owner(checkpointer, user_id, thread_id)
            # 保留目录直到 checkpoint 删除成功，数据库失败时仍可重试清理。
            checkpointer.delete_thread(thread_id)
            get_paths().delete_thread_dir(thread_id, user_id=user_id)

    @staticmethod
    def _check_checkpoint_owner(checkpointer: BaseCheckpointSaver, user_id: str, thread_id: str) -> None:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        checkpoint = checkpointer.get_tuple(config)
        if checkpoint is not None:
            owner = checkpoint.metadata.get("user_id")
            if owner is not None and owner != user_id:
                raise PermissionError("checkpoint 属于其他用户，不能删除")

    def upload_file(self, user_id: str, thread_id: str, file_name: str, file_bytes: bytes) -> None:
        """同名上传覆盖原文件；文档按现有配置转码，失败时保留原文件并记录日志。"""
        file_name = _validate_filename(file_name)
        if not isinstance(file_bytes, bytes):
            raise TypeError("file_bytes 必须是 bytes")
        with _thread_lock(user_id, thread_id):
            uploads = self._uploads_dir(user_id, thread_id)
            uploads.mkdir(parents=True, exist_ok=True)
            self._save_upload(uploads, file_name, file_bytes)

    def _save_upload(self, uploads: Path, file_name: str, file_bytes: bytes) -> None:
        original = uploads / file_name
        previous_companion = resolve_companion(original)
        # 私有副本贯穿发布和转码，避免沙箱修改原文件后转换到不同内容。
        with tempfile.TemporaryDirectory(prefix="deerflow-thread-upload-") as temporary:
            source = Path(temporary) / file_name
            source.write_bytes(file_bytes)
            saved = copy_upload_file_no_symlink(uploads, file_name, source)
            self._remove_previous_companion(uploads, previous_companion)
            invalidate_overwritten_upload(saved)
            self._publish_companion(saved, source)

    @staticmethod
    def _remove_previous_companion(uploads: Path, companion: Path | None) -> None:
        if companion is not None:
            try:
                delete_file_safe(uploads, companion.name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _publish_companion(original: Path, source: Path) -> None:
        from deerflow.utils.file_conversion import CONVERTIBLE_EXTENSIONS

        if original.suffix.lower() not in CONVERTIBLE_EXTENSIONS:
            return
        with tempfile.TemporaryDirectory(prefix="deerflow-thread-markdown-") as temporary:
            output = Path(temporary) / original.with_suffix(".md").name
            converted = _convert_document(source, output)
            if converted is None:
                logger.warning("文件已上传，但 Markdown 转换失败: %s", original.name)
                return
            companion = publish_upload_copy(original.parent, output.name, converted, claim_seed=output.name, seen_names={original.name})
            register_companion(original, companion)

    def download_file(self, user_id: str, thread_id: str, file_name: str) -> bytes:
        file_name = _validate_filename(file_name)
        with _thread_lock(user_id, thread_id):
            uploads = self._uploads_dir(user_id, thread_id)
            return _read_upload_file(uploads / file_name)

    def delete_file(self, user_id: str, thread_id: str, file_name: str) -> None:
        """只连带删除归属记录验证通过的 Markdown，不按同名猜测删除。"""
        file_name = _validate_filename(file_name)
        with _thread_lock(user_id, thread_id):
            uploads = self._uploads_dir(user_id, thread_id)
            original = uploads / file_name
            companion = resolve_companion(original)
            self._remove_previous_companion(uploads, companion)
            delete_file_safe(uploads, file_name)
            invalidate_overwritten_upload(original)

    def thread_files(self, user_id: str, thread_id: str) -> list[str]:
        with _thread_lock(user_id, thread_id):
            directory = self._thread_dir(user_id, thread_id)
            if not directory.exists():
                return []
            uploads = self._uploads_dir(user_id, thread_id)
            files = list_files_in_dir(uploads)["files"]
            companions = {companion.name for item in files if (companion := resolve_companion(uploads / item["filename"])) is not None}
            return [item["filename"] for item in files if item["filename"] not in companions]
