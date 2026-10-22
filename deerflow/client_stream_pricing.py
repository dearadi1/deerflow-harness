"""客户端每轮价格快照及费用计算，协议模型不依赖此运行时实现。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, localcontext
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from deerflow_protocol import ModelPrice, TokenCost

from deerflow.config.model_config import ModelConfig

_COST_FIELDS = ("input_normal_cost", "input_cache_hit_cost", "output_cost", "total_cost")


def exclude_attributed_child_usage(messages: Sequence[Any]) -> list[Any]:
    """最终快照可能已将子任务写回父消息；只在客户端视图中扣除，避免跨模型重复收费。"""
    dispatches = {}
    corrected = list(messages)
    seen_calls = set()
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            dispatches.update({call["id"]: index for call in message.tool_calls if call.get("id")})
        elif isinstance(message, ToolMessage) and message.additional_kwargs.get("subagent_token_usage_attributed") is True:
            usage = message.additional_kwargs.get("subagent_token_usage")
            dispatch = dispatches.get(message.tool_call_id)
            if dispatch is None or (dispatch, message.tool_call_id) in seen_calls or not isinstance(usage, Mapping):
                continue
            parent = corrected[dispatch]
            own = dict(parent.usage_metadata or {})
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                own[field] = own.get(field, 0) - usage.get(field, 0)
            corrected[dispatch] = parent.model_copy(update={"usage_metadata": own})
            seen_calls.add((dispatch, message.tool_call_id))
    return corrected


def unpriced_cost(reason: str, price: ModelPrice | None = None) -> TokenCost:
    return TokenCost(currency="CNY", status="unpriced", reason=reason, price=price, input_normal_cost=None, input_cache_hit_cost=None, output_cost=None, total_cost=None)


def calculate_cost(usage: Mapping[str, Any], price: ModelPrice | None) -> TokenCost:
    if price is None:
        return unpriced_cost("missing_price")
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (input_tokens, output_tokens)):
        return unpriced_cost("invalid_usage", price)
    details = usage.get("input_token_details") or {}
    cache_read = details.get("cache_read") if isinstance(details, Mapping) else None
    if cache_read is None:
        # 单价相同或没有输入时，缓存未知不影响金额；否则不能把缺失信息伪装成零命中。
        if input_tokens > 0 and price.price_input_normal != price.price_input_cache_hit:
            return unpriced_cost("missing_cache_usage", price)
        cache_read = 0
    if isinstance(cache_read, bool) or not isinstance(cache_read, int) or not 0 <= cache_read <= input_tokens:
        return unpriced_cost("invalid_cache_usage", price)
    with localcontext() as context:
        context.prec = 50
        normal_cost = Decimal(input_tokens - cache_read) * price.price_input_normal / 1_000_000
        cache_cost = Decimal(cache_read) * price.price_input_cache_hit / 1_000_000
        output_cost = Decimal(output_tokens) * price.price_output_normal / 1_000_000
        return TokenCost(currency="CNY", status="priced", price=price, input_normal_cost=normal_cost, input_cache_hit_cost=cache_cost, output_cost=output_cost, total_cost=normal_cost + cache_cost + output_cost)


def sum_costs(costs: Iterable[TokenCost]) -> TokenCost:
    costs = list(costs)
    if any(cost.status != "priced" for cost in costs):
        return unpriced_cost("incomplete_pricing")
    with localcontext() as context:
        context.prec = 50
        totals = {field: sum((getattr(cost, field) for cost in costs), Decimal(0)) for field in _COST_FIELDS}
    return TokenCost(currency="CNY", status="priced", **totals)


class ClientStreamPricing:
    def __init__(self, models: Iterable[ModelConfig] = (), *, default_model: str | None = None):
        self._profiles = {model.name: (model.model, ModelPrice.model_validate(model.price.model_dump()) if model.price is not None else None) for model in models}
        self.default_model = default_model or next(iter(self._profiles), None)

    def resolve_model(self, identity: str | None, *, preferred: str | None = None) -> str | None:
        if not identity:
            return preferred
        if identity in self._profiles:
            return identity
        matches = [name for name, (provider_model, _) in self._profiles.items() if provider_model == identity]
        if preferred in matches:
            return preferred
        # 同一个服务商模型可能在多个端点具有不同单价；歧义时不擅自选价。
        return matches[0] if len(matches) == 1 else identity

    def cost(self, model_name: str | None, usage: Mapping[str, Any]) -> TokenCost:
        if not model_name:
            return unpriced_cost("missing_model")
        profile = self._profiles.get(model_name)
        return calculate_cost(usage, profile[1] if profile is not None else None)

    def message_model(self, metadata: Mapping, response: Mapping, *, preferred: str | None) -> str | None:
        configured = metadata.get("model_name")
        if configured:
            return self.resolve_model(configured, preferred=preferred)
        identities = [value for value in (response.get("model_name"), response.get("model"), metadata.get("ls_model_name")) if value]
        for identity in identities:
            resolved = self.resolve_model(identity, preferred=preferred)
            if resolved in self._profiles:
                return resolved
        # 返回值可能是服务商的规范名，和请求中的部署别名不同；能识别请求模型时以其计价。
        return identities[0] if identities else preferred
