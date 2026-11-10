"""恢复已生成的文件正文；缺失路径时可使用调用方指定的工作区暂存路径。"""

from __future__ import annotations

from typing import Any

from deerflow.agents.middlewares.truncated_tool_arguments import parse_truncated_arguments

_ARGUMENT_NAMES = frozenset({"description", "path", "content", "append"})


def recover_write_file_arguments(value: object, *, continue_append: bool = False, fallback_path: str | None = None) -> dict[str, Any] | None:
    arguments = parse_truncated_arguments(value, argument_names=_ARGUMENT_NAMES, partial_strings=frozenset({"content", "description"})) if isinstance(value, str) else value
    if not isinstance(arguments, dict) or not arguments.keys() <= _ARGUMENT_NAMES:
        return None
    path = arguments.get("path")
    content = arguments.get("content")
    description = arguments.get("description", "")
    append = arguments.get("append", continue_append)
    if path is None or (isinstance(path, str) and not path.strip()):
        path = fallback_path
        # 暂存文件独立于原目标；不能把缺失目标的追加意图套用到旧文件上。
        if isinstance(append, bool):
            append = False
    if not isinstance(path, str) or not path or not isinstance(content, str) or not content:
        return None
    if not isinstance(description, str) or not isinstance(append, bool):
        return None
    try:
        path.encode("utf-8")
        content.encode("utf-8")
        description.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return {"description": description, "path": path, "append": append, "content": content}
