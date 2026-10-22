from pydantic import BaseModel, Field, ValidationInfo, field_validator

from deerflow.config._boolean_guards import reject_boolean

DEFAULT_MAX_SUGGESTIONS = 3
MAX_SUGGESTIONS_LIMIT = 5


class SuggestionsConfig(BaseModel):
    """Configuration for automatic follow-up suggestions."""

    enabled: bool = Field(default=True, description="Whether to enable follow-up question suggestions at the end of an AI response")
    max_suggestions: int = Field(
        default=DEFAULT_MAX_SUGGESTIONS,
        ge=1,
        le=MAX_SUGGESTIONS_LIMIT,
        description="Maximum number of follow-up suggestions to generate.",
    )
    model_name: str | None = Field(default=None, description="追问模型配置名称；留空时使用本轮主智能体模型")
    max_chars: int = Field(default=100, ge=10, le=500, description="每条推荐追问的最大字符数")

    @field_validator("max_suggestions", "max_chars", mode="before")
    @classmethod
    def _reject_boolean_limits(cls, value: object, info: ValidationInfo) -> object:
        return reject_boolean(value, info, kind="an integer")
