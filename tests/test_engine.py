import asyncio

from ai_support.engine import SupportEngine
from ai_support.models import Audio, Image, IncomingMessage, Lang
from ai_support.templates import t
from fakes import FakeLLM, FakeSTT, answer, png


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
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "", images=[Image(png())]))
    assert t("pii_reminder", Lang.UZ_LATN) in r.text
    assert r.screen_id == "card_add_form"


def test_image_is_passed_to_model():
    llm = FakeLLM()
    data = png()
    run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "что это?", images=[Image(data)]))
    assert llm.calls[0][2][0].data == data
    assert llm.calls[0][2][0].media_type == "image/png"
    assert llm.calls[0][3] == ""  # caption present: no extra note


def test_image_without_caption_is_answered_from_the_image():
    llm = FakeLLM(answer("Это ошибка камеры.", screen="auth_phone_error_camera_permission"))
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "", images=[Image(png())]))
    _, text, images, note = llm.calls[0]
    assert text == "" and len(images) == 1
    assert "uz_latn" in note  # default reply language hint when nothing else is known
    assert r.text == "Это ошибка камеры."


def test_image_without_caption_keeps_previous_language():
    llm = FakeLLM(answer("Ок", lang="ru"))
    engine = SupportEngine(llm, FakeSTT())
    run(engine, IncomingMessage("1", "Здравствуйте, у меня ошибка"))
    run(engine, IncomingMessage("1", "", images=[Image(png())]))
    assert "ru" in llm.calls[1][3]


def test_caption_with_image_goes_as_text():
    llm = FakeLLM()
    run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "nega bu chiqyapti?", images=[Image(png())]))
    assert llm.calls[0][1] == "nega bu chiqyapti?"


def test_unreadable_image_without_text():
    llm = FakeLLM()
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "", images=[Image(b"not an image")]))
    assert r.text == t("image_unsupported", Lang.UZ_LATN)
    assert not llm.calls


def test_unreadable_image_with_text_still_answers_text():
    llm = FakeLLM()
    run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "SMS kelmayapti", images=[Image(b"bad")]))
    assert llm.calls[0][1] == "SMS kelmayapti" and llm.calls[0][2] == []


def test_album_images_are_limited():
    llm = FakeLLM()
    run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "", images=[Image(png()) for _ in range(8)]))
    assert len(llm.calls[0][2]) == 5


def test_image_turn_is_remembered_in_history():
    llm = FakeLLM(answer("Ок", screen="card_add_form"))
    engine = SupportEngine(llm, FakeSTT())
    run(engine, IncomingMessage("1", "", images=[Image(png())]))
    run(engine, IncomingMessage("1", "а дальше?"))
    assert "card_add_form" in llm.calls[1][0][0].text


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


import pytest  # noqa: E402

from ai_support.language import is_small_talk  # noqa: E402


@pytest.mark.parametrize("text,lang", [
    ("Salom", Lang.UZ_LATN), ("Assalomu alaykum!", Lang.UZ_LATN), ("menga yordam kerak", Lang.UZ_LATN),
    ("Ассалому алайкум", Lang.UZ_CYRL), ("Привет", Lang.RU), ("Здравствуйте, нужна помощь", Lang.RU),
    ("hi", Lang.EN), ("Hello, I need help", Lang.EN),
])
def test_greeting_asks_problem_briefly_without_model(text, lang):
    llm = FakeLLM()
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", text))
    assert not llm.calls and r.show_menu and not r.escalate
    assert r.text == f"{t('ask_problem', lang)}\n\n{t('menu_hint', lang)}"
    assert len(r.text) < 120  # no list of capabilities


@pytest.mark.parametrize("text", [
    "Salom, kartam bloklandi", "Привет, не приходит SMS", "hi, my payment failed", "salom pul qaytaring",
    "Здравствуйте, верните деньги",
])
def test_greeting_with_problem_goes_to_model(text):
    assert not is_small_talk(text)
    llm = FakeLLM(answer("Ok"))
    run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", text))
    assert llm.calls


def test_model_greeting_topic_shows_menu():
    llm = FakeLLM(answer("Какая у вас проблема?", topic="greeting"))
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "ну вот такое дело"))
    assert r.show_menu


def test_screenshot_without_caption_is_not_small_talk():
    llm = FakeLLM(answer("Это экран добавления карты.", screen="card_add_form"))
    r = run(SupportEngine(llm, FakeSTT()), IncomingMessage("1", "", images=[Image(png())]))
    assert llm.calls and not r.show_menu
