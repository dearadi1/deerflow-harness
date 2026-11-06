"""长度截断的工具恢复通道，当前只注册 write_file 适配器。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.agents.middlewares.message_utils import is_genuine_user_message
from deerflow.agents.middlewares.model_length_termination_detectors import default_detectors
from deerflow.agents.middlewares.safety_termination_detectors import default_detectors as safety_detectors
from deerflow.agents.middlewares.tool_call_args import pair_tool_call_results
from deerflow.agents.middlewares.tool_call_metadata import clone_ai_message_with_tool_calls
from deerflow.agents.middlewares.tool_transform_meta import append_tool_transform
from deerflow.agents.middlewares.write_file_length_recovery import recover_write_file_arguments

RECOVERY_MARKER = "model_length_recovery"
RECOVERY_EVENT = "tool_call_recovered"
RECOVERY_ADAPTERS = {"write_file": recover_write_file_arguments}
CONTINUATION_PROMPT = (
    "上一轮 write_file 因模型单次输出达到上限而被截断，已恢复的正文片段已成功写入，但文件尚未完成。"
    "请自动继续原任务，不要停下来等待用户追问。先用 read_file 读取该文件当前末尾，"
    "从实际落盘的末尾接着生成剩余内容，使用相同 path 和 append=true；不要重复或覆盖已写入正文。"
    "请把 path、append、description 放在 content 之前，分成较短的片段写入；"
    "若末尾是半句或半行，先续完它。完成文件并核验后再给最终答复。"
)


class RawToolCallCapture(BaseCallbackHandler):
    """在 LangChain 丢弃原始 JSON 前保留分片，避免误信其宽松解析的路径。"""

    run_inline = True

    def __init__(self) -> None:
        self.calls: dict[int, dict[str, Any]] = {}
        self.has_unassigned_fragments = False

    def on_llm_new_token(self, token: str, *, chunk=None, **kwargs: Any) -> None:
        message = getattr(chunk, "message", None)
        for fragment in getattr(message, "tool_call_chunks", None) or []:
            self._capture_fragment(fragment)

    def _capture_fragment(self, fragment: dict[str, Any]) -> None:
        index = fragment.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            self.has_unassigned_fragments = True
            return
        call = self.calls.setdefault(index, {"id": None, "name": [], "arguments": []})
        if fragment.get("id"):
            call["id"] = fragment["id"]
        for source, target in (("name", "name"), ("args", "arguments")):
            value = fragment.get(source)
            if isinstance(value, str):
                call[target].append(value)

    def bind(self, request: ModelRequest) -> ModelRequest:
        callbacks = request.model.callbacks
        if callbacks is None or isinstance(callbacks, list):
            callbacks = [*(callbacks or []), self]
        else:
            callbacks = callbacks.copy()
            callbacks.add_handler(self)
        return request.override(model=request.model.model_copy(update={"callbacks": callbacks}))

    def attach(self, response, detect):
        if isinstance(response, ExtendedModelResponse):
            return replace(response, model_response=self.attach(response.model_response, detect))
        if isinstance(response, ModelResponse):
            return replace(response, result=[self.attach(message, detect) for message in response.result])
        if not isinstance(response, AIMessage) or not (self.calls or self.has_unassigned_fragments) or detect(response) is None:
            return response
        raw_calls = [
            {"id": call["id"], "type": "function", "function": {"name": "".join(call["name"]), "arguments": "".join(call["arguments"])}}
            for _, call in sorted(self.calls.items())
        ]
        # 无法归属的分片意味着原始参数不可信，不能回退到 SDK 猜出的结构化参数。
        if self.has_unassigned_fragments:
            raw_calls = []
        return response.model_copy(update={"additional_kwargs": {**response.additional_kwargs, "tool_calls": raw_calls}})


def recovery_marker(message: AIMessage) -> dict[str, Any] | None:
    marker = message.additional_kwargs.get(RECOVERY_MARKER)
    if not isinstance(marker, dict) or marker.get("version") != 1 or marker.get("tool_name") != "write_file":
        return None
    matches = [call for call in message.tool_calls if call.get("id") == marker.get("tool_call_id") and call.get("name") == "write_file"]
    return marker if len(matches) == 1 else None


def _write_succeeded(result: ToolMessage | None) -> bool:
    if result is None or result.status == "error":
        return False
    metadata = result.additional_kwargs.get("deerflow_tool_meta")
    if isinstance(metadata, dict) and metadata.get("status") != "success":
        return False
    marker = result.additional_kwargs.get(RECOVERY_MARKER)
    recovered = isinstance(marker, dict) and marker.get("version") == 1 and marker.get("tool_name") == "write_file" and marker.get("tool_call_id") == result.tool_call_id
    return result.content == "OK" or recovered


def pending_write_recovery(messages: list[Any]) -> dict[str, Any] | None:
    start = next((index + 1 for index in range(len(messages) - 1, -1, -1) if is_genuine_user_message(messages[index])), 0)
    pending = None
    for occurrence in pair_tool_call_results(messages[start:]):
        call = occurrence.tool_call
        if call.get("name") != "write_file" or not _write_succeeded(occurrence.result):
            continue
        marker = recovery_marker(occurrence.message)
        if marker is not None and marker["tool_call_id"] == call.get("id"):
            pending = call["args"]
        elif pending is not None and call.get("args", {}).get("path") == pending.get("path"):
            pending = None
    return pending


def _continue_after_write(message: ToolMessage, marker: dict[str, Any]) -> ToolMessage:
    if message.tool_call_id != marker["tool_call_id"] or message.content != "OK" or not _write_succeeded(message):
        return message
    additional_kwargs = {**message.additional_kwargs, RECOVERY_MARKER: marker}
    append_tool_transform(additional_kwargs, "write_file_continuation", by="ModelLengthFinishReasonMiddleware")
    return message.model_copy(update={"content": f"OK\n\n{CONTINUATION_PROMPT}", "additional_kwargs": additional_kwargs})


def continue_write_result(request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command:
    """把续写指令绑定到真实成功的工具结果，避免伪造用户追问。"""
    if request.tool_call.get("name") != "write_file":
        return result
    last = next((message for message in reversed(request.state.get("messages") or []) if isinstance(message, AIMessage)), None)
    marker = recovery_marker(last) if last is not None else None
    if marker is None or marker["tool_call_id"] != request.tool_call.get("id"):
        return result
    if isinstance(result, ToolMessage):
        return _continue_after_write(result, marker)
    if not isinstance(result.update, dict):
        return result
    messages = result.update.get("messages")
    if isinstance(messages, ToolMessage):
        updated = _continue_after_write(messages, marker)
    elif isinstance(messages, (list, tuple)):
        updated = type(messages)(_continue_after_write(message, marker) if isinstance(message, ToolMessage) else message for message in messages)
    else:
        return result
    return replace(result, update={**result.update, "messages": updated})


def _candidate_calls(message: AIMessage) -> list[dict[str, Any]]:
    raw_calls = message.additional_kwargs.get("tool_calls")
    if raw_calls is None:
        return [*message.tool_calls, *message.invalid_tool_calls]
    if not isinstance(raw_calls, list):
        return []
    calls = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            return []
        function = raw.get("function")
        if not isinstance(function, dict):
            return []
        calls.append({"id": raw.get("id"), "name": function.get("name"), "args": function.get("arguments")})
    return calls


def _complete_arguments(value: Any) -> dict[str, Any] | None:
    try:
        arguments = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return None
    return arguments if isinstance(arguments, dict) else None


def _prepare_calls(calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """只修复一个被截断的写入，其余调用必须具有完整参数和唯一身份。"""
    prepared, incomplete, writes = [], [], []
    seen = set()
    for call in calls:
        call_id, name = call.get("id"), call.get("name")
        if not isinstance(call_id, str) or not call_id or call_id in seen or not isinstance(name, str) or not name:
            return None
        seen.add(call_id)
        arguments = _complete_arguments(call.get("args"))
        if name == "write_file":
            writes.append(call)
        if arguments is None:
            if name not in RECOVERY_ADAPTERS:
                return None
            incomplete.append(call)
        prepared.append({"id": call_id, "name": name, "args": arguments, "type": "tool_call"})
    candidates = incomplete or writes
    return (prepared, candidates[0]) if len(candidates) == 1 else None


def recover_tool_call(message: AIMessage, messages: list[Any]) -> AIMessage | None:
    if message.additional_kwargs.get("safety_termination") or any(detector.detect(message) for detector in safety_detectors()):
        return None
    prepared = _prepare_calls(_candidate_calls(message))
    if prepared is None:
        return None
    calls, call = prepared
    name = call.get("name")
    adapter = RECOVERY_ADAPTERS.get(name) if isinstance(name, str) else None
    arguments = adapter(call.get("args")) if adapter is not None else None
    if arguments is None:
        return None
    pending = pending_write_recovery(messages[:-1])
    if pending is not None and pending.get("path") == arguments["path"]:
        # 续写的 append 若排在截断正文之后，则继承已明确下达的追加语义。
        arguments = adapter(call.get("args"), continue_append=True)
    for candidate in calls:
        if candidate["id"] == call["id"]:
            candidate["args"] = arguments
    cleaned = clone_ai_message_with_tool_calls(message.model_copy(update={"invalid_tool_calls": []}), [])
    metadata = dict(cleaned.response_metadata)
    if isinstance(metadata.get("id"), str) and metadata["id"].startswith("resp_"):
        metadata.pop("id")
    marker = {"version": 1, "tool_name": name, "tool_call_id": call["id"]}
    return cleaned.model_copy(update={"tool_calls": calls, "response_metadata": metadata, "additional_kwargs": {**cleaned.additional_kwargs, RECOVERY_MARKER: marker}})


def awaiting_write_recovery(message: AIMessage) -> bool:
    if recovery_marker(message) is not None:
        return False
    candidates = [*message.tool_calls, *message.invalid_tool_calls, *(getattr(message, "tool_call_chunks", None) or [])]
    candidates.extend(_candidate_calls(message))
    if not any(call.get("name") == "write_file" for call in candidates):
        return False
    return any(detector.detect(message) is not None for detector in default_detectors())


def recovery_event(message: AIMessage) -> dict[str, Any]:
    # 专用中间事件只传工具调用，不重复发布正文和用量。
    return {"type": RECOVERY_EVENT, "message": {"type": "ai", "id": message.id, "content": "", "tool_calls": message.tool_calls, "additional_kwargs": {RECOVERY_MARKER: message.additional_kwargs[RECOVERY_MARKER]}}}
