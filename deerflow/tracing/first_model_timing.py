"""分开记录图内准备与首次模型响应，不采集提示词、凭据或生成内容。"""

import logging
import time
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig

from deerflow.client_stream import _content_parts

logger = logging.getLogger(__name__)


class FirstModelTimingCallback(BaseCallbackHandler):
    # 回调只记两个时间点，避免每个流分片都投递一次执行器任务。
    run_inline = True
    ignore_chain = True
    ignore_agent = True
    ignore_retriever = True

    def __init__(self) -> None:
        self._graph_started_at = time.monotonic()
        self._model_started_at: float | None = None
        self._model_run_id: UUID | None = None
        self._response_logged = False

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs) -> None:
        if self._model_run_id is not None or (metadata or {}).get("langgraph_node") != "model":
            return
        self._model_run_id = run_id
        self._model_started_at = time.monotonic()
        logger.info("Client first model started: graph_to_model_ms=%.1f", (self._model_started_at - self._graph_started_at) * 1000)

    def _record_response(self, run_id: UUID, kind: str) -> None:
        if self._response_logged or run_id != self._model_run_id or self._model_started_at is None:
            return
        self._response_logged = True
        logger.info("Client first model response: model_to_response_ms=%.1f kind=%s", (time.monotonic() - self._model_started_at) * 1000, kind)

    def on_llm_new_token(self, token, *, run_id, chunk=None, **kwargs) -> None:
        if self._response_logged or run_id != self._model_run_id:
            return
        message = getattr(chunk, "message", None)
        # 与客户端正文／思考规则一致，供应商的空内容块不能提前结束计时。
        has_content = message is not None and any(_content_parts(message).values())
        if token or has_content or getattr(message, "tool_call_chunks", None):
            self._record_response(run_id, "chunk")

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        # 非流式模型只能观测完整响应，明确标注，不能当作模型首 token。
        self._record_response(run_id, "complete")


def with_first_model_timing(config: RunnableConfig) -> RunnableConfig:
    if not logger.isEnabledFor(logging.INFO):
        return config
    from langchain_core.callbacks.manager import BaseCallbackManager

    callbacks = config.get("callbacks")
    if isinstance(callbacks, BaseCallbackManager):
        callbacks = callbacks.copy()
        callbacks.add_handler(FirstModelTimingCallback(), inherit=True)
    else:
        callbacks = [*(callbacks or []), FirstModelTimingCallback()]
    return {**config, "callbacks": callbacks}
