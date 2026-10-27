import json
import logging
import os

import httpx
from langchain.tools import tool

from deerflow.config import get_app_config

logger = logging.getLogger(__name__)

_RESULT_CONTAINERS = {
    "webpage": "webpages",
    "document": "documents",
    "scholar": "scholars",
    "image": "images",
    "video": "videos",
    "podcast": "podcasts",
}
_FORMAT_ERROR = "Metaso returned an unexpected response format"


class _MetasoError(ValueError):
    """只携带可公开的错误信息，避免将响应正文或密钥带入工具输出。"""


def _tool_options(tool_name: str) -> dict:
    config = get_app_config().get_tool_config(tool_name)
    return dict(config.model_extra or {}) if config is not None else {}


def _get_metaso_client(options: dict, accept: str) -> httpx.AsyncClient:
    api_key = options.get("api_key") or os.getenv("METASO_API_KEY")
    if not isinstance(api_key, str) or not api_key.strip():
        raise _MetasoError("METASO_API_KEY is not configured")
    return httpx.AsyncClient(
        base_url="https://metaso.cn/api/v1/",
        headers={"Authorization": f"Bearer {api_key.strip()}", "Accept": accept},
        timeout=options.get("timeout", 30.0),
    )


def _max_results(options: dict) -> int:
    value = options.get("max_results", 10)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _MetasoError("max_results must be a positive integer")
    try:
        count = int(value)
    except ValueError:
        raise _MetasoError("max_results must be a positive integer") from None
    if count < 1:
        raise _MetasoError("max_results must be a positive integer")
    return count


def _search_payload(query: str, scope: str, options: dict) -> dict:
    if scope not in _RESULT_CONTAINERS:
        raise _MetasoError("Unsupported Metaso search scope")
    payload = {"q": query, "scope": scope, "size": str(_max_results(options))}
    for option, parameter in (
        ("include_summary", "includeSummary"),
        ("include_raw_content", "includeRawContent"),
        ("concise_snippet", "conciseSnippet"),
    ):
        value = options.get(option, False)
        if not isinstance(value, bool):
            raise _MetasoError(f"{option} must be a boolean")
        payload[parameter] = value
    return payload


def _normalize_result(result: dict) -> dict:
    return {
        "title": str(result.get("title") or ""),
        "url": str(result.get("url") or result.get("link") or result.get("imageUrl") or ""),
        "snippet": str(result.get("snippet") or ""),
    }


def _search_results(data: object, scope: str) -> list[dict]:
    container = _RESULT_CONTAINERS[scope]
    if not isinstance(data, dict) or data.get("error") or data.get("errCode"):
        raise _MetasoError(_FORMAT_ERROR)
    if container not in data:
        raise _MetasoError(_FORMAT_ERROR)
    results = data[container]
    if results is None:
        return []
    if not isinstance(results, list):
        raise _MetasoError(_FORMAT_ERROR)
    objects = [result for result in results if isinstance(result, dict)]
    if results and not objects:
        raise _MetasoError(_FORMAT_ERROR)
    return [_normalize_result(result) for result in objects]


def _error_result(error: Exception) -> str:
    # 不记录异常正文：配置错误或上游错误可能包含密钥及请求内容。
    logger.warning("Metaso tool failed: %s", type(error).__name__)
    if isinstance(error, _MetasoError):
        return f"Error: {error}"
    if isinstance(error, httpx.HTTPStatusError):
        return f"Error: Metaso request failed (HTTP {error.response.status_code})"
    if isinstance(error, httpx.TimeoutException):
        return "Error: Metaso request timed out"
    return f"Error: Metaso request failed ({type(error).__name__})"


@tool("web_search", parse_docstring=True)
async def web_search_tool(query: str, scope: str = "webpage") -> str:
    """Search webpages, documents, scholarly resources, images, videos, or podcasts.

    Return a JSON list with only title, url, and snippet for each result, or an error string starting with "Error: ".

    Args:
        query: The question or keywords to search for.
        scope: The search scope: webpage for webpages, document for documents, scholar for scholarly resources, image for images, video for videos, or podcast for podcasts.
    """
    try:
        options = _tool_options("web_search")
        payload = _search_payload(query, scope, options)
        async with _get_metaso_client(options, "application/json") as client:
            response = await client.post("search", json=payload)
            response.raise_for_status()
            results = _search_results(response.json(), scope)
        return json.dumps(results, indent=2, ensure_ascii=False)
    except Exception as error:
        return _error_result(error)


@tool("web_fetch", parse_docstring=True)
async def web_fetch_tool(url: str) -> str:
    """Fetch the main content of a webpage and return the raw text.

    Only fetch complete URLs provided directly by the user or found in search results.
    Do not add www. to URLs. URLs must include the scheme.
    This tool cannot access private pages that require login.

    Args:
        url: The complete URL of the webpage whose main content should be fetched.
    """
    try:
        options = _tool_options("web_fetch")
        async with _get_metaso_client(options, "text/plain") as client:
            response = await client.post("reader", json={"url": url})
            response.raise_for_status()
            if "application/json" in response.headers.get("content-type", "").lower():
                raise _MetasoError(_FORMAT_ERROR)
            return response.text
    except Exception as error:
        return _error_result(error)
