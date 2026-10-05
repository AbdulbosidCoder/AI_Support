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
from ai_support.models import Lang
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
    assert chat.sent[-1][0] == "Javob"


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
    assert chat.sent[-1] == ("Kartalarim bo'limiga kiring.", None)


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
