"""Client ratings of the bot/operators, assessments of clients, levels and the anonymous rating."""
import asyncio

import pytest

from ai_support import guardrails
from ai_support.engine import SupportEngine
from ai_support.feedback import (
    BOT, OPERATOR, Assessment, FeedbackStore, RatingError, client_level, opens_conversation, parse_rating, render_rating,
)
from ai_support.llm import Turn
from ai_support.models import BotReply, IncomingMessage, Lang
from ai_support.templates import _T, t
from fakes import FakeLLM, FakeSTT, answer


def store():
    return FeedbackStore(":memory:")


def test_parse_rating_values():
    assert parse_rating("5") == (5, "stars") and parse_rating("1") == (1, "stars")
    assert parse_rating("none") == (1, "no_answer") and parse_rating("nohelp") == (1, "not_helped")
    assert parse_rating("6") is None and parse_rating("0") is None and parse_rating("x") is None


def test_only_answers_and_handoffs_open_a_conversation():
    assert opens_conversation(BotReply("ok", Lang.RU, topic="add_card"))
    assert opens_conversation(BotReply("handoff", Lang.RU, escalate=True))
    assert not opens_conversation(BotReply("hi", Lang.RU, topic="greeting", show_menu=True))
    assert not opens_conversation(BotReply(t("voice_unavailable", Lang.RU), Lang.RU))  # fixed error reply, no topic


def ended(s, client, value, handoff=None, operator=None):
    """A conversation that ended and was rated with `value`."""
    s.bot_answered("telegram", client, "ru", "add_card")
    if handoff is not None:
        s.escalated("telegram", client, handoff, "ru")
    if operator is not None:
        s.operator_replied("telegram", client, handoff, "ru", "9", operator)
    _, request = s.end("telegram", client, "client")
    return s.rate(request.id, client, value)


def test_nothing_to_end_without_a_conversation():
    assert store().end("telegram", "42", "client") is None


def test_bot_rated_when_conversation_never_reached_a_person():
    s = store()
    s.bot_answered("telegram", "42", "uz_latn", "add_card")
    s.bot_answered("telegram", "42", "uz_latn", "history")  # same conversation, not a second rating
    conversation, request = s.end("telegram", "42", "client")
    assert request.target == BOT and conversation.topic == "history" and request.language == "uz_latn"
    assert s.end("telegram", "42", "client") is None  # already ended
    assert s.rate(request.id, "42", "4").stars == 4


def test_operator_rated_once_conversation_reached_a_person():
    s = store()
    s.bot_answered("telegram", "42", "ru", "add_card")
    s.escalated("telegram", "42", 7, "ru")
    s.bot_answered("telegram", "42", "ru", "")  # the bot answering meanwhile does not undo the hand-off
    s.operator_replied("telegram", "42", 7, "ru", "9", "Aziza")
    assert s.conversation_for_handoff(7).operator_name == "Aziza"
    _, request = s.end("telegram", "42", "operator")
    assert (request.target, request.handoff_id, request.operator_name) == (OPERATOR, 7, "Aziza")
    assert s.conversation_for_handoff(7) is None


def test_operator_never_answered_can_be_rated_no_answer():
    s = store()
    r = ended(s, "42", "none", handoff=7)
    assert r.target == OPERATOR and r.outcome == "no_answer" and r.stars == 1


def test_operator_reply_after_end_reopens_conversation():
    s = store()
    ended(s, "42", "5", handoff=7, operator="Aziza")
    s.operator_replied("telegram", "42", 7, "ru", "9", "Aziza")
    assert s.conversation("telegram", "42").handoff_id == 7


def test_client_can_change_rating_only_for_themselves():
    s = store()
    r = ended(s, "42", "2")
    assert s.rate(r.id, "42", "5").stars == 5
    with pytest.raises(RatingError):
        s.rate(r.id, "43", "1")
    with pytest.raises(RatingError):
        s.rate(r.id, "42", "9")


def test_app_store_style_score_counts_no_answer_as_one_star():
    s = store()
    for i, value in enumerate(("5", "5", "4", "none", "nohelp")):
        ended(s, value, value, handoff=i, operator="Op")
    score = s.score(OPERATOR)
    assert score.count == 5 and score.average == 3.2
    assert score.by_stars[5] == 2 and score.by_stars[1] == 2 and score.no_answer == 1 and score.not_helped == 1


@pytest.mark.parametrize("tones,code", [
    ([], "new"), (["calm"], "B"), (["polite"], "B"), (["polite", "polite"], "A"), (["polite", "calm"], "A"),
    (["polite", "calm", "calm"], "B"), (["rude"], "C"), (["calm", "calm", "rude"], "C"),
    (["polite"] * 5 + ["rude"], "B"), (["polite"] * 10 + ["rude", "rude"], "C"),
])
def test_client_levels(tones, code):
    assert client_level(tones).code == code


def test_operator_tone_wins_over_ai_for_the_same_case():
    s = store()
    s.assess("telegram", "42", "ai", Assessment("calm"), handoff_id=1)
    s.assess("telegram", "42", OPERATOR, Assessment("rude"), handoff_id=1, assessor="op")
    s.assess("telegram", "42", "ai", Assessment("polite"))  # a case the bot handled alone
    assert sorted(s.tones("telegram", "42")) == ["polite", "rude"]


def test_operator_note_keeps_their_tone():
    s = store()
    s.assess("telegram", "42", OPERATOR, Assessment("polite"), handoff_id=1, assessor="op")
    s.note("telegram", "42", 1, "op", "Долг не обновился", "Добавить квитанцию")
    assert s.tones("telegram", "42") == ["polite"]


def test_assessment_text_is_masked():
    s = store()
    s.assess("telegram", "42", "ai", Assessment("calm", "Карта 8600 1234 5678 9012 не добавляется", ""))
    row = s._db.execute("SELECT problem FROM client_assessments").fetchone()
    assert "8600 1234 5678 9012" not in row["problem"]


def test_unknown_tone_rejected():
    with pytest.raises(ValueError):
        store().assess("telegram", "42", "ai", Assessment("angry"))


def test_rating_is_anonymous():
    s = store()
    ended(s, "998901234567", "5", handoff=1, operator="Aziza")
    s.assess("telegram", "998901234567", OPERATOR, Assessment("rude", "Грубил"), handoff_id=1, assessor="Aziza")
    text = render_rating(s)
    assert "998901234567" not in text and "Грубил" not in text
    assert "5.0 ★★★★★" in text and "Aziza: 5.0★ (1)" in text and "C · требует внимания: 1" in text


def test_empty_rating():
    assert "оценок пока нет" in render_rating(store())


# --- hard rules: rating texts promise nothing, assessments never change answers ----------------

@pytest.mark.parametrize("key", [k for k in _T if k.startswith("rate_")])
@pytest.mark.parametrize("lang", list(Lang))
def test_rating_texts_break_no_hard_rule(key, lang):
    assert guardrails.find_violations(t(key, lang)) == []


def test_engine_assessment_uses_history_and_does_not_change_answers():
    llm = FakeLLM(answer("Kartalarim bo'limiga kiring.", "uz_latn", topic="add_card"),
                  assessment=Assessment("rude", "Карта", ""))
    engine = SupportEngine(llm, FakeSTT())
    assert asyncio.run(engine.assess_client("42")) is None and llm.assessed == []  # nothing to judge yet
    first = asyncio.run(engine.handle(IncomingMessage("42", "Karta qo'shilmayapti")))
    assert asyncio.run(engine.assess_client("42")).tone == "rude"
    assert llm.assessed[0][0] == Turn("user", "Karta qo'shilmayapti")
    again = asyncio.run(engine.handle(IncomingMessage("42", "Karta qo'shilmayapti")))
    assert again.text == first.text and again.escalate == first.escalate


def test_engine_assessment_failure_is_quiet():
    engine = SupportEngine(FakeLLM(), FakeSTT())
    asyncio.run(engine.handle(IncomingMessage("42", "Karta qo'shilmayapti")))
    engine._llm.error = "down"
    assert asyncio.run(engine.assess_client("42")) is None
