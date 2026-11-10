"""只修复 JSON 的截断边界，保留各工具对字段缺失与字符串恢复的独立约束。"""

from __future__ import annotations

import json
import re
from typing import Any

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


def _read_argument(value: str, position: int, decoder: json.JSONDecoder, argument_names: frozenset[str], partial_strings: frozenset[str]) -> tuple[str, Any, int] | None:
    try:
        key, position = decoder.raw_decode(value, _skip_whitespace(value, position))
    except ValueError:
        return None
    if not isinstance(key, str) or key not in argument_names:
        return None
    position = _skip_whitespace(value, position)
    if position >= len(value) or value[position] != ":":
        return None
    position = _skip_whitespace(value, position + 1)
    try:
        parsed, end = decoder.raw_decode(value, position)
        return key, parsed, end
    except ValueError:
        parsed = _decode_string_prefix(value[position:]) if key in partial_strings else None
        return (key, parsed, len(value)) if parsed is not None else None


def _unfinished_field(value: str, remaining_names: set[str]) -> bool:
    value = value.strip()
    if not value:
        return True
    if value.startswith('"') and '"' not in value[1:]:
        return any(name.startswith(value[1:]) for name in remaining_names)
    return any(re.fullmatch(r'"' + name + r'"\s*:?\s*', value) for name in remaining_names)


def parse_truncated_arguments(value: str, *, argument_names: frozenset[str], partial_strings: frozenset[str], allow_missing_fields: bool = False) -> dict[str, Any] | None:
    if allow_missing_fields and not value.strip():
        return {}
    position = _skip_whitespace(value, 0)
    if position >= len(value) or value[position] != "{":
        return None
    position += 1
    decoder = json.JSONDecoder()
    arguments: dict[str, Any] = {}
    while position < len(value):
        if allow_missing_fields and _unfinished_field(value[position:], argument_names - arguments.keys()):
            return arguments
        if allow_missing_fields and value[position:].strip() == "}" and not arguments:
            return arguments
        entry = _read_argument(value, position, decoder, argument_names, partial_strings)
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
