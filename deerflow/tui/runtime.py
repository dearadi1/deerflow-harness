"""将客户端协议转换为终端视图动作，在工作线程内消费并保留主会话结束边界。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Protocol

from deerflow.protocol import ClientStreamEvent

from .view_state import (
    Action,
    AssistantDelta,
    AssistantError,
    RunEnded,
    RunStarted,
    ThreadTitle,
    ToolResult,
    ToolStarted,
)


class _ClientLike(Protocol):
    def stream(self, message: str, *, thread_id: str | None = None, **kwargs: Any) -> Iterator[ClientStreamEvent]:
        """Yield streaming events for *message* (see ``DeerFlowClient.stream``)."""


def translate(event: ClientStreamEvent) -> list[Action]:
    """将协议模型转换为视图动作，子智能体结束不终止主会话。"""
    if event.agent_id != "main":
        return []
    if event.type == "answer_content":
        return [AssistantDelta(id=event.data.id, text=event.data.content)] if event.data.content else []
    if event.type == "tool_calls":
        return [ToolStarted(tool_call_id=call.tool_call_id, tool_name=call.function.name, args=json.loads(call.function.arguments)) for call in event.data]
    if event.type == "tool_result":
        data = event.data
        return [ToolResult(tool_call_id=data.tool_call_id, content=_extract_text(data.content), is_error=data.status == "error", tool_name=_as_str(data.name))]
    if event.type == "end":
        return [RunEnded(usage=event.data.usage_metadata.model_dump(exclude_unset=True))]
    if event.type == "title" and event.data.content.strip():
        return [ThreadTitle(title=event.data.content.strip())]
    return []


def _as_str(value: Any) -> str:
    # Provider stream chunks can carry an explicit ``None`` id/name (the key is
    # present, so ``.get(k, "")`` would return None, and ``str(None) == "None"``
    # — a truthy value that would defeat the empty-id guard downstream).
    return "" if value is None else str(value)


def stream_actions(client: _ClientLike, message: str, *, thread_id: str | None = None, **kwargs: Any) -> Iterator[Action]:
    """Yield a bracketed action stream for one agent run.

    Always begins with ``RunStarted`` and ends with ``RunEnded`` (even on error,
    where an ``AssistantError`` row is emitted first).
    """
    yield RunStarted()
    try:
        for event in client.stream(message, thread_id=thread_id, **kwargs):
            yield from translate(event)
            if event.type == "end" and event.agent_id == "main":
                return  # RunEnded already emitted by translate()
        yield RunEnded()
    except Exception as exc:  # noqa: BLE001 - surface any model/runtime error in-UI
        yield AssistantError(str(exc) or exc.__class__.__name__)
        yield RunEnded()


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)
