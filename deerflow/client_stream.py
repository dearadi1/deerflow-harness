"""将 LangGraph 的不同流来源归一为客户端协议，按每轮隔离解析状态。"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage, messages_from_dict

from deerflow.protocol import ClientStreamEvent, ContextUsedData, SuggestionsData, TokenCost, parse_stream_event

from deerflow.client_stream_pricing import ClientStreamPricing, exclude_attributed_child_usage, sum_costs, unpriced_cost

logger = logging.getLogger(__name__)


def _merge_usage(current: dict, incoming: Mapping, *, incremental: bool) -> dict:
    """分片允许负数修正；完整快照只补足差额，避免重复记账。"""
    merged = copy.deepcopy(current)
    for key, value in incoming.items():
        if isinstance(value, Mapping):
            merged[key] = _merge_usage(merged.get(key, {}), value, incremental=incremental)
        elif isinstance(value, int) and not isinstance(value, bool):
            previous = merged.get(key, 0)
            merged[key] = previous + value if incremental else max(previous, value)
    return merged


def _message(value: Any) -> Any:
    if isinstance(value, dict) and value.get("type") in {"ai", "AIMessageChunk", "tool", "human", "system"}:
        return messages_from_dict([{"type": value["type"], "data": value}])[0]
    return value


def _content_parts(message: AIMessage) -> dict[str, str]:
    parts: dict[str, list[str]] = {"answer_content": [], "reasoning_content": []}
    content = message.content
    if isinstance(content, str):
        parts["answer_content"].append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts["answer_content"].append(block)
            elif isinstance(block, dict):
                kind = block.get("type")
                if kind in {"text", "text_delta", "output_text"}:
                    parts["answer_content"].append(block.get("text", ""))
                elif kind in {"reasoning", "thinking", "reasoning_content", "reasoning_delta", "thinking_delta"}:
                    parts["reasoning_content"].append(block.get("reasoning") or block.get("thinking") or block.get("text") or "")
    # 部分供应商同时填充标准块和私有字段，只选一个来源以免重复输出思考。
    if not any(parts["reasoning_content"]):
        reasoning = message.additional_kwargs.get("reasoning_content") or message.additional_kwargs.get("reasoning")
        if isinstance(reasoning, str):
            parts["reasoning_content"].append(reasoning)
    return {kind: "".join(values) for kind, values in parts.items()}


class ClientStreamParser:
    """来源处理器可按 mode 扩展；状态键包含 agent 和消息身份，隔离并行调用。"""

    def __init__(self, thread_id: str, run_id: str, *, pricing: ClientStreamPricing | None = None) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self.seq = 0
        self._pricing = pricing or ClientStreamPricing()
        self._agent_models: dict[str, str | None] = {"main": self._pricing.default_model}
        self._message_models: dict[tuple[str, str], str | None] = {}
        self._costs: dict[tuple[str, str], TokenCost] = {}
        self._text: dict[tuple[str, str, str], str] = {}
        self._usage: dict[tuple[str, str], dict] = {}
        self._complete_usage: set[tuple[str, str]] = set()
        self._tools: dict[tuple[str, str], dict[int, dict]] = {}
        self._tool_summaries: dict[tuple[str, str], list[tuple]] = {}
        self._results: set[tuple[str, str]] = set()
        self._history: set[tuple[str, str]] = set()
        self._anonymous_ids: dict[tuple, str] = {}
        self._invocation_ids: dict[tuple, str] = {}
        self._message_aliases: dict[tuple[str, str], str] = {}
        self._anonymous_objects: dict[int, Any] = {}
        self._metadata: dict[tuple[str, str], dict] = {}
        self._children: dict[str, str] = {}
        self._child_usage: dict[str, dict] = {}
        self._ended: set[str] = set()
        self._title: str | None = None
        self._suggestions: SuggestionsData | None = None
        self._context_fingerprints: dict[tuple[str, str], str] = {}
        self._handlers: dict[str, Callable[..., Iterator[ClientStreamEvent]]] = {
            "messages": self._messages,
            "values": self._values,
            "custom": self._custom,
        }

    def register_source(self, mode: str, handler: Callable[..., Iterator[ClientStreamEvent]]) -> None:
        """新数据源通过入口注册，避免未知来源误走状态快照解析。"""
        self._handlers[mode] = handler

    def event(self, kind: str, agent_id: str, data: Any) -> ClientStreamEvent:
        # LangGraph 没有统一 seq；在事件实际生成处编号，主子智能体共享发布顺序。
        self.seq += 1
        return parse_stream_event({"type": kind, "agent_id": agent_id, "data": copy.deepcopy(data), "thread_id": self.thread_id, "seq": self.seq})

    def parse(self, item: Any) -> Iterator[ClientStreamEvent]:
        namespace: tuple = ()
        if isinstance(item, tuple) and len(item) == 3:
            namespace, mode, payload = item
        elif isinstance(item, tuple) and len(item) == 2:
            mode, payload = item
        else:
            mode, payload = "values", item
        handler = self._handlers.get(mode)
        if handler is None:
            logger.debug("忽略尚未注册的客户端流来源：%s", mode)
            return
        yield from handler(payload, namespace=namespace)

    def _agent(self, metadata: Mapping | None = None, namespace: tuple = ()) -> str:
        metadata = metadata or {}
        parent_id = metadata.get("parent_tool_call_id")
        agent_id = parent_id or metadata.get("agent_id")
        if agent_id:
            agent_id = str(agent_id)
            if agent_id != "main":
                self._children.setdefault(agent_id, str(parent_id or agent_id))
            return agent_id
        # 命名空间标识图实例；不能用模型名或子智能体类型替代实例身份。
        if namespace:
            agent_id = "/".join(namespace)
            self._children.setdefault(agent_id, agent_id)
            return agent_id
        return "main"

    def _message_id(self, message: Any, agent_id: str, metadata: Mapping) -> str:
        native_id = getattr(message, "id", None)
        invocation = metadata.get("run_id") or metadata.get("langgraph_checkpoint_ns")
        key = (agent_id, invocation, metadata.get("langgraph_step")) if invocation else (agent_id, id(message))
        if native_id:
            # 首片缺少 ID 时已发布稳定替代 ID；后续原生 ID 和快照继续绑定同一消息。
            if key in self._anonymous_ids:
                self._message_aliases[(agent_id, str(native_id))] = self._anonymous_ids[key]
            message_id = self._message_aliases.get((agent_id, str(native_id)), str(native_id))
            if invocation:
                self._invocation_ids[key] = message_id
            return message_id
        # 用量常在末尾独立分片到达；缺少消息 ID 时沿用该模型调用已经绑定的身份。
        if invocation and key in self._invocation_ids:
            return self._invocation_ids[key]
        if key not in self._anonymous_ids:
            self._anonymous_ids[key] = str(uuid.uuid4())
        if not invocation:
            # 保留对象身份，避免 Python 复用已释放对象的地址造成错误去重。
            self._anonymous_objects[id(message)] = message
        return self._anonymous_ids[key]

    def _messages(self, payload: Any, *, namespace: tuple = ()) -> Iterator[ClientStreamEvent]:
        metadata: Mapping = {}
        if isinstance(payload, (tuple, list)) and len(payload) == 2:
            payload, metadata = payload
        if not isinstance(metadata, Mapping):
            metadata = {}
        agent_id = self._agent(metadata, namespace)
        yield from self._emit_message(_message(payload), agent_id, metadata)

    def _values(self, payload: Any, *, namespace: tuple = ()) -> Iterator[ClientStreamEvent]:
        if not isinstance(payload, Mapping):
            return
        agent_id = self._agent(namespace=namespace)
        title = payload.get("title")
        if agent_id == "main" and isinstance(title, str) and title and title != self._title:
            self._title = title
            yield self.event("title", agent_id, {"id": f"title_{uuid.uuid4().hex}", "content": title})
        messages = [_message(message) for message in payload.get("messages", [])]
        messages = exclude_attributed_child_usage(messages)
        boundary = next((index for index, message in enumerate(messages) if isinstance(message, HumanMessage) and message.additional_kwargs.get("run_id") == self.run_id), None)
        if boundary is not None:
            self._history.update((agent_id, str(message.id)) for message in messages[:boundary] if getattr(message, "id", None))
        for message in messages[boundary + 1 if boundary is not None else 0:]:
            yield from self._emit_message(message, agent_id, {})

    def _emit_message(self, message: Any, agent_id: str, metadata: Mapping) -> Iterator[ClientStreamEvent]:
        if not isinstance(message, (AIMessage, ToolMessage)):
            return
        message_id = self._message_id(message, agent_id, metadata)
        key = (agent_id, message_id)
        if key in self._history:
            return
        if isinstance(message, ToolMessage):
            yield from self._tool_result(message, agent_id, message_id)
            return
        self._bind_message_model(key, message, metadata)
        incremental = isinstance(message, AIMessageChunk)
        parts = _content_parts(message)
        extra = message.additional_kwargs
        changed_metadata = extra != self._metadata.get(key) and bool(extra.get("deerflow_error_fallback"))
        self._metadata[key] = copy.deepcopy(extra)
        for kind in ("reasoning_content", "answer_content"):
            text_key = (*key, kind)
            previous = self._text.get(text_key, "")
            text = parts[kind]
            delta = text if incremental else text[len(previous):] if text.startswith(previous) else ""
            if delta:
                self._text[text_key] = previous + delta
            if delta or (kind == "answer_content" and changed_metadata):
                data = {"id": message_id, "content": delta}
                if changed_metadata:
                    data["additional_kwargs"] = copy.deepcopy(extra)
                yield self.event(kind, agent_id, data)
        if incremental and message.tool_call_chunks:
            yield from self._tool_chunks(message, agent_id, message_id)
            if message.chunk_position == "last":
                yield from self._flush_tools(key)
        elif not incremental:
            yield from self._complete_tools(message, agent_id, message_id)
        yield from self._account_usage(key, message.usage_metadata, incremental=incremental)

    def _bind_message_model(self, key: tuple[str, str], message: AIMessage, metadata: Mapping) -> None:
        response = message.response_metadata or {}
        has_identity = any((metadata.get("model_name"), response.get("model_name"), response.get("model"), metadata.get("ls_model_name")))
        if has_identity or key not in self._message_models:
            preferred = self._agent_models.get(key[0])
            self._message_models[key] = self._pricing.message_model(metadata, response, preferred=preferred)

    @staticmethod
    def _wire_call(index: int, message_id: str, tool_call_id: str, name: str, arguments: str) -> dict:
        return {"index": index, "id": message_id, "tool_call_id": tool_call_id, "type": "function", "function": {"name": name, "arguments": arguments}}

    def _tool_chunks(self, message: AIMessageChunk, agent_id: str, message_id: str) -> Iterator[ClientStreamEvent]:
        calls = self._tools.setdefault((agent_id, message_id), {})
        for fragment in message.tool_call_chunks:
            index = fragment.get("index")
            call_id = fragment.get("id")
            if index is None:
                # 无 ID 且无 index 的并行分片无法安全归属，交给完整消息恢复。
                if not call_id:
                    continue
                index = next((position for position, call in calls.items() if call["id"] == call_id), None)
            if index is None:
                index = max(calls, default=-1) + 1
            call = calls.setdefault(index, {"id": None, "name": "", "arguments": "", "pending": []})
            if call_id:
                call["id"] = call_id
            name = fragment.get("name") or ""
            arguments = fragment.get("args") or ""
            if name:
                call["name"] += name
                call["pending"].append((name, ""))
            if arguments:
                call["arguments"] += arguments
                call["pending"].append(("", arguments))
            if call["id"]:
                yield from self._pending_fragments(agent_id, message_id, index, call)

    def _pending_fragments(self, agent_id: str, message_id: str, index: int, call: dict) -> Iterator[ClientStreamEvent]:
        for name, arguments in call["pending"]:
            yield self.event("tool_chunk", agent_id, [self._wire_call(index, message_id, call["id"], name, arguments)])
        call["pending"].clear()

    def _complete_tools(self, message: AIMessage, agent_id: str, message_id: str) -> Iterator[ClientStreamEvent]:
        key = (agent_id, message_id)
        calls = self._tools.setdefault(key, {})
        for position, complete in enumerate(message.tool_calls):
            # 完整快照可能重排调用列表；有 ID 时以调用身份为准，不能按位置覆盖。
            index = next((index for index, call in calls.items() if complete.get("id") and call["id"] == complete["id"]), position)
            existing = calls.get(index)
            call_id = complete.get("id") or (existing or {}).get("id")
            if not call_id:
                continue
            arguments = complete["args"]
            arguments = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
            if existing and existing["pending"]:
                existing["id"] = call_id
                yield from self._pending_fragments(agent_id, message_id, index, existing)
            calls[index] = {"id": call_id, "name": complete["name"], "arguments": arguments, "pending": []}
        yield from self._flush_tools(key)

    def _flush_tools(self, key: tuple[str, str]) -> Iterator[ClientStreamEvent]:
        calls = []
        for index, call in sorted(self._tools.get(key, {}).items()):
            if not call["id"] or not call["name"]:
                return
            try:
                json.loads(call["arguments"] or "{}")
            except (ValueError, TypeError):
                return
            calls.append(self._wire_call(index, key[1], call["id"], call["name"], call["arguments"] or "{}"))
        # JSON 空白和键顺序可能被完整快照重写，不应因此重复发布同一次调用。
        signature = [(call["tool_call_id"], call["function"]["name"], json.loads(call["function"]["arguments"])) for call in calls]
        if calls and signature != self._tool_summaries.get(key):
            self._tool_summaries[key] = signature
            yield self.event("tool_calls", key[0], calls)

    def _tool_result(self, message: ToolMessage, agent_id: str, message_id: str) -> Iterator[ClientStreamEvent]:
        # 工具结果去重依赖调用 ID，避免不同快照重建了消息对象或消息 ID。
        key = (agent_id, message.tool_call_id)
        if key in self._results:
            return
        self._results.add(key)
        data = {"id": message_id, "content": message.content, "tool_call_id": message.tool_call_id, "name": message.name, "status": message.status}
        if message.artifact is not None:
            data["artifact"] = message.artifact
        if message.additional_kwargs:
            data["additional_kwargs"] = message.additional_kwargs
        yield self.event("tool_result", agent_id, data)
        child_usage = message.additional_kwargs.get("subagent_token_usage")
        child_status = message.additional_kwargs.get("subagent_status")
        if (child_usage or child_status) and message.tool_call_id not in self._ended:
            yield from self._end_child(message.tool_call_id, child_usage, status=child_status or "unknown")

    def _account_usage(self, key: tuple[str, str], usage: Any, *, incremental: bool, defer: bool = False) -> Iterator[ClientStreamEvent]:
        if key in self._complete_usage:
            return
        if isinstance(usage, Mapping) and usage:
            self._usage[key] = _merge_usage(self._usage.get(key, {}), usage, incremental=incremental)
        # 多个工具请求属于同一次模型生成；先归并分片，再按 AI 消息只发布一次。
        if not incremental and not defer:
            yield from self._finish_usage(key)

    def _finish_usage(self, key: tuple[str, str]) -> Iterator[ClientStreamEvent]:
        if key in self._complete_usage or key not in self._usage:
            return
        self._complete_usage.add(key)
        model_name = self._message_models.get(key)
        cost = self._pricing.cost(model_name, self._usage[key])
        self._costs[key] = cost
        yield self.event("token_usage", key[0], {"id": key[1], "usage_metadata": self._usage[key], "model_name": model_name, "cost": cost})

    def _agent_usage(self, agent_id: str) -> dict:
        total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for (owner, _), usage in self._usage.items():
            if owner == agent_id:
                total = _merge_usage(total, usage, incremental=True)
        # task 的统计包含未通过步骤暴露的模型调用，覆盖已展示步骤的同名汇总字段。
        if agent_id in self._child_usage:
            total.update(copy.deepcopy(self._child_usage[agent_id]))
        return total

    def _agent_cost(self, agent_id: str) -> TokenCost:
        observed = {key for key in self._message_models if key[0] == agent_id}
        accounted = {key for key in self._costs if key[0] == agent_id}
        if observed != accounted:
            return unpriced_cost("incomplete_usage")
        if agent_id in self._child_usage:
            reported = self._child_usage[agent_id]
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                captured = sum(usage.get(field, 0) for (owner, _), usage in self._usage.items() if owner == agent_id)
                if field in reported and captured != reported[field]:
                    return unpriced_cost("incomplete_usage")
        return sum_costs(self._costs[key] for key in accounted)

    def _custom(self, payload: Any, *, namespace: tuple = ()) -> Iterator[ClientStreamEvent]:
        if not isinstance(payload, Mapping):
            return
        kind = payload.get("type", "")
        if not isinstance(kind, str):
            return
        if kind in {"context_compaction_start", "context_compaction_end", "context_used"}:
            agent_id = self._agent(payload, namespace)
            if kind == "context_used":
                yield from self._context_used(payload["data"], agent_id)
            else:
                yield self.event(kind, agent_id, payload["data"])
            return
        if kind == "suggestions_generated" and not namespace:
            yield from self._generated_suggestions(payload)
            return
        if kind == "title_generated" and not namespace:
            yield from self._generated_title(payload)
            return
        task_id = payload.get("task_id")
        if not task_id or not isinstance(kind, str) or not kind.startswith("task_"):
            return
        self._children.setdefault(task_id, task_id)
        if payload.get("model_name"):
            self._agent_models[task_id] = self._pricing.resolve_model(payload["model_name"])
        if isinstance(payload.get("usage"), Mapping):
            self._child_usage[task_id] = dict(payload["usage"])
        if kind == "task_running":
            metadata = {"run_id": task_id, "langgraph_step": payload.get("message_index"), "model_name": payload.get("model_name")}
            yield from self._emit_message(_message(payload.get("message")), task_id, metadata)
        elif kind in {"task_completed", "task_failed", "task_cancelled", "task_timed_out"}:
            yield from self._end_child(task_id, payload.get("usage"), status=kind.removeprefix("task_"))

    def _generated_title(self, payload: Mapping) -> Iterator[ClientStreamEvent]:
        """复用逐消息计费流程，标题调用计入本轮汇总但不混入回答。"""
        message_id, title = payload.get("id"), payload.get("title")
        if not isinstance(message_id, str) or not message_id or not isinstance(title, str) or not title:
            return
        if title != self._title:
            self._title = title
            yield self.event("title", "main", {"id": message_id, "content": title})
        key = ("main", message_id)
        self._message_models[key] = self._pricing.resolve_model(payload.get("model_name"))
        yield from self._account_usage(key, payload.get("usage_metadata"), incremental=False)

    def _context_used(self, payload: Any, agent_id: str) -> Iterator[ClientStreamEvent]:
        data = ContextUsedData.model_validate(payload)
        changed = []
        for context in data.contexts:
            key = (agent_id, context.source)
            if self._context_fingerprints.get(key) != context.content_sha256:
                self._context_fingerprints[key] = context.content_sha256
                changed.append(context)
        if changed:
            yield self.event("context_used", agent_id, ContextUsedData(id=data.id, contexts=changed))

    def _generated_suggestions(self, payload: Mapping) -> Iterator[ClientStreamEvent]:
        message_id = payload.get("id")
        if not isinstance(message_id, str) or not message_id:
            return
        key = ("main", message_id)
        self._message_models[key] = self._pricing.resolve_model(payload.get("model_name"))
        if payload.get("questions"):
            self._suggestions = SuggestionsData(id=message_id, questions=payload["questions"])
        yield from self._account_usage(key, payload.get("usage_metadata"), incremental=False, defer=self._suggestions is not None and self._suggestions.id == message_id)

    def _end_child(self, agent_id: str, usage: Any, *, status: str = "completed") -> Iterator[ClientStreamEvent]:
        self._children.setdefault(agent_id, agent_id)
        if isinstance(usage, Mapping):
            self._child_usage[agent_id] = dict(usage)
        if agent_id in self._ended:
            return
        for key in self._usage:
            if key[0] == agent_id:
                yield from self._finish_usage(key)
        self._ended.add(agent_id)
        yield self.event("end", agent_id, {"id": agent_id, "usage_metadata": self._agent_usage(agent_id), "status": status, "cost": self._agent_cost(agent_id)})

    def finish(self) -> Iterator[ClientStreamEvent]:
        suggestions_key = ("main", self._suggestions.id) if self._suggestions is not None else None
        for key in self._tools:
            yield from self._flush_tools(key)
        for key in self._usage:
            if key != suggestions_key:
                yield from self._finish_usage(key)
        for agent_id in self._children:
            if agent_id not in self._ended:
                yield from self._end_child(agent_id, None, status="unknown")
        # 追问及其用量一起延迟发布，既保持尾部顺序，也在总费用计算前完成记账。
        if self._suggestions is not None:
            yield self.event("suggestions", "main", self._suggestions)
            yield from self._finish_usage(("main", self._suggestions.id))
        total = self._agent_usage("main")
        for agent_id in self._children:
            total = _merge_usage(total, self._agent_usage(agent_id), incremental=True)
        cost = sum_costs(self._agent_cost(agent_id) for agent_id in ("main", *self._children))
        yield self.event("end", "main", {"id": self.run_id, "usage_metadata": total, "cost": cost})
