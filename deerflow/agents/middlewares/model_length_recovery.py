"""长度截断的工具恢复通道，当前只注册 write_file 适配器。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares.message_utils import is_genuine_user_message
from deerflow.agents.middlewares.model_length_termination_detectors import default_detectors
from deerflow.agents.middlewares.safety_termination_detectors import default_detectors as safety_detectors
from deerflow.agents.middlewares.tool_call_args import pair_tool_call_results
from deerflow.agents.middlewares.tool_call_metadata import clone_ai_message_with_tool_calls
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
    if len(message.tool_calls) != 1 or marker.get("tool_call_id") != message.tool_calls[0].get("id"):
        return None
    return marker if message.tool_calls[0].get("name") == "write_file" else None


def _write_succeeded(result: ToolMessage | None) -> bool:
    if result is None or result.status == "error":
        return False
    metadata = result.additional_kwargs.get("deerflow_tool_meta")
    if isinstance(metadata, dict) and metadata.get("status") != "success":
        return False
    return result.content == "OK"


def pending_write_recovery(messages: list[Any]) -> dict[str, Any] | None:
    start = next((index + 1 for index in range(len(messages) - 1, -1, -1) if is_genuine_user_message(messages[index])), 0)
    pending = None
    for occurrence in pair_tool_call_results(messages[start:]):
        call = occurrence.tool_call
        if call.get("name") != "write_file" or not _write_succeeded(occurrence.result):
            continue
        if recovery_marker(occurrence.message) is not None:
            pending = call["args"]
        elif pending is not None and call.get("args", {}).get("path") == pending.get("path"):
            pending = None
    return pending


def continuation_request(request: ModelRequest) -> ModelRequest:
    if pending_write_recovery(list(request.state.get("messages") or [])) is None:
        return request
    reminder = HumanMessage(content=CONTINUATION_PROMPT, name="write_file_continuation", additional_kwargs={"hide_from_ui": True})
    return request.override(messages=[*request.messages, reminder])


def _candidate_call(message: AIMessage) -> dict[str, Any] | None:
    structured = [*message.tool_calls, *message.invalid_tool_calls]
    if len(structured) > 1:
        return None
    raw_calls = message.additional_kwargs.get("tool_calls")
    if raw_calls is not None:
        if not isinstance(raw_calls, list) or len(raw_calls) != 1 or not isinstance(raw_calls[0], dict):
            return None
        raw = raw_calls[0]
        function = raw.get("function")
        if not isinstance(function, dict):
            return None
        return {"id": raw.get("id"), "name": function.get("name"), "args": function.get("arguments")}
    return structured[0] if len(structured) == 1 else None


def recover_tool_call(message: AIMessage, messages: list[Any]) -> AIMessage | None:
    if message.additional_kwargs.get("safety_termination") or any(detector.detect(message) for detector in safety_detectors()):
        return None
    call = _candidate_call(message)
    if call is None or not isinstance(call.get("id"), str) or not call["id"]:
        return None
    name = call.get("name")
    adapter = RECOVERY_ADAPTERS.get(name) if isinstance(name, str) else None
    arguments = adapter(call.get("args")) if adapter is not None else None
    if arguments is None:
        return None
    pending = pending_write_recovery(messages[:-1])
    if pending is not None and pending.get("path") == arguments["path"]:
        # 续写的 append 若排在截断正文之后，则继承已明确下达的追加语义。
        arguments = adapter(call.get("args"), continue_append=True)
    recovered = {"id": call["id"], "name": name, "args": arguments, "type": "tool_call"}
    cleaned = clone_ai_message_with_tool_calls(message.model_copy(update={"invalid_tool_calls": []}), [])
    metadata = dict(cleaned.response_metadata)
    if isinstance(metadata.get("id"), str) and metadata["id"].startswith("resp_"):
        metadata.pop("id")
    marker = {"version": 1, "tool_name": name, "tool_call_id": call["id"]}
    return cleaned.model_copy(update={"tool_calls": [recovered], "response_metadata": metadata, "additional_kwargs": {**cleaned.additional_kwargs, RECOVERY_MARKER: marker}})


def awaiting_write_recovery(message: AIMessage) -> bool:
    if recovery_marker(message) is not None:
        return False
    candidates = [*message.tool_calls, *message.invalid_tool_calls, *(getattr(message, "tool_call_chunks", None) or [])]
    if (raw := _candidate_call(message)) is not None:
        candidates.append(raw)
    if not any(call.get("name") == "write_file" for call in candidates):
        return False
    return any(detector.detect(message) is not None for detector in default_detectors())


def recovery_event(message: AIMessage) -> dict[str, Any]:
    # 专用中间事件只传工具调用，不重复发布正文和用量。
    return {"type": RECOVERY_EVENT, "message": {"type": "ai", "id": message.id, "content": "", "tool_calls": message.tool_calls, "additional_kwargs": {RECOVERY_MARKER: message.additional_kwargs[RECOVERY_MARKER]}}}
