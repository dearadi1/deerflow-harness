"""验证宿主注册、真实工具调用和逐轮参数透传，防止引用来源或查询范围串用。"""

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import create_model

from deerflow import callable as callbacks
from deerflow.client import DeerFlowClient
from deerflow.community.hours_knowledge.tools import knowledge_base_search_tool
from deerflow.tools.types import Runtime


def make_runtime(context):
    return Runtime(state={}, context=context, config={}, stream_writer=lambda value: None, tool_call_id="search_1", store=None)


def make_item(document_id="doc_1", section_number=0, **overrides):
    return {
        "document_id": document_id,
        "title": "同名文档",
        "section_number": section_number,
        "score": 0.016393442,
        "content": f"段落 {section_number}",
        "download_path": f"https://example.com/file?section_number={section_number}&document_id={document_id}",
        "external_link": "https://example.com/paper",
        **overrides,
    }


class HoursKnowledgeToolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(callbacks, "_knowledge_search", None))
        self.parameters = {"kb_ids": ["kb_1"], "dense_weight": 0.8, "sparse_weight": 1.2}
        self.runtime = make_runtime({"knowledge_search_kwargs": self.parameters})

    def invoke(self, **overrides):
        return knowledge_base_search_tool.invoke({
            "runtime": self.runtime, "description": "查找实验依据", "query": "聚合物合成条件", **overrides,
        })

    def test_registered_callback_receives_host_parameters_and_groups_by_document(self):
        items = [make_item(), make_item("doc_2"), make_item(section_number=2)]
        callback = Mock(return_value=items)
        callbacks.configure_knowledge_search(callback)
        original = deepcopy(items)
        result = self.invoke(top_k=8)
        callback.assert_called_once_with(**self.parameters, query="聚合物合成条件", top_k=8)
        self.assertEqual(result, [
            {"title": "同名文档", "sections": [
                {"score": item["score"], "content": item["content"], "url": item["download_path"]}
                for item in (items[0], items[2])
            ]},
            {"title": "同名文档", "sections": [
                {"score": items[1]["score"], "content": items[1]["content"], "url": items[1]["download_path"]},
            ]},
        ])
        self.assertEqual(items, original)
        self.assertNotIn("query", self.parameters)

    def test_async_callback_and_pydantic_results_preserve_url_fallbacks(self):
        result_model = create_model("SearchResultItem", **{key: (type(value), ...) for key, value in make_item().items()})
        items = [
            result_model(**make_item()),
            make_item(section_number=1, download_path=None),
            make_item(section_number=2, download_path="", external_link=None),
            {key: value for key, value in make_item(section_number=3).items() if key not in {"download_path", "external_link"}},
        ]
        callback = AsyncMock(return_value=items)
        callbacks.configure_knowledge_search(callback)
        result = asyncio.run(knowledge_base_search_tool.ainvoke({
            "runtime": self.runtime, "description": "查找依据", "query": "聚合条件",
        }))
        self.assertEqual([section["url"] for section in result[0]["sections"]], [
            make_item()["download_path"], "https://example.com/paper", "", "",
        ])
        callback.assert_awaited_once_with(**self.parameters, query="聚合条件", top_k=5)

    def test_empty_results_and_errors(self):
        self.assertTrue(self.invoke().startswith("Error: 未注册knowledge_search"))
        callbacks.configure_knowledge_search(Mock(return_value=[]))
        self.assertEqual(self.invoke(), [])
        callbacks.configure_knowledge_search(Mock(side_effect=RuntimeError("检索不可用")))
        self.assertEqual(self.invoke(), "Error: 检索不可用")
        callbacks.configure_knowledge_search(AsyncMock(side_effect=RuntimeError("异步检索不可用")))
        self.assertEqual(self.invoke(), "Error: 异步检索不可用")
        for response in (None, {}, [None], [{"title": "缺少来源"}]):
            with self.subTest(response=response):
                callbacks.configure_knowledge_search(Mock(return_value=response))
                self.assertTrue(self.invoke().startswith("Error: "))

    def test_invalid_inputs_do_not_call_backend(self):
        callback = Mock(return_value=[])
        callbacks.configure_knowledge_search(callback)
        for inputs in ({"description": " "}, {"query": " "}, {"top_k": 0}, {"top_k": 101}, {"top_k": "bad"}):
            with self.subTest(inputs=inputs):
                self.assertTrue(self.invoke(**inputs).startswith("Error: "))
        callback.assert_not_called()

    def test_model_schema_has_only_requested_inputs_in_order(self):
        schema = knowledge_base_search_tool.tool_call_schema.model_json_schema()
        self.assertEqual(list(schema["properties"]), ["description", "query", "top_k"])
        self.assertEqual(schema["properties"]["top_k"]["default"], 5)
        self.assertEqual(knowledge_base_search_tool.name, "knowledge_base_search")


class HoursKnowledgeStreamTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(callbacks, "_knowledge_search", Mock(return_value=[])))
        self.client = object.__new__(DeerFlowClient)
        self.prepare_client()
        self.stub_external_runtime()
        self.contexts = []

    def prepare_client(self):
        self.client._model_name = None
        self.client._thinking_enabled = False
        self.client._plan_mode = False
        self.client._subagent_enabled = False
        self.client._agent_name = None
        self.client._environment = None
        self.client._checkpointer = object()
        self.client._checkpoint_channel_mode = "full"
        self.client._ensure_agent = Mock()
        self.client._agent = SimpleNamespace(stream=Mock(side_effect=self.invoke_tool))

    def stub_external_runtime(self):
        for name, replacement in {
            "build_tracing_callbacks": Mock(return_value=[]),
            "inject_langfuse_metadata": Mock(),
            "ensure_checkpoint_mode_compatible": Mock(),
            "_stream_with_sandbox_lease_cleanup": lambda items, context: items,
        }.items():
            self.enterContext(patch(f"deerflow.client.{name}", replacement))
        self.enterContext(patch("deerflow.runtime.client_replay.ClientTurnReplay.prepare"))

    def invoke_tool(self, state, *, config, context, stream_mode):
        self.contexts.append(context)
        self.assertNotIn("knowledge_search_kwargs", state["messages"][0].additional_kwargs)
        result = knowledge_base_search_tool.invoke({
            "runtime": make_runtime(context), "description": "检索依据", "query": "合成条件",
        })
        self.assertEqual(result, [])
        return iter(())

    def test_stream_parameters_reach_callback_without_leaking_to_next_turn(self):
        parameters = {"kb_ids": ["kb_1"], "dense_weight": 0.8}
        list(self.client.stream("检索", thread_id="thread_1", knowledge_search_kwargs=parameters))
        callbacks._knowledge_search.assert_called_once_with(**parameters, query="合成条件", top_k=5)
        self.contexts[0]["knowledge_search_kwargs"]["kb_ids"].append("kb_2")
        self.assertEqual(parameters["kb_ids"], ["kb_1"])
        list(self.client.stream("再检索", thread_id="thread_1"))
        self.assertNotIn("knowledge_search_kwargs", self.contexts[1])
        callbacks._knowledge_search.assert_called_with(query="合成条件", top_k=5)


if __name__ == "__main__":
    unittest.main()
