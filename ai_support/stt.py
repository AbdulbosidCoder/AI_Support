"""Speech-to-text for voice messages.

Claude does not transcribe audio, so STT is a separate pluggable service. The default
implementation calls a Whisper-compatible HTTP transcription endpoint (multipart upload,
returns {"text": ...}); swap in another provider by implementing `SpeechToText`.
"""
from __future__ import annotations

from typing import Protocol

import httpx

from .models import Audio


class STTError(Exception):
    pass


class SpeechToText(Protocol):
    async def transcribe(self, audio: Audio) -> str: ...


class DisabledSTT:
    async def transcribe(self, audio: Audio) -> str:
        raise STTError("speech-to-text is not configured")


class WhisperHTTPSTT:
    def __init__(self, url: str, api_key: str, model: str = "whisper-1", timeout: float = 60.0):
        self._url = url
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model
        self._timeout = timeout

    async def transcribe(self, audio: Audio) -> str:
        files = {"file": (audio.filename, audio.data, audio.media_type)}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(self._url, headers=self._headers, files=files, data={"model": self._model})
                r.raise_for_status()
                text = r.json().get("text", "")
        except (httpx.HTTPError, ValueError) as e:
            raise STTError(str(e)) from e
        if not text.strip():
            raise STTError("empty transcription")
        return text.strip()
