"""发布上下文观测事件；与压缩状态和后台记忆写入保持独立。"""

from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langgraph.errors import GraphBubbleUp

from deerflow.utils.custom_events import aemit_custom_event, emit_custom_event

logger = logging.getLogger(__name__)


def emit_context_event(runtime: Any, payload: dict | None) -> None:
    writer = getattr(runtime, "stream_writer", None)
    if payload is None or not callable(writer):
        return
    try:
        emit_custom_event(payload, writer=writer)
    except GraphBubbleUp:
        raise
    except Exception:
        logger.debug("上下文事件发送失败，不影响模型执行", exc_info=True)


async def aemit_context_event(runtime: Any, payload: dict | None) -> None:
    writer = getattr(runtime, "stream_writer", None)
    if payload is None or not callable(writer):
        return
    try:
        await aemit_custom_event(payload, writer=writer)
    except GraphBubbleUp:
        raise
    except Exception:
        logger.debug("上下文事件发送失败，不影响模型执行", exc_info=True)


def context_used_payload(**sources: str | None) -> dict | None:
    contexts = [
        {"source": source, "content": content, "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
        for source, content in sources.items()
        if isinstance(content, str) and content.strip()
    ]
    if not contexts:
        return None
    return {"type": "context_used", "data": {"id": f"context_{uuid.uuid4().hex}", "contexts": contexts}}


@dataclass
class CompactionObservation:
    id: str = field(default_factory=lambda: f"compaction_{uuid.uuid4().hex}")
    status: str = "failed"
    reason: str | None = "summary_generation_failed"
    counts: dict = field(default_factory=dict)
    result: Any = None
    llm_call_skipped: bool = False

    def start_payload(self, prepared: tuple) -> dict:
        messages, preserved, _, tokens = prepared
        self.counts = {"input_tokens": tokens, "compacted_message_count": len(messages), "kept_message_count": len(preserved)}
        return {"type": "context_compaction_start", "data": {"id": self.id, **self.counts}}

    def end_payload(self) -> dict | None:
        if self.reason == "not_triggered" and self.result is None:
            return None
        data = {"id": self.id, "status": self.status, "reason": self.reason, **self.counts}
        if self.result is not None:
            data.update(status="completed", reason=None, summary_text=self.result.summary_text, llm_call_skipped=self.llm_call_skipped)
        return {"type": "context_compaction_end", "data": data}


# 中间件可被缓存并用于并发请求，观测状态必须按调用隔离。
_compaction_observation: ContextVar[CompactionObservation | None] = ContextVar("compaction_observation", default=None)


def skip_compaction(reason: str) -> None:
    observation = _compaction_observation.get()
    if observation is not None:
        observation.status, observation.reason = "skipped", reason


def mark_reused_summary(reused: bool) -> None:
    observation = _compaction_observation.get()
    if observation is not None:
        observation.llm_call_skipped = reused


@contextmanager
def observe_compaction(runtime: Any):
    observation = CompactionObservation()
    token = _compaction_observation.set(observation)
    try:
        yield observation
    except BaseException as error:
        observation.status, observation.reason = "failed", type(error).__name__
        raise
    finally:
        _compaction_observation.reset(token)
        emit_context_event(runtime, observation.end_payload())


@asynccontextmanager
async def aobserve_compaction(runtime: Any):
    observation = CompactionObservation()
    token = _compaction_observation.set(observation)
    try:
        yield observation
    except BaseException as error:
        observation.status, observation.reason = "failed", type(error).__name__
        raise
    finally:
        _compaction_observation.reset(token)
        await aemit_context_event(runtime, observation.end_payload())
