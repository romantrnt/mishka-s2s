import asyncio
import json

import httpx
import pytest

from app.foundation.s2s import OllamaLLMProvider
from test_story_backend_context import MODEL, URL, contract


def capture_http(monkeypatch):
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        lines = [
            {"message": {"content": "{}"}},
            {"done": True, "done_reason": "stop"},
        ]
        return httpx.Response(200, content="\n".join(map(json.dumps, lines)))

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    return sent


@pytest.mark.asyncio
async def test_story_http_payload_requires_integer_duration_and_closed_plot(monkeypatch):
    sent = capture_http(monkeypatch)
    provider = OllamaLLMProvider(URL, MODEL, 1, story_backend_contract=await contract())
    messages = [{"role": "system", "content": "Synthetic complete story request."}]
    ordinary = provider._payload(messages, True)

    assert [part async for part in provider.stream_story(messages, "story", lambda: False)] == ["{}"]
    schema = sent[-1]["format"]
    keys = {"title", "plot", "age_band", "estimated_duration_sec", "safety_tags", "ending_state"}
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == keys
    properties = schema["properties"]
    assert properties["title"]["type"] == "string" and properties["title"]["minLength"] >= 1
    plot = properties["plot"]
    sections = {"setting", "problem", "attempt", "resolution"}
    assert plot["type"] == "object" and plot["additionalProperties"] is False
    assert set(plot["required"]) == set(plot["properties"]) == sections
    assert all(item["type"] == "string" and item["minLength"] >= 1 for item in plot["properties"].values())
    assert properties["age_band"]["type"] == "string"
    assert set(properties["age_band"]["enum"]) == {"1-3", "3-5", "4-5", "5-7", "6-7"}
    assert properties["estimated_duration_sec"] == {"type": "integer", "minimum": 45, "maximum": 150}
    assert properties["safety_tags"] == {"type": "array", "items": {"type": "string"}}
    assert properties["ending_state"] == {"type": "string", "enum": ["safe_resolved"]}
    assert sent[-1]["truncate"] is False and sent[-1]["shift"] is False

    assert [part async for part in provider.stream(messages, "dialogue", lambda: False)] == ["{}"]
    assert sent[-1] == ordinary and "format" not in sent[-1]
    assert [part async for part in provider.stream_story(messages, "repeat-story", lambda: False)] == ["{}"]
    assert sent[-1]["format"] == schema


@pytest.mark.asyncio
@pytest.mark.parametrize("story", [False, True])
async def test_formatted_story_and_ordinary_http_stream_preserve_cancellation(monkeypatch, story):
    sent = capture_http(monkeypatch)
    provider = OllamaLLMProvider(URL, MODEL, 1, story_backend_contract=await contract())
    cancelled = False
    parts = []
    stream = provider.stream_story if story else provider.stream
    with pytest.raises(asyncio.CancelledError):
        async for part in stream([], "cancel", lambda: cancelled):
            parts.append(part)
            cancelled = True
    assert parts == ["{}"] and len(sent) == 1
