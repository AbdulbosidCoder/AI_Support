import asyncio

from ai_support.engine import SupportEngine
from ai_support.models import Audio, Image, IncomingMessage, Lang
from ai_support.templates import t
from fakes import FakeLLM, FakeSTT, answer


def run(engine, msg):
    return asyncio.run(engine.handle(msg))


def test_forbidden_model_answer_is_replaced_and_escalated():
    llm = FakeLLM(answer("Pulingiz 2 soat ichida qaytadi.", lang="uz_latn"))
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "pulim qayerda?"))
    assert "2 soat" not in r.text
    assert r.text == t("guardrail", Lang.UZ_LATN)
    assert r.escalate and r.guardrail_triggered


def test_restricted_request_always_escalates_even_if_model_does_not():
    llm = FakeLLM(answer("Я не могу отменить платёж сам.", escalate=False))
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "Отмените платёж"))
    assert r.escalate
    assert t("handoff", Lang.RU) in r.text


def test_pii_in_model_answer_is_masked():
    llm = FakeLLM(answer("Ваша карта 8600 1234 5678 9012 добавлена? Проверьте «Kartalarim»."))
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "карта 8600 1234 5678 9012"))
    assert "1234 5678" not in r.text
    assert "1234 5678" not in r.client_text


def test_pii_on_screenshot_adds_reminder():
    llm = FakeLLM(answer("Это экран добавления карты.", lang="uz_latn", pii=True, screen="card_add_form"))
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "", images=[Image(b"x")]))
    assert t("pii_reminder", Lang.UZ_LATN) in r.text
    assert r.screen_id == "card_add_form"


def test_image_is_passed_to_model():
    llm = FakeLLM()
    run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "что это?", images=[Image(b"img")]))
    assert llm.calls[0][2][0].data == b"img"


def test_llm_failure_hands_off():
    r = run(SupportEngine(FakeLLM(error="down"), FakeSTT()), IncomingMessage("1", "Здравствуйте, не работает"))
    assert r.escalate and r.text == t("error", Lang.RU)


def test_voice_without_stt_asks_for_text():
    llm = FakeLLM()
    r = run(SupportEngine(llm, FakeSTT(None)), IncomingMessage("1", audio=Audio(b"ogg")))
    assert r.text == t("voice_unavailable", Lang.UZ_LATN)
    assert not llm.calls


def test_voice_transcript_goes_to_model():
    llm = FakeLLM()
    run(SupportEngine(llm, FakeSTT("SMS kod kelmayapti")), IncomingMessage("1", audio=Audio(b"ogg")))
    assert llm.calls[0][1] == "SMS kod kelmayapti"


def test_history_is_kept_masked_and_starts_with_user():
    llm = FakeLLM()
    engine = SupportEngine(llm, FakeSTT())
    run(engine, IncomingMessage("1", "мой номер +998 93 069 14 81"))
    run(engine, IncomingMessage("1", "и что дальше?"))
    history = llm.calls[1][0]
    assert history[0].role == "user" and "069" not in history[0].text


def test_model_language_is_used_for_templates():
    llm = FakeLLM(answer("Карта временно ограничена.", lang="uz_cyrl", escalate=True))
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "картам блокланди"))
    assert r.language == Lang.UZ_CYRL and t("handoff", Lang.UZ_CYRL) in r.text


def test_empty_message():
    r = run(SupportEngine(FakeLLM(), FakeSTT()), IncomingMessage("1", "  "))
    assert r.text == t("empty", Lang.UZ_LATN)


def test_explicit_operator_request():
    r = SupportEngine(FakeLLM(), FakeSTT()).handoff("1")
    assert r.escalate
