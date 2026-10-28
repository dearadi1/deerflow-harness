"""验证按 AI 消息计费、缓存扣除和跨模型汇总，避免费用看似完整但重复或漏算。"""

import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from pydantic import ValidationError

from deerflow.protocol import ModelPrice, TokenCost, parse_stream_event
from deerflow.agents.assembly_descriptor import _model_parameters
from deerflow.client import DeerFlowClient
from deerflow.client_stream import ClientStreamParser
from deerflow.client_stream_pricing import ClientStreamPricing, calculate_cost
from deerflow.config.model_config import ModelConfig, ModelPriceConfig
from deerflow.models.factory import create_chat_model


class StreamPricingTests(unittest.TestCase):
    @staticmethod
    def rates(multiplier=1):
        return {"price_input_normal": 12 * multiplier, "price_output_normal": 36 * multiplier, "price_input_cache_hit": Decimal("1.5") * multiplier}

    def model(self, name="main_model", *, provider=None, multiplier=1, price=True):
        return ModelConfig(name=name, model=provider or name, use="test.Model", price=self.rates(multiplier) if price else None)

    @staticmethod
    def usage(input_tokens=10085, output_tokens=174, cache_read=9016):
        return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens, "input_token_details": {"cache_read": cache_read}, "output_token_details": {"reasoning": 100}}

    def parser(self, *models, default_model=None):
        return ClientStreamParser("thread_001", "run_001", pricing=ClientStreamPricing(models or [self.model()], default_model=default_model))

    def test_actual_counts_and_cache_discount_are_charged_exactly_once(self):
        cost = calculate_cost(self.usage(), ModelPrice.model_validate(self.rates()))
        self.assertEqual(cost.status, "priced")
        self.assertEqual(cost.input_normal_cost, Decimal("0.012828"))
        self.assertEqual(cost.input_cache_hit_cost, Decimal("0.013524"))
        self.assertEqual(cost.output_cost, Decimal("0.006264"))
        self.assertEqual(cost.total_cost, Decimal("0.032616"))
        payload = cost.model_dump(mode="json", exclude_unset=True)
        self.assertEqual(payload["total_cost"], "0.032616")
        event = parse_stream_event({"type": "token_usage", "agent_id": "main", "thread_id": "thread_001", "seq": 1, "data": {"id": "m", "usage_metadata": self.usage(), "model_name": "main_model", "cost": payload}})
        self.assertIsInstance(event.data.cost, TokenCost)
        self.assertEqual(parse_stream_event(event.model_dump_json(exclude_unset=True)), event)
        self.assertEqual(event.data.cost.total_cost, cost.total_cost)

    def test_free_prices_and_single_token_precision_are_preserved(self):
        cost = calculate_cost(self.usage(1, 0, 1), ModelPrice.model_validate(self.rates()))
        self.assertEqual(cost.total_cost, Decimal("0.0000015"))
        free = ModelPrice(price_input_normal=0, price_output_normal=0, price_input_cache_hit=0)
        self.assertEqual(calculate_cost(self.usage(), free).total_cost, 0)
        self.assertEqual(calculate_cost({"input_tokens": 1, "output_tokens": 1}, free).status, "priced")

    def test_missing_price_and_cache_information_never_become_zero_cost(self):
        rates = ModelPrice.model_validate(self.rates())
        cases = [
            (self.usage(), None, "missing_price"),
            ({"input_tokens": 10, "output_tokens": 1}, rates, "missing_cache_usage"),
            (self.usage(1, 1, 2), rates, "invalid_cache_usage"),
            (self.usage(1, 1, -1), rates, "invalid_cache_usage"),
            (self.usage(-1, 1, 0), rates, "invalid_usage"),
        ]
        for usage, price, reason in cases:
            with self.subTest(reason=reason):
                cost = calculate_cost(usage, price)
                self.assertEqual(cost.status, "unpriced")
                self.assertEqual(cost.reason, reason)
                self.assertIsNone(cost.total_cost)

    def test_parallel_requests_share_one_cost_and_serial_messages_stay_separate(self):
        parser = self.parser()
        events = []
        for position in (1, 2):
            calls = [{"id": f"call_{position}_{index}", "name": "lookup", "args": {}} for index in (0, 1)]
            message = AIMessage(id=f"model_{position}", content="", tool_calls=calls, usage_metadata=self.usage())
            events.extend(parser.parse(("messages", (message, {}))))
            events.extend(parser.parse(("values", {"messages": [message]})))
            for call in calls:
                events.extend(parser.parse(("messages", (ToolMessage(id=f"result_{call['id']}", content="结果", tool_call_id=call["id"]), {}))))
        events.extend(parser.finish())
        usage = [event for event in events if event.type == "token_usage"]
        self.assertEqual([event.data.id for event in usage], ["model_1", "model_2"])
        self.assertTrue(all(event.data.cost.total_cost == Decimal("0.032616") for event in usage))
        self.assertEqual(events[-1].data.cost.total_cost, Decimal("0.065232"))

    def test_signed_chunks_are_priced_only_after_the_complete_usage_is_known(self):
        parser = self.parser()
        events = list(parser.parse(("messages", (AIMessageChunk(id="m", content="", usage_metadata=self.usage(100, 20, 80)), {}))))
        events += list(parser.parse(("messages", (AIMessageChunk(id="m", content="", usage_metadata=self.usage(-10, -2, -5)), {}))))
        self.assertFalse(any(event.type == "token_usage" for event in events))
        events += list(parser.parse(("values", {"messages": [AIMessage(id="m", content="", usage_metadata=self.usage(90, 18, 75))]})))
        actual = next(event for event in events if event.type == "token_usage")
        expected = calculate_cost(self.usage(90, 18, 75), ModelPrice.model_validate(self.rates()))
        self.assertEqual(actual.data.cost, expected)

    def test_children_with_different_models_sum_their_own_prices(self):
        parser = self.parser(self.model(), self.model("child_model", multiplier=2))
        events = list(parser.parse(("messages", (AIMessage(id="main", content="主", usage_metadata=self.usage()), {}))))
        child = AIMessage(id="child", content="子", usage_metadata=self.usage())
        events += list(parser.parse(("custom", {"type": "task_running", "task_id": "task_a", "model_name": "child_model", "message": child.model_dump(), "message_index": 1})))
        events += list(parser.parse(("custom", {"type": "task_completed", "task_id": "task_a", "model_name": "child_model", "usage": self.usage()})))
        events += list(parser.finish())
        usage = [event for event in events if event.type == "token_usage"]
        self.assertEqual([event.data.model_name for event in usage], ["main_model", "child_model"])
        self.assertEqual(usage[1].data.cost.total_cost, Decimal("0.065232"))
        self.assertEqual(events[-1].data.cost.total_cost, Decimal("0.097848"))
        self.assertEqual(next(event for event in events if event.type == "end" and event.agent_id == "task_a").data.cost.total_cost, Decimal("0.065232"))

    def test_final_snapshot_attribution_does_not_bill_child_usage_at_parent_prices(self):
        parser = self.parser(self.model(), self.model("child_model", multiplier=2))
        child = AIMessage(id="child", content="", usage_metadata=self.usage())
        events = list(parser.parse(("custom", {"type": "task_running", "task_id": "task_a", "model_name": "child_model", "message": child.model_dump(), "message_index": 1})))
        own = self.usage()
        attributed = {**own, **{field: own[field] * 2 for field in ("input_tokens", "output_tokens", "total_tokens")}}
        parent = AIMessage(id="parent", content="", tool_calls=[{"id": "task_a", "name": "task", "args": {}}], usage_metadata=attributed)
        result = ToolMessage(id="result", content="完成", tool_call_id="task_a", additional_kwargs={"subagent_status": "completed", "subagent_token_usage": self.usage(), "subagent_token_usage_attributed": True})
        events += list(parser.parse(("values", {"messages": [parent, result]})))
        events += list(parser.finish())
        usage = next(event for event in events if event.type == "token_usage" and event.agent_id == "main")
        self.assertEqual(usage.data.usage_metadata.model_dump(exclude_unset=True), own)
        self.assertEqual(usage.data.cost.total_cost, Decimal("0.032616"))
        self.assertEqual(events[-1].data.cost.total_cost, Decimal("0.097848"))
        self.assertEqual(parent.usage_metadata, attributed)

    def test_authoritative_child_totals_cannot_hide_unobserved_model_cost(self):
        parser = self.parser()
        events = list(parser.parse(("custom", {"type": "task_completed", "task_id": "task_a", "model_name": "main_model", "usage": self.usage()})))
        events += list(parser.finish())
        self.assertEqual(events[0].data.cost.reason, "incomplete_usage")
        self.assertEqual(events[-1].data.cost.status, "unpriced")
        self.assertIsNone(events[-1].data.cost.total_cost)

    def test_snapshot_model_fallback_does_not_override_observed_provider_model(self):
        parser = self.parser(self.model(), self.model("fallback", provider="vendor_fallback", multiplier=2))
        chunk = AIMessageChunk(id="m", content="", response_metadata={"model_name": "vendor_fallback"}, usage_metadata=self.usage())
        events = list(parser.parse(("messages", (chunk, {}))))
        events += list(parser.parse(("values", {"messages": [AIMessage(id="m", content="", usage_metadata=self.usage())]})))
        usage = next(event for event in events if event.type == "token_usage")
        self.assertEqual(usage.data.model_name, "fallback")
        self.assertEqual(usage.data.cost.total_cost, Decimal("0.065232"))

    def test_provider_canonical_name_does_not_hide_a_known_deployment_price(self):
        parser = self.parser(self.model("deployment", provider="ep_123"))
        message = AIMessage(id="m", content="", usage_metadata=self.usage(), response_metadata={"model_name": "vendor_canonical"})
        events = list(parser.parse(("messages", (message, {"ls_model_name": "ep_123"}))))
        self.assertEqual(events[0].data.model_name, "deployment")
        self.assertEqual(events[0].data.cost.total_cost, Decimal("0.032616"))

    def test_ambiguous_provider_alias_and_unknown_model_are_not_mispriced(self):
        pricing = ClientStreamPricing([self.model("a", provider="shared"), self.model("b", provider="shared", multiplier=2)])
        self.assertEqual(pricing.resolve_model("shared", preferred="b"), "b")
        self.assertEqual(pricing.cost(pricing.resolve_model("shared"), self.usage()).status, "unpriced")
        self.assertEqual(pricing.cost(pricing.resolve_model("unknown"), self.usage()).status, "unpriced")

    def test_config_price_is_validated_and_frozen_for_the_current_turn(self):
        raw = yaml.safe_load("name: main_model\nmodel: main_model\nuse: test.Model\nprice:\n  price_input_normal: 12.0\n  price_output_normal: 36.0\n  price_input_cache_hit: 1.5\n")
        model = ModelConfig.model_validate(raw)
        pricing = ClientStreamPricing([model])
        model.price = ModelPriceConfig(**self.rates(2))
        self.assertEqual(pricing.cost("main_model", self.usage()).total_cost, Decimal("0.032616"))
        for invalid in (True, -1, "NaN", "Infinity"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ModelPriceConfig(**{**self.rates(), "price_input_normal": invalid})

    def test_price_is_not_forwarded_to_provider_or_assembly_identity(self):
        captured = {}
        class RecordingModel(FakeListChatModel):
            def __init__(self, **kwargs):
                captured.update(kwargs)
                super().__init__(**kwargs)
        model = self.model()
        model.responses = ["回答"]
        app = SimpleNamespace(models=[model], get_model_config=lambda name: model)
        with patch("deerflow.models.factory.resolve_class", return_value=RecordingModel):
            create_chat_model("main_model", app_config=app, attach_tracing=False)
        self.assertNotIn("price", captured)
        self.assertNotIn("price", _model_parameters(model))

    def test_list_models_includes_exact_json_prices_and_preserves_zero(self):
        client = object.__new__(DeerFlowClient)
        model = self.model()
        model.price = ModelPriceConfig(price_input_normal="12.000000000000000001", price_output_normal="36.0", price_input_cache_hit=0)
        model.api_key = "test_placeholder"
        model.display_name = "模型展示名称"
        model.description = "模型介绍"
        model.supports_vision = True
        model.supports_thinking = True
        client._app_config = SimpleNamespace(models=[model], token_usage=SimpleNamespace(enabled=True))

        response = client.list_models()
        payload = json.loads(json.dumps(response))
        self.assertEqual(payload["models"][0]["price"], {"price_input_normal": "12.000000000000000001", "price_output_normal": "36.0", "price_input_cache_hit": "0"})
        expected_fields = {
            "name": model.name,
            "display_name": "模型展示名称",
            "description": "模型介绍",
            "supports_vision": True,
            "supports_thinking": True,
            "supports_reasoning_effort": False,
        }
        self.assertEqual({key: payload["models"][0][key] for key in expected_fields}, expected_fields)
        self.assertEqual(payload["token_usage"], {"enabled": True})
        self.assertNotIn("api_key", payload["models"][0])
        response["models"][0]["price"]["price_input_normal"] = "999"
        self.assertEqual(model.price.price_input_normal, Decimal("12.000000000000000001"))

    def test_list_models_without_price_returns_null(self):
        client = object.__new__(DeerFlowClient)
        client._app_config = SimpleNamespace(models=[self.model(price=False), SimpleNamespace(name="legacy_model")])

        payload = json.loads(json.dumps(client.list_models()))
        self.assertEqual([model["price"] for model in payload["models"]], [None, None])
        self.assertEqual([model["supports_vision"] for model in payload["models"]], [False, False])
        self.assertEqual(payload["token_usage"], {"enabled": False})

    def test_downstream_price_mutation_cannot_change_later_costs(self):
        parser = self.parser()
        events = list(parser.parse(("messages", (AIMessage(id="a", content="", usage_metadata=self.usage()), {}))))
        events[0].data.cost.price.price_input_normal = Decimal("999")
        later = list(parser.parse(("messages", (AIMessage(id="b", content="", usage_metadata=self.usage()), {}))))
        self.assertEqual(later[0].data.cost.total_cost, Decimal("0.032616"))
        self.assertEqual(list(parser.finish())[-1].data.cost.total_cost, Decimal("0.065232"))

    def test_invalid_priced_payloads_cannot_claim_a_complete_total(self):
        for payload in (
            {"status": "priced", "total_cost": "0"},
            {"status": "unpriced", "reason": "missing_price", "total_cost": "0"},
            {"status": "priced", "input_normal_cost": "1", "input_cache_hit_cost": "2", "output_cost": "3", "total_cost": "9"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                TokenCost.model_validate(payload)

    def test_public_client_passes_model_price_to_stream_parser(self):
        client = object.__new__(DeerFlowClient)
        for key, value in {"_model_name": "main_model", "_thinking_enabled": False, "_plan_mode": False, "_subagent_enabled": False, "_agent_name": None, "_environment": None, "_checkpoint_channel_mode": "full", "_checkpointer": object(), "_app_config": SimpleNamespace(models=[self.model()]), "_ensure_agent": Mock()}.items():
            setattr(client, key, value)
        def stream(state, **kwargs):
            message = AIMessage(id="m", content="回答", usage_metadata=self.usage())
            yield ("messages", (message, {}))
            yield ("values", {"messages": [*state["messages"], message]})
        client._agent = SimpleNamespace(stream=stream)
        replacements = {"build_tracing_callbacks": Mock(return_value=[]), "inject_langfuse_metadata": Mock(), "ensure_checkpoint_mode_compatible": Mock()}
        with patch.multiple("deerflow.client", **replacements), patch("deerflow.runtime.client_replay.ClientTurnReplay.prepare"), patch("deerflow.client._stream_with_sandbox_lease_cleanup", side_effect=lambda items, context: items):
            events = list(client.stream("问题", thread_id="thread_001", user_id="user_001"))
        event = next(event for event in events if event.type == "token_usage")
        self.assertEqual(event.data.cost.total_cost, Decimal("0.032616"))
        self.assertEqual(event.data.model_name, "main_model")
        self.assertEqual(events[-1].data.cost.total_cost, Decimal("0.032616"))
        self.assertEqual(parse_stream_event(event.model_dump_json()), event)


if __name__ == "__main__":
    unittest.main()
