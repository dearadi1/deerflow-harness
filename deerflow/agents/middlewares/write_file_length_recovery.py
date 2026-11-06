"""只补齐文件参数的 JSON 边界，不猜测缺失的路径或正文。"""

from __future__ import annotations

import json
import re
from typing import Any

_ARGUMENT_NAMES = frozenset({"description", "path", "content", "append"})
_INCOMPLETE_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{0,3})?$")


def _decode_string_prefix(value: str) -> str | None:
    if not value.startswith('"'):
        return None
    # 只丢弃无法构成字符的末尾转义；中间的坏 JSON 不能伪装成正常截断。
    suffix = _INCOMPLETE_ESCAPE.search(value)
    if suffix is not None:
        slash_count = len(value[: suffix.start()]) - len(value[: suffix.start()].rstrip("\\"))
        if slash_count % 2 == 0:
            value = value[: suffix.start()]
    try:
        content = json.loads(value + '"')
    except (ValueError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    # 半个代理对尚未表示一个 Unicode 字符，交给下一次续写重新生成。
    if content and 0xD800 <= ord(content[-1]) <= 0xDBFF:
        content = content[:-1]
    return content


def _skip_whitespace(value: str, position: int) -> int:
    while position < len(value) and value[position] in " \t\r\n":
        position += 1
    return position


def _read_argument(value: str, position: int, decoder: json.JSONDecoder) -> tuple[str, Any, int] | None:
    try:
        key, position = decoder.raw_decode(value, _skip_whitespace(value, position))
    except ValueError:
        return None
    if not isinstance(key, str) or key not in _ARGUMENT_NAMES:
        return None
    position = _skip_whitespace(value, position)
    if position >= len(value) or value[position] != ":":
        return None
    position = _skip_whitespace(value, position + 1)
    try:
        parsed, end = decoder.raw_decode(value, position)
        return key, parsed, end
    except ValueError:
        parsed = _decode_string_prefix(value[position:]) if key in {"content", "description"} else None
        return (key, parsed, len(value)) if parsed is not None else None


def _parse_arguments(value: str) -> dict[str, Any] | None:
    position = _skip_whitespace(value, 0)
    if position >= len(value) or value[position] != "{":
        return None
    position += 1
    decoder = json.JSONDecoder()
    arguments: dict[str, Any] = {}
    while position < len(value):
        entry = _read_argument(value, position, decoder)
        if entry is None or entry[0] in arguments:
            return None
        key, parsed, position = entry
        arguments[key] = parsed
        position = _skip_whitespace(value, position)
        if position == len(value):
            return arguments
        if value[position] == "}":
            return arguments if not value[position + 1 :].strip() else None
        if value[position] != ",":
            return None
        position = _skip_whitespace(value, position + 1)
    return arguments


def recover_write_file_arguments(value: object, *, continue_append: bool = False) -> dict[str, Any] | None:
    arguments = _parse_arguments(value) if isinstance(value, str) else value
    if not isinstance(arguments, dict) or not arguments.keys() <= _ARGUMENT_NAMES:
        return None
    path = arguments.get("path")
    content = arguments.get("content")
    description = arguments.get("description", "")
    append = arguments.get("append", continue_append)
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
