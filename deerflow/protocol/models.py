"""客户端流协议的公共模型；只依赖 Pydantic 和标准库。"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, localcontext
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter, model_validator

StreamEventType = Literal["reasoning_content", "answer_content", "title", "suggestions", "tool_chunk", "tool_calls", "tool_result", "token_usage", "context_compaction_start", "context_compaction_end", "context_used", "end"]
Identifier = Annotated[str, Field(min_length=1)]


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


def _money_value(value: Any) -> Decimal:
    """允许 JSON 十进制字符串，避免跨语言接收时引入二进制浮点误差。"""
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("金额必须是有限十进制数") from error


Money = Annotated[Decimal, BeforeValidator(_money_value), Field(ge=0, allow_inf_nan=False)]


class ModelPrice(ProtocolModel):
    """人民币单价，单位为元/百万 Token。"""

    price_input_normal: Money
    price_output_normal: Money
    price_input_cache_hit: Money


class TokenCost(ProtocolModel):
    currency: Literal["CNY"] = "CNY"
    status: Literal["priced", "unpriced"]
    reason: str | None = None
    price: ModelPrice | None = None
    input_normal_cost: Money | None = None
    input_cache_hit_cost: Money | None = None
    output_cost: Money | None = None
    total_cost: Money | None = None

    @model_validator(mode="after")
    def validate_cost_status(self) -> TokenCost:
        amounts = (self.input_normal_cost, self.input_cache_hit_cost, self.output_cost, self.total_cost)
        if self.status == "priced":
            if any(value is None for value in amounts):
                raise ValueError("已计价费用必须包含全部分项和总额")
            with localcontext() as context:
                context.prec = 50
                if sum(amounts[:3], Decimal(0)) != self.total_cost:
                    raise ValueError("费用总额必须等于各分项之和")
        elif not self.reason or any(value is not None for value in amounts):
            raise ValueError("无法计价时必须说明原因，金额应为空")
        return self


class MessageMetadata(BaseModel):
    """供应商及中间件的扩展元数据保持开放，避免协议升级丢失原始信息。"""

    model_config = ConfigDict(extra="allow", strict=True)


class InputTokenDetails(ProtocolModel):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, int | None] = Field(init=False)

    cache_read: int | None = None
    cache_creation: int | None = None
    audio: int | None = None
    image: int | None = None
    text: int | None = None


class OutputTokenDetails(ProtocolModel):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, int | None] = Field(init=False)

    reasoning: int | None = None
    audio: int | None = None
    image: int | None = None
    text: int | None = None


class TokenUsage(ProtocolModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_token_details: InputTokenDetails | None = None
    output_token_details: OutputTokenDetails | None = None


class ContentData(ProtocolModel):
    id: Identifier
    content: str
    additional_kwargs: MessageMetadata | None = None


class TitleData(ProtocolModel):
    id: Identifier
    content: str


class SuggestionsData(ProtocolModel):
    id: Identifier
    questions: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(min_length=1, max_length=5)]

    @model_validator(mode="after")
    def validate_questions(self) -> SuggestionsData:
        normalized = [question.strip() for question in self.questions]
        if any(not question for question in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("推荐追问必须非空且互不重复")
        return self


class ToolFunctionChunk(ProtocolModel):
    name: str
    arguments: str

    @model_validator(mode="after")
    def validate_fragment(self) -> ToolFunctionChunk:
        if self.name and self.arguments:
            raise ValueError("工具分片不能同时包含名称和参数")
        return self


class ToolFunction(ProtocolModel):
    name: Identifier
    arguments: str

    @model_validator(mode="after")
    def validate_arguments(self) -> ToolFunction:
        try:
            json.loads(self.arguments)
        except ValueError as error:
            raise ValueError("完整工具参数必须是可解析的 JSON 字符串") from error
        return self


class ToolChunkData(ProtocolModel):
    index: Annotated[int, Field(ge=0)]
    id: Identifier
    tool_call_id: Identifier
    type: Literal["function"] = "function"
    function: ToolFunctionChunk


class ToolCallData(ProtocolModel):
    index: Annotated[int, Field(ge=0)]
    id: Identifier
    tool_call_id: Identifier
    type: Literal["function"] = "function"
    function: ToolFunction


class PresentedFileResult(ProtocolModel):
    """present_files 工具对单个文件的处理结果。"""

    path: str
    msg: str


class ToolResultData(ProtocolModel):
    id: Identifier
    content: str | list[PresentedFileResult] | list[str | dict[str, Any]]
    tool_call_id: Identifier
    name: str | None = None
    status: str = "success"
    artifact: Any = None
    additional_kwargs: MessageMetadata | None = None


class TokenUsageData(ProtocolModel):
    id: Identifier
    usage_metadata: TokenUsage
    model_name: str | None = None
    cost: TokenCost | None = None


class EndData(TokenUsageData):
    status: str | None = None


class ContextCompactionStartData(ProtocolModel):
    id: Identifier
    input_tokens: Annotated[int, Field(ge=0)]
    compacted_message_count: Annotated[int, Field(ge=0)]
    kept_message_count: Annotated[int, Field(ge=0)]


class ContextCompactionEndData(ProtocolModel):
    id: Identifier
    status: Literal["completed", "failed", "skipped"]
    reason: str | None = None
    summary_text: str | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    compacted_message_count: Annotated[int, Field(ge=0)] = 0
    kept_message_count: Annotated[int, Field(ge=0)] = 0
    llm_call_skipped: bool = False

    @model_validator(mode="after")
    def validate_outcome(self) -> ContextCompactionEndData:
        if self.status == "completed":
            if not self.summary_text:
                raise ValueError("压缩成功必须包含摘要")
        elif not self.reason or self.summary_text is not None:
            raise ValueError("压缩失败或跳过必须说明原因，且不能携带未生效的摘要")
        return self


class ContextItem(ProtocolModel):
    source: Literal["memory", "project", "documents", "durable_context"]
    content: Annotated[str, Field(min_length=1)]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ContextUsedData(ProtocolModel):
    id: Identifier
    contexts: Annotated[list[ContextItem], Field(min_length=1)]


Payload = TypeVar("Payload")


class StreamEvent(ProtocolModel, Generic[Payload]):
    """所有流事件的公共封装；按 type 解析时使用具体事件或联合适配器。"""

    type: StreamEventType
    agent_id: Identifier
    data: Payload
    thread_id: Identifier
    seq: Annotated[int, Field(ge=1)]


class ReasoningContentEvent(StreamEvent[ContentData]):
    type: Literal["reasoning_content"] = "reasoning_content"


class AnswerContentEvent(StreamEvent[ContentData]):
    type: Literal["answer_content"] = "answer_content"


class TitleEvent(StreamEvent[TitleData]):
    type: Literal["title"] = "title"


class SuggestionsEvent(StreamEvent[SuggestionsData]):
    type: Literal["suggestions"] = "suggestions"


class ToolChunkEvent(StreamEvent[list[ToolChunkData]]):
    type: Literal["tool_chunk"] = "tool_chunk"


class ToolCallsEvent(StreamEvent[list[ToolCallData]]):
    type: Literal["tool_calls"] = "tool_calls"


class ToolResultEvent(StreamEvent[ToolResultData]):
    type: Literal["tool_result"] = "tool_result"


class TokenUsageEvent(StreamEvent[TokenUsageData]):
    type: Literal["token_usage"] = "token_usage"


class EndEvent(StreamEvent[EndData]):
    type: Literal["end"] = "end"


class ContextCompactionStartEvent(StreamEvent[ContextCompactionStartData]):
    type: Literal["context_compaction_start"] = "context_compaction_start"


class ContextCompactionEndEvent(StreamEvent[ContextCompactionEndData]):
    type: Literal["context_compaction_end"] = "context_compaction_end"


class ContextUsedEvent(StreamEvent[ContextUsedData]):
    type: Literal["context_used"] = "context_used"


ClientStreamEvent = Annotated[
    ReasoningContentEvent | AnswerContentEvent | TitleEvent | SuggestionsEvent | ToolChunkEvent | ToolCallsEvent | ToolResultEvent | TokenUsageEvent | ContextCompactionStartEvent | ContextCompactionEndEvent | ContextUsedEvent | EndEvent,
    Field(discriminator="type"),
]
STREAM_EVENT_ADAPTER = TypeAdapter(ClientStreamEvent)


def parse_stream_event(value: Any) -> ClientStreamEvent:
    """从模型、字典或 JSON 恢复具体事件类型，供下游在统一入口识别协议。"""
    if isinstance(value, (str, bytes, bytearray)):
        return STREAM_EVENT_ADAPTER.validate_json(value)
    return STREAM_EVENT_ADAPTER.validate_python(value)


def stream_event_json_schema() -> dict[str, Any]:
    return STREAM_EVENT_ADAPTER.json_schema()
