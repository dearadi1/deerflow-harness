"""验证知识库结果经过实时和历史预算处理后仍保留接口及长度约束。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from deerflow.agents.middlewares.tool_output_budget_middleware import (
    ToolOutputBudgetMiddleware,
    _patch_model_messages,
    _patch_result,
    _patch_tool_message,
)
from deerflow.community.hours_knowledge.output_budget import budget_knowledge_base_search
from deerflow.community.hours_knowledge import callable as callbacks
from deerflow.community.hours_knowledge.tools import knowledge_base_search_tool
from deerflow.config.tool_output_config import ToolOutputConfig
from deerflow.tools.types import Runtime


def make_documents():
    return [
        {"title": "文档一", "sections": [
            {"score": 0.9, "content": "完整段落", "url": "https://example.com/1"},
            {"score": 0.8, "content": '中文\n"引用"\\路径😀' * 1000, "url": "https://example.com/2"},
        ]},
        {"title": "文档二", "sections": [
            {"score": 0.7, "content": "后续段落", "url": "https://example.com/3"},
        ]},
    ]


def make_message(name="knowledge_base_search", content=None):
    return ToolMessage(
        name=name, tool_call_id="search_1",
        content=json.dumps(make_documents(), ensure_ascii=False) if content is None else content,
    )


class KnowledgeBaseSearchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.config = ToolOutputConfig(externalize_min_chars=400, fallback_max_chars=600)

    def assert_bounded_result(self, result, limit=400):
        self.assertLessEqual(len(result.content), limit)
        documents = json.loads(result.content)
        self.assertEqual(documents[0]["title"], "文档一")
        self.assertEqual(documents[0]["sections"][0], make_documents()[0]["sections"][0])
        shortened = documents[0]["sections"][1]
        self.assertEqual(shortened["score"], 0.8)
        self.assertEqual(shortened["url"], "https://example.com/2")
        self.assertTrue(shortened["content"].endswith("[内容已截断]"))
        self.assertEqual(set(documents[0]), {"title", "sections"})
        self.assertEqual(set(shortened), {"score", "content", "url"})
        self.assertEqual(len(documents), 1)

    def test_budget_preserves_schema_without_externalization_or_source_protocol(self):
        with (
            patch("deerflow.agents.middlewares.tool_output_budget_middleware._externalize") as externalize,
            patch("deerflow.agents.middlewares.tool_output_budget_middleware.budget_source_artifact") as source_budget,
        ):
            result = _patch_tool_message(make_message(), self.config, "/unused")
        self.assert_bounded_result(result)
        externalize.assert_not_called()
        source_budget.assert_not_called()

    def test_tool_override_and_fallback_use_smallest_positive_limit(self):
        for externalize, fallback, override, expected in (
            (400, 600, 300, 300), (400, 300, 500, 300),
            (400, 600, 0, 600), (400, 0, 300, 300),
        ):
            with self.subTest(externalize=externalize, fallback=fallback, override=override):
                config = ToolOutputConfig(
                    externalize_min_chars=externalize, fallback_max_chars=fallback,
                    tool_overrides={"knowledge_base_search": override},
                )
                self.assert_bounded_result(_patch_tool_message(make_message(), config, None), expected)

    def test_history_and_command_results_share_structured_budget(self):
        original = make_message()
        history = _patch_model_messages([original], self.config)
        self.assert_bounded_result(history[0])
        self.assertGreater(len(original.content), 400)
        command = Command(update={"messages": [original]})
        patched = _patch_result(command, self.config, None)
        self.assert_bounded_result(patched.update["messages"][0])

    def test_small_or_exempt_or_disabled_results_are_unchanged(self):
        small = make_message(content='[{"title":"标题","sections":[]}]')
        self.assertIs(_patch_tool_message(small, self.config, None), small)
        large = make_message()
        exempt = self.config.model_copy(update={"exempt_tools": ["knowledge_base_search"]})
        self.assertIs(_patch_tool_message(large, exempt, None), large)
        disabled = ToolOutputConfig(externalize_min_chars=0, fallback_max_chars=0)
        self.assertIs(_patch_tool_message(large, disabled, None), large)

    def test_compaction_can_preserve_complete_content(self):
        documents = [{"title": "标题", "sections": []}]
        original = json.dumps(documents, ensure_ascii=False, indent=20)
        compact = budget_knowledge_base_search(original, 30)
        self.assertEqual(json.loads(compact), documents)
        self.assertLessEqual(len(compact), 30)

    def test_tiny_budgets_and_oversized_metadata_return_valid_empty_list(self):
        cases = (
            make_documents(),
            [{"title": "标题" * 1000, "sections": []}],
            [{"title": "标题", "sections": [{"score": 1, "content": "正文", "url": "x" * 1000}]}],
        )
        for documents in cases:
            for limit in (1, 2, 10, 50):
                with self.subTest(documents=documents[0]["title"][:10], limit=limit):
                    result = budget_knowledge_base_search(json.dumps(documents), limit)
                    self.assertEqual(json.loads(result), [])
                    self.assertLessEqual(len(result), max(2, limit))

    def test_unknown_results_and_other_tools_keep_generic_behavior(self):
        for content in ('Error: 检索失败', '{}', '[{"title":"缺少段落"}]'):
            self.assertIsNone(budget_knowledge_base_search(content, 10))
        result = _patch_tool_message(make_message(name="other_search"), self.config, None)
        self.assertIn("chars omitted", result.content)

    def test_json_blocks_preserve_media_and_valid_json_text(self):
        image = {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        result = _patch_tool_message(
            make_message(content=[{"type": "json", "json": make_documents()}, image]), self.config, None,
        )
        self.assertEqual(result.content[1], image)
        text_result = make_message(content=result.content[0]["text"])
        self.assert_bounded_result(text_result)

    def test_complete_sections_and_document_order_are_preserved(self):
        documents = [{"title": str(index), "sections": [
            {"score": index, "content": "段落" * 10, "url": str(index)},
        ]} for index in range(20)]
        result = budget_knowledge_base_search(json.dumps(documents), 400)
        retained = json.loads(result)
        self.assertLessEqual(len(result), 400)
        self.assertGreater(len(retained), 1)
        self.assertEqual([item["title"] for item in retained], [str(index) for index in range(len(retained))])
        for index, item in enumerate(retained[:-1]):
            self.assertEqual(item, documents[index])

    def test_registered_search_tool_message_keeps_contract_after_budgeting(self):
        runtime = Runtime(state={}, context={}, config={}, stream_writer=lambda value: None, tool_call_id="search_1", store=None)
        passages = [
            {"document_id": "doc_1", "title": document["title"], **section, "download_path": section["url"]}
            for document in make_documents()[:1]
            for section in document["sections"]
        ]
        with patch.object(callbacks, "_knowledge_search", return_value=passages):
            result = knowledge_base_search_tool.invoke({
                "type": "tool_call", "id": "search_1", "name": "knowledge_base_search",
                "args": {"runtime": runtime, "description": "查找依据", "query": "测试查询"},
            })
        self.assertIsInstance(result, ToolMessage)
        self.assert_bounded_result(_patch_tool_message(result, self.config, None))


class KnowledgeBaseSearchBudgetHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_async_hooks_and_disabled_middleware(self):
        middleware = ToolOutputBudgetMiddleware(ToolOutputConfig(externalize_min_chars=400, fallback_max_chars=600))
        request = SimpleNamespace(tool_call={"name": "knowledge_base_search", "id": "search_1"}, runtime=None)
        async def async_handler(request):
            return make_message()

        with (
            patch("deerflow.agents.middlewares.tool_output_budget_middleware._resolve_outputs_path", return_value=None),
            patch("deerflow.agents.middlewares.tool_output_budget_middleware._resolve_sandbox", return_value=None),
            patch("deerflow.agents.middlewares.tool_output_budget_middleware._resolve_thread_id", return_value=None),
        ):
            sync_result = middleware.wrap_tool_call(request, lambda request: make_message())
            async_result = await middleware.awrap_tool_call(request, async_handler)
        self.assertEqual(sync_result.content, async_result.content)
        self.assertLessEqual(len(sync_result.content), 400)
        disabled = ToolOutputBudgetMiddleware(ToolOutputConfig(enabled=False))
        original = make_message()
        self.assertIs(disabled.wrap_tool_call(request, lambda request: original), original)


if __name__ == "__main__":
    unittest.main()
