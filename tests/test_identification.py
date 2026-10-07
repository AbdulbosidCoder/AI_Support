"""Инструкция команды «Xonsaroy Pay ilovasida identifikatsiyadan o'tish» в базе знаний."""
import pytest

from ai_support.config import Settings
from ai_support.guardrails import find_violations
from ai_support.knowledge import KnowledgeBase
from ai_support.prompt import build_system_prompt

KB = KnowledgeBase.load(Settings().kb_dir)
TOPICS = {f'{t["section"]}/{t["id"]}': t for t in KB.topics}
TOPIC = TOPICS["registration/identification"]
LANGS = ["uz_latn", "uz_cyrl", "ru", "en"]


def test_topic_has_five_requirements_note_and_call_center():
    steps = TOPIC["answer_steps"]
    for word in ["Номер телефона", "SIM-карта", "Паспорт или ID-карта", "Паспортные данные", "лица"]:
        assert any(word in s for s in steps[:5]), word
    assert "одному человеку" in steps[5]
    assert any("Call-центр" in s for s in steps)


@pytest.mark.parametrize("lang", LANGS)
def test_official_text_in_every_language(lang):
    text = TOPIC["official_text"][lang]
    for n in range(1, 6):
        assert f"{n})" in text, (lang, n)
    assert "Xonsaroy Pay" in text


def test_official_uz_latn_keeps_team_wording():
    text = TOPIC["official_text"]["uz_latn"]
    assert "Telefon raqami, pasport ma’lumotlari va yuz bir shaxsga tegishli bo‘lishi kerak" in text
    assert "Xonsaroy Pay Call-markaziga murojaat qilishingiz mumkin" in text


def test_identification_texts_have_no_forbidden_phrases():
    texts = [TOPIC["title"], *TOPIC["answer_steps"], *TOPIC["official_text"].values()]
    for text in texts:
        assert not find_violations(text), text


def test_identification_escalates_and_forbids_promises():
    assert TOPIC["escalate_if"]
    forbidden = " ".join(TOPIC["forbidden"])
    assert "причины отказа" in forbidden
    assert "Не обещать" in forbidden


def test_myid_points_to_requirements():
    assert any("registration/identification" in s for s in TOPICS["registration/myid"]["answer_steps"])


def test_prompt_renders_official_text():
    p = build_system_prompt(KB)
    assert "topic=registration/identification" in p
    assert "Официальный текст команды" in p
    for lang in LANGS:
        assert f"[{lang}] " + TOPIC["official_text"][lang] in p
