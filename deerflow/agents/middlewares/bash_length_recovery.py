"""恢复截断 bash 的展示参数，命令片段仅供模型重生成时参考，禁止执行。"""

from __future__ import annotations

from typing import Any

from deerflow.agents.middlewares.truncated_tool_arguments import parse_truncated_arguments

BASH_COMMAND_PREVIEW_CHARS = 100
BASH_REGENERATION_PROMPT = (
    "本次 bash 调用因模型单次输出达到上限而被截断，命令未执行。"
    "调用中的 command 最多只保留已生成命令的前 100 个字符，仅供参考，不能作为完整命令执行。"
    "请自动继续原任务，重新生成完整且较短的 bash 调用；必要时拆分为多个完整命令，"
    "或先使用 write_file 分段写入脚本，再执行脚本。不要只补上被截断的后半段，也不要等待用户追问。"
)
_ARGUMENT_NAMES = frozenset({"description", "command"})


def recover_bash_arguments(value: object) -> dict[str, Any] | None:
    arguments = (
        parse_truncated_arguments(value, argument_names=_ARGUMENT_NAMES, partial_strings=_ARGUMENT_NAMES, allow_missing_fields=True)
        if isinstance(value, str)
        else value
    )
    if not isinstance(arguments, dict) or not arguments.keys() <= _ARGUMENT_NAMES:
        return None
    description, command = arguments.get("description", ""), arguments.get("command", "")
    if not isinstance(description, str) or not isinstance(command, str):
        return None
    try:
        description.encode("utf-8")
        command.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return {"description": description, "command": command[:BASH_COMMAND_PREVIEW_CHARS]}
