"""按轮自动路由的服务端开关、模型映射和分析预算。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from deerflow.config._boolean_guards import reject_boolean

RoutingMode = Literal["fast", "thinking", "plan", "subagent"]
ROUTING_MODES: tuple[RoutingMode, ...] = ("fast", "thinking", "plan", "subagent")


class RoutingModels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1)
    vision: str | None = Field(default=None, min_length=1)


class RoutingProfiles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fast: RoutingModels = Field(default_factory=RoutingModels)
    thinking: RoutingModels = Field(default_factory=RoutingModels)
    plan: RoutingModels = Field(default_factory=RoutingModels)
    subagent: RoutingModels = Field(default_factory=RoutingModels)


class RoutingContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recent_turns: int = Field(default=4, ge=1, le=12)
    input_token_budget: int = Field(default=4096, ge=1024, le=32768)
    expanded_input_token_budget: int = Field(default=8192, ge=1024, le=65536)
    summary_token_budget: int = Field(default=512, ge=64, le=2048)
    max_analysis_calls: int = Field(default=2, ge=1, le=2)

    @field_validator("recent_turns", "input_token_budget", "expanded_input_token_budget", "summary_token_budget", "max_analysis_calls", mode="before")
    @classmethod
    def reject_boolean_limits(cls, value: object, info: ValidationInfo) -> object:
        return reject_boolean(value, info, kind="an integer")

    @model_validator(mode="after")
    def validate_budgets(self):
        if self.expanded_input_token_budget < self.input_token_budget:
            raise ValueError("扩充预算不能小于首次分析预算")
        if self.summary_token_budget >= self.input_token_budget:
            raise ValueError("任务摘要预算必须小于分析输入预算")
        return self


class AutoRoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    model_name: str | None = Field(default=None, min_length=1, description="分析模型；留空使用第一个已配置模型")
    prompt: str | None = Field(default=None, description="替换内置分类指导语；输出结构和预算仍由框架约束")
    timeout_seconds: float = Field(default=8, gt=0, le=120, allow_inf_nan=False)
    max_output_tokens: int = Field(default=768, ge=128, le=4096)
    routes: RoutingProfiles = Field(default_factory=RoutingProfiles)
    context: RoutingContextConfig = Field(default_factory=RoutingContextConfig)
    fallback_mode: RoutingMode = "thinking"

    @field_validator("timeout_seconds", "max_output_tokens", mode="before")
    @classmethod
    def reject_boolean_limits(cls, value: object, info: ValidationInfo) -> object:
        return reject_boolean(value, info, kind="a number")
