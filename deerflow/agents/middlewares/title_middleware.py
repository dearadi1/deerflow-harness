"""Middleware for automatic thread title generation."""

import logging
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NotRequired, override
from unicodedata import category

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config, get_stream_writer
from langgraph.constants import TAG_NOSTREAM
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder
from deerflow.agents.middlewares.pii_redaction_middleware import redact_texts
from deerflow.config.title_config import get_title_config
from deerflow.models import create_chat_model
from deerflow.utils.custom_events import StreamWriter, aemit_custom_event, emit_custom_event
from deerflow.utils.messages import ORIGINAL_USER_CONTENT_KEY, get_original_user_content_text

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage

    from deerflow.config.app_config import AppConfig
    from deerflow.config.title_config import TitleConfig

logger = logging.getLogger(__name__)


class TitleMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""

    title: NotRequired[str | None]
    uploaded_files: NotRequired[list[dict] | None]


class TitleMiddleware(AgentMiddleware[TitleMiddlewareState]):
    """Automatically generate a title for the thread after the first user message."""

    state_schema = TitleMiddlewareState

    def __init__(
        self,
        *,
        app_config: "AppConfig | None" = None,
        title_config: "TitleConfig | None" = None,
        extensions=None,
    ):
        super().__init__()
        self._app_config = app_config
        self._title_config = title_config
        if extensions is None:
            from deerflow.extensions import get_agent_build_extensions

            extensions = get_agent_build_extensions()
        self._extensions = extensions

    def _get_title_config(self):
        if self._title_config is not None:
            return self._title_config
        if self._app_config is not None:
            return self._app_config.title
        return get_title_config()

    def _normalize_content(self, content: object) -> str:
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts = [self._normalize_content(item) for item in content]
            return "\n".join(part for part in parts if part)

        if isinstance(content, dict):
            text_value = content.get("text")
            if isinstance(text_value, str):
                return text_value

            nested_content = content.get("content")
            if nested_content is not None:
                return self._normalize_content(nested_content)

        return ""

    @staticmethod
    def _message_type(message: object) -> str | None:
        message_type = getattr(message, "type", None)
        if message_type is None and isinstance(message, dict):
            message_type = message.get("type") or message.get("role")
        if message_type == "user":
            return "human"
        if message_type == "assistant":
            return "ai"
        return message_type if isinstance(message_type, str) else None

    @staticmethod
    def _message_content(message: object) -> object:
        if isinstance(message, dict):
            return message.get("content", "")
        return getattr(message, "content", "")

    @staticmethod
    def _is_dynamic_context_reminder_message(message: object) -> bool:
        if is_dynamic_context_reminder(message):
            return True
        if isinstance(message, dict):
            additional_kwargs = message.get("additional_kwargs")
            return isinstance(additional_kwargs, dict) and bool(additional_kwargs.get("dynamic_context_reminder"))
        return False

    @staticmethod
    def _is_user_message_for_title(message: object) -> bool:
        return TitleMiddleware._message_type(message) == "human" and not TitleMiddleware._is_dynamic_context_reminder_message(message)

    def _get_title_user_message(self, state: TitleMiddlewareState) -> str:
        messages = state.get("messages") or []
        user_message = next((m for m in messages if self._is_user_message_for_title(m)), None)
        if user_message is None:
            return ""
        if isinstance(user_message, dict):
            additional_kwargs = user_message.get("additional_kwargs")
        else:
            additional_kwargs = getattr(user_message, "additional_kwargs", None)
        if isinstance(additional_kwargs, Mapping) and isinstance(additional_kwargs.get(ORIGINAL_USER_CONTENT_KEY), str):
            user_msg_content = get_original_user_content_text(self._message_content(user_message), additional_kwargs)
        else:
            # Keep TitleMiddleware's richer normalization for ordinary structured content.
            user_msg_content = self._message_content(user_message)
        return self._normalize_content(user_msg_content)

    @staticmethod
    def _clean_attachment_filename(filename: object) -> str | None:
        """Return a safe, readable upload filename for use as a thread title."""
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            return None

        # File names enter the title as display text, never as a URL. Preserve
        # readable Unicode and punctuation while preventing control characters
        # from changing the thread-list layout.
        cleaned = "".join(" " if category(char).startswith("C") else char for char in filename)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or None

    def _attachment_only_title(self, state: TitleMiddlewareState) -> str | None:
        """Return a local title for a first turn containing attachments only."""
        if self._get_title_user_message(state).strip():
            return None

        files = state.get("uploaded_files")
        if not isinstance(files, list):
            return None

        filenames = []
        seen_attachment_ids: set[str] = set()
        for file in files:
            if not isinstance(file, Mapping):
                continue
            filename = file.get("filename")
            if not isinstance(filename, str):
                continue
            cleaned = self._clean_attachment_filename(filename)
            if cleaned is None:
                continue
            # UploadsMiddleware builds the path from a verified basename.
            # Deduplicate that stable attachment identity before display-name
            # cleanup: distinct names can intentionally normalize alike.
            attachment_id = file.get("path")
            if not isinstance(attachment_id, str) or not attachment_id:
                attachment_id = filename
            if attachment_id in seen_attachment_ids:
                continue
            seen_attachment_ids.add(attachment_id)
            filenames.append(cleaned)
        if len(filenames) == 1:
            return self._truncate_attachment_filename(filenames[0])
        if len(filenames) > 1:
            return self._attachment_count_title(len(filenames))
        return None

    def _should_generate_title(self, state: TitleMiddlewareState, *, allow_partial_exchange: bool = False) -> bool:
        """Check if we should generate a title for this thread."""
        config = self._get_title_config()
        if not config.enabled:
            return False

        # Check if thread already has a title in state
        if state.get("title"):
            return False

        # Check if this is the first turn (has at least one user message and one assistant response).
        # Defensively coerce a None ``messages`` channel (possible when reading a
        # partially-initialized checkpoint) into an empty list so ``len()`` is safe.
        messages = state.get("messages") or []
        min_messages = 1 if allow_partial_exchange else 2
        if len(messages) < min_messages:
            return False

        # Count user and assistant messages
        user_messages = [m for m in messages if self._is_user_message_for_title(m)]
        assistant_messages = [m for m in messages if self._message_type(m) == "ai"]

        # Normal path: title only after first complete exchange. Interrupted path
        # (``allow_partial_exchange=True``) accepts a lone first-turn user message
        # so a fallback title can still be persisted when the run is cancelled
        # before any AI chunk reaches the checkpoint.
        return len(user_messages) == 1 and (len(assistant_messages) >= 1 or allow_partial_exchange)

    def _build_title_prompt(self, state: TitleMiddlewareState) -> tuple[str, str]:
        """Extract user/assistant messages and build the title prompt.

        Returns (prompt_string, user_msg) so callers can use user_msg as fallback.
        """
        config = self._get_title_config()
        messages = state.get("messages") or []

        assistant_msg_content = next((self._message_content(m) for m in messages if self._message_type(m) == "ai"), "")

        user_msg = self._get_title_user_message(state)
        assistant_msg = self._strip_think_tags(self._normalize_content(assistant_msg_content))

        # This model is invoked directly, outside the main model wrappers.
        # Redact complete fields before truncation can split an identifier.
        redacted_user, redacted_assistant = redact_texts([user_msg, assistant_msg], getattr(self._app_config, "pii_redaction", None))
        prompt = config.prompt_template.format(
            max_words=config.max_words,
            user_msg=redacted_user[:500],
            assistant_msg=redacted_assistant[:500],
        )
        return prompt, user_msg

    def _strip_think_tags(self, text: str) -> str:
        """Remove <think>...</think> blocks emitted by reasoning models (e.g. minimax, DeepSeek-R1)."""
        return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    def _parse_title(self, content: object) -> str:
        """Normalize model output into a clean title string."""
        config = self._get_title_config()
        title_content = self._normalize_content(content)
        title_content = self._strip_think_tags(title_content)
        title = title_content.strip().strip('"').strip("'")
        return title[: config.max_chars] if len(title) > config.max_chars else title

    def _fallback_title(self, user_msg: str) -> str:
        if not user_msg.strip():
            return "New Conversation"

        config = self._get_title_config()
        fallback_chars = min(config.max_chars, 50)
        if len(user_msg) > fallback_chars:
            # Reserve room for the ellipsis so this path honours ``max_chars``
            # exactly as ``_parse_title`` does on the model path.
            ellipsis = "..."
            body = min(fallback_chars, config.max_chars - len(ellipsis))
            return user_msg[:body].rstrip() + ellipsis
        return user_msg

    def _truncate_attachment_filename(self, filename: str) -> str:
        """Truncate a file-name title while retaining its extension when possible."""
        config = self._get_title_config()
        max_chars = config.max_chars
        if len(filename) <= max_chars:
            return filename

        ellipsis = "..."
        extension = Path(filename).suffix.lstrip(".")
        remaining = max_chars - len(ellipsis) - len(extension)
        if extension and remaining > 0:
            return filename[:remaining].rstrip() + ellipsis + extension
        return self._truncate_title(filename)

    def _attachment_count_title(self, count: int) -> str:
        """Return a bounded, readable title for multiple validated uploads."""
        config = self._get_title_config()
        for title in (f"{count} files uploaded", f"{count} files"):
            if len(title) <= config.max_chars:
                return title
        return self._truncate_title(str(count))

    def _truncate_title(self, title: str) -> str:
        """Bound a local attachment title without overriding title.max_chars."""
        max_chars = self._get_title_config().max_chars
        if len(title) <= max_chars:
            return title
        ellipsis = "..."
        return title[: max_chars - len(ellipsis)].rstrip() + ellipsis

    def _get_runnable_config(self) -> dict[str, Any]:
        """Inherit the parent RunnableConfig and add middleware tag.

        This ensures RunJournal identifies LLM calls from this middleware
        as ``middleware:title`` instead of ``lead_agent``.
        """
        try:
            parent = get_config()
        except Exception:
            parent = {}
        config = {**parent}
        config["run_name"] = "title_agent"
        config["tags"] = [
            *(config.get("tags") or []),
            "middleware:title",
            TAG_NOSTREAM,
        ]
        return config

    def _generate_title_result(self, state: TitleMiddlewareState, *, allow_partial_exchange: bool = False) -> dict | None:
        """Generate a local fallback title without blocking on an LLM call."""
        if not self._should_generate_title(state, allow_partial_exchange=allow_partial_exchange):
            return None

        attachment_title = self._attachment_only_title(state)
        if attachment_title is not None:
            return {"title": attachment_title}

        user_msg = self._get_title_user_message(state)
        return {"title": self._fallback_title(user_msg)}

    def _title_model_request(self, state: TitleMiddlewareState) -> tuple["BaseChatModel", str, str, dict[str, Any]]:
        """同步与异步入口使用同一模型配置，避免标题策略分叉。"""
        config = self._get_title_config()
        prompt, user_msg = self._build_title_prompt(state)
        logger.info("标题生成模型提示词输入:\n%s", prompt)
        # 图级回调已携带追踪处理器，模型侧再次绑定会重复生成调用记录。
        model_kwargs = {"thinking_enabled": False, "attach_tracing": False}
        if self._app_config is not None:
            model_kwargs["app_config"] = self._app_config
        model = create_chat_model(name=config.model_name, **model_kwargs)
        return model, prompt, user_msg, self._get_runnable_config()

    def _needs_title_model(self, state: TitleMiddlewareState) -> bool:
        return bool(
            self._get_title_config().model_name
            and self._get_title_user_message(state).strip()
            and self._attachment_only_title(state) is None
        )

    def _title_model_result(self, response: "AIMessage", user_msg: str) -> tuple[dict, dict]:
        """标题消息不进入对话历史；通过自定义事件保留其身份和真实用量。"""
        title = self._parse_title(response.content) or self._fallback_title(user_msg)
        payload = {
            "type": "title_generated",
            "id": response.id or f"title_{uuid.uuid4().hex}",
            "title": title,
            "model_name": self._get_title_config().model_name,
            "usage_metadata": response.usage_metadata,
        }
        return {"title": title}, payload

    @staticmethod
    def _title_stream_writer() -> StreamWriter:
        try:
            return get_stream_writer()
        except RuntimeError:
            # 直接调用中间件时没有图上下文，仍需正常返回标题。
            return lambda payload: None

    def _generate_model_title_result(self, state: TitleMiddlewareState, *, task_store=None) -> dict | None:
        fallback = self._generate_title_result(state)
        if fallback is None or not self._needs_title_model(state):
            return fallback
        try:
            from deerflow_extension_api import SystemOperationKind

            from deerflow.extensions.notify import observe_system_model_call_sync

            model, prompt, user_msg, invoke_config = self._title_model_request(state)
            response = observe_system_model_call_sync(
                self._extensions,
                SystemOperationKind.TITLE,
                messages=prompt,
                model_name=self._get_title_config().model_name,
                invoke_config=invoke_config,
                invoke=lambda: model.invoke(prompt, config=invoke_config),
                task_store=task_store,
            )
            result, payload = self._title_model_result(response, user_msg)
            emit_custom_event(payload, writer=self._title_stream_writer())
            return result
        except Exception:
            logger.warning("LLM 标题生成失败，回退到本地标题", exc_info=True)
            return fallback

    async def _agenerate_title_result(
        self,
        state: TitleMiddlewareState,
        *,
        task_store=None,
    ) -> dict | None:
        """Generate a configured LLM title asynchronously and fall back locally."""
        fallback = self._generate_title_result(state)
        if fallback is None or not self._needs_title_model(state):
            return fallback

        try:
            model, prompt, user_msg, invoke_config = self._title_model_request(state)

            from deerflow_extension_api import SystemOperationKind

            from deerflow.extensions.notify import observe_system_model_call

            response = await observe_system_model_call(
                self._extensions,
                SystemOperationKind.TITLE,
                messages=prompt,
                model_name=self._get_title_config().model_name,
                invoke_config=invoke_config,
                invoke=lambda: model.ainvoke(prompt, config=invoke_config),
                task_store=task_store,
            )
            result, payload = self._title_model_result(response, user_msg)
            await aemit_custom_event(payload, writer=self._title_stream_writer())
            return result
        except Exception:
            logger.warning("异步 LLM 标题生成失败，回退到本地标题", exc_info=True)
        return fallback

    @override
    def after_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        from deerflow_extension_api import task_store_from_runtime

        return self._generate_model_title_result(state, task_store=task_store_from_runtime(runtime))

    @override
    async def aafter_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        from deerflow_extension_api import task_store_from_runtime

        return await self._agenerate_title_result(
            state,
            task_store=task_store_from_runtime(runtime),
        )
