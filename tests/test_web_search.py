import json

import httpx
import pytest

from gateway.config import Settings
from gateway.web_search import WebSearchError, extract_search_result, search_web


def response_body():
    return {
        "id": "resp_test", "status": "completed", "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{
                "type": "output_text", "text": "It is raining. [1]",
                "annotations": [
                    {"type": "url_citation", "start_index": 15, "end_index": 18,
                     "title": "Weather service", "url": "https://weather.example/today"},
                    {"type": "url_citation", "start_index": 0, "end_index": 2,
                     "title": "Unsafe", "url": "javascript:alert(1)"},
                ],
            }]},
        ],
    }


def test_extract_search_result_preserves_safe_inline_citations():
    assert extract_search_result(response_body()) == {
        "answer": "It is raining. [1]",
        "citations": [{"start": 15, "end": 18, "title": "Weather service",
                       "url": "https://weather.example/today"}],
    }


def test_extract_search_result_requires_real_search_and_answer():
    body = response_body()
    body["output"] = body["output"][1:]
    with pytest.raises(WebSearchError, match="No web search"):
        extract_search_result(body)
    with pytest.raises(WebSearchError, match="did not finish"):
        extract_search_result({"status": "incomplete", "output": []})
    body = response_body()
    body["output"][1]["content"][0]["annotations"] = []
    with pytest.raises(WebSearchError, match="no source citations"):
        extract_search_result(body)


async def test_search_web_uses_required_hosted_search_and_existing_key(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response_body())

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "gateway.web_search.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = Settings(openai_api_key="test-key", planner_model="gpt-5.6-terra")
    result = await search_web(settings, "weather today")

    assert result["answer"] == "It is raining. [1]"
    assert requests[0].headers["Authorization"] == "Bearer test-key"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "gpt-5.6-terra"
    assert payload["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert payload["tool_choice"] == "required"
    assert payload["store"] is False


async def test_search_web_reports_http_error_without_leaking_details(monkeypatch):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "gateway.web_search.httpx.AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(500)), **kwargs
        ),
    )
    with pytest.raises(WebSearchError, match="unavailable right now"):
        await search_web(Settings(openai_api_key="test-key"), "weather today")
