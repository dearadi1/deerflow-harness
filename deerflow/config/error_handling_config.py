"""回答错误提示只控制展示，不改变中间件的重试或停止策略。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_ERROR_MESSAGE = "当前访问繁忙，您可稍后重试，或切换其他模型"


class ErrorHandlingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fallback_message: str = Field(default=DEFAULT_ERROR_MESSAGE, strict=True, description="answer_content 中统一展示的错误提示，真实详情保留在 additional_kwargs.error")

    @field_validator("fallback_message")
    @classmethod
    def validate_fallback_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("错误提示不能为空或仅包含空白字符")
        return value
