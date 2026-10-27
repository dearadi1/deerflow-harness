"""通过离线 HTTP 验证各检索范围的真实响应结构及统一错误约定。"""

import asyncio
import json
import unittest
from unittest.mock import Mock, patch

import httpx

from deerflow.community.metaso import tools
from deerflow.config.tool_config import ToolConfig


class MetasoToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.options = {}
        self.requests = []
        self.clients = []
        self.response = httpx.Response(200, json={"webpages": []})
        self.error = None
        self.client_class = httpx.AsyncClient
        self.config = Mock()
        self.config.get_tool_config.side_effect = self.tool_config
        self.enterContext(patch.object(tools, "get_app_config", return_value=self.config))
        self.enterContext(patch.dict("os.environ", {"METASO_API_KEY": "env-test-key"}))
        self.enterContext(patch.object(tools.httpx, "AsyncClient", side_effect=self.client))

    def tool_config(self, name):
        return ToolConfig(name=name, group="web", use=f"deerflow.community.metaso.tools:{name}_tool", **self.options.get(name, {}))

    def client(self, **kwargs):
        client = self.client_class(transport=httpx.MockTransport(self.handle_request), **kwargs)
        self.clients.append(client)
        return client

    def handle_request(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response

    async def search(self, scope="webpage"):
        return await tools.web_search_tool.ainvoke({"query": "人工智能", "scope": scope})

    async def fetch(self):
        return await tools.web_fetch_tool.ainvoke({"url": "https://example.com/article"})

    async def test_all_scope_shapes_return_only_public_fields(self):
        cases = [
            ("webpage", "webpages", {"link": "https://example.com/web", "snippet": "网页摘要", "date": "2025年04月30日"}),
            ("document", "documents", {"link": "https://example.com/doc", "snippet": "文档摘要", "authors": ["作者"]}),
            ("scholar", "scholars", {"link": "https://example.com/paper", "snippet": "论文摘要", "date": "2017-11-20"}),
            ("image", "images", {"imageUrl": "https://example.com/image.jpg", "imageWidth": 474, "imageHeight": 252}),
            ("video", "videos", {"link": "https://example.com/video", "snippet": "视频摘要", "duration": "1180", "coverImage": "https://example.com/cover.jpg"}),
            ("podcast", "podcasts", {"link": "https://example.com/podcast", "snippet": "", "authors": ["佚名"], "duration": "288"}),
        ]
        for scope, container, fields in cases:
            with self.subTest(scope=scope):
                item = {"title": "人工智能", "score": "high", "position": 1, **fields}
                self.response = httpx.Response(200, json={"credits": 3, container: [item], "total": 10})
                results = json.loads(await self.search(scope))
                self.assertEqual(results, [{
                    "title": "人工智能",
                    "url": fields.get("link") or fields["imageUrl"],
                    "snippet": fields.get("snippet", ""),
                }])
                self.assertTrue(self.clients[-1].is_closed)

    async def test_search_defaults_and_config_parameters(self):
        await tools.web_search_tool.ainvoke({"query": "人工智能"})
        request = self.requests[-1]
        self.assertEqual(str(request.url), "https://metaso.cn/api/v1/search")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["authorization"], "Bearer env-test-key")
        self.assertEqual(request.headers["accept"], "application/json")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(json.loads(request.content), {
            "q": "人工智能", "scope": "webpage", "size": "10",
            "includeSummary": False, "includeRawContent": False, "conciseSnippet": False,
        })
        self.options["web_search"] = {"api_key": "search-key", "max_results": 2, "timeout": 7, "include_summary": True, "include_raw_content": True, "concise_snippet": True}
        await self.search()
        request = self.requests[-1]
        payload = json.loads(request.content)
        self.assertEqual(request.headers["authorization"], "Bearer search-key")
        self.assertEqual(payload["size"], "2")
        self.assertTrue(all(payload[key] for key in ("includeSummary", "includeRawContent", "conciseSnippet")))
        self.assertEqual(request.extensions["timeout"]["read"], 7)

    async def test_fetch_uses_own_config_and_preserves_full_text(self):
        self.options = {"web_search": {"api_key": "search-key"}, "web_fetch": {"api_key": "fetch-key", "timeout": 8}}
        content = "# 中文标题\n\n" + "正文" * 3000
        self.response = httpx.Response(200, text=content)
        self.assertEqual(await self.fetch(), content)
        request = self.requests[-1]
        self.assertEqual(str(request.url), "https://metaso.cn/api/v1/reader")
        self.assertEqual(request.headers["authorization"], "Bearer fetch-key")
        self.assertEqual(request.headers["accept"], "text/plain")
        self.assertEqual(json.loads(request.content), {"url": "https://example.com/article"})
        self.assertEqual(request.extensions["timeout"]["read"], 8)
        self.assertTrue(self.clients[-1].is_closed)

    async def test_empty_and_mixed_results(self):
        for results in ([], None):
            self.response = httpx.Response(200, json={"webpages": results})
            self.assertEqual(json.loads(await self.search()), [])
        self.response = httpx.Response(200, json={"webpages": [None, "bad", {"title": None, "link": None, "snippet": None}, {"title": "第二项", "url": "https://example.com"}]})
        results = json.loads(await self.search())
        self.assertEqual([(item["title"], item["url"], item["snippet"]) for item in results], [("", "", ""), ("第二项", "https://example.com", "")])

    async def test_malformed_search_payloads_return_error(self):
        for data in (None, [], "bad", {}, {"videos": []}, {"webpages": {}}, {"webpages": [None, 1]}, {"error": "secret", "webpages": []}, {"errCode": 401, "webpages": []}):
            with self.subTest(data=data):
                self.response = httpx.Response(200, json=data)
                result = await self.search()
                self.assertTrue(result.startswith("Error: "))
                self.assertNotIn("secret", result)
                self.assertTrue(self.clients[-1].is_closed)

    async def test_http_and_transport_errors_close_clients(self):
        for invoke in (self.search, self.fetch):
            for status in (401, 429, 500):
                self.response = httpx.Response(status, text="private upstream details")
                result = await invoke()
                self.assertIn(f"HTTP {status}", result)
                self.assertTrue(result.startswith("Error: "))
                self.assertNotIn("private", result)
                self.assertTrue(self.clients[-1].is_closed)
            for error in (httpx.ReadTimeout("private"), httpx.ConnectError("private")):
                self.error = error
                result = await invoke()
                self.assertTrue(result.startswith("Error: "))
                self.assertNotIn("private", result)
                self.assertTrue(self.clients[-1].is_closed)
            self.error = None

    async def test_invalid_json_and_reader_error_envelope(self):
        self.response = httpx.Response(200, text="not JSON")
        self.assertTrue((await self.search()).startswith("Error: "))
        self.response = httpx.Response(200, json={"errCode": 403, "message": "private"})
        self.assertTrue((await self.fetch()).startswith("Error: "))

    async def test_missing_key_and_invalid_config_do_not_send_requests(self):
        with patch.dict("os.environ", {"METASO_API_KEY": ""}):
            self.assertTrue((await self.search()).startswith("Error: "))
            self.assertTrue((await self.fetch()).startswith("Error: "))
        for value in (0, -1, True, 1.5, "bad"):
            self.options["web_search"] = {"max_results": value}
            self.assertTrue((await self.search()).startswith("Error: "))
        self.options["web_search"] = {"include_summary": "false"}
        self.assertTrue((await self.search()).startswith("Error: "))
        self.assertEqual(self.requests, [])

    async def test_invalid_scope_returns_error(self):
        self.assertTrue((await self.search("invalid")).startswith("Error: "))
        self.assertEqual(self.requests, [])

    async def test_environment_fallback_without_tool_config(self):
        self.config.get_tool_config.side_effect = None
        self.config.get_tool_config.return_value = None
        self.assertEqual(json.loads(await self.search()), [])
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer env-test-key")

    async def test_cancellation_propagates_and_closes_client(self):
        self.error = asyncio.CancelledError()
        for invoke in (self.search, self.fetch):
            with self.assertRaises(asyncio.CancelledError):
                await invoke()
            self.assertTrue(self.clients[-1].is_closed)

    def test_public_tool_schema_only_exposes_requested_parameters(self):
        self.assertEqual(set(tools.web_search_tool.args), {"query", "scope"})
        self.assertEqual(tools.web_search_tool.args["scope"]["default"], "webpage")
        self.assertEqual(set(tools.web_fetch_tool.args), {"url"})


if __name__ == "__main__":
    unittest.main()
