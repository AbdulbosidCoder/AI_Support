"""When a conversation closes (client, operator or SESSION_IDLE_MINUTES of silence), the client rates the
service and the AI and the operator assess the client. Nothing of the assessment reaches the client."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from ai_support.channels.telegram_bot import TelegramSupportBot
from ai_support.chatlog import ChatLog
from ai_support.config import Settings
from ai_support.engine import SupportEngine
from ai_support.feedback import Assessment, FeedbackStore
from ai_support.handoffs import HandoffStore
from ai_support.models import Lang
from ai_support.templates import t
from fakes import FakeLLM, FakeSTT, answer
from test_telegram import LEAKS, ClientChat, client_msg, end_callback, operator_msg, registered

SUPPORT = -100123
CLIENT = 42


class TG:
    """Telegram stand-in: records everything sent, to the client and to the support chat."""

    def __init__(self):
        self.sent = []
        self._next = 100

    async def send_message(self, chat, text, reply_markup=None, reply_to_message_id=None, **_):
        self._next += 1
        self.sent.append(NS(chat=chat, text=text, markup=reply_markup, reply_to=reply_to_message_id, id=self._next))
        return NS(message_id=self._next)

    async def send_chat_action(self, *_):
        pass

    async def forward_message(self, chat, *_):
        self._next += 1
        return NS(message_id=self._next)

    async def delete_message(self, *_):
        pass

    async def edit_message_reply_markup(self, *_, **__):
        pass

    def to(self, chat):
        return [m for m in self.sent if m.chat == chat]


def make(escalate=False, idle=30):
    llm = FakeLLM(answer("Передаю специалисту." if escalate else "Kartalarim bo'limiga kiring.",
                         "ru" if escalate else "uz_latn", escalate=escalate, reason="no answer" if escalate else ""),
                  assessment=Assessment("rude", "Квартплата не обновилась", ""))
    feedback, handoffs, chatlog = FeedbackStore(":memory:"), HandoffStore(":memory:"), ChatLog(":memory:")
    bot = TelegramSupportBot(Settings(support_chat_id=SUPPORT, operator_first=False, session_idle_minutes=idle),
                             SupportEngine(llm, FakeSTT()), registered(), handoffs, feedback, chatlog)
    chat, tg = ClientChat(), TG()
    asyncio.run(bot.on_client_message(client_msg(chat, "Квартплата не обновилась"), tg))
    return bot, feedback, llm, chat, tg


def with_operator():
    bot, feedback, llm, chat, tg = make(escalate=True)
    post = tg.to(SUPPORT)[0].id
    asyncio.run(bot.on_operator_reply(operator_msg(ClientChat(), "Проверьте раздел «Tarix».", post), tg))
    return bot, feedback, llm, chat, tg, post


def later(minutes):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def tone_tap(message_id, tone="polite"):
    answers = []

    async def answer_(text=None, **_):
        answers.append(text)
    return NS(data=f"ctone:{tone}", from_user=NS(id=9, username="op", full_name="Operator"),
              message=NS(chat=NS(id=SUPPORT), message_id=message_id), answer=answer_), answers


def test_client_ends_operator_conversation_then_operator_and_ai_assess():
    bot, feedback, llm, chat, tg, post = with_operator()
    assessed_before = len(llm.assessed)
    asyncio.run(bot.on_end_button(end_callback(chat), tg))
    rating = tg.to(CLIENT)[-1]
    assert rating.text == t("rate_operator", Lang.RU)
    # The AI assessed the whole conversation, the operator's answer included.
    assert len(llm.assessed) == assessed_before + 1
    assert any("Tarix" in turn.text for turn in llm.assessed[-1])
    # The operator is asked to assess the client, under the escalation post, after the client's rating prompt.
    ask = tg.to(SUPPORT)[-1]
    assert tg.sent.index(ask) > tg.sent.index(rating)
    assert ask.reply_to == post and "Оцените клиента" in ask.text and "завершён — клиент" in ask.text
    assert "Квартплата не обновилась" in ask.text  # the AI's view of the case
    buttons = [b.callback_data for row in ask.markup.inline_keyboard for b in row]
    assert buttons == ["ctone:polite", "ctone:calm", "ctone:rude", "hnote"]
    # Tapping a tone under that post saves the operator's assessment of this case.
    cb, answers = tone_tap(ask.id, "polite")
    asyncio.run(bot.on_client_tone(cb))
    assert answers[-1].startswith("Сохранено: вежливо")
    assert feedback.tones("telegram", str(CLIENT)) == ["polite"]  # the operator's tone wins over the AI's
    # Nothing of the assessment reached the client.
    assert not any(any(w in m.text for w in LEAKS) for m in tg.to(CLIENT))


def test_operator_ends_conversation_asks_operator_to_assess():
    bot, feedback, llm, chat, tg, post = with_operator()
    cb, answers = tone_tap(post)
    cb.data = "hend"
    asyncio.run(bot.on_operator_end_button(cb, tg))
    assert tg.to(CLIENT)[-1].text == f"{t('ended_by_operator', Lang.RU)}\n{t('rate_operator', Lang.RU)}"
    ask = tg.to(SUPPORT)[-1]
    assert "завершён — специалист" in ask.text and ask.reply_to == post


def test_bot_only_conversation_ai_assesses_and_no_operator_post():
    bot, feedback, llm, chat, tg = make()
    support_posts = len(tg.to(SUPPORT))
    asyncio.run(bot.on_end_button(end_callback(chat), tg))
    assert tg.to(CLIENT)[-1].text == t("rate_bot", Lang.UZ_LATN)
    assert len(llm.assessed) == 1 and feedback.tones("telegram", str(CLIENT)) == ["rude"]
    assert len(tg.to(SUPPORT)) == support_posts


def test_idle_conversation_closes_and_asks_for_rating():
    bot, feedback, llm, chat, tg = make()
    assert asyncio.run(bot.expire_idle(tg, later(29))) == 0
    assert feedback.conversation("telegram", str(CLIENT)) is not None
    assert asyncio.run(bot.expire_idle(tg, later(31))) == 1
    assert feedback.conversation("telegram", str(CLIENT)) is None
    assert tg.to(CLIENT)[-1].text == f"{t('ended_by_timeout', Lang.UZ_LATN)}\n{t('rate_bot', Lang.UZ_LATN)}"
    assert len(llm.assessed) == 1
    row = feedback._db.execute("SELECT closed_by FROM conversations").fetchone()
    assert row["closed_by"] == "timeout"
    assert asyncio.run(bot.expire_idle(tg, later(120))) == 0  # closed once


def test_idle_operator_conversation_closes_and_operator_assesses():
    bot, feedback, llm, chat, tg, post = with_operator()
    assert asyncio.run(bot.expire_idle(tg, later(31))) == 1
    assert tg.to(CLIENT)[-1].text == f"{t('ended_by_timeout', Lang.RU)}\n{t('rate_operator', Lang.RU)}"
    ask = tg.to(SUPPORT)[-1]
    assert "автоматически, нет сообщений 30 мин" in ask.text and ask.reply_to == post
    # The operator is free for the next client.
    assert bot.handoffs.busy_operators(30) == {}


def test_idle_close_can_be_turned_off():
    bot, feedback, llm, chat, tg = make(idle=0)
    assert asyncio.run(bot.expire_idle(tg, later(600))) == 0
    assert feedback.conversation("telegram", str(CLIENT)) is not None


def test_timeout_texts_promise_nothing():
    for lang in Lang:
        text = t("ended_by_timeout", lang).lower()
        assert not any(w in text for w in ("qaytadi", "вернутся", "refund", "soat", "час", "hour", "blok", "блок"))
