from pydantic import BaseModel, ConfigDict, Field


class BuiltinToolsConfig(BaseModel):
    """控制可选内置工具，默认保留既有能力。"""

    list_uploaded_files: bool = Field(default=True, description="启用历史上传文件发现工具")
    review_skill_package: bool = Field(default=True, description="启用不激活技能的包审查工具")


class ToolGroupConfig(BaseModel):
    """Config section for a tool group"""

    name: str = Field(..., description="Unique name for the tool group")
    model_config = ConfigDict(extra="allow")


class ToolConfig(BaseModel):
    """Config section for a tool"""

    name: str = Field(..., description="Unique name for the tool")
    group: str = Field(..., description="Group name for the tool")
    use: str = Field(
        ...,
        description="Variable name of the tool provider(e.g. deerflow.sandbox.tools:bash_tool)",
    )
    model_config = ConfigDict(extra="allow")
