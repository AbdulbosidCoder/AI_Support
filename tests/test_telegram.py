from ai_support.channels.telegram_bot import TelegramSupportBot
from ai_support.config import Settings
from ai_support.engine import SupportEngine
from fakes import FakeLLM, FakeSTT


def test_router_builds_with_and_without_support_chat():
    for chat in (None, -100123):
        bot = TelegramSupportBot(Settings(support_chat_id=chat), SupportEngine(FakeLLM(), FakeSTT()))
        assert bot.router.message.handlers


import asyncio
import io
from types import SimpleNamespace as NS

from fakes import png


class FakeBot:
    async def download(self, obj):
        return io.BytesIO(obj.content)


def file(content=b"", size=None, mime=None):
    return NS(content=content, file_size=size if size is not None else len(content), mime_type=mime)


def msg(text=None, caption=None, photo=None, document=None, reply_to=None, chat=7, voice=None, audio=None):
    return NS(text=text, caption=caption, photo=photo, document=document, reply_to_message=reply_to,
              chat=NS(id=chat), voice=voice, audio=audio, media_group_id=None)


def incoming(*messages):
    bot = TelegramSupportBot(Settings(), SupportEngine(FakeLLM(), FakeSTT()))
    return asyncio.run(bot.to_incoming(list(messages), FakeBot()))


def test_photo_with_caption():
    data = png()
    m = incoming(msg(caption="Bu nima xato?", photo=[file(b"small"), file(data)]))
    assert m.text == "Bu nima xato?" and m.images[0].data == data  # largest size is used


def test_photo_without_caption():
    m = incoming(msg(photo=[file(png())]))
    assert m.text == "" and len(m.images) == 1


def test_image_sent_as_document():
    m = incoming(msg(caption="screenshot", document=file(png(), mime="image/png")))
    assert len(m.images) == 1 and m.text == "screenshot"


def test_non_image_document_ignored():
    m = incoming(msg(caption="hujjat", document=file(b"%PDF", mime="application/pdf")))
    assert m.images == [] and m.text == "hujjat"


def test_too_large_image_document_marked_unreadable():
    m = incoming(msg(document=file(b"", size=25 * 1024 * 1024, mime="image/jpeg")))
    assert len(m.images) == 1 and m.images[0].data == b""


def test_album_caption_and_all_photos():
    m = incoming(msg(photo=[file(png())]), msg(caption="ikkalasi ham xato", photo=[file(png())]))
    assert m.text == "ikkalasi ham xato" and len(m.images) == 2


def test_text_reply_to_own_screenshot_includes_it():
    earlier = msg(photo=[file(png())])
    m = incoming(msg(text="bu nima degani?", reply_to=earlier))
    assert m.text == "bu nima degani?" and len(m.images) == 1


def test_reply_to_other_chat_image_ignored():
    earlier = msg(photo=[file(png())], chat=99)
    m = incoming(msg(text="?", reply_to=earlier))
    assert m.images == []


# --- /start, language choice, menu ---------------------------------------------------------------

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from ai_support.menu import CHOOSE_LANGUAGE, OPERATOR_LABEL, QUICK_QUESTIONS, SETTINGS_LABEL
from ai_support.models import BotReply, Lang
from ai_support.templates import t
from ai_support.users import UserStore
from fakes import answer


class Chat:
    """Records what the bot sends to the client."""

    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None, **_):
        self.sent.append((text, reply_markup))

    async def send_chat_action(self, *_):
        pass

    async def send_message(self, chat, text, **_):
        self.sent.append((text, None))


def client_msg(chat, text=None, uid=42, lang_code="ru"):
    return NS(text=text, caption=None, photo=None, document=None, reply_to_message=None, voice=None, audio=None,
              media_group_id=None, chat=NS(id=uid),
              from_user=NS(id=uid, username="ali", full_name="Ali", language_code=lang_code),
              answer=chat.answer, message_id=1)


def callback(chat, data, uid=42):
    async def noop(*_, **__):
        pass
    return NS(data=data, from_user=NS(id=uid, username="ali", full_name="Ali", language_code="ru"),
              message=NS(chat=NS(id=uid), answer=chat.answer, edit_reply_markup=noop), answer=noop)


def make_bot(llm=None):
    users = UserStore(":memory:")
    return TelegramSupportBot(Settings(), SupportEngine(llm or FakeLLM(), FakeSTT()), users), users


def test_first_start_greets_in_three_languages_and_registers_user():
    bot, users = make_bot()
    chat = Chat()
    asyncio.run(bot.on_start(client_msg(chat, "/start")))
    text, kb = chat.sent[-1]
    assert text == CHOOSE_LANGUAGE and isinstance(kb, InlineKeyboardMarkup)
    assert [b[0].callback_data for b in kb.inline_keyboard] == [f"lang:{l.value}" for l in Lang]
    u = users.get("telegram", "42")
    assert u is not None and u.language is None and u.platform_lang == "ru"


def test_choosing_language_saves_it_and_shows_menu():
    bot, users = make_bot()
    chat = Chat()
    asyncio.run(bot.on_start(client_msg(chat, "/start")))
    asyncio.run(bot.on_language_chosen(callback(chat, "lang:ru")))
    assert users.get("telegram", "42").language == Lang.RU
    (saved, kb), (text, mini) = chat.sent[-2:]
    assert saved == t("language_saved", Lang.RU) and isinstance(kb, ReplyKeyboardMarkup)
    assert kb.keyboard[-1][1].text == SETTINGS_LABEL[Lang.RU]
    assert t("welcome", Lang.RU) in text
    assert [row[0].callback_data for row in mini.inline_keyboard] == [f"q:{q.id}" for q in QUICK_QUESTIONS]


def test_second_start_does_not_ask_language_again():
    bot, users = make_bot()
    chat = Chat()
    asyncio.run(bot.on_start(client_msg(chat, "/start")))
    asyncio.run(bot.on_language_chosen(callback(chat, "lang:en")))
    asyncio.run(bot.on_start(client_msg(chat, "/start")))
    assert t("welcome", Lang.EN) in chat.sent[-1][0] and users.count() == 1
    assert CHOOSE_LANGUAGE not in [text for text, _ in chat.sent[2:]]


def test_unknown_language_code_ignored():
    bot, users = make_bot()
    chat = Chat()
    asyncio.run(bot.on_start(client_msg(chat, "/start")))
    asyncio.run(bot.on_language_chosen(callback(chat, "lang:xx")))
    assert users.get("telegram", "42").language is None


def test_settings_button_offers_language_change():
    bot, users = make_bot()
    chat = Chat()
    asyncio.run(bot.on_client_message(client_msg(chat, SETTINGS_LABEL[Lang.UZ_LATN]), chat))
    text, kb = chat.sent[-1]
    assert text == t("settings", Lang.UZ_LATN)  # no language chosen yet: Uzbek by default
    assert kb.inline_keyboard[0][0].callback_data == "settings:language"
    asyncio.run(bot.on_change_language(callback(chat, "settings:language")))
    assert chat.sent[-1][0] == CHOOSE_LANGUAGE


def test_quick_question_asked_in_chosen_language():
    llm = FakeLLM(answer("Javob", "uz_cyrl"))
    bot, users = make_bot(llm)
    chat = Chat()
    users.touch("telegram", "42", "42")
    users.set_language("telegram", "42", Lang.UZ_CYRL)
    q = QUICK_QUESTIONS[0]
    asyncio.run(bot.on_client_message(client_msg(chat, q.label[Lang.RU]), chat))  # old keyboard label
    assert llm.calls[0][1] == q.question[Lang.UZ_CYRL]
    assert chat.sent[-2][0] == "Javob" and chat.sent[-1][0] == t("rate_bot", Lang.UZ_CYRL)


def test_quick_question_forbidden_answer_replaced():
    bot, users = make_bot(FakeLLM(answer("Pulingiz 2 soat ichida qaytadi.", "uz_latn")))
    chat = Chat()
    q = next(q for q in QUICK_QUESTIONS if q.id == "payment_problem")
    asyncio.run(bot.on_client_message(client_msg(chat, q.label[Lang.UZ_LATN]), chat))
    assert chat.sent[-1][0] == t("guardrail", Lang.UZ_LATN)


def test_operator_button_hands_off_in_chosen_language():
    llm = FakeLLM()
    bot, users = make_bot(llm)
    chat = Chat()
    users.touch("telegram", "42", "42")
    users.set_language("telegram", "42", Lang.EN)
    asyncio.run(bot.on_client_message(client_msg(chat, OPERATOR_LABEL[Lang.EN]), chat))
    assert chat.sent[-1][0] == t("handoff", Lang.EN) and not llm.calls


def test_any_message_registers_user():
    bot, users = make_bot()
    chat = Chat()
    asyncio.run(bot.on_client_message(client_msg(chat, "Salom", uid=7), chat))
    assert users.get("telegram", "7") is not None


def test_greeting_gets_short_question_and_mini_menu_without_model():
    llm = FakeLLM()
    bot, users = make_bot(llm)
    chat = Chat()
    asyncio.run(bot.on_client_message(client_msg(chat, "Salom"), chat))
    text, kb = chat.sent[-1]
    assert text.startswith(t("ask_problem", Lang.UZ_LATN)) and not llm.calls
    assert isinstance(kb, InlineKeyboardMarkup) and kb.inline_keyboard[0][0].callback_data.startswith("q:")


def test_mini_menu_button_asks_question_in_chosen_language():
    llm = FakeLLM(answer("Kartalarim bo'limiga kiring.", "uz_latn", topic="add_card"))
    bot, users = make_bot(llm)
    chat = Chat()
    users.touch("telegram", "42", "42")
    users.set_language("telegram", "42", Lang.RU)
    asyncio.run(bot.on_quick_question(callback(chat, "q:add_card"), chat))
    assert llm.calls[0][1] == QUICK_QUESTIONS[0].question[Lang.RU]
    assert chat.sent[-2] == ("Kartalarim bo'limiga kiring.", None)
    assert chat.sent[-1][0] == t("rate_bot", Lang.UZ_LATN)


def test_mini_menu_forbidden_answer_replaced():
    bot, users = make_bot(FakeLLM(answer("Перевод успешно завершён.", "ru")))
    chat = Chat()
    users.touch("telegram", "42", "42")
    users.set_language("telegram", "42", Lang.RU)
    asyncio.run(bot.on_quick_question(callback(chat, "q:transfer_not_received"), chat))
    assert chat.sent[-1][0] == t("guardrail", Lang.RU)


def test_unknown_quick_question_ignored():
    llm = FakeLLM()
    bot, _ = make_bot(llm)
    chat = Chat()
    asyncio.run(bot.on_quick_question(callback(chat, "q:nope"), chat))
    assert chat.sent == [] and not llm.calls


# --- Routing in the support chat: a group vs your own private chat (SUPPORT_CHAT_ID = your id) ---

from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.types import Update


class RoutingBot(TelegramSupportBot):
    """Records which handler took a message instead of talking to Telegram."""

    def __init__(self, support):
        self.routed = []
        super().__init__(Settings(support_chat_id=support), SupportEngine(FakeLLM(), FakeSTT()), UserStore(":memory:"))

    async def on_client_message(self, message, bot):
        self.routed.append("client")

    async def on_operator_reply(self, message, bot):
        self.routed.append("operator")

    async def on_approve(self, message):
        self.routed.append("approve")


def route(support, chat_id, text, reply_to=None, handoffs=()):
    bot = RoutingBot(support)
    for mid in handoffs:
        bot.handoffs.open("telegram", "555", str(chat_id), str(mid), BotReply("…", Lang.RU, escalate=True))
    dp = Dispatcher()
    dp.include_router(bot.router)
    chat = {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup", "title": "support"}
    data = {"message_id": 10, "date": datetime.now(), "chat": chat, "text": text,
            "from": {"id": abs(chat_id), "is_bot": False, "first_name": "Op"}}
    if reply_to is not None:
        data["reply_to_message"] = {"message_id": reply_to, "date": datetime.now(), "chat": chat, "text": "Эскалация"}
    asyncio.run(dp.feed_update(Bot("1:TEST"), Update(update_id=1, message=data)))
    return bot.routed


def test_group_reply_to_escalation_goes_to_client():
    assert route(-100123, -100123, "Ответ", reply_to=5, handoffs=[5]) == ["operator"]


def test_group_other_messages_ignored():
    assert route(-100123, -100123, "Привет коллеги") == []
    assert route(-100123, -100123, "Ответ", reply_to=6, handoffs=[5]) == []


def test_own_id_as_support_chat_reply_to_escalation_is_operator_reply():
    assert route(42, 42, "Ответ оператора", reply_to=5, handoffs=[5]) == ["operator"]


def test_own_id_as_support_chat_still_answered_as_client():
    assert route(42, 42, "To'lov o'tmadi") == ["client"]
    assert route(42, 42, "Это про мой скрин", reply_to=6, handoffs=[5]) == ["client"]


# --- Saved escalations, operator replies and learning from them ---------------------------------

from ai_support import guardrails
from ai_support.handoffs import HandoffStore


class SupportBot:
    """Telegram stand-in for escalations: records what goes to which chat."""

    def __init__(self):
        self.sent = []
        self._next = 100

    async def send_message(self, chat, text, **_):
        self._next += 1
        self.sent.append((chat, text))
        return NS(message_id=self._next)

    async def send_chat_action(self, *_):
        pass

    async def forward_message(self, *_):
        pass


def operator_msg(chat, text, reply_to, support=-100123, uid=9):
    return NS(text=text, caption=None, chat=NS(id=support), reply_to_message=NS(message_id=reply_to),
              from_user=NS(id=uid, username="op", full_name="Operator"), answer=chat.answer)


def escalated(llm=None, handoffs=None):
    """A client question the bot could not answer, escalated to the support chat."""
    llm = llm or FakeLLM(answer("Передаю специалисту.", escalate=True, reason="no answer in KB", topic="other"))
    handoffs = handoffs or HandoffStore(":memory:")
    bot = TelegramSupportBot(Settings(support_chat_id=-100123), SupportEngine(llm, FakeSTT()),
                             UserStore(":memory:"), handoffs)
    tg = SupportBot()
    client = Chat()
    asyncio.run(bot.on_client_message(client_msg(client, "Квартплата не обновилась после оплаты"), tg))
    return bot, tg, llm, handoffs


def test_operator_reply_relayed_saved_and_offered_as_candidate():
    bot, tg, _, handoffs = escalated()
    post = tg._next
    support = Chat()
    asyncio.run(bot.on_operator_reply(operator_msg(support, "Обновление долга занимает время, проверьте завтра.", post), tg))
    assert tg.sent[-2] == (42, "Обновление долга занимает время, проверьте завтра.")
    assert tg.sent[-1] == (42, t("rate_operator", Lang.RU))
    c = handoffs.candidates()[0]
    assert c.question == "Квартплата не обновилась после оплаты" and c.language == "ru"
    assert f"#{c.id}" in support.sent[-1][0] and "/approve" in support.sent[-1][0]
    assert handoffs.learned() == []


def test_escalation_link_survives_bot_restart(tmp_path):
    db = tmp_path / "bot.sqlite3"
    bot, tg, _, _ = escalated(handoffs=HandoffStore(db))
    post = tg._next
    restarted = TelegramSupportBot(Settings(support_chat_id=-100123), SupportEngine(FakeLLM(), FakeSTT()),
                                   UserStore(":memory:"), HandoffStore(db))
    assert restarted.is_handoff_reply(operator_msg(Chat(), "Javob", post))
    asyncio.run(restarted.on_operator_reply(operator_msg(Chat(), "Javob", post), tg))
    assert tg.sent[-2] == (42, "Javob")


def test_approve_updates_bot_knowledge():
    bot, tg, llm, handoffs = escalated()
    asyncio.run(bot.on_operator_reply(operator_msg(Chat(), "Чек об оплате есть в «Tarix».", tg._next), tg))
    cid = handoffs.candidates()[0].id
    support = Chat()
    asyncio.run(bot.on_candidates(operator_msg(support, "/candidates", None)))
    assert f"#{cid}" in support.sent[-1][0]
    asyncio.run(bot.on_approve(operator_msg(support, f"/approve {cid}", None)))
    assert "добавлен" in support.sent[-1][0]
    assert "Чек об оплате есть в «Tarix»." in llm.system_prompts[-1]
    assert handoffs.candidate(cid).status == "approved"


def test_forbidden_operator_answer_not_approved_and_bot_unchanged():
    bot, tg, llm, handoffs = escalated()
    asyncio.run(bot.on_operator_reply(operator_msg(Chat(), "Pulingiz 2 soat ichida qaytadi.", tg._next), tg))
    cid = handoffs.candidates()[0].id
    support = Chat()
    asyncio.run(bot.on_approve(operator_msg(support, f"/approve {cid}", None)))
    assert "не добавлен" in support.sent[-1][0] and llm.system_prompts == []
    asyncio.run(bot.on_approve(operator_msg(support, f"/approve {cid} To'lovni takrorlamang, tekshiruvga yuboramiz.", None)))
    assert "To'lovni takrorlamang" in llm.system_prompts[-1] and "2 soat ichida" not in llm.system_prompts[-1]


def test_reject_keeps_answer_out_of_knowledge():
    bot, tg, llm, handoffs = escalated()
    asyncio.run(bot.on_operator_reply(operator_msg(Chat(), "Javob", tg._next), tg))
    cid = handoffs.candidates()[0].id
    support = Chat()
    asyncio.run(bot.on_reject(operator_msg(support, f"/reject {cid}", None)))
    assert "отклонён" in support.sent[-1][0] and handoffs.learned() == [] and llm.system_prompts == []


def test_review_commands_need_an_id():
    bot, _, _, _ = escalated()
    support = Chat()
    asyncio.run(bot.on_approve(operator_msg(support, "/approve", None)))
    assert "/approve N" in support.sent[-1][0]


def test_clients_cannot_approve():
    # Review commands only work in the support chat; a client's /approve is just a message to the bot.
    assert route(-100123, 42, "/approve 1") == ["client"]
    assert route(-100123, -100123, "/approve 1") == ["approve"]


# --- Ratings and assessments of clients --------------------------------------------------------

from ai_support.feedback import Assessment, FeedbackStore

LEAKS = ("Уровень", "тон", "грубо", "вежливо", "AI-оценка", "Грубил", "требует внимания")


class RatingTG(SupportBot):
    """Telegram stand-in that also records deleted rating prompts."""

    def __init__(self):
        super().__init__()
        self.deleted = []

    async def send_message(self, chat, text, reply_markup=None, **_):
        posted = await super().send_message(chat, text)
        self.sent[-1] = (chat, text, reply_markup)
        return posted

    async def delete_message(self, chat, message_id):
        self.deleted.append((chat, message_id))


class ClientChat(Chat):
    """Client chat whose sent messages have ids, like Telegram."""

    def __init__(self):
        super().__init__()
        self._next = 0

    async def answer(self, text, reply_markup=None, **_):
        await super().answer(text, reply_markup)
        self._next += 1
        return NS(message_id=self._next)


def rate_callback(chat, data, uid=42):
    edits = []

    async def noop(*_, **__):
        pass

    async def edit_text(text, **_):
        edits.append(text)
    cb = NS(data=data, from_user=NS(id=uid, username="ali", full_name="Ali", language_code="ru"),
            message=NS(chat=NS(id=uid), answer=chat.answer, edit_text=edit_text), answer=noop)
    return cb, edits


def rating_bot(llm):
    feedback = FeedbackStore(":memory:")
    bot = TelegramSupportBot(Settings(support_chat_id=-100123), SupportEngine(llm, FakeSTT()),
                             UserStore(":memory:"), HandoffStore(":memory:"), feedback)
    return bot, feedback


def rate_data(kb):
    return kb.inline_keyboard[0][3].callback_data  # the 4⭐ button


def test_bot_answer_asks_rating_and_only_latest_keeps_buttons():
    llm = FakeLLM(answer("Kartalarim bo'limiga kiring.", "uz_latn", topic="add_card"))
    bot, feedback = rating_bot(llm)
    chat, tg = ClientChat(), RatingTG()
    asyncio.run(bot.on_client_message(client_msg(chat, "Karta qo'shilmayapti"), tg))
    text, kb = chat.sent[-1]
    assert text == t("rate_bot", Lang.UZ_LATN)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels[:5] == ["1⭐", "2⭐", "3⭐", "4⭐", "5⭐"] and t("rate_not_helped", Lang.UZ_LATN) in labels
    assert t("rate_no_answer", Lang.UZ_LATN) not in labels  # the bot always answers
    asyncio.run(bot.on_client_message(client_msg(chat, "Yana savol"), tg))
    assert tg.deleted == [(42, 2)]  # the first prompt is removed


def test_client_rates_bot_and_ai_assesses_client_once():
    llm = FakeLLM(answer("Kartalarim bo'limiga kiring.", "uz_latn", topic="add_card"))
    bot, feedback = rating_bot(llm)
    chat, tg = ClientChat(), RatingTG()
    asyncio.run(bot.on_client_message(client_msg(chat, "Karta qo'shilmayapti"), tg))
    data = rate_data(chat.sent[-1][1])
    cb, edits = rate_callback(chat, data)
    asyncio.run(bot.on_rate(cb, tg))
    assert edits == [f"{t('rate_thanks', Lang.UZ_LATN)} 4⭐"]
    assert feedback.score("bot").count == 1 and len(llm.assessed) == 1
    asyncio.run(bot.on_rate(rate_callback(chat, data.replace(":4", ":5"))[0], tg))  # changed the rating
    assert feedback.score("bot").average == 5.0 and len(llm.assessed) == 1


def test_low_bot_rating_points_to_operator_without_promises():
    llm = FakeLLM(answer("Kartalarim bo'limiga kiring.", "ru", topic="add_card"))
    bot, _ = rating_bot(llm)
    chat, tg = ClientChat(), RatingTG()
    asyncio.run(bot.on_client_message(client_msg(chat, "Не добавляется карта"), tg))
    request_id = chat.sent[-1][1].inline_keyboard[0][0].callback_data.split(":")[1]
    asyncio.run(bot.on_rate(rate_callback(chat, f"rate:{request_id}:nohelp")[0], tg))
    assert chat.sent[-1][0] == t("rate_bot_low", Lang.RU)
    assert all(not guardrails.find_violations(text) for text, _ in chat.sent)
    assert not any(any(w in text for w in LEAKS) for text, _ in chat.sent)


def test_other_client_cannot_rate():
    llm = FakeLLM(answer("Ok", "ru", topic="add_card"))
    bot, feedback = rating_bot(llm)
    chat, tg = ClientChat(), RatingTG()
    asyncio.run(bot.on_client_message(client_msg(chat, "Карта"), tg))
    asyncio.run(bot.on_rate(rate_callback(chat, rate_data(chat.sent[-1][1]), uid=43)[0], tg))
    assert feedback.score().count == 0


def test_escalation_carries_ai_assessment_level_and_tone_buttons_for_support_only():
    llm = FakeLLM(answer("Передаю специалисту.", "ru", escalate=True, reason="no answer"),
                  assessment=Assessment("rude", "Долг не обновился", "Показывать квитанцию"))
    bot, feedback = rating_bot(llm)
    chat, tg = ClientChat(), RatingTG()
    asyncio.run(bot.on_client_message(client_msg(chat, "Квартплата не обновилась"), tg))
    support_chat, summary, kb = tg.sent[-1]
    assert support_chat == -100123
    assert "тон: грубо" in summary and "Суть проблемы: Долг не обновился" in summary
    assert "Предложения: Показывать квитанцию" in summary and "Уровень клиента: C" in summary
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["ctone:polite", "ctone:calm", "ctone:rude"]
    # The client sees only the hand-off, no rating of the bot and nothing about the assessment.
    assert [text for text, _ in chat.sent] == ["Передаю специалисту.\n\n" + t("handoff", Lang.RU)]


def escalated_with_feedback(llm=None):
    llm = llm or FakeLLM(answer("Передаю специалисту.", "ru", escalate=True, reason="no answer"))
    bot, feedback = rating_bot(llm)
    chat, tg = ClientChat(), RatingTG()
    asyncio.run(bot.on_client_message(client_msg(chat, "Квартплата не обновилась"), tg))
    return bot, feedback, chat, tg, tg._next


def test_escalation_expires_open_bot_rating():
    llm = FakeLLM(answer("Kartalarim bo'limiga kiring.", "ru", topic="add_card"))
    bot, feedback = rating_bot(llm)
    chat, tg = ClientChat(), RatingTG()
    asyncio.run(bot.on_client_message(client_msg(chat, "Карта"), tg))
    data = rate_data(chat.sent[-1][1])
    asyncio.run(bot.on_client_message(client_msg(chat, OPERATOR_LABEL[Lang.RU]), tg))
    assert tg.deleted == [(42, 2)]
    asyncio.run(bot.on_rate(rate_callback(chat, data)[0], tg))
    assert feedback.score().count == 0


def test_operator_reply_asks_client_to_rate_operator():
    bot, feedback, chat, tg, post = escalated_with_feedback()
    asyncio.run(bot.on_operator_reply(operator_msg(Chat(), "Проверьте раздел «Tarix».", post), tg))
    client, text, kb = tg.sent[-1]
    assert client == 42 and text == t("rate_operator", Lang.RU)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert t("rate_no_answer", Lang.RU) in labels and t("rate_not_helped", Lang.RU) in labels
    first_prompt = tg._next
    asyncio.run(bot.on_operator_reply(operator_msg(Chat(), "Ещё уточнение.", post), tg))
    assert tg.deleted == [(42, first_prompt)]
    data = tg.sent[-1][2].inline_keyboard[1][0].callback_data  # "no answer"
    asyncio.run(bot.on_rate(rate_callback(chat, data)[0], tg))
    score = feedback.score("operator")
    assert score.count == 1 and score.no_answer == 1 and feedback.operator_scores() == [("Operator", 1.0, 1)]


def tone_callback(post, tone, chat=-100123):
    answers = []

    async def answer(text=None, **_):
        answers.append(text)
    return NS(data=f"ctone:{tone}", from_user=NS(id=9, username="op", full_name="Operator"),
              message=NS(chat=NS(id=chat), message_id=post), answer=answer), answers


def test_operator_rates_client_tone_and_note():
    bot, feedback, chat, tg, post = escalated_with_feedback()
    cb, answers = tone_callback(post, "rude")
    asyncio.run(bot.on_client_tone(cb))
    assert answers == ["Сохранено: грубо. Уровень клиента: C · требует внимания"]
    assert feedback.tones("telegram", "42") == ["rude"]  # the operator's tone replaces the AI's for this case
    support = Chat()
    asyncio.run(bot.on_client_note(operator_msg(support, "/client Долг не обновился | Добавить push", post)))
    assert "Заметка о клиенте сохранена" in support.sent[-1][0]
    row = feedback._db.execute("SELECT * FROM client_assessments WHERE source = 'operator' ORDER BY id DESC").fetchone()
    assert (row["tone"], row["problem"], row["suggestions"]) == ("rude", "Долг не обновился", "Добавить push")
    # Nothing of this reached the client.
    assert not any(any(w in text for w in LEAKS) for text, _ in chat.sent)


def test_client_note_needs_reply_to_escalation():
    bot, *_ = escalated_with_feedback()
    support = Chat()
    asyncio.run(bot.on_client_note(NS(text="/client текст", chat=NS(id=-100123), reply_to_message=None,
                                      from_user=None, answer=support.answer)))
    assert "/client" in support.sent[-1][0]


def test_tone_button_on_unknown_post():
    bot, *_ = escalated_with_feedback()
    cb, answers = tone_callback(999, "calm")
    asyncio.run(bot.on_client_tone(cb))
    assert answers == ["Эскалация не найдена."]


def test_rating_command_shows_anonymous_rating():
    bot, feedback, chat, tg, post = escalated_with_feedback()
    asyncio.run(bot.on_operator_reply(operator_msg(Chat(), "Ответ", post), tg))
    asyncio.run(bot.on_rate(rate_callback(chat, rate_data(tg.sent[-1][2]))[0], tg))
    support = Chat()
    asyncio.run(bot.on_rating(operator_msg(support, "/rating", None)))
    text = support.sent[-1][0]
    assert "Анонимный рейтинг" in text and "4.0 ★★★★☆" in text and "chat 42" not in text and "@ali" not in text


def test_client_command_routed_before_operator_relay():
    """/client as a reply to an escalation post is a note, never relayed to the client."""
    class R(RoutingBot):
        async def on_client_note(self, message):
            self.routed.append("note")

    bot = R(-100123)
    bot.handoffs.open("telegram", "555", "-100123", "5", BotReply("…", Lang.RU, escalate=True))
    dp = Dispatcher()
    dp.include_router(bot.router)
    chat = {"id": -100123, "type": "supergroup", "title": "support"}
    data = {"message_id": 10, "date": datetime.now(), "chat": chat, "text": "/client грубил",
            "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
            "from": {"id": 9, "is_bot": False, "first_name": "Op"},
            "reply_to_message": {"message_id": 5, "date": datetime.now(), "chat": chat, "text": "Эскалация"}}
    asyncio.run(dp.feed_update(Bot("1:TEST"), Update(update_id=1, message=data)))
    assert bot.routed == ["note"]
