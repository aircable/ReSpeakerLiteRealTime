"""A small Responses web-search bridge for the Realtime voice session."""

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings

logger = logging.getLogger(__name__)
SEARCH_TIMEOUT_SECONDS = 30


class WebSearchError(Exception):
    """The search produced no usable, sourced answer."""


def _web_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def extract_search_result(response: dict[str, Any]) -> dict[str, Any]:
    """Preserve citation spans so the UI can display clickable inline sources."""
    if response.get("status") != "completed":
        raise WebSearchError("Web search did not finish. Please try again.")
    output = response.get("output") or []
    if not any(item.get("type") == "web_search_call" for item in output):
        raise WebSearchError("No web search was performed. Please try again.")
    for item in output:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") != "output_text":
                continue
            answer = content.get("text", "")
            if not isinstance(answer, str) or not answer.strip():
                continue
            citations = []
            for annotation in content.get("annotations") or []:
                if annotation.get("type") != "url_citation":
                    continue
                url = _web_url(annotation.get("url"))
                start, end = annotation.get("start_index"), annotation.get("end_index")
                if url and isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(answer):
                    citations.append(
                        {"start": start, "end": end, "url": url,
                         "title": str(annotation.get("title") or url)},
                    )
            if citations:
                return {"answer": answer, "citations": citations}
            raise WebSearchError("Web search returned no source citations. Please try again.")
    raise WebSearchError("Web search returned no answer. Please try again.")


async def search_web(settings: Settings, query: str) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise WebSearchError("Web search is unavailable because the OpenAI API key is missing.")
    payload = {
        "model": settings.planner_model,
        "reasoning": {"effort": "low"},
        "tools": [{"type": "web_search", "search_context_size": "low"}],
        "tool_choice": "required",
        "store": False,
        "input": (
            "Search the live web for the user's request. Give a concise factual answer "
            "with source citations. If sources disagree, say so. Request: " + query
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=payload,
            )
            response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Web search request failed: %s", type(exc).__name__)
        raise WebSearchError("Web search is unavailable right now. Please try again.") from exc
    if settings.openai_trace:
        logger.info(
            "OpenAI trace web_search_response model=%s response_id=%s status=%s usage=%s",
            settings.planner_model, result.get("id"), result.get("status"), result.get("usage", {}),
        )
    return extract_search_result(result)
