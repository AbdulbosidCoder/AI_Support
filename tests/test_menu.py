"""Menu and language choice: every label resolves, and no fixed text breaks a hard rule."""
import asyncio

import pytest

from ai_support import guardrails
from ai_support.engine import SupportEngine
from ai_support.menu import (
    CHANGE_LANGUAGE_LABEL, CHOOSE_LANGUAGE, LANGUAGE_CHOICES, OPERATOR_LABEL, QUICK_QUESTIONS, SETTINGS_LABEL,
    match_menu, menu_rows,
)
from ai_support.models import IncomingMessage, Lang
from ai_support.templates import t
from fakes import FakeLLM, FakeSTT, answer


def test_every_language_offered_and_greeting_in_three_languages():
    assert [lang for lang, _ in LANGUAGE_CHOICES] == list(Lang)
    assert LANGUAGE_CHOICES[0][0] == Lang.UZ_LATN  # Uzbek first: the main language
    for word in ("Assalomu alaykum", "Здравствуйте", "Hello"):
        assert word in CHOOSE_LANGUAGE


@pytest.mark.parametrize("lang", list(Lang))
def test_menu_has_all_buttons_in_each_language(lang):
    flat = [label for row in menu_rows(lang) for label in row]
    assert len(flat) == len(QUICK_QUESTIONS) + 2 and len(set(flat)) == len(flat)
    assert all(len(row) <= 2 for row in menu_rows(lang))
    assert match_menu(OPERATOR_LABEL[lang]).kind == "operator"
    assert match_menu(SETTINGS_LABEL[lang]).kind == "settings"
    for q in QUICK_QUESTIONS:
        assert match_menu(q.label[lang]).question is q
        assert q.question[lang].strip()


def test_free_text_is_not_a_menu_button():
    for text in ("", "Karta qo'shish qanday?", "оператор", "Settings please"):
        assert match_menu(text) is None


def _fixed_texts():
    texts = [CHOOSE_LANGUAGE] + [label for _, label in LANGUAGE_CHOICES]
    for lang in Lang:
        texts += [OPERATOR_LABEL[lang], SETTINGS_LABEL[lang], CHANGE_LANGUAGE_LABEL[lang]]
        texts += [t(k, lang) for k in ("welcome", "menu_hint", "language_saved", "settings")]
        for q in QUICK_QUESTIONS:
            texts += [q.label[lang], q.question[lang]]
    return texts


@pytest.mark.parametrize("text", _fixed_texts())
def test_fixed_menu_texts_have_no_forbidden_answers(text):
    assert guardrails.find_violations(text) == []


@pytest.mark.parametrize("q", QUICK_QUESTIONS, ids=lambda q: q.id)
def test_quick_questions_are_not_restricted_requests(q):
    # A button press must not look like a refund/unblock/limit request by itself.
    for lang in Lang:
        assert guardrails.restricted_request(q.question[lang]) is None


FORBIDDEN_MODEL_ANSWERS = [
    ("payment_problem", "Pulingiz 2 soat ichida qaytadi.", Lang.UZ_LATN),
    ("transfer_not_received", "Перевод успешно завершён, деньги у получателя.", Lang.RU),
    ("payment_problem", "Your money will be refunded within 24 hours.", Lang.EN),
    ("add_card", "Kartangizni blokdan chiqardik.", Lang.UZ_LATN),
]


@pytest.mark.parametrize("qid,bad,lang", FORBIDDEN_MODEL_ANSWERS)
def test_quick_question_answer_still_goes_through_guardrails(qid, bad, lang):
    q = next(q for q in QUICK_QUESTIONS if q.id == qid)
    llm = FakeLLM(answer(bad, lang.value))
    engine = SupportEngine(llm, FakeSTT())
    reply = asyncio.run(engine.handle(IncomingMessage("1", q.question[lang])))
    assert llm.calls[0][1] == q.question[lang]
    assert reply.text == t("guardrail", lang) and reply.escalate and reply.guardrail_triggered
