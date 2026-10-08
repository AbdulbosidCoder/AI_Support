"""A new conversation goes to a free operator first; the AI takes over when nobody is free or the operator is silent."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from ai_support import guardrails
from ai_support.channels.telegram_bot import TelegramSupportBot, operator_mention
from ai_support.config import Settings
from ai_support.engine import SupportEngine
from ai_support.feedback import FeedbackStore
from ai_support.handoffs import WAITING, WITH_AI, WITH_OPERATOR, HandoffStore
from ai_support.models import BotReply, Lang
from ai_support.operators import Operator, pick_free
from ai_support.templates import t
from fakes import FakeLLM, FakeSTT, answer

SUPPORT = -100123


class TG:
    """Telegram stand-in: records everything sent, to clients and to the support chat."""

    def __init__(self):
        self.sent = []
        self._next = 100

    async def send_message(self, chat, text, reply_markup=None, entities=None, **_):
        self._next += 1
        self.sent.append(NS(chat=chat, text=text, markup=reply_markup, entities=entities, id=self._next))
        return NS(message_id=self._next)

    async def send_chat_action(self, *_):
        pass

    async def forward_message(self, chat, *_):
        self._next += 1
        return NS(message_id=self._next)

    async def edit_message_reply_markup(self, **_):
        pass

    def to(self, chat):
        return [m.text for m in self.sent if m.chat == chat]


class Client:
    def __init__(self, uid):
        self.uid = uid
        self.sent = []
        self._next = 0

    async def answer(self, text, reply_markup=None, **_):
        self.sent.append(text)
        self._next += 1
        return NS(message_id=self._next)

    def msg(self, text):
        async def react(*_):
            pass
        return NS(text=text, caption=None, photo=None, document=None, reply_to_message=None, voice=None, audio=None,
                  media_group_id=None, chat=NS(id=self.uid), message_id=1, answer=self.answer, react=react,
                  from_user=NS(id=self.uid, username=f"c{self.uid}", full_name=f"Client {self.uid}",
                               language_code="ru"))


def make(llm=None, operators=(("9", "Dilnoza", None),), first=True, wait=60, handoffs=None):
    llm = llm or FakeLLM(answer("Kartalarim bo'limiga kiring.", "uz_latn", topic="add_card"))
    from test_telegram import registered
    users = registered(42)
    for uid in (43, 44):
        registered(uid, users=users)
    bot = TelegramSupportBot(Settings(support_chat_id=SUPPORT, operator_first=first, operator_wait_seconds=wait),
                             SupportEngine(llm, FakeSTT()), users, handoffs or HandoffStore(":memory:"),
                             FeedbackStore(":memory:"))
    for uid, name, username in operators:
        bot.operators.add(uid, name, username)
    return bot, llm, TG()


def later(seconds=61):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def operator_reply(text, post, uid=9):
    async def answer(*_, **__):
        pass
    return NS(text=text, caption=None, chat=NS(id=SUPPORT), reply_to_message=NS(message_id=post),
              from_user=NS(id=uid, username=None, full_name="Dilnoza"), answer=answer)


def say(bot, tg, client, text):
    asyncio.run(bot.on_client_message(client.msg(text), tg))


def test_new_conversation_goes_to_a_free_operator_not_the_model():
    bot, llm, tg = make()
    client = Client(42)
    say(bot, tg, client, "Karta qo'shilmayapti")
    assert llm.calls == []
    assert client.sent[-1] == f"{t('assistant_name', Lang.UZ_LATN)}:\n{t('connecting_operator', Lang.UZ_LATN)}"
    post = tg.sent[-1]
    assert post.chat == SUPPORT and post.text.startswith("Dilnoza, новое обращение. Ответьте в течение 60 с")
    # No username: Telegram notifies the operator through a text mention at the start of the post.
    assert post.entities[0].type == "text_mention" and post.entities[0].user.id == 9
    h = bot.handoffs.get(1)
    assert (h.status, h.assigned_id, h.client_text) == (WAITING, "9", "Karta qo'shilmayapti")
    # While waiting, the client's messages go to the operator.
    say(bot, tg, client, "Yordam bering")
    assert llm.calls == [] and tg.sent[-1].text.startswith("💬 Клиент (эскалация #1)")


def test_operator_answers_in_time_and_keeps_the_conversation():
    bot, llm, tg = make()
    client = Client(42)
    say(bot, tg, client, "Karta qo'shilmayapti")
    post = tg.sent[-1].id
    asyncio.run(bot.on_operator_reply(operator_reply("Qaysi karta?", post), tg))
    assert tg.to(42)[-1] == f"{t('operator_name', Lang.UZ_LATN)}:\nQaysi karta?"
    assert asyncio.run(bot.expire_waits(tg, later())) == 0
    assert bot.handoffs.get(1).status == WITH_OPERATOR and llm.calls == []


def test_silent_operator_ai_greets_and_answers_what_the_client_asked():
    bot, llm, tg = make()
    client = Client(42)
    say(bot, tg, client, "Karta qo'shilmayapti")
    say(bot, tg, client, "Humo karta")
    assert asyncio.run(bot.expire_waits(tg, later(30))) == 0  # not yet
    assert asyncio.run(bot.expire_waits(tg, later())) == 1
    assert bot.handoffs.get(1).status == WITH_AI
    assert tg.to(42)[-1] == f"{t('assistant_name', Lang.UZ_LATN)}:\n{t('ai_takeover', Lang.UZ_LATN)}"
    # The model answers everything the client wrote while waiting, in one go.
    assert llm.calls[-1][1] == "Karta qo'shilmayapti\nHumo karta"
    assert client.sent[-1] == f"{t('assistant_name', Lang.UZ_LATN)}:\nKartalarim bo'limiga kiring."
    assert any(text.startswith("⏱ Dilnoza не ответил за 60 с") for text in tg.to(SUPPORT))
    # From now on the AI has the conversation and is rated for it.
    conversation = bot.feedback.conversation("telegram", "42")
    assert conversation.handoff_id is None and conversation.target == "bot"
    say(bot, tg, client, "Yana savol")
    assert len(llm.calls) == 2
    # The operator is free again for the next client.
    assert bot.free_operator().user_id == "9"


def test_silent_operator_after_a_greeting_ai_asks_how_it_can_help():
    bot, llm, tg = make()
    client = Client(42)
    say(bot, tg, client, "Salom")
    asyncio.run(bot.expire_waits(tg, later()))
    greeting = tg.sent[-1]
    assert greeting.chat == 42 and llm.calls == []
    assert greeting.text == (f"{t('assistant_name', Lang.UZ_LATN)}:\n{t('ai_takeover', Lang.UZ_LATN)}\n\n"
                             f"{t('ai_how_help', Lang.UZ_LATN)}")
    assert greeting.markup.inline_keyboard  # the topics menu
    assert "Assalomu" not in t("ai_takeover", Lang.UZ_LATN)  # the client was greeted on /start already


def test_all_operators_busy_ai_greets_at_once():
    bot, llm, tg = make()
    first, second = Client(42), Client(43)
    say(bot, tg, first, "Karta qo'shilmayapti")
    posts = len(tg.to(SUPPORT))
    say(bot, tg, second, "To'lov o'tmadi")
    assert len(tg.to(SUPPORT)) == posts  # nobody to connect: no post
    assert tg.to(43)[-1] == f"{t('assistant_name', Lang.UZ_LATN)}:\n{t('ai_takeover', Lang.UZ_LATN)}"
    assert llm.calls[-1][1] == "To'lov o'tmadi" and second.sent[-1].endswith("Kartalarim bo'limiga kiring.")
    # The next message continues with the AI, without a second greeting.
    say(bot, tg, second, "Yana")
    assert len(llm.calls) == 2 and tg.to(43).count(tg.to(43)[-1]) == 1


def test_busy_greeting_only_asks_when_the_client_just_said_hello():
    bot, llm, tg = make()
    say(bot, tg, Client(42), "Karta qo'shilmayapti")
    say(bot, tg, Client(43), "Salom")
    assert llm.calls == [] and tg.to(43)[-1].endswith(t("ai_how_help", Lang.UZ_LATN))


def test_next_client_goes_to_the_other_free_operator():
    bot, llm, tg = make(operators=(("9", "Dilnoza", None), ("10", "Aziz", "aziz_op")))
    say(bot, tg, Client(42), "Karta qo'shilmayapti")
    say(bot, tg, Client(43), "To'lov o'tmadi")
    assert [bot.handoffs.get(i).assigned_id for i in (1, 2)] == ["9", "10"]
    assert tg.to(SUPPORT)[-1].startswith("@aziz_op, новое обращение")
    say(bot, tg, Client(44), "Salom")
    assert llm.calls == [] and tg.to(44)[-1].endswith(t("ai_how_help", Lang.UZ_LATN))


def test_late_operator_reply_takes_the_conversation_back_from_the_ai():
    bot, llm, tg = make()
    client = Client(42)
    say(bot, tg, client, "Karta qo'shilmayapti")
    post = tg.sent[-1].id
    asyncio.run(bot.expire_waits(tg, later()))
    asyncio.run(bot.on_operator_reply(operator_reply("Kechirasiz, men shu yerdaman.", post), tg))
    assert tg.to(42)[-1] == f"{t('operator_name', Lang.UZ_LATN)}:\nKechirasiz, men shu yerdaman."
    assert bot.handoffs.get(1).status == WITH_OPERATOR
    calls = len(llm.calls)
    say(bot, tg, client, "Humo karta")
    assert len(llm.calls) == calls and tg.sent[-1].text.startswith("💬 Клиент")
    assert bot.feedback.conversation("telegram", "42").target == "operator"
    assert bot.free_operator() is None  # busy with this client again


def test_ending_the_conversation_frees_the_operator():
    bot, llm, tg = make()
    say(bot, tg, Client(42), "Karta qo'shilmayapti")
    assert bot.free_operator() is None
    asyncio.run(bot._end(tg, 42, "client"))
    assert bot.free_operator().user_id == "9"
    assert asyncio.run(bot.expire_waits(tg, later())) == 0  # nothing waits any more
    say(bot, tg, Client(43), "To'lov o'tmadi")
    assert bot.handoffs.get(2).assigned_id == "9" and llm.calls == []


def test_quick_question_also_goes_to_the_operator_first():
    from test_telegram import Chat, callback
    bot, llm, tg = make()
    cb = callback(Chat(), "q:history")
    cb.message.__dict__.update(photo=None, voice=None, document=None, audio=None)  # as on a real Message
    asyncio.run(bot.on_quick_question(cb, tg))
    assert llm.calls == [] and bot.handoffs.get(1).status == WAITING


def test_without_operators_or_switched_off_the_ai_answers_first():
    for kwargs in ({"operators": ()}, {"first": False}):
        bot, llm, tg = make(**kwargs)
        client = Client(42)
        say(bot, tg, client, "Karta qo'shilmayapti")
        assert len(llm.calls) == 1 and t("ai_takeover", Lang.UZ_LATN) not in tg.to(42)


def test_inactive_or_unregistered_operators_are_not_connected():
    bot, llm, tg = make(operators=())
    bot.operators.add_phone("+998901112233", "Kamola")  # added, not registered in the bot yet
    bot.operators.add("11", "Old")
    bot.operators.set_active("11", False)
    say(bot, tg, Client(42), "Karta qo'shilmayapti")
    assert len(llm.calls) == 1


def test_ai_escalation_is_assigned_to_a_free_operator_without_a_timeout():
    llm = FakeLLM(answer("Передаю специалисту.", "ru", escalate=True, reason="no answer"))
    bot, _, tg = make(llm=llm, first=False)
    say(bot, tg, Client(42), "Квартплата не обновилась")
    h = bot.handoffs.get(1)
    assert (h.assigned_id, h.wait_until) == ("9", None)
    assert tg.to(SUPPORT)[0].startswith("Dilnoza, обращение назначено вам.")
    assert asyncio.run(bot.expire_waits(tg, later(3600))) == 0


# --- hard rules still hold after the AI takes over ---------------------------------------------

@pytest.mark.parametrize("bad", [
    "Pulingiz 2 soat ichida qaytadi.",
    "To'lov muvaffaqiyatli o'tdi, pul qaytarildi.",
    "Kartangiz blokdan chiqarildi.",
])
def test_forbidden_answer_replaced_after_ai_takeover(bad):
    bot, llm, tg = make(llm=FakeLLM(answer(bad, "uz_latn", topic="p2p_pending")))
    client = Client(42)
    say(bot, tg, client, "Pulim qachon qaytadi?")
    asyncio.run(bot.expire_waits(tg, later()))
    assert all(bad not in text for text in client.sent + tg.to(42))
    assert client.sent[-1].startswith(f"{t('assistant_name', Lang.UZ_LATN)}:\n{t('guardrail', Lang.UZ_LATN)}")


@pytest.mark.parametrize("key", ["connecting_operator", "ai_takeover", "ai_how_help"])
@pytest.mark.parametrize("lang", list(Lang))
def test_operator_first_texts_break_no_hard_rule(key, lang):
    assert guardrails.find_violations(t(key, lang)) == []


# --- the store and the choice of operator --------------------------------------------------------

def reply():
    return BotReply("x", Lang.RU, escalate=True, client_text="q")


def test_idle_conversation_no_longer_keeps_the_operator_busy():
    store = HandoffStore(":memory:")
    hid = store.open("telegram", "42", "-1", "5", reply())
    store.assign(hid, "9", "Dilnoza", 60)
    assert store.busy_operators(30) == {"9": 1}
    assert store.busy_operators(30, now=datetime.now(timezone.utc) + timedelta(minutes=31)) == {}
    assert store.to_ai(hid) and not store.to_ai(hid)  # only once
    assert store.busy_operators(30) == {}


def test_pick_free_least_loaded_then_round_robin():
    ops = [Operator(uid, uid, None, True, None, "") for uid in ("1", "2", "3")]
    assert pick_free(ops, {}, {"1": 5, "2": 3}).user_id == "3"  # never assigned first
    assert pick_free(ops, {"3": 1}, {"1": 5, "2": 3}).user_id == "2"
    assert pick_free(ops, {"1": 1, "2": 1, "3": 1}, {}) is None
    assert pick_free(ops, {"1": 1, "2": 2, "3": 2}, {}, max_sessions=3).user_id == "1"


def test_old_database_gets_the_assignment_columns(tmp_path):
    db = tmp_path / "bot.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TABLE handoffs (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            client_chat_id TEXT NOT NULL, support_chat_id TEXT NOT NULL, support_message_id TEXT NOT NULL,
            language TEXT NOT NULL, topic TEXT, screen_id TEXT, reason TEXT, client_text TEXT, bot_text TEXT,
            context TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (support_chat_id, support_message_id))""")
        con.execute("""INSERT INTO handoffs (channel, client_chat_id, support_chat_id, support_message_id, language,
            context, created_at) VALUES ('telegram', '42', '-1', '5', 'ru', '[]', '2026-10-01T00:00:00+00:00')""")
    store = HandoffStore(db)
    old = store.get(1)
    assert old.status is None and old.assigned_id is None
    assert store.busy_operators() == {} and store.due() == []


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("OPERATOR_FIRST", "0")
    monkeypatch.setenv("OPERATOR_WAIT_SECONDS", "90")
    monkeypatch.setenv("OPERATOR_MAX_SESSIONS", "2")
    s = Settings.from_env()
    assert (s.operator_first, s.operator_wait_seconds, s.operator_max_sessions) == (False, 90, 2)
    monkeypatch.delenv("OPERATOR_FIRST")
    assert Settings.from_env().operator_first and Settings().operator_wait_seconds == 60


def test_mention_counts_utf16_for_names_with_emoji():
    op = Operator("9", "Дилноза 🌸", None, True, None, "")
    text, entities = operator_mention(op)
    assert text == "Дилноза 🌸" and entities[0].length == 10
    assert operator_mention(Operator("9", "D", "dilnoza", True, None, ""))[0] == "@dilnoza"


def test_operator_request_lets_the_ai_answer_first_and_escalates_when_it_cannot():
    bot, llm, tg = make()
    client = Client(42)
    asyncio.run(bot.on_client_message(client.msg("operator kerak"), tg))
    assert client.sent[-1] == t("operator_ai_first", Lang.UZ_LATN) and not tg.to(SUPPORT)
    asyncio.run(bot.on_client_message(client.msg("Kartani qanday qo'shaman?"), tg))
    # The AI answered at once: no operator was connected first.
    assert llm.calls and client.sent[-1].endswith("Kartalarim bo'limiga kiring.") and not tg.to(SUPPORT)
    # It did not help: the client asks for a specialist and gets one.
    asyncio.run(bot.on_operator_button(NS(data="op", from_user=client.msg("").from_user, message=client.msg(""),
                                          answer=_noop), tg))
    assert tg.to(SUPPORT) and bot.feedback.conversation("telegram", "42").handoff_id is not None


async def _noop(*_, **__):
    pass


def test_question_the_ai_cannot_answer_after_operator_request_goes_to_the_support_chat():
    bot, llm, tg = make(FakeLLM(answer("Mutaxassisga yuboraman.", "uz_latn", escalate=True, reason="no KB answer")))
    client = Client(42)
    asyncio.run(bot.on_client_message(client.msg("operator bilan bog'lang"), tg))
    asyncio.run(bot.on_client_message(client.msg("Ish haqi kartamdan noma'lum komissiya yechildi"), tg))
    assert llm.calls and tg.to(SUPPORT) and bot.feedback.conversation("telegram", "42").handoff_id is not None
