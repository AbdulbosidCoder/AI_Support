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


def _topic_or_question(data: str | None) -> bool:
    """A topic ("cat:<id>") or question ("q:<id>") button; page buttons ("cat:<id>:<page>") are not."""
    parts = (data or "").split(":")
    return (parts[0] == "cat" and len(parts) == 2) or parts[0] == "q"


def buttons(markup) -> list[str]:
    """Button texts, except topic and question buttons: those may start with a simple emoji (menu.ICONS)."""
    if isinstance(markup, InlineKeyboardMarkup):
        return [b.text for row in markup.inline_keyboard for b in row if not _topic_or_question(b.callback_data)]
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

def test_no_emoji_in_any_fixed_client_text_or_other_buttons():
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


# --- emoji on topic and question buttons, pages of questions -------------------------------------

def test_topic_and_question_buttons_carry_a_simple_emoji_but_the_question_stays_plain():
    from ai_support.menu import ICONS, button_label, quick_question
    for lang in Lang:
        topics = [b for row in main_keyboard(lang).inline_keyboard for b in row if _topic_or_question(b.callback_data)]
        assert len(topics) == len(CATEGORIES) and all(EMOJI.search(b.text) for b in topics)
        for c in CATEGORIES:
            for row in category_keyboard(c, lang, page=0).inline_keyboard:
                for b in row:
                    if (b.callback_data or "").startswith("q:"):
                        assert b.text.startswith(ICONS[b.callback_data[2:]])
    q = quick_question("sms_code")
    assert not EMOJI.search(q.label[Lang.UZ_LATN]) and not EMOJI.search(q.question[Lang.UZ_LATN])
    assert button_label("sms_code", "💬 SMS") == "💬 SMS"  # an admin's own emoji is not doubled
    assert button_label("custom_123", "Yangi savol") == "Yangi savol"  # panel questions get none


def test_many_questions_are_paged_with_previous_and_next():
    from ai_support.menu import NEXT_PAGE_LABEL, PREV_PAGE_LABEL, QUESTIONS_PER_PAGE, category
    cat = category("cards")
    questions = [(f"x{i}", f"Savol {i}") for i in range(QUESTIONS_PER_PAGE * 2 + 1)]  # 3 pages

    def page(n):
        kb = category_keyboard(cat, Lang.UZ_LATN, questions, n).inline_keyboard
        asked = [b.callback_data for row in kb for b in row if b.callback_data.startswith("q:")]
        nav = {b.text: b.callback_data for b in kb[-2]} if len(kb) > 1 else {}
        return asked, nav, kb[-1][0].callback_data

    asked, nav, back = page(0)
    assert asked == [f"q:x{i}" for i in range(QUESTIONS_PER_PAGE)] and back == "menu"
    assert nav == {NEXT_PAGE_LABEL[Lang.UZ_LATN]: "cat:cards:1"}
    asked, nav, _ = page(1)
    assert nav == {PREV_PAGE_LABEL[Lang.UZ_LATN]: "cat:cards:0", NEXT_PAGE_LABEL[Lang.UZ_LATN]: "cat:cards:2"}
    asked, nav, _ = page(2)
    assert asked == [f"q:x{QUESTIONS_PER_PAGE * 2}"] and nav == {PREV_PAGE_LABEL[Lang.UZ_LATN]: "cat:cards:1"}
    assert page(99)[0] == page(2)[0]  # an old page button past the end shows the last page
    # Few questions: one page, no paging row.
    kb = category_keyboard(cat, Lang.UZ_LATN, questions[:2]).inline_keyboard
    assert len(kb) == 3 and kb[-1][0].callback_data == "menu"


def test_client_pages_through_a_topic_in_the_same_message():
    from test_telegram import AppChat, app_msg
    from ai_support.menu import NEXT_PAGE_LABEL, QUESTIONS_PER_PAGE, category
    bot, users = make_bot()
    chat = AppChat()
    asyncio.run(bot.on_start(app_msg(chat, "/start")))
    menu = chat.order[-1]
    total = len(category("registration").questions)
    assert total > QUESTIONS_PER_PAGE
    asyncio.run(bot.on_category(chat.tap(menu, "cat:registration")))
    text, kb = chat.messages[menu]
    assert text.startswith("Ro'yxatdan o'tish va kirish (1/2)")
    assert kb.inline_keyboard[-2][0].text == NEXT_PAGE_LABEL[Lang.UZ_LATN]
    asyncio.run(bot.on_category(chat.tap(menu, "cat:registration:1")))
    text, kb = chat.messages[menu]
    asked = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data.startswith("q:")]
    assert chat.order == [menu] and text.startswith("Ro'yxatdan o'tish va kirish (2/2)")
    assert asked == [f"q:{q.id}" for q in category("registration").questions[QUESTIONS_PER_PAGE:]]
    assert not EMOJI.search(text)
    asyncio.run(bot.on_category(chat.tap(menu, "cat:registration:x")))  # a broken page number: first page
    assert chat.messages[menu][0].startswith("Ro'yxatdan o'tish va kirish (1/2)")


@pytest.mark.parametrize("qid,bad", [
    ("vpn_on_login", "VPN o'chirildi, hisobingiz blokdan chiqarildi."),
    ("app_settings", "Limitingizni oshirib qo'ydik."),
])
def test_questions_on_a_later_page_still_go_through_guardrails(qid, bad):
    from test_telegram import AppChat, app_msg
    bot, users = make_bot(FakeLLM(answer(bad, "uz_latn")))
    chat = AppChat()
    asyncio.run(bot.on_start(app_msg(chat, "/start")))
    menu = chat.order[-1]
    asyncio.run(bot.on_quick_question(chat.tap(menu, f"q:{qid}"), chat))
    sent = [m[0] for m in chat.messages.values()]
    assert any(t("guardrail", Lang.UZ_LATN) in x for x in sent) and not any(bad in x for x in sent)
