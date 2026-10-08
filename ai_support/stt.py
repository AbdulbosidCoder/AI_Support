"""Speech-to-text for voice messages.

Claude does not transcribe audio, so STT is a separate pluggable service. The default
implementation calls a Whisper-compatible HTTP transcription endpoint (multipart upload,
returns {"text": ...}); swap in another provider by implementing `SpeechToText`.

Uzbek is the hard case: without a hint the recogniser often takes Uzbek speech for Turkish,
Kazakh or Russian and writes nonsense. So every request carries the client's language (the one
they chose or last wrote in) as an ISO code, and a short prompt in that language and script with
the app's own words (Xonsaroy Pay, to'lov, o'tkazma, MyID...), which steers spelling and script.
"""
from __future__ import annotations

from typing import Protocol

import httpx

from .models import Audio, Lang


class STTError(Exception):
    pass


class SpeechToText(Protocol):
    async def transcribe(self, audio: Audio, language: Lang | None = None) -> str: ...


class DisabledSTT:
    async def transcribe(self, audio: Audio, language: Lang | None = None) -> str:
        raise STTError("speech-to-text is not configured")


# ISO 639-1 code the transcription API expects; both Uzbek scripts are one spoken language.
ISO_CODES = {Lang.UZ_LATN: "uz", Lang.UZ_CYRL: "uz", Lang.RU: "ru", Lang.EN: "en"}

# Prompts are "previous text" for the recogniser: a few lines a client of the app could say,
# in the expected language and script. They carry the product terms it would otherwise misspell.
PROMPTS = {
    Lang.UZ_LATN: ("Assalomu alaykum. Xonsaroy Pay ilovasida to'lov o'tmadi, pulim kartadan yechildi. "
                   "O'tkazma Pending holatida. SMS kod kelmayapti, MyID orqali identifikatsiya, PINFL, "
                   "kvartira qarzi, shartnoma raqami."),
    Lang.UZ_CYRL: ("Ассалому алайкум. Xonsaroy Pay иловасида тўлов ўтмади, пулим картадан ечилди. "
                   "Ўтказма Pending ҳолатида. SMS код келмаяпти, MyID орқали идентификация, ПИНФЛ, "
                   "квартира қарзи, шартнома рақами."),
    Lang.RU: ("Здравствуйте. В приложении Xonsaroy Pay платёж не прошёл, деньги списались с карты. "
              "Перевод в статусе Pending. Не приходит SMS-код, идентификация через MyID, ПИНФЛ, "
              "задолженность за квартиру, номер договора."),
    Lang.EN: ("Hello. In the Xonsaroy Pay app my payment failed but the money was debited from my card. "
              "The transfer is Pending. The SMS code does not arrive, MyID identification, PINFL, "
              "apartment debt, contract number."),
}
# Language not known yet: no forced language, only the product names (the bot's clients mostly speak Uzbek).
DEFAULT_PROMPT = "Xonsaroy Pay, to'lov, o'tkazma, karta, SMS kod, MyID, PINFL, Pending."


class WhisperHTTPSTT:
    """OpenAI-compatible /audio/transcriptions (gpt-4o-transcribe, gpt-4o-mini-transcribe, whisper-1...)."""

    def __init__(self, url: str, api_key: str, model: str = "gpt-4o-transcribe", timeout: float = 60.0,
                 language_hint: bool = True):
        self._url = url
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model
        self._timeout = timeout
        self._language_hint = language_hint

    def request_data(self, language: Lang | None) -> dict[str, str]:
        data = {"model": self._model, "response_format": "json", "temperature": "0"}
        if language is not None and self._language_hint:
            data["language"] = ISO_CODES[language]
            data["prompt"] = PROMPTS[language]
        else:
            data["prompt"] = DEFAULT_PROMPT
        return data

    async def transcribe(self, audio: Audio, language: Lang | None = None) -> str:
        files = {"file": (audio.filename, audio.data, audio.media_type)}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(self._url, headers=self._headers, files=files,
                                      data=self.request_data(language))
                r.raise_for_status()
                text = r.json().get("text", "")
        except (httpx.HTTPError, ValueError) as e:
            raise STTError(str(e)) from e
        if not text.strip():
            raise STTError("empty transcription")
        return text.strip()
