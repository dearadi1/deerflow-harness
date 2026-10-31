"""根据应用配置选择主代理提示词，统一 Gateway 与嵌入式客户端行为。"""

from typing import Any

from deerflow.config.app_config import AppConfig, get_app_config


def apply_prompt_template(*args: Any, app_config: AppConfig | None = None, **kwargs: Any) -> str:
    resolved_config = app_config if app_config is not None else get_app_config()
    if resolved_config.lead_prompt_language == "zh":
        from deerflow.agents.lead_agent.prompt_zh import apply_prompt_template as render_prompt
    else:
        from deerflow.agents.lead_agent.prompt import apply_prompt_template as render_prompt

    return render_prompt(*args, app_config=resolved_config, **kwargs)
