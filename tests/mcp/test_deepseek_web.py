"""保护搜索接口边界：错误参数不付费发出，失败不伪装成来源，异常响应不吞掉。"""
import asyncio
import json

import httpx
from mcp.types import CallToolRequestParams
from pydantic import ValidationError
import pytest

from konden.mcp.deepseek_web import call_tool


PAGE = {
    "title": "官方资料", "url": "https://example.com/source", "snippet": "短摘要",
    "summary": "可以核对的搜索摘要", "content": "", "date": None, "site_name": "Example",
    "site_icon": None, "rerank_score": 2.4, "authority": 3,
}


def invoke(name, arguments, handler):
    async def scenario():
        async with httpx.AsyncClient(base_url="https://api.deepseek.com",
                                     headers={"Authorization": "Bearer test-key"},
                                     transport=httpx.MockTransport(handler)) as client:
            return await call_tool(client, None, CallToolRequestParams(name=name, arguments=arguments))
    return asyncio.run(scenario())


def test_search_preserves_query_and_source_evidence_without_requesting_full_pages():
    def handler(request):
        assert request.url == "https://api.deepseek.com/v1/tools/search"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "query": "发布说明 site:example.com after:2026-10-01", "count": 10, "need_content": False,
        }
        return httpx.Response(200, json={"results": [PAGE]})

    result = invoke("web_search", {"query": "发布说明 site:example.com after:2026-10-01"}, handler)
    assert not result.is_error
    assert result.structured_content == {"results": [PAGE]}


def test_search_passes_explicit_question_count_and_content_choice():
    def handler(request):
        assert json.loads(request.content) == {
            "query": "search", "ori_question": "原始问题", "count": 1, "need_content": True,
        }
        return httpx.Response(200, json={"results": []})

    result = invoke("web_search", {"query": "search", "ori_question": "原始问题",
                                   "count": 1, "need_content": True}, handler)
    assert not result.is_error
    assert result.structured_content == {"results": []}


def test_fetch_preserves_markdown_and_attaches_requested_source():
    url = "https://example.com/article?lang=zh"

    def handler(request):
        assert request.url == "https://api.deepseek.com/v1/tools/fetch"
        assert json.loads(request.content) == {"url": url}
        return httpx.Response(200, json={"title": "原文", "content": "# 标题\n\n正文"})

    result = invoke("web_fetch", {"url": url}, handler)
    assert not result.is_error
    assert result.structured_content == {"url": url, "title": "原文", "content": "# 标题\n\n正文"}


@pytest.mark.parametrize("name,arguments", [
    ("web_search", {"query": " "}),
    ("web_search", {"query": "查" * 129}),
    ("web_search", {"query": "search", "count": 51}),
    ("web_search", {"query": "search", "count": True}),
    ("web_search", {"query": "search", "ori_question": " "}),
    ("web_fetch", {"url": "file:///private.txt"}),
    ("web_fetch", {"url": "https://user:password@example.com"}),
])
def test_invalid_arguments_are_rejected_before_any_paid_request(name, arguments):
    def handler(request):
        raise AssertionError("无效参数不能发送给计费接口")

    result = invoke(name, arguments, handler)
    assert result.is_error
    assert result.structured_content["error"]


@pytest.mark.parametrize("status,hint", [
    (401, "API Key"), (402, "余额不足"), (403, "拒绝"), (404, "正文"),
    (429, "稍后重试"), (504, "稍后重试"),
])
def test_api_failures_keep_http_evidence_and_actionable_hint(status, hint):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "provider error"}})

    result = invoke("web_fetch", {"url": "https://example.com"}, handler)
    assert result.is_error
    assert f"HTTP {status}" in result.structured_content["error"]
    assert "provider error" in result.structured_content["error"]
    assert hint in result.structured_content["hint"]


def test_network_timeout_is_reported_as_failure():
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    result = invoke("web_search", {"query": "search"}, handler)
    assert result.is_error
    assert "read timed out" in result.structured_content["error"]


def test_malformed_success_response_is_not_downgraded_to_an_empty_search():
    with pytest.raises(ValidationError):
        invoke("web_search", {"query": "search"}, lambda request: httpx.Response(200, json={"results": [{}]}))


def test_unknown_internal_error_is_not_converted_to_a_recoverable_tool_failure():
    def handler(request):
        raise RuntimeError("broken contract")

    with pytest.raises(RuntimeError, match="broken contract"):
        invoke("web_search", {"query": "search"}, handler)
