"""按记录日期追加文件，避免多个服务进程同时轮转、重命名同一日志。"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path


def get_log_directory(directory: str | Path | None = None) -> Path:
    selected = directory or os.environ.get("DEER_FLOW_LOG_DIR", "").strip()
    from deerflow.config.runtime_paths import resolve_path, runtime_home

    return resolve_path(selected) if selected else runtime_home() / "logs"


def get_log_level(fallback: str | None = None) -> int:
    selected = os.environ.get("DEER_FLOW_LOG_LEVEL", "").strip()
    if not selected:
        selected = fallback or "INFO"
    levels = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
              "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}
    try:
        return levels[selected.strip().upper()]
    except KeyError as error:
        raise ValueError("日志等级必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL") from error


class DailyFileHandler(logging.Handler):
    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._descriptor: int | None = None
        self._file_key: tuple[str, int] | None = None

    def _select_file(self, record: logging.LogRecord) -> int:
        day = datetime.fromtimestamp(record.created).astimezone().date().isoformat()
        key = (day, os.getpid())
        if key != self._file_key or self._descriptor is None:
            self._close_file()
            path = self.directory / f"deerflow-{day}.jsonl"
            self._descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._file_key = key
        return self._descriptor

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = (self.format(record) + "\n").encode("utf-8", errors="backslashreplace")
            descriptor = self._select_file(record)
            # 每条 JSON 用一次追加写入，进程间无需争抢重命名或轮转锁。
            if os.write(descriptor, data) != len(data):
                raise OSError("日志未完整写入文件")
        except Exception:
            self.handleError(record)

    def _close_file(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def close(self) -> None:
        self.acquire()
        try:
            self._close_file()
            super().close()
        finally:
            self.release()
