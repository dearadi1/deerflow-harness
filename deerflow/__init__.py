from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_client import DeerFlowClient, DeerFlowClientPool
    from .callable import configure_knowledge_search, configure_upload_file
    from .skill_client import DeerFlowSkillClient
    from .thread_client import DeerFlowThreadClient

__all__ = [
    "DeerFlowClient",
    "DeerFlowClientPool",
    "DeerFlowThreadClient",
    "DeerFlowSkillClient",
    "configure_upload_file",
    "configure_knowledge_search",
]

_PUBLIC_MODULES = {
    "DeerFlowClient": ".agent_client",
    "DeerFlowClientPool": ".agent_client",
    "DeerFlowThreadClient": ".thread_client",
    "DeerFlowSkillClient": ".skill_client",
    "configure_upload_file": ".callable",
    "configure_knowledge_search": ".callable",
}


def __getattr__(name: str) -> Any:
    # 协议消费者只需要 Pydantic，不能因父包初始化而加载客户端运行时。
    module_name = _PUBLIC_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
