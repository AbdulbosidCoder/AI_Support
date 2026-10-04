"""ClaudeSupportLLM against a mocked HTTP transport: request shape and response parsing."""
import asyncio
import json

import anthropic
import httpx2 as httpx
import pytest

from ai_support.llm import ClaudeSupportLLM, LLMError, Turn
from ai_support.models import Image

SCHEMA = {"type": "object"}
GOOD = {"language": "ru", "topic": "other", "screen_id": None, "answer": "Ок", "needs_escalation": False,
        "escalation_reason": "", "pii_visible": False}


def make(status=200, stop_reason="end_turn", text=json.dumps(GOOD), seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "x"}})
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })
    client = anthropic.AsyncAnthropic(api_key="test", max_retries=0,
                                      http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return ClaudeSupportLLM("SYSTEM", SCHEMA, client=client)


def test_request_shape_and_parse():
    seen = []
    llm = make(seen=seen)
    ans = asyncio.run(llm.answer([Turn("user", "привет"), Turn("assistant", "здравствуйте")], "карта", [Image(b"i")]))
    assert ans.answer == "Ок"
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1]["content"][0]["type"] == "image"
    assert len(body["messages"]) == 3


@pytest.mark.parametrize("kwargs", [
    {"status": 500}, {"status": 429}, {"stop_reason": "refusal"}, {"stop_reason": "max_tokens"}, {"text": "not json"},
])
def test_failures_raise_llm_error(kwargs):
    with pytest.raises(LLMError):
        asyncio.run(make(**kwargs).answer([], "x", []))
