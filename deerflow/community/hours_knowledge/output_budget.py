"""在知识库结果预算内保留可解析的文档与段落结构。"""

import json
from typing import Any


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_document(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("title"), str) or not isinstance(value.get("sections"), list):
        return False
    return all(
        isinstance(section, dict)
        and "score" in section
        and isinstance(section.get("content"), str)
        and isinstance(section.get("url"), str)
        for section in value["sections"]
    )


def _fit_section(section: dict[str, Any], max_chars: int) -> dict[str, Any] | None:
    # 引用信息必须完整；预算不足时舍弃段落，避免产生无法访问的 URL。
    marker = "…[内容已截断]"
    shortened = {**section, "content": marker}
    if len(_encode(shortened)) > max_chars:
        return None
    lower, upper = 0, len(section["content"])
    while lower < upper:
        middle = (lower + upper + 1) // 2
        shortened["content"] = section["content"][:middle] + marker
        if len(_encode(shortened)) <= max_chars:
            lower = middle
        else:
            upper = middle - 1
    shortened["content"] = section["content"][:lower] + marker
    return shortened


def _clip_documents(documents: list[dict[str, Any]], max_chars: int) -> str:
    retained: list[dict[str, Any]] = []
    size = 2
    for document in documents:
        kept_document = {"title": document["title"], "sections": []}
        overhead = len(_encode(kept_document)) + bool(retained)
        remaining = max_chars - size - overhead
        if remaining < 0:
            return _encode(retained)
        for section in document["sections"]:
            section_size = len(_encode(section)) + bool(kept_document["sections"])
            if section_size <= remaining:
                kept_document["sections"].append(section)
                remaining -= section_size
                continue
            shortened = _fit_section(section, remaining - bool(kept_document["sections"]))
            if shortened is not None:
                kept_document["sections"].append(shortened)
            if kept_document["sections"]:
                retained.append(kept_document)
            return _encode(retained)
        retained.append(kept_document)
        size = max_chars - remaining
    return _encode(retained)


def budget_knowledge_base_search(content: str, max_chars: int) -> str | None:
    """仅裁剪成功结果；错误字符串和未知结构交由通用预算处理。"""
    try:
        documents = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(documents, list) or not all(_is_document(document) for document in documents):
        return None
    # 空 JSON 数组至少占两个字符，极小配置也不能生成损坏的 JSON。
    max_chars = max(2, max_chars)
    compact = _encode(documents)
    if len(compact) <= max_chars:
        return compact
    return _clip_documents(documents, max_chars)
