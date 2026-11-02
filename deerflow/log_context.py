"""按执行作用域关联日志，避免同步生成器挂起时泄漏用户和线程信息。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from typing import Any, ParamSpec, TypeVar

_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")
_context: ContextVar[dict[str, Any]] = ContextVar("deerflow_log_context", default={})
LOG_FIELDS = (
    "user_id", "thread_id", "component", "operation", "status", "duration_ms",
    "skill_name", "category", "source_user_id", "target_user_id", "file_path",
    "new_path", "area", "enabled", "byte_count", "count", "event_count",
    "error_type", "replay_last_turn",
)


def get_log_context() -> dict[str, Any]:
    return dict(_context.get())


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


class OperationLog:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.started_at = time.monotonic()
        self.finished = False
        self.logger.info("操作开始", extra={"status": "started"})

    def finish(self, status: str, *, error: BaseException | None = None, **details: Any) -> None:
        if self.finished:
            return
        self.finished = True
        details.update(status=status, duration_ms=round((time.monotonic() - self.started_at) * 1000, 3))
        if error is not None:
            details["error_type"] = type(error).__name__
        exception = (type(error), error, error.__traceback__) if error is not None else None
        level = logging.ERROR if status == "failed" else logging.INFO
        self.logger.log(level, "操作结束：%s", status, extra=details, exc_info=exception)


@contextmanager
def log_operation(logger: logging.Logger, **fields: Any) -> Iterator[OperationLog]:
    with log_context(**fields):
        operation = OperationLog(logger)
        try:
            yield operation
        except BaseException as error:
            status = "failed" if isinstance(error, Exception) else "cancelled"
            operation.finish(status, error=error)
            raise
        else:
            operation.finish("completed")


def _operation_fields(component: str, method: Callable, arguments: dict[str, Any]) -> dict[str, Any]:
    owner = arguments.get("self")
    category = getattr(getattr(owner, "_category", None), "value", None)
    user_id = arguments.get("user_id", arguments.get("source_user_id"))
    fields = {"component": component, "operation": method.__name__, "user_id": user_id,
              "thread_id": arguments.get("thread_id"), "category": category}
    aliases = {"name": "skill_name", "path": "file_path", "file_name": "file_path"}
    # 仅收集操作定位字段，不记录消息、技能正文、文件内容或任意配置参数。
    for name, value in arguments.items():
        field = aliases.get(name, name)
        if field in LOG_FIELDS and field not in {"user_id", "thread_id"}:
            fields[field] = value
    if isinstance(arguments.get("file_bytes"), (bytes, bytearray)):
        fields["byte_count"] = len(arguments["file_bytes"])
    return fields


def logged_operation(component: str) -> Callable[[Callable[_Parameters, _Result]], Callable[_Parameters, _Result]]:
    def decorate(method: Callable[_Parameters, _Result]) -> Callable[_Parameters, _Result]:
        method_signature = signature(method)
        logger = logging.getLogger(method.__module__)

        @wraps(method)
        def wrapped(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Result:
            arguments = method_signature.bind(*args, **kwargs)
            arguments.apply_defaults()
            fields = _operation_fields(component, method, arguments.arguments)
            with log_operation(logger, **fields) as operation:
                result = method(*args, **kwargs)
                details = _result_details(result)
                operation.finish("completed", **details)
                return result

        return wrapped
    return decorate


def _result_details(result: Any) -> dict[str, int]:
    if isinstance(result, bytes):
        return {"byte_count": len(result)}
    if isinstance(result, list):
        return {"count": len(result)}
    return {}


class LoggedIterator(Generator):
    """只在推进和关闭时绑定上下文，包含下游工具日志及流的清理路径。"""

    def __init__(self, iterator: Iterator, logger: logging.Logger, **fields: Any) -> None:
        self.iterator = iterator
        self.logger = logger
        self.fields = fields
        self.operation: OperationLog | None = None
        self.event_count = 0
        self.failed = False
        self.ended = False
        self.closed = False

    def __next__(self):
        return self._advance("__next__")

    def send(self, value):
        return self._advance("__next__") if value is None else self._advance("send", value)

    def throw(self, *args):
        return self._advance("throw", *args)

    def _advance(self, method: str, *args):
        if self.closed:
            raise StopIteration
        with log_context(**self.fields):
            if self.operation is None:
                self.operation = OperationLog(self.logger)
            try:
                event = getattr(self.iterator, method)(*args)
            except StopIteration:
                self._finish("failed" if self.failed else "completed")
                raise
            except BaseException as error:
                self._finish("failed" if isinstance(error, Exception) else "cancelled", error)
                raise
            self.event_count += 1
            self.failed |= getattr(event, "type", None) == "error"
            self.ended |= getattr(event, "type", None) == "end"
            return event

    def _finish(self, status: str, error: BaseException | None = None) -> None:
        if self.operation is not None:
            self.operation.finish(status, error=error, event_count=self.event_count)
        self.closed = True

    def close(self) -> None:
        with log_context(**self.fields):
            try:
                close = getattr(self.iterator, "close", None)
                if close is not None:
                    close()
            except BaseException as error:
                self._finish("failed", error)
                raise
            else:
                status = "failed" if self.failed else "completed" if self.ended else "cancelled"
                self._finish(status)
