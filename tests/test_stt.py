import asyncio

import httpx
import pytest
from fakes import FakeLLM, FakeSTT, answer

from ai_support.config import Settings
from ai_support.engine import SupportEngine
from ai_support.models import Audio, IncomingMessage, Lang
from ai_support.stt import DEFAULT_PROMPT, PROMPTS, STTError, WhisperHTTPSTT


def run(coro):
    return asyncio.run(coro)


def test_default_model_is_gpt4o_transcribe(monkeypatch):
    monkeypatch.delenv("STT_MODEL", raising=False)
    monkeypatch.delenv("STT_LANGUAGE_HINT", raising=False)
    s = Settings.from_env()
    assert s.stt_model == "gpt-4o-transcribe" and s.stt_language_hint


def test_language_hint_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("STT_LANGUAGE_HINT", "0")
    assert not Settings.from_env().stt_language_hint


@pytest.mark.parametrize("lang,iso", [(Lang.UZ_LATN, "uz"), (Lang.UZ_CYRL, "uz"), (Lang.RU, "ru"), (Lang.EN, "en")])
def test_request_carries_language_and_prompt_in_its_script(lang, iso):
    data = WhisperHTTPSTT("u", "k").request_data(lang)
    assert data["language"] == iso and data["prompt"] == PROMPTS[lang]
    assert data["model"] == "gpt-4o-transcribe" and data["temperature"] == "0"


def test_prompts_are_in_the_right_script():
    assert "to'lov" in PROMPTS[Lang.UZ_LATN] and "тўлов" in PROMPTS[Lang.UZ_CYRL]
    assert "платёж" in PROMPTS[Lang.RU] and "payment" in PROMPTS[Lang.EN]


def test_unknown_language_is_not_forced():
    data = WhisperHTTPSTT("u", "k").request_data(None)
    assert "language" not in data and data["prompt"] == DEFAULT_PROMPT


def test_hint_off_sends_no_language():
    data = WhisperHTTPSTT("u", "k", language_hint=False).request_data(Lang.UZ_LATN)
    assert "language" not in data


REAL_CLIENT = httpx.AsyncClient


def _with_transport(monkeypatch, handler):
    def client(*args, **kwargs):
        return REAL_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def test_transcribe_posts_language_to_the_service(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode("utf-8", "replace")
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"text": " SMS kod kelmayapti "})

    _with_transport(monkeypatch, handler)
    stt = WhisperHTTPSTT("https://stt.test/v1/audio/transcriptions", "key")
    assert run(stt.transcribe(Audio(b"ogg"), Lang.UZ_LATN)) == "SMS kod kelmayapti"
    assert seen["auth"] == "Bearer key"
    assert 'name="language"\r\n\r\nuz\r\n' in seen["body"]
    assert 'name="model"\r\n\r\ngpt-4o-transcribe\r\n' in seen["body"]


def test_empty_or_failed_transcription_is_an_error(monkeypatch):
    _with_transport(monkeypatch, lambda r: httpx.Response(200, json={"text": "  "}))
    with pytest.raises(STTError):
        run(WhisperHTTPSTT("https://stt.test/x", "k").transcribe(Audio(b"ogg"), Lang.RU))
    _with_transport(monkeypatch, lambda r: httpx.Response(500))
    with pytest.raises(STTError):
        run(WhisperHTTPSTT("https://stt.test/x", "k").transcribe(Audio(b"ogg"), Lang.RU))


def test_engine_hints_the_chosen_language():
    stt = FakeSTT("pulim yechildi")
    run(SupportEngine(FakeLLM(), stt).handle(IncomingMessage("1", audio=Audio(b"ogg"), language=Lang.UZ_CYRL)))
    assert stt.languages == [Lang.UZ_CYRL]


def test_last_written_language_wins_over_the_chosen_one():
    stt = FakeSTT("деньги списались")
    engine = SupportEngine(FakeLLM(answer(lang="ru")), stt)
    run(engine.handle(IncomingMessage("1", "Не проходит платёж", language=Lang.UZ_LATN)))
    run(engine.handle(IncomingMessage("1", audio=Audio(b"ogg"), language=Lang.UZ_LATN)))
    assert stt.languages == [Lang.RU]


def test_caption_language_wins_for_a_voice_message():
    stt = FakeSTT("to'lov o'tmadi")
    run(SupportEngine(FakeLLM(), stt).handle(
        IncomingMessage("1", "Hello, my payment", audio=Audio(b"ogg"), language=Lang.RU)))
    assert stt.languages == [Lang.EN]


def test_unknown_client_language_is_not_guessed():
    stt = FakeSTT("salom")
    run(SupportEngine(FakeLLM(), stt).handle(IncomingMessage("1", audio=Audio(b"ogg"))))
    assert stt.languages == [None]


def test_voice_note_tells_the_model_about_recognition_and_reply_language():
    llm = FakeLLM()
    run(SupportEngine(llm, FakeSTT("SMS kod kelmayapti")).handle(
        IncomingMessage("1", audio=Audio(b"ogg"), language=Lang.UZ_LATN)))
    note = llm.calls[0][3]
    assert "голосового" in note and "uz_latn" in note


def test_cyrillic_client_gets_cyrillic_even_if_speech_came_back_in_latin():
    llm = FakeLLM()
    run(SupportEngine(llm, FakeSTT("SMS kod kelmayapti")).handle(
        IncomingMessage("1", audio=Audio(b"ogg"), language=Lang.UZ_CYRL)))
    assert "uz_cyrl" in llm.calls[0][3]


def test_forbidden_answer_to_a_voice_message_is_still_replaced():
    llm = FakeLLM(answer("Pulingiz 2 soat ichida qaytadi.", lang="uz_latn"))
    r = run(SupportEngine(llm, FakeSTT("pulim qachon qaytadi")).handle(
        IncomingMessage("1", audio=Audio(b"ogg"), language=Lang.UZ_LATN)))
    assert r.guardrail_triggered and r.escalate and "2 soat" not in r.text
