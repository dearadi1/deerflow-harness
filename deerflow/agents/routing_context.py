"""为路由分析构造有界的数据视图，不改写主智能体的对话历史。"""

import json
import re
from collections.abc import Mapping

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from deerflow.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder
from deerflow.agents.middlewares.message_utils import is_genuine_user_message
from deerflow.agents.middlewares.pii_redaction_middleware import redact_text
from deerflow.utils.llm_text import extract_response_text
from deerflow.utils.messages import get_original_user_content_text

_TRUNCATED = "\n[已截断]\n"
_MAX_HISTORY_MESSAGES = 256
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".avif", ".heic")


def bounded_text(text: str, budget: int) -> str:
    """按 UTF-8 字节保守限制输入，避免字符估算低估中文和未知供应商分词。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    available = max(0, budget - len(_TRUNCATED.encode("utf-8")))
    if not available:
        return ""
    start = (available + 1) // 2
    end = available // 2
    return encoded[:start].decode("utf-8", errors="ignore") + _TRUNCATED + (encoded[-end:].decode("utf-8", errors="ignore") if end else "")


def request_size(messages: list) -> int:
    # 字节上界加固定消息封装余量，无需在线下载分词器，也不计入真实用量。
    return sum(len(message.content.encode("utf-8")) + 32 for message in messages)


def file_metadata(files: object, pii_config=None) -> list[dict]:
    if not isinstance(files, list):
        return []
    selected = []
    for item in files[:20]:
        if isinstance(item, Mapping):
            selected.append({key: bounded_text(redact_text(item[key], pii_config), 160) for key in ("filename", "mime_type", "type") if isinstance(item.get(key), str)})
        elif isinstance(item, str):
            selected.append({"filename": bounded_text(redact_text(item, pii_config), 160)})
    return selected


def has_current_images(message: HumanMessage) -> bool:
    if isinstance(message.content, list) and any(isinstance(block, dict) and block.get("type") in ("image", "image_url") for block in message.content):
        return True
    files = message.additional_kwargs.get("files")
    for item in files if isinstance(files, list) else []:
        filename = item.get("filename") if isinstance(item, Mapping) else item
        mime_type = item.get("mime_type") if isinstance(item, Mapping) else None
        if isinstance(filename, str) and filename.lower().endswith(_IMAGE_SUFFIXES):
            return True
        if isinstance(mime_type, str) and mime_type.startswith("image/"):
            return True
    return False


def _user_text(message: HumanMessage) -> str:
    return get_original_user_content_text(message.content, message.additional_kwargs)


def _history_rows(state: dict, pii_config=None) -> list[tuple[bool, str]]:
    rows = []
    for message in (state.get("messages") or [])[-_MAX_HISTORY_MESSAGES:]:
        if is_genuine_user_message(message) and not is_dynamic_context_reminder(message):
            files = file_metadata(message.additional_kwargs.get("files"), pii_config)
            text = bounded_text(redact_text(_user_text(message), pii_config), 2048)
            if files:
                text += "\n附件：" + json.dumps(files, ensure_ascii=False)
            rows.append((True, text))
        elif isinstance(message, AIMessage) and not message.tool_calls:
            rows.append((False, bounded_text(redact_text(extract_response_text(message.content), pii_config), 2048)))
    return rows


def routing_memory(state: dict) -> dict:
    memory = state.get("auto_routing")
    if not isinstance(memory, dict) or memory.get("version") != 1:
        return {}
    return {
        "summary": memory.get("summary") if isinstance(memory.get("summary"), str) else "",
        "mode": memory.get("mode") if memory.get("mode") in ("fast", "thinking", "plan", "subagent") else "thinking",
        "needs_vision": memory.get("needs_vision") is True,
    }


def _recent_rows(rows: list[tuple[bool, str]], turns: int) -> list[tuple[bool, str]]:
    start, seen = len(rows), 0
    for index in range(len(rows) - 1, -1, -1):
        if rows[index][0]:
            seen += 1
            if seen > turns:
                break
            start = index
    return rows[start:]


def _render_rows(rows: list[tuple[bool, str]]) -> str:
    return "\n\n".join(f"{'用户' if is_user else '助手'}：{text}" for is_user, text in rows)


def _related_rows(rows: list[tuple[bool, str]], references: list[str]) -> str:
    terms = [term for reference in references for term in re.findall(r"[\w.\-/]{2,80}", reference)][:24]
    ranked = sorted(enumerate(rows), key=lambda entry: (sum(term.lower() in entry[1][1].lower() for term in terms), entry[0]), reverse=True)
    selected = sorted(ranked[:6], key=lambda entry: entry[0])
    return _render_rows([row for _, row in selected])


def _task_state(state: dict) -> str:
    todos = state.get("todos")
    goal = state.get("goal")
    return json.dumps({
        "todos": todos[:12] if isinstance(todos, list) else None,
        "goal": {key: goal.get(key) for key in ("objective", "status")} if isinstance(goal, dict) else None,
    }, ensure_ascii=False, default=str)


def _fit_fields(fields: dict[str, str], system: str, budget: int) -> list:
    weights = {"question": 0.32, "recent": 0.25, "task_summary": 0.12, "history_summary": 0.08, "task_state": 0.08, "files": 0.07, "related_history": 0.08}
    available = budget - len(system.encode("utf-8")) - 256
    if available < 128:
        raise ValueError("路由提示词超过分析输入预算")
    selected = {key: bounded_text(value, int(available * weights[key])) for key, value in fields.items()}
    # 简短字段留下的预算优先补给当前问题及近期上下文。
    remaining = available - sum(len(value.encode("utf-8")) for value in selected.values())
    for key in fields:
        limit = len(selected[key].encode("utf-8")) + remaining
        selected[key] = bounded_text(fields[key], limit)
        remaining = max(0, available - sum(len(value.encode("utf-8")) for value in selected.values()))
    messages = [SystemMessage(content=system), HumanMessage(content=json.dumps(selected, ensure_ascii=False))]
    while request_size(messages) > budget:
        key = max(selected, key=lambda name: len(selected[name].encode("utf-8")))
        selected[key] = bounded_text(selected[key], max(0, len(selected[key].encode("utf-8")) - max(64, request_size(messages) - budget)))
        messages[1] = HumanMessage(content=json.dumps(selected, ensure_ascii=False))
    return messages


def build_routing_messages(message: HumanMessage, state: dict, *, app_config, system: str, expanded: bool = False, references: list[str] | None = None) -> list:
    context = app_config.auto_routing.context
    pii_config = getattr(app_config, "pii_redaction", None)
    rows = _history_rows(state, pii_config)
    recent = _recent_rows(rows, context.recent_turns)
    older = rows[:-len(recent)] if recent else rows
    memory = routing_memory(state)
    fields = {
        "question": _user_text(message),
        "recent": _render_rows(recent),
        "task_summary": bounded_text(redact_text(json.dumps(memory, ensure_ascii=False), pii_config), context.summary_token_budget),
        "history_summary": state.get("summary_text") if isinstance(state.get("summary_text"), str) else "",
        "task_state": _task_state(state),
        "files": json.dumps(file_metadata(message.additional_kwargs.get("files"), pii_config), ensure_ascii=False),
        "related_history": _related_rows(older, references or []) if expanded else "",
    }
    fields = {key: redact_text(value, getattr(app_config, "pii_redaction", None)) for key, value in fields.items()}
    budget = context.expanded_input_token_budget if expanded else context.input_token_budget
    return _fit_fields(fields, system, budget)
