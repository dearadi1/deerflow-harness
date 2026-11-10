"""恢复长度截断的 write_file 和 bash；其他调用保留终止保护。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.agents.middlewares.bash_length_recovery import BASH_COMMAND_PREVIEW_CHARS, BASH_REGENERATION_PROMPT
from deerflow.agents.middlewares.model_length_recovery import (
    CONTINUATION_PROMPT,
    FALLBACK_CONTINUATION_PROMPT,
    RECOVERY_ADAPTERS,
    RawToolCallCapture,
    bash_regeneration_result,
    continue_write_result,
    recover_tool_call,
    recovery_event,
    recovery_marker,
)
from deerflow.agents.middlewares.model_length_termination_detectors import (
    ModelLengthTermination,
    ModelLengthTerminationDetector,
    default_detectors,
)
from deerflow.agents.middlewares.model_response import append_visible_text, has_tool_call_intent, has_visible_content
from deerflow.agents.middlewares.tool_call_metadata import clone_ai_message_with_tool_calls
from deerflow.utils.custom_events import aemit_custom_event, emit_custom_event

MODEL_LENGTH_CAPPED_STOP_REASON = "model_length_capped"
_MODEL_LENGTH_CAPPED_CONTENT = "模型在生成完整最终回复前已达到输出上限，请继续对话以恢复生成。"
logger = logging.getLogger(__name__)


def _tool_call_summary(message: AIMessage, cleaned_content: Any) -> tuple[int, list[str]]:
    """Count suppressed tool calls and return their deduplicated names."""
    names: list[str] = []
    structured_calls: list[Any] = [*(message.tool_calls or []), *(getattr(message, "invalid_tool_calls", None) or [])]
    additional_kwargs = message.additional_kwargs or {}
    if structured_calls:
        # LangChain commonly keeps both parsed calls and their raw provider copy.
        calls = structured_calls
    else:
        calls = list(additional_kwargs.get("tool_calls") or [])
        function_call = additional_kwargs.get("function_call")
        if not calls and isinstance(function_call, dict):
            calls.append(function_call)
        if not calls and isinstance(message.content, list) and isinstance(cleaned_content, list):
            # The shared cleanup keeps non-call blocks by identity. Summarize
            # the removed blocks without duplicating its provider type list.
            kept_blocks = {id(block) for block in cleaned_content}
            calls = [block for block in message.content if id(block) not in kept_blocks]

    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        function = call.get("function")
        if not isinstance(name, str) and isinstance(function, dict):
            name = function.get("name")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return len(calls), names


class ModelLengthFinishReasonMiddleware(AgentMiddleware[AgentState]):
    """恢复文件片段或提示重生成 bash，其余长度截断仍终止工具执行。"""

    def __init__(self, detectors: list[ModelLengthTerminationDetector] | None = None) -> None:
        super().__init__()
        self._detectors: list[ModelLengthTerminationDetector] = list(detectors) if detectors else default_detectors()

    def release_policy_parameters(self) -> dict[str, object]:
        from deerflow_extension_api import canonical_hash

        return {
            "suppress_truncated_tool_calls": True,
            "length_notice_hash": canonical_hash(_MODEL_LENGTH_CAPPED_CONTENT),
            "recovery_tools": sorted(RECOVERY_ADAPTERS),
            "continuation_channel": "tool_result",
            "preserve_complete_sibling_calls": True,
            "continuation_prompt_hash": canonical_hash(CONTINUATION_PROMPT),
            "missing_write_path": "workspace_recovery_file",
            "fallback_continuation_prompt_hash": canonical_hash(FALLBACK_CONTINUATION_PROMPT),
            "bash_recovery_execution": False,
            "bash_command_preview_chars": BASH_COMMAND_PREVIEW_CHARS,
            "bash_regeneration_prompt_hash": canonical_hash(BASH_REGENERATION_PROMPT),
        }

    def _detect(self, message: AIMessage) -> ModelLengthTermination | None:
        for detector in self._detectors:
            try:
                hit = detector.detect(message)
            except Exception:  # noqa: BLE001 - provider detectors must not break a run
                logger.exception("ModelLengthTerminationDetector %r raised; treating as no-match", getattr(detector, "name", type(detector).__name__))
                continue
            if hit is not None:
                return hit
        return None

    def _apply(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        messages = list(state.get("messages") or [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None

        last = messages[-1]
        termination = self._detect(last)
        if termination is None:
            return None

        if recovery_marker(last) is not None:
            return None
        recovered = recover_tool_call(last, messages)
        if recovered is not None:
            # 保留可执行调用让图正常经过 tools -> model，长度触顶不再提前结束该轮。
            return {"messages": [recovered]}

        ctx = getattr(runtime, "context", None)
        thread_id = ctx.get("thread_id") if isinstance(ctx, dict) else None
        run_id = ctx.get("run_id") if isinstance(ctx, dict) else None
        stamped_stop_reason = False
        if isinstance(ctx, dict):
            # Preserve any earlier cap reason carried across hidden continuation turns.
            if "stop_reason" not in ctx:
                ctx["stop_reason"] = MODEL_LENGTH_CAPPED_STOP_REASON
                stamped_stop_reason = True
        logger.info(
            "Provider model length cap detected",
            extra={
                "thread_id": thread_id,
                "run_id": run_id,
                "message_id": getattr(last, "id", None),
                "detector": termination.detector,
                "reason_field": termination.reason_field,
                "reason_value": termination.reason_value,
                "stamped_stop_reason": stamped_stop_reason,
            },
        )

        # Unlike dangling-call repair, a length stop must also suppress invalid
        # calls, so clear them before syncing the native provider content.
        content_source = clone_ai_message_with_tool_calls(last.model_copy(update={"invalid_tool_calls": []}), [])
        contains_tool_call = has_tool_call_intent(last) or content_source.content is not last.content
        contains_visible_content = has_visible_content(content_source)
        if not contains_tool_call and contains_visible_content:
            return None

        additional_kwargs = dict(last.additional_kwargs or {})
        suppressed_count, suppressed_names = _tool_call_summary(last, content_source.content) if contains_tool_call else (0, [])
        if contains_tool_call:
            # Tool arguments may be incomplete at a max-token boundary.
            additional_kwargs.pop("tool_calls", None)
            additional_kwargs.pop("function_call", None)
        additional_kwargs["model_length_termination"] = {
            "detector": termination.detector,
            "reason_field": termination.reason_field,
            "reason_value": termination.reason_value,
            "suppressed_tool_call_count": suppressed_count,
            "suppressed_tool_call_names": suppressed_names,
        }
        replacement = content_source.model_copy(
            update={
                "content": append_visible_text(content_source, _MODEL_LENGTH_CAPPED_CONTENT),
                "tool_calls": [],
                "invalid_tool_calls": [],
                "additional_kwargs": additional_kwargs,
                # Keep the provider's termination evidence unchanged.
                "response_metadata": last.response_metadata,
            }
        )
        return {"messages": [replacement]}

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        update = self._apply(state, runtime)
        if (payload := self._recovery_payload(update)) is not None:
            emit_custom_event(payload, writer=runtime.stream_writer)
        return update

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        update = self._apply(state, runtime)
        if (payload := self._recovery_payload(update)) is not None:
            await aemit_custom_event(payload, writer=runtime.stream_writer)
        return update

    @staticmethod
    def _recovery_payload(update: dict[str, Any] | None) -> dict[str, Any] | None:
        if update is None or recovery_marker(update["messages"][0]) is None:
            return None
        return recovery_event(update["messages"][0])

    @override
    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelCallResult:
        capture = RawToolCallCapture()
        response = handler(capture.bind(request))
        return capture.attach(response, self._detect)

    @override
    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]) -> ModelCallResult:
        capture = RawToolCallCapture()
        response = await handler(capture.bind(request))
        return capture.attach(response, self._detect)

    @override
    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]) -> ToolMessage | Command:
        if (result := bash_regeneration_result(request)) is not None:
            return result
        return continue_write_result(request, handler(request))

    @override
    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]) -> ToolMessage | Command:
        if (result := bash_regeneration_result(request)) is not None:
            return result
        return continue_write_result(request, await handler(request))
