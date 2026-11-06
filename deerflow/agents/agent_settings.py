"""将期望的执行档位收敛为模型真正接受的参数。"""

from deerflow.models.reasoning import resolve_reasoning_contract, resolve_reasoning_request
from deerflow.protocol import AgentSetting


def compatible_agent_setting(model_config, *, thinking_enabled: bool, is_plan_mode: bool, subagent_enabled: bool) -> AgentSetting:
    contract = resolve_reasoning_contract(model_config)
    requested_thinking = thinking_enabled or is_plan_mode or subagent_enabled or contract.thinking_required
    resolved = resolve_reasoning_request(contract, thinking_enabled=requested_thinking, reasoning_effort=None)
    # 思考不受支持时整体降档，不能通过仅修改回传字段伪造递进关系。
    return AgentSetting(
        model=model_config.name,
        thinking_enabled=resolved.thinking_enabled,
        is_plan_mode=resolved.thinking_enabled and (is_plan_mode or subagent_enabled),
        subagent_enabled=resolved.thinking_enabled and subagent_enabled,
    )


def setting_overrides(setting: AgentSetting) -> dict:
    return {
        "model_name": setting.model,
        "thinking_enabled": setting.thinking_enabled,
        "is_plan_mode": setting.is_plan_mode,
        "subagent_enabled": setting.subagent_enabled,
    }


def setting_mode(setting: AgentSetting) -> str:
    if setting.subagent_enabled:
        return "subagent"
    if setting.is_plan_mode:
        return "plan"
    return "thinking" if setting.thinking_enabled else "fast"
