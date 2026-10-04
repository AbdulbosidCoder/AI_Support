"""Claude client for support answers (text and screenshots)."""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Protocol

import anthropic

from .models import Image

log = logging.getLogger(__name__)


@dataclass
class ModelAnswer:
    language: str
    topic: str
    screen_id: str | None
    answer: str
    needs_escalation: bool
    escalation_reason: str
    pii_visible: bool


class LLMError(Exception):
    pass


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str


class SupportLLM(Protocol):
    async def answer(self, history: list[Turn], text: str, images: list[Image]) -> ModelAnswer: ...


def build_user_content(text: str, images: list[Image]) -> list[dict]:
    content: list[dict] = []
    for img in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.media_type,
                "data": base64.standard_b64encode(img.data).decode("ascii"),
            },
        })
    body = text.strip() or "(клиент прислал только изображение без текста)"
    content.append({"type": "text", "text": f"<client_message>\n{body}\n</client_message>"})
    return content


class ClaudeSupportLLM:
    def __init__(self, system_prompt: str, schema: dict, model: str = "claude-opus-5-5",
                 effort: str = "medium", client: anthropic.AsyncAnthropic | None = None):
        self._client = client or anthropic.AsyncAnthropic()
        self._system = system_prompt
        self._schema = schema
        self._model = model
        self._effort = effort

    async def answer(self, history: list[Turn], text: str, images: list[Image]) -> ModelAnswer:
        messages = [{"role": t.role, "content": t.text} for t in history]
        messages.append({"role": "user", "content": build_user_content(text, images)})
        try:
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=16000,
                # System prompt + knowledge base is the same for every request: cache it.
                system=[{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort, "format": {"type": "json_schema", "schema": self._schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            raise LLMError("rate limited") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"api error {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("connection error") from e

        if response.stop_reason == "refusal":
            raise LLMError("model refused")
        if response.stop_reason == "max_tokens":
            raise LLMError("answer truncated")
        raw = next((b.text for b in response.content if b.type == "text"), None)
        if raw is None:
            raise LLMError("no text block")
        try:
            data = json.loads(raw)
            return ModelAnswer(**{k: data[k] for k in ModelAnswer.__dataclass_fields__})
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise LLMError("invalid structured output") from e
