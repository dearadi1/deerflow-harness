"""通过宿主注册的检索函数查询 Hours 知识库，按文档组织引用段落。"""

from copy import deepcopy
from inspect import isawaitable
from typing import Any

from langchain.tools import tool
from pydantic import BaseModel

from deerflow.callable import knowledge_search
from deerflow.tools.sync import make_sync_tool_wrapper
from deerflow.tools.types import Runtime


def _section(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": item["score"],
        "content": item["content"],
        "url": item.get("download_path", "") or item.get("external_link", "") or "",
    }


def _group_sections(items: list) -> list[dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, BaseModel):
            item = item.model_dump()
        # 同名文档可能来自不同知识库，必须依据文档 ID 而非标题合并。
        source = sources.setdefault(item["document_id"], {"title": item["title"], "sections": []})
        source["sections"].append(_section(item))
    return list(sources.values())


def _search_parameters(runtime: Runtime, query: str, top_k: int) -> dict[str, Any]:
    # TODO：暂由 stream(**kwargs) 的 knowledge_search_kwargs 提供 kb_ids 等宿主参数；
    # 后续改为正式的知识库上下文。深拷贝避免宿主修改嵌套参数后污染同轮后续检索。
    parameters = deepcopy(dict((runtime.context or {}).get("knowledge_search_kwargs") or {}))
    parameters.update(query=query, top_k=top_k)
    return parameters


async def _search(runtime: Runtime, description: str, query: str, top_k: int) -> list[dict[str, Any]] | str:
    try:
        if not description.strip() or not query.strip():
            raise ValueError("description 和 query 不能为空")
        if isinstance(top_k, bool) or not 1 <= top_k <= 100:
            raise ValueError("top_k 必须为 1 到 100 之间的整数")
        items = knowledge_search(**_search_parameters(runtime, query, top_k))
        if isawaitable(items):
            items = await items
        if not isinstance(items, list):
            raise ValueError("知识库检索结果必须为段落列表")
        return _group_sections(items)
    except Exception as error:
        return f"Error: {error}"


_search_sync = make_sync_tool_wrapper(_search, "knowledge_base_search")


@tool("knowledge_base_search", parse_docstring=True)
def knowledge_base_search_tool(
    runtime: Runtime,
    description: str,
    query: str,
    top_k: int = 5,
) -> list[dict[str, Any]] | str:
    """检索知识库，按来源文档归并匹配段落，便于回答中引用依据。

    每个来源包含标题及段落列表，段落提供相关性得分、内容和引用链接。
    检索失败时返回以 "Error: " 开头的字符串。

    Args:
        description: 简述本次检索的目的，作为第一个参数提供。
        query: 根据多轮对话中的用户意图组织完整、独立的查询，将代词和上下文指代替换为明确名称或术语。
        top_k: 最多检索的匹配段落数，范围为 1 到 100，默认 5。
    """
    return _search_sync(runtime, description, query, top_k)


knowledge_base_search_tool.handle_validation_error = lambda error: f"Error: {error}"
