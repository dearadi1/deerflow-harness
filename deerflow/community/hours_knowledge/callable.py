"""宿主在进程启动时注册回调，逐次调用参数由各入口传入。"""

from collections.abc import Callable


_knowledge_search: Callable | None = None

def configure_knowledge_search(knowledge_search: Callable) -> None:
    global _knowledge_search
    _knowledge_search = knowledge_search

def knowledge_search(**kwargs):
    if _knowledge_search is None:
        raise ValueError("未注册knowledge_search，请先调用configure_knowledge_search")
    return _knowledge_search(**kwargs)
