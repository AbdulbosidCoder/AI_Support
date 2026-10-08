"""One greeting per visit, the problem asked once, no emoji in client messages, language before phone."""
import asyncio
import re
from types import SimpleNamespace as NS

import pytest
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from ai_support import guardrails
from ai_support.channels.telegram_bot import (
    category_keyboard, end_keyboard, language_keyboard, main_keyboard, phone_keyboard, rating_keyboard,
    settings_keyboard, welcome_text,
)
from ai_support.engine import SupportEngine, strip_greeting
from ai_support.feedback import BOT, OPERATOR
from ai_support.menu import CATEGORIES, CHOOSE_LANGUAGE, LANGUAGE_CHOICES
from ai_support.models import IncomingMessage, Lang
from ai_support.templates import _T, strip_emoji, t
from fakes import FakeLLM, FakeSTT, answer
from test_telegram import Chat, callback, client_msg, contact_msg, make_bot, registered

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿️‍]")
GREETINGS = ("Assalomu alaykum", "Ассалому алайкум", "Здравствуйте", "Hello")


def greetings(text: str) -> int:
    return sum(text.count(g) for g in GREETINGS)


def buttons(markup) -> list[str]:
    if isinstance(markup, InlineKeyboardMarkup):
        return [b.text for row in markup.inline_keyboard for b in row]
    if isinstance(markup, ReplyKeyboardMarkup):
        return [b.text for row in markup.keyboard for b in row]
    return []


# --- language before the phone number -------------------------------------------------------------

def test_first_visit_asks_language_then_phone():
    bot, users = make_bot(register=False)
    chat = Chat()
    asyncio.run(bot.on_start(client_msg(chat, "/start")))
    assert chat.sent[-1][0] == CHOOSE_LANGUAGE
    asyncio.run(bot.on_language_chosen(callback(chat, "lang:uz_latn")))
    assert chat.sent[-1][0] == t("register_ask", Lang.UZ_LATN)


def test_start_without_phone_asks_language_again_before_phone():
    # Chose a language earlier but never shared the number: /start begins with the language, not the phone.
    bot, users = make_bot(register=False)
    users.touch("telegram", "42", "42", "ali", "Ali", "ru")
    users.set_language("telegram", "42", Lang.RU)
    chat = Chat()
    asyncio.run(bot.on_start(client_msg(chat, "/start")))
    assert chat.sent[-1][0] == CHOOSE_LANGUAGE
    assert t("register_ask", Lang.RU) not in [text for text, _ in chat.sent]
    asyncio.run(bot.on_language_chosen(callback(chat, "lang:en")))
    assert chat.sent[-1][0] == t("register_ask", Lang.EN)


# --- one greeting, one question ------------------------------------------------------------------

def test_registration_flow_greets_once_and_asks_the_problem_once():
    llm = FakeLLM(answer("Assalomu alaykum! Kartalarim bo'limiga kiring 💳", "uz_latn", topic="add_card"))
    bot, users = make_bot(llm, register=False)
    chat = Chat()
    asyncio.run(bot.on_start(client_msg(chat, "/start")))
    asyncio.run(bot.on_language_chosen(callback(chat, "lang:uz_latn")))
    asyncio.run(bot.on_contact(contact_msg(chat)))
    welcome = chat.sent[-1][0]
    assert greetings(welcome) == 0  # the language screen already greeted
    assert welcome.count("Qanday muammo yuz berdi?") == 1
    asyncio.run(bot.on_client_message(client_msg(chat, "Karta qo'shilmayapti"), chat))
    texts = [text for text, _ in chat.sent]
    # The language screen (in three languages) is the only greeting.
    assert texts[0] == CHOOSE_LANGUAGE and sum(greetings(x) for x in texts[1:]) == 0
    assert texts[-1].endswith("Kartalarim bo'limiga kiring")  # the model's greeting and emoji are gone
    for text, markup in chat.sent:
        for s in [text] + buttons(markup):
            assert not EMOJI.search(s), s


def test_returning_client_start_greets_once():
    bot, users = make_bot()
    chat = Chat()
    asyncio.run(bot.on_start(client_msg(chat, "/start")))
    text = chat.sent[-1][0]
    assert greetings(text) == 1 and text.count(t("welcome", Lang.UZ_LATN)) == 1


@pytest.mark.parametrize("lang", list(Lang))
def test_welcome_asks_once_and_greets_only_when_asked(lang):
    for saved in (False, True):
        assert greetings(welcome_text(lang, saved)) == 0
    assert greetings(welcome_text(lang, greet=True)) == 1
    assert t("ask_problem", lang) not in welcome_text(lang, greet=True)


def test_greeting_reply_does_not_greet_again():
    bot, users = make_bot()
    chat = Chat()
    asyncio.run(bot.on_client_message(client_msg(chat, "Assalomu alaykum"), chat))
    assert greetings(chat.sent[-1][0]) == 0


@pytest.mark.parametrize("lang", list(Lang))
def test_fixed_replies_after_the_first_screen_never_greet(lang):
    for key in ("ask_problem", "welcome", "ai_takeover", "ai_how_help", "main_menu", "registered", "menu_hint"):
        assert greetings(t(key, lang)) == 0, key


@pytest.mark.parametrize("raw,clean", [
    ("Assalomu alaykum! Kartalarim bo'limiga kiring.", "Kartalarim bo'limiga kiring."),
    ("Здравствуйте, откройте «Kartalarim».", "Откройте «Kartalarim»."),
    ("Hello! Open «Kartalarim».", "Open «Kartalarim»."),
    ("Salomatlik sug'urtasi bo'limi yo'q.", "Salomatlik sug'urtasi bo'limi yo'q."),  # not a greeting
    ("Hisobingizni tekshiring.", "Hisobingizni tekshiring."),
    ("Assalomu alaykum!", "Assalomu alaykum!"),  # nothing else to say: kept
])
def test_strip_greeting(raw, clean):
    assert strip_greeting(raw) == clean


# --- no emoji ------------------------------------------------------------------------------------

def test_no_emoji_in_any_fixed_client_text():
    texts = [CHOOSE_LANGUAGE] + [label for _, label in LANGUAGE_CHOICES]
    texts += [v for per_lang in _T.values() for v in per_lang.values()]
    for lang in Lang:
        texts += [welcome_text(lang), welcome_text(lang, saved=True), welcome_text(lang, greet=True)]
        markups = [main_keyboard(lang), settings_keyboard(lang), end_keyboard(lang), phone_keyboard(lang),
                   language_keyboard()] + [category_keyboard(c, lang) for c in CATEGORIES]
        markups += [rating_keyboard(NS(id=1, target=target), lang) for target in (BOT, OPERATOR)]
        for m in markups:
            texts += buttons(m)
    bad = [x for x in texts if EMOJI.search(x)]
    assert bad == []


def test_strip_emoji_keeps_text_and_arrows():
    assert strip_emoji("✅ To'lov → «Xizmatlar» 💸") == "To'lov → «Xizmatlar»"
    assert strip_emoji("👨‍💼 Operator\n\n1️⃣ Qadam") == "Operator\n\n1 Qadam"
    assert strip_emoji("Oddiy matn") == "Oddiy matn"


@pytest.mark.parametrize("bad,lang", [
    ("✅ Pulingiz qaytarildi 💸", Lang.UZ_LATN),
    ("Здравствуйте! 🎉 Деньги вернутся в течение 2 часов.", Lang.RU),
    ("Hello! 🔓 We unblocked your card.", Lang.EN),
])
def test_forbidden_answer_with_greeting_and_emoji_still_replaced(bad, lang):
    engine = SupportEngine(FakeLLM(answer(bad, lang.value)), FakeSTT())
    reply = asyncio.run(engine.handle(IncomingMessage("1", "pulim qani")))
    assert reply.text == t("guardrail", lang) and reply.escalate and reply.guardrail_triggered


def test_model_answer_without_emoji_and_greeting_reaches_client():
    engine = SupportEngine(FakeLLM(answer("Здравствуйте! 📱 Откройте «Kartalarim» → «Qo'shish».", "ru",
                                          topic="add_card")), FakeSTT())
    reply = asyncio.run(engine.handle(IncomingMessage("1", "как добавить карту")))
    assert reply.text == "Откройте «Kartalarim» → «Qo'shish»."
    assert guardrails.find_violations(reply.text) == []
