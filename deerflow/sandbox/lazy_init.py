"""每轮只在真正访问沙盒时准备技能视图，普通子代理继承主代理的准备任务。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.acquire_serialization import AcquireSerializer
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.skills.presets import PublicSkillPreset, public_skill_preset_scope

LAZY_SANDBOX_CONTEXT_KEY = "__deerflow_lazy_sandbox"
_INITIALIZATION_LOCKS: AcquireSerializer = AcquireSerializer(thread_name_prefix="sandbox-lazy-init")
_INHERITED_INITIALIZATION: ContextVar[LazySandboxInitialization | None] = ContextVar("deerflow_lazy_sandbox", default=None)


class LazySandboxInitialization:
    def __init__(
        self,
        *,
        context: dict[str, Any],
        user_id: str,
        preset: PublicSkillPreset | None,
        initialize: Callable[[], Any],
        initialize_async: Callable[[], Awaitable[Any]],
    ) -> None:
        self._context = context
        self._thread_id = context["thread_id"]
        self._user_id = user_id
        self._preset = preset
        self._initialize = initialize
        self._initialize_async = initialize_async
        self.completed = False
        self._sandbox_id: str | None = None

    def __deepcopy__(self, memo):
        # 工具及子代理复制上下文时必须共享一次性初始化，不能复制锁或重新同步远端目录。
        return self

    def _validate_scope(self, runtime) -> None:
        context = runtime.context or {}
        if context.get("thread_id") != self._thread_id or resolve_runtime_user_id(runtime) != self._user_id:
            raise SandboxRuntimeError("延迟沙盒初始化不能跨用户或线程使用")

    def _complete(self) -> None:
        self._sandbox_id = self._context.get("sandbox_id")
        self.completed = True

    def _publish(self, runtime) -> None:
        if self._sandbox_id is not None:
            runtime.state["sandbox"] = {"sandbox_id": self._sandbox_id}

    def publish_if_completed(self, runtime) -> bool:
        self._validate_scope(runtime)
        if not self.completed:
            return False
        self._publish(runtime)
        return True

    def ensure(self, runtime) -> None:
        if self.publish_if_completed(runtime):
            return
        with _INITIALIZATION_LOCKS.hold(self):
            if not self.completed:
                with public_skill_preset_scope(self._preset):
                    self._initialize()
                self._complete()
            self._publish(runtime)

    async def ensure_async(self, runtime) -> None:
        if self.publish_if_completed(runtime):
            return
        async with _INITIALIZATION_LOCKS.hold_async(self):
            if not self.completed:
                with public_skill_preset_scope(self._preset):
                    await self._initialize_async()
                self._complete()
            self._publish(runtime)


def get_lazy_sandbox_initialization(context) -> LazySandboxInitialization | None:
    initialization = (context or {}).get(LAZY_SANDBOX_CONTEXT_KEY)
    return initialization if isinstance(initialization, LazySandboxInitialization) else None


def inherited_sandbox_initialization() -> LazySandboxInitialization | None:
    return _INHERITED_INITIALIZATION.get()


@contextmanager
def sandbox_initialization_scope(context) -> Iterator[None]:
    """仅在工具执行片段传播；子代理复制上下文后可先于主代理首次访问沙盒。"""
    token = _INHERITED_INITIALIZATION.set(get_lazy_sandbox_initialization(context))
    try:
        yield
    finally:
        _INHERITED_INITIALIZATION.reset(token)
