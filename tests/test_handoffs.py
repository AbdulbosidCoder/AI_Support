"""Saved hand-offs and learning from operator answers: nothing reaches the knowledge base without a
person's approval, and an answer breaking a hard rule can never be approved or shown to the model."""
import pytest

from ai_support.config import Settings
from ai_support.handoffs import APPROVED, PENDING, REJECTED, HandoffStore, ReviewError
from ai_support.knowledge import KnowledgeBase
from ai_support.llm import Turn
from ai_support.models import BotReply, Lang
from ai_support.prompt import build_system_prompt

KB = KnowledgeBase.load(Settings().kb_dir)


def escalation(client_text="Karta qo'shilmayapti, SMS kelmayapti", lang=Lang.UZ_LATN, topic="cards/add_card"):
    return BotReply("Operatorga uzatdim.", lang, escalate=True, escalation_reason="no answer in KB",
                    topic=topic, client_text=client_text)


def store_with_handoff(path=":memory:", **kw):
    store = HandoffStore(path)
    hid = store.open("telegram", "42", "-100", "7", escalation(**kw), [Turn("user", "Salom"), Turn("assistant", "?")])
    return store, hid


def test_handoff_survives_restart(tmp_path):
    db = tmp_path / "bot.sqlite3"
    store, hid = store_with_handoff(db)
    store.close()
    found = HandoffStore(db).find("-100", "7")
    assert found is not None and found.id == hid and found.client_chat_id == "42"
    assert HandoffStore(db).find("-100", "8") is None


def test_operator_reply_creates_pending_candidate_not_in_kb():
    store, hid = store_with_handoff()
    c, created = store.add_operator_reply(hid, "SMS-xabarnoma ulangan raqamni tekshiring.", "9", "Op")
    assert created and c.status == PENDING and c.language == "uz_latn" and c.topic == "cards/add_card"
    assert c.question == "Karta qo'shilmayapti, SMS kelmayapti"
    assert store.learned() == []  # not learned until a person approves it


def test_further_replies_extend_the_pending_candidate():
    store, hid = store_with_handoff()
    store.add_operator_reply(hid, "Birinchi qadam.")
    c, created = store.add_operator_reply(hid, "Ikkinchi qadam.")
    assert not created and c.answer == "Birinchi qadam.\nIkkinchi qadam."
    assert store.operator_replies(hid) == ["Birinchi qadam.", "Ikkinchi qadam."]


def test_pii_masked_in_saved_conversation_and_candidate():
    store = HandoffStore(":memory:")
    hid = store.open("telegram", "42", "-100", "7", escalation(client_text="Karta 8600 1234 5678 9012 ishlamayapti"),
                     [Turn("user", "PINFL 12345678901234")])
    c, _ = store.add_operator_reply(hid, "8600 1234 5678 9012 kartangiz balans: 1 250 000, +998 90 123 45 67")
    assert "1234 5678" not in c.question and "8600 **** **** 9012" in c.question
    assert "1234 5678" not in c.answer and "1 250 000" not in c.answer and "123 45 67" not in c.answer
    row = store._db.execute("SELECT context FROM handoffs WHERE id = ?", (hid,)).fetchone()
    assert "12345678901234" not in row["context"]


def test_question_taken_from_conversation_when_client_pressed_operator():
    store, hid = store_with_handoff(client_text="")
    c, _ = store.add_operator_reply(hid, "Javob")
    assert c.question == "Salom"


def test_no_candidate_without_any_question():
    store = HandoffStore(":memory:")
    hid = store.open("telegram", "42", "-100", "7", escalation(client_text=""), [])
    c, created = store.add_operator_reply(hid, "Javob")
    assert c is None and not created and store.operator_replies(hid) == ["Javob"]


def test_approve_adds_to_kb_and_prompt():
    store, hid = store_with_handoff()
    c, _ = store.add_operator_reply(hid, "SMS-xabarnoma ulangan raqamni tekshiring.")
    approved = store.approve(c.id, "@lead")
    assert approved.status == APPROVED
    prompt = build_system_prompt(KB.with_learned(store.learned()))
    assert "## Проверенные ответы операторов" in prompt
    assert "SMS-xabarnoma ulangan raqamni tekshiring." in prompt and "Karta qo'shilmayapti" in prompt


def test_approve_with_edited_answer_replaces_operator_wording():
    store, hid = store_with_handoff()
    c, _ = store.add_operator_reply(hid, "Ali aka, sizning kartangizni tekshirdik.")
    store.approve(c.id, "@lead", "Kartaga SMS-xabarnoma ulanganini tekshiring.")
    assert store.learned()[0]["answer"] == "Kartaga SMS-xabarnoma ulanganini tekshiring."


def test_reject_never_reaches_kb():
    store, hid = store_with_handoff()
    c, _ = store.add_operator_reply(hid, "Javob")
    assert store.reject(c.id, "@lead").status == REJECTED
    assert store.learned() == [] and store.candidates() == []
    with pytest.raises(ReviewError, match="already_reviewed"):
        store.approve(c.id, "@lead")


def test_replies_after_review_do_not_change_candidate():
    store, hid = store_with_handoff()
    c, _ = store.add_operator_reply(hid, "Javob")
    store.approve(c.id, "@lead")
    c2, created = store.add_operator_reply(hid, "Pulingiz 2 soat ichida qaytadi")
    assert not created and c2.answer == "Javob"


def test_unknown_candidate():
    with pytest.raises(ReviewError, match="not_found"):
        HandoffStore(":memory:").approve(99, "@lead")


# Operator answers are often about one client's case ("we refunded you", "unblocked"). Such wording
# must never become a general answer for every client, in any language.
FORBIDDEN_OPERATOR_ANSWERS = [
    "Pulingiz 2 soat ichida qaytadi.",
    "Пулингиз қайтади, кутинг.",
    "Мы вернём деньги в течение 3 дней.",
    "Возврат подтверждён.",
    "Платёж прошёл успешно.",
    "Мы разблокировали вашу карту.",
    "Limit oshirildi.",
    "Your money will be refunded within 24 hours.",
    "We have unblocked your account.",
    "Antifraud блокирует, если больше 5 переводов в день.",
]


@pytest.mark.parametrize("text", FORBIDDEN_OPERATOR_ANSWERS)
def test_forbidden_operator_answer_cannot_be_approved(text):
    store, hid = store_with_handoff()
    c, _ = store.add_operator_reply(hid, text)
    assert c.violations()
    with pytest.raises(ReviewError, match="forbidden"):
        store.approve(c.id, "@lead")
    assert store.learned() == [] and store.candidate(c.id).status == PENDING


@pytest.mark.parametrize("text", FORBIDDEN_OPERATOR_ANSWERS)
def test_forbidden_edited_answer_cannot_be_approved(text):
    store, hid = store_with_handoff()
    c, _ = store.add_operator_reply(hid, "Javob")
    with pytest.raises(ReviewError, match="forbidden"):
        store.approve(c.id, "@lead", text)
    assert store.learned() == []


@pytest.mark.parametrize("text", FORBIDDEN_OPERATOR_ANSWERS)
def test_forbidden_learned_entry_never_rendered_into_prompt(text):
    # Even if such a row got into the database some other way, the model never sees it.
    kb = KB.with_learned([{"id": 1, "language": "ru", "topic": "", "question": "Где деньги?", "answer": text}])
    assert text not in build_system_prompt(kb)


def test_forbidden_answer_approved_after_fixing_it():
    store, hid = store_with_handoff()
    c, _ = store.add_operator_reply(hid, "Pulingiz 2 soat ichida qaytadi.")
    store.approve(c.id, "@lead", "To'lovni takrorlamang, operatsiyani tekshiruvga yuboramiz.")
    assert store.learned()[0]["answer"] == "To'lovni takrorlamang, operatsiyani tekshiruvga yuboramiz."


def test_prompt_without_learned_answers_unchanged():
    assert "Проверенные ответы операторов" not in build_system_prompt(KB)
