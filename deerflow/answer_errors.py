"""标记框架追加的错误正文，仅在客户端输出时替换，保留内部执行语义。"""

from typing import Any

from langchain_core.messages import AIMessage

ANSWER_ERRORS_KEY = "deerflow_answer_errors"


def answer_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(_block_text(block) for block in content)


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict) and block.get("type") in ("text", "text_delta", "output_text"):
        text = block.get("text", "")
        return text if isinstance(text, str) else ""
    return ""


def _error_records(text: str, metadata: dict) -> list[dict]:
    records = metadata.get(ANSWER_ERRORS_KEY)
    if not isinstance(records, list):
        return []
    return sorted((record for record in records if _valid_record(text, record)), key=lambda record: record["start"])


def _valid_record(text: str, record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    start, notice, detail = record.get("start"), record.get("text"), record.get("error")
    return type(start) is int and start >= 0 and isinstance(notice, str) and bool(notice) and isinstance(detail, str) and bool(detail) and text[start : start + len(notice)] == notice


def mark_answer_error(message: AIMessage, notice: str, *, detail: str | None = None) -> AIMessage:
    """按精确位置记录框架文案，避免把模型正文中的相似文本误判为错误。"""
    text = answer_text(message.content)
    if not notice or not text.endswith(notice):
        return message
    metadata = dict(message.additional_kwargs or {})
    record = {"start": len(text) - len(notice), "text": notice, "error": detail or notice}
    records = _error_records(text, metadata)
    if record not in records:
        records.append(record)
    metadata[ANSWER_ERRORS_KEY] = records
    return message.model_copy(update={"additional_kwargs": metadata})


def project_answer_error(message: AIMessage, fallback_message: str) -> tuple[str, dict]:
    """同一消息叠加多个保护时只展示一次提示，全部真实详情仍向外保留。"""
    text = answer_text(message.content)
    metadata = dict(message.additional_kwargs or {})
    records = _error_records(text, metadata)
    metadata.pop(ANSWER_ERRORS_KEY, None)
    if not records:
        return text, metadata
    parts: list[str] = []
    details: list[str] = []
    cursor = 0
    for record in records:
        start = record["start"]
        if start < cursor:
            continue
        parts.extend((text[cursor:start], fallback_message if not details else ""))
        if record["error"] not in details:
            details.append(record["error"])
        cursor = start + len(record["text"])
    parts.append(text[cursor:])
    metadata["error"] = "\n\n".join(details)
    return "".join(parts), metadata
