"""Telegram adapter. All answer logic lives in SupportEngine; this file only moves messages.

Every client is registered in the user store with the language they chose on /start;
the reply keyboard carries quick questions and settings (see ai_support/menu.py).

Escalations are posted to SUPPORT_CHAT_ID. An operator answers by replying to that post,
and the bot relays the reply to the client. Escalations and operator replies are saved
(ai_support/handoffs.py); an operator answer becomes a knowledge-base candidate, and operators
approve or reject candidates in the support chat with /candidates, /approve and /reject.

Ratings (ai_support/feedback.py): after a bot answer the client rates the bot; once the case reached
an operator, every operator reply asks the client to rate the operator instead (only the latest
request keeps its buttons). The escalation post carries the AI's assessment of the client and buttons
for the operator's own assessment (polite / calm / rude); /client adds the operator's note and /rating
shows the anonymous app-store style rating. Assessments and levels never reach the client.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from ..config import Settings
from ..engine import SupportEngine
from ..factory import build_engine, load_knowledge
from ..feedback import (
    BOT, OPERATOR, TONE_LABELS, TONES, Assessment, FeedbackStore, RatingError, RatingRequest, asks_bot_rating,
    client_level, render_assessment, render_rating,
)
from ..handoffs import Candidate, HandoffStore, ReviewError
from ..menu import (
    CHANGE_LANGUAGE_LABEL, CHOOSE_LANGUAGE, LANGUAGE_CHOICES, QUICK_QUESTIONS, match_menu, menu_rows, quick_question,
)
from ..models import Audio, BotReply, Image, IncomingMessage, Lang
from ..prompt import build_system_prompt
from ..templates import t
from ..users import User, UserStore
from .media_group import MediaGroupCollector

log = logging.getLogger(__name__)

# Telegram bot API download limit is 20 MB; larger images are rejected before downloading.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
CHANNEL = "telegram"
# Telegram message limit is 4096 characters; candidate texts are shortened in lists.
PREVIEW_CHARS = 700
CLIENT_NOTE_HELP = ("Ответьте на пост эскалации: /client <суть проблемы> | <предложения клиента>. "
                    "Тон клиента — кнопками под постом.")
TONE_BUTTONS = {"polite": "😊 Вежливо", "calm": "😐 Спокойно", "rude": "😠 Грубо"}
REVIEW_HELP = (
    "/approve N — добавить в базу знаний как есть\n"
    "/approve N <исправленный ответ> — добавить с вашим текстом (уберите детали конкретного клиента)\n"
    "/reject N — не добавлять"
)


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"lang:{lang.value}")] for lang, label in LANGUAGE_CHOICES
    ])


def settings_keyboard(lang: Lang) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=CHANGE_LANGUAGE_LABEL[lang], callback_data="settings:language")],
    ])


def quick_keyboard(lang: Lang) -> InlineKeyboardMarkup:
    """Mini menu under a message: the common questions, one tap each."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=q.label[lang], callback_data=f"q:{q.id}")] for q in QUICK_QUESTIONS
    ])


def rating_keyboard(request: RatingRequest, lang: Lang) -> InlineKeyboardMarkup:
    """1-5 stars, plus "no answer" (operator only) and "could not help"."""
    stars = [InlineKeyboardButton(text=f"{n}⭐", callback_data=f"rate:{request.id}:{n}") for n in range(1, 6)]
    other = [InlineKeyboardButton(text=t("rate_not_helped", lang), callback_data=f"rate:{request.id}:nohelp")]
    if request.target == OPERATOR:
        other.insert(0, InlineKeyboardButton(text=t("rate_no_answer", lang), callback_data=f"rate:{request.id}:none"))
    return InlineKeyboardMarkup(inline_keyboard=[stars, other])


def tone_keyboard() -> InlineKeyboardMarkup:
    """Under an escalation post: the operator's assessment of how the client talked."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=TONE_BUTTONS[tone], callback_data=f"ctone:{tone}") for tone in TONES],
    ])


def main_menu(lang: Lang) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in menu_rows(lang)],
        resize_keyboard=True, is_persistent=True,
    )


def message_text(message: Message) -> str:
    """The client's words: text for plain messages, caption for photos/documents/voice."""
    return (message.text or message.caption or "").strip()


def image_source(message: Message):
    """The downloadable image in a message (largest photo size or an image document), if any."""
    if message.photo:
        return message.photo[-1]
    doc = message.document
    if doc and (doc.mime_type or "").startswith("image/") and (doc.file_size or 0) <= MAX_IMAGE_BYTES:
        return doc
    return None


class TelegramSupportBot:
    def __init__(self, settings: Settings, engine: SupportEngine, users: UserStore | None = None,
                 handoffs: HandoffStore | None = None, feedback: FeedbackStore | None = None):
        self.settings = settings
        self.engine = engine
        self.users = users or UserStore(":memory:")
        # Escalation posts in the support chat -> client, operator replies and KB candidates.
        self.handoffs = handoffs or HandoffStore(":memory:")
        # Client ratings of the bot/operators and assessments of clients (internal only).
        self.feedback = feedback or FeedbackStore(":memory:")
        self._albums: MediaGroupCollector[tuple[Message, Bot]] | None = None
        self.router = Router()
        self._register()

    def _register(self) -> None:
        r = self.router
        support = self.settings.support_chat_id
        if support is not None:
            # Commands first: a /client note is itself a reply to the escalation post.
            r.message.register(self.on_client_note, F.chat.id == support, Command("client"))
            r.message.register(self.on_rating, F.chat.id == support, Command("rating"))
            r.message.register(self.on_operator_reply, F.chat.id == support, self.is_handoff_reply)
            r.callback_query.register(self.on_client_tone, F.data.startswith("ctone:"),
                                      F.message.chat.id == support)
            r.message.register(self.on_candidates, F.chat.id == support, Command("candidates"))
            r.message.register(self.on_approve, F.chat.id == support, Command("approve"))
            r.message.register(self.on_reject, F.chat.id == support, Command("reject"))
            if support < 0:
                # A group: everything else there is operators talking. A positive id is a private
                # chat (e.g. your own id, to test alone): there you are also a client, so other
                # messages get normal answers.
                r.message.register(self.ignore, F.chat.id == support)
        r.message.register(self.on_start, CommandStart())
        r.message.register(self.on_operator_command, Command("operator"))
        r.message.register(self.on_language_command, Command("language"))
        r.message.register(self.on_client_message, F.chat.type == "private")
        r.callback_query.register(self.on_language_chosen, F.data.startswith("lang:"))
        r.callback_query.register(self.on_change_language, F.data == "settings:language")
        r.callback_query.register(self.on_quick_question, F.data.startswith("q:"))
        r.callback_query.register(self.on_rate, F.data.startswith("rate:"))

    async def ignore(self, message: Message) -> None:
        return None

    def is_handoff_reply(self, message: Message) -> bool:
        """A reply to one of the bot's escalation posts."""
        reply_to = message.reply_to_message
        return reply_to is not None and self.handoffs.find(str(message.chat.id), str(reply_to.message_id)) is not None

    def register_user(self, message: Message) -> User:
        """Store the client (first contact) or refresh their details; keeps the chosen language."""
        u = message.from_user
        return self.users.touch(
            CHANNEL, str(u.id if u else message.chat.id), str(message.chat.id),
            username=u.username if u else None, full_name=u.full_name if u else None,
            platform_lang=u.language_code if u else None,
        )

    async def on_start(self, message: Message) -> None:
        user = self.register_user(message)
        if user.language is None:
            # First visit: greet in Uzbek, Russian and English and ask for the language.
            await message.answer(CHOOSE_LANGUAGE, reply_markup=language_keyboard())
            return
        await self._send_welcome(message, user.language)

    async def on_language_command(self, message: Message) -> None:
        self.register_user(message)
        await message.answer(CHOOSE_LANGUAGE, reply_markup=language_keyboard())

    async def on_change_language(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(CHOOSE_LANGUAGE, reply_markup=language_keyboard())

    async def on_language_chosen(self, callback: CallbackQuery) -> None:
        try:
            lang = Lang(callback.data.split(":", 1)[1])
        except ValueError:
            await callback.answer()
            return
        user_id = str(callback.from_user.id)
        chat = callback.message.chat if callback.message is not None else None
        chat_id = str(chat.id) if chat else user_id
        if self.users.get(CHANNEL, user_id) is None:
            u = callback.from_user
            self.users.touch(CHANNEL, user_id, chat_id, u.username, u.full_name, u.language_code)
        self.users.set_language(CHANNEL, user_id, lang)
        self.engine.remember_language(chat_id, lang)
        await callback.answer(t("language_saved", lang))
        if callback.message is not None:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:  # message too old or already edited: the choice is saved anyway
                pass
            await self._send_welcome(callback.message, lang, saved=True)

    async def _send_welcome(self, message: Message, lang: Lang, saved: bool = False) -> None:
        # The persistent keyboard comes with the first message, the mini menu with the question.
        first = t("language_saved", lang) if saved else t("ask_problem", lang)
        await message.answer(first, reply_markup=main_menu(lang))
        await message.answer(f"{t('welcome', lang)}\n\n{t('menu_hint', lang)}", reply_markup=quick_keyboard(lang))

    async def on_quick_question(self, callback: CallbackQuery, bot: Bot) -> None:
        await callback.answer()
        q = quick_question(callback.data.split(":", 1)[1])
        if q is None or callback.message is None:
            return
        user = self.users.get(CHANNEL, str(callback.from_user.id))
        lang = user.lang if user else Lang.UZ_LATN
        chat_id = callback.message.chat.id
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        reply = await self.engine.handle(IncomingMessage(user_id=str(chat_id), text=q.question[lang]))
        await self._deliver(callback.message, bot, reply, callback.from_user)

    async def on_operator_command(self, message: Message, bot: Bot) -> None:
        user = self.register_user(message)
        await self._deliver(message, bot, self.engine.handoff(str(message.chat.id), user.lang))

    async def on_client_message(self, message: Message, bot: Bot) -> None:
        user = self.register_user(message)
        action = match_menu(message.text or "")
        if action is not None:
            await self._on_menu(message, bot, user, action)
            return
        if message.media_group_id:
            if self._albums is None:
                self._albums = MediaGroupCollector(self._on_album)
            self._albums.add(message.media_group_id, (message, bot))
            return
        await self._answer([message], bot)

    async def _on_album(self, items: list[tuple[Message, Bot]]) -> None:
        await self._answer([m for m, _ in items], items[0][1])

    async def _on_menu(self, message: Message, bot: Bot, user: User, action) -> None:
        if action.kind == "operator":
            await self._deliver(message, bot, self.engine.handoff(str(message.chat.id), user.lang))
        elif action.kind == "settings":
            await message.answer(t("settings", user.lang), reply_markup=settings_keyboard(user.lang))
        else:
            # A quick question is answered as if the client typed it, in their chosen language.
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            msg = IncomingMessage(user_id=str(message.chat.id), text=action.question.question[user.lang])
            await self._deliver(message, bot, await self.engine.handle(msg))

    async def _answer(self, messages: list[Message], bot: Bot) -> None:
        """Answer one client message, or one album, as a single question."""
        first = messages[0]
        await bot.send_chat_action(first.chat.id, ChatAction.TYPING)
        msg = await self.to_incoming(messages, bot)
        reply = await self.engine.handle(msg)
        await self._deliver(first, bot, reply)

    async def to_incoming(self, messages: list[Message], bot: Bot) -> IncomingMessage:
        first = messages[0]
        # In an album only one item usually carries the caption.
        text = "\n".join(t for t in (message_text(m) for m in messages) if t)
        msg = IncomingMessage(user_id=str(first.chat.id), text=text)
        for m in messages:
            src = image_source(m)
            if src is not None:
                msg.images.append(Image(await self._download(bot, src)))
            elif m.document and (m.document.mime_type or "").startswith("image/"):
                msg.images.append(Image(b""))  # too large to download: the engine answers "unsupported"
        # A text question sent as a reply to the client's own earlier screenshot: include that image.
        reply_to = first.reply_to_message
        if not msg.images and reply_to is not None and reply_to.chat.id == first.chat.id:
            src = image_source(reply_to)
            if src is not None:
                msg.images.append(Image(await self._download(bot, src)))
                if not text:
                    msg.text = message_text(reply_to)
        if first.voice:
            msg.audio = Audio(await self._download(bot, first.voice), first.voice.mime_type or "audio/ogg", "voice.ogg")
        elif first.audio:
            msg.audio = Audio(await self._download(bot, first.audio), first.audio.mime_type or "audio/mpeg",
                              first.audio.file_name or "audio.mp3")
        return msg

    async def _deliver(self, message: Message, bot: Bot, reply: BotReply, user=None) -> None:
        await message.answer(reply.text, reply_markup=quick_keyboard(reply.language) if reply.show_menu else None)
        client_id = str(message.chat.id)
        if reply.escalate:
            # The case goes to a person: the operator is rated for it, not the bot.
            await self._remove_prompts(bot, self.feedback.case_escalated(CHANNEL, client_id))
            await self._escalate(message, bot, reply, user)
        elif asks_bot_rating(reply):
            request, replaced = self.feedback.ask(CHANNEL, client_id, BOT, reply.language.value, reply.topic)
            await self._remove_prompts(bot, replaced)
            prompt = await message.answer(t("rate_bot", reply.language), reply_markup=rating_keyboard(request, reply.language))
            self._remember_prompt(request, prompt)

    def _remember_prompt(self, request: RatingRequest, prompt) -> None:
        message_id = getattr(prompt, "message_id", None)
        if message_id is not None:
            self.feedback.set_prompt_ref(request.id, str(message_id))

    @staticmethod
    async def _remove_prompts(bot: Bot, requests: list[RatingRequest]) -> None:
        """Only the latest rating request keeps its buttons; older prompts are deleted."""
        for r in requests:
            if r.prompt_ref:
                try:
                    await bot.delete_message(int(r.client_id), int(r.prompt_ref))
                except Exception:  # too old to delete or already gone: the request is closed anyway
                    pass

    async def _escalate(self, message: Message, bot: Bot, reply: BotReply, user=None) -> None:
        chat = self.settings.support_chat_id
        if chat is None:
            log.warning("escalation without SUPPORT_CHAT_ID: chat=%s reason=%s", message.chat.id, reply.escalation_reason)
            return
        user = user or message.from_user
        who = f"@{user.username}" if user and user.username else (user.full_name if user else "?")
        client_id = str(message.chat.id)
        # Internal only: the AI's view of the client and the client's level, for the operator.
        assessment = await self.engine.assess_client(client_id)
        ai = render_assessment("ai", assessment) if assessment else "AI-оценка клиента: нет данных"
        # The level counts this case too (saved below, once the hand-off has an id).
        level = client_level(self.feedback.tones(CHANNEL, client_id) + ([assessment.tone] if assessment else []))
        summary = (
            f"Эскалация от {who} (chat {message.chat.id})\n"
            f"Причина: {reply.escalation_reason or '-'}\n"
            f"Язык: {reply.language.value} · Тема: {reply.topic or '-'} · Экран: {reply.screen_id or '-'}\n"
            f"Уровень клиента: {level}\n\n"
            f"Клиент: {reply.client_text or '(изображение/голос)'}\n\n"
            f"Ответ бота: {reply.text}\n\n"
            f"{ai}\n\n"
            "Ответьте реплаем на это сообщение, и бот перешлёт ответ клиенту. "
            "Оцените тон клиента кнопками ниже; /client <суть> | <предложения> реплаем — заметка о клиенте."
        )
        posted = await bot.send_message(chat, summary[:4096], reply_markup=tone_keyboard())
        handoff_id = self.handoffs.open(CHANNEL, client_id, str(chat), str(posted.message_id), reply,
                                        self.engine.recent_turns(client_id))
        if assessment is not None:
            self.feedback.assess(CHANNEL, client_id, "ai", assessment, handoff_id)
        if message.photo or message.voice or message.document or message.audio:
            await bot.forward_message(chat, message.chat.id, message.message_id)

    async def on_operator_reply(self, message: Message, bot: Bot) -> None:
        handoff = self.handoffs.find(str(message.chat.id), str(message.reply_to_message.message_id))
        text = message.text or message.caption
        if handoff is None or not text:
            return
        await bot.send_message(int(handoff.client_chat_id), text)
        u = message.from_user
        await self._ask_operator_rating(bot, handoff, u)
        candidate, created = self.handoffs.add_operator_reply(
            handoff.id, text, str(u.id) if u else None, u.full_name if u else None,
        )
        if candidate is not None and created:
            await message.answer(f"Ответ сохранён. Кандидат #{candidate.id} в базу знаний:\n\n"
                                 f"{candidate_text(candidate)}\n\n{REVIEW_HELP.replace('N', str(candidate.id))}")

    async def _ask_operator_rating(self, bot: Bot, handoff, operator) -> None:
        lang = _lang(handoff.language)
        request, replaced = self.feedback.ask(
            CHANNEL, handoff.client_chat_id, OPERATOR, lang.value, handoff.topic, handoff.id,
            str(operator.id) if operator else None, operator.full_name if operator else None,
        )
        await self._remove_prompts(bot, replaced)
        prompt = await bot.send_message(int(handoff.client_chat_id), t("rate_operator", lang),
                                        reply_markup=rating_keyboard(request, lang))
        self._remember_prompt(request, prompt)

    async def on_rate(self, callback: CallbackQuery, bot: Bot) -> None:
        """The client tapped a rating button."""
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not parts[1].isdigit() or callback.message is None:
            await callback.answer()
            return
        client_id = str(callback.message.chat.id)
        before = self.feedback.request(int(parts[1]))
        lang = _lang(before.language) if before else Lang.UZ_LATN
        try:
            rated = self.feedback.rate(int(parts[1]), client_id, parts[2])
        except RatingError as e:
            await callback.answer(t("rate_expired", lang) if str(e) == "expired" else None)
            return
        await callback.answer(t("rate_thanks", lang))
        mark = {"no_answer": t("rate_no_answer", lang), "not_helped": t("rate_not_helped", lang)}
        try:
            await callback.message.edit_text(f"{t('rate_thanks', lang)} {mark.get(rated.outcome, f'{rated.stars}⭐')}")
        except Exception:  # message too old to edit: the rating is saved anyway
            pass
        if rated.target == BOT and rated.stars <= 2:
            await callback.message.answer(t("rate_bot_low", lang))
        if rated.target == BOT and before.status != "rated":
            # A case the bot handled alone: the AI assesses the client once, for the support team only.
            assessment = await self.engine.assess_client(client_id)
            if assessment is not None:
                self.feedback.assess(CHANNEL, client_id, "ai", assessment)

    async def on_client_tone(self, callback: CallbackQuery) -> None:
        """An operator rated how the client talked, with the buttons under the escalation post."""
        tone = (callback.data or "").split(":", 1)[1]
        handoff = self.handoffs.find(str(callback.message.chat.id), str(callback.message.message_id))
        if handoff is None or tone not in TONES:
            await callback.answer("Эскалация не найдена.")
            return
        u = callback.from_user
        self.feedback.assess(CHANNEL, handoff.client_chat_id, OPERATOR, Assessment(tone), handoff.id,
                             f"@{u.username}" if u.username else u.full_name)
        level = self.feedback.level(CHANNEL, handoff.client_chat_id)
        await callback.answer(f"Сохранено: {TONE_LABELS[tone]}. Уровень клиента: {level.label}")

    async def on_client_note(self, message: Message) -> None:
        """/client <суть проблемы> | <предложения>, as a reply to an escalation post."""
        reply_to = message.reply_to_message
        handoff = self.handoffs.find(str(message.chat.id), str(reply_to.message_id)) if reply_to else None
        body = (message.text or "").partition(" ")[2].strip()
        if handoff is None or not body:
            await message.answer(CLIENT_NOTE_HELP)
            return
        problem, _, suggestions = body.partition("|")
        self.feedback.note(CHANNEL, handoff.client_chat_id, handoff.id, reviewer_name(message), problem, suggestions)
        await message.answer(f"Заметка о клиенте сохранена. Уровень клиента: "
                             f"{self.feedback.level(CHANNEL, handoff.client_chat_id)}")

    async def on_rating(self, message: Message) -> None:
        await message.answer(render_rating(self.feedback)[:4096])

    async def on_candidates(self, message: Message) -> None:
        pending = self.handoffs.candidates()
        if not pending:
            await message.answer("Новых кандидатов в базу знаний нет.")
            return
        body = "\n\n".join(candidate_text(c) for c in pending)
        await message.answer(f"Кандидаты в базу знаний ({len(pending)}):\n\n{body}\n\n{REVIEW_HELP}"[:4096])

    async def on_approve(self, message: Message) -> None:
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer(REVIEW_HELP)
            return
        try:
            c = self.handoffs.approve(int(parts[1]), reviewer_name(message), parts[2] if len(parts) > 2 else None)
        except ReviewError as e:
            await message.answer(review_error_text(int(parts[1]), e))
            return
        self.reload_knowledge()
        await message.answer(f"Кандидат #{c.id} добавлен в базу знаний, бот использует его со следующего вопроса.")

    async def on_reject(self, message: Message) -> None:
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer(REVIEW_HELP)
            return
        try:
            c = self.handoffs.reject(int(parts[1]), reviewer_name(message))
        except ReviewError as e:
            await message.answer(review_error_text(int(parts[1]), e))
            return
        await message.answer(f"Кандидат #{c.id} отклонён.")

    def reload_knowledge(self) -> None:
        self.engine.set_system_prompt(build_system_prompt(load_knowledge(self.settings, self.handoffs)))

    @staticmethod
    async def _download(bot: Bot, obj) -> bytes:
        buf = await bot.download(obj)
        return buf.read()


def _lang(value: str) -> Lang:
    try:
        return Lang(value)
    except ValueError:
        return Lang.UZ_LATN


def _short(text: str) -> str:
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


def candidate_text(c: Candidate) -> str:
    lines = [f"#{c.id} · {c.language} · тема {c.topic or '-'}", f"Вопрос: {_short(c.question)}",
             f"Ответ: {_short(c.answer)}"]
    violations = c.violations()
    if violations:
        lines.append("⚠ В ответе есть то, что бот говорить не может ("
                     + ", ".join(v.category for v in violations)
                     + "). Одобрить можно только с исправленным текстом.")
    return "\n".join(lines)


def reviewer_name(message: Message) -> str:
    u = message.from_user
    if u is None:
        return str(message.chat.id)
    return f"@{u.username}" if u.username else f"{u.full_name} ({u.id})"


def review_error_text(candidate_id: int, e: ReviewError) -> str:
    reason = str(e)
    if reason == "not_found":
        return f"Кандидата #{candidate_id} нет."
    if reason == "already_reviewed":
        return f"Кандидат #{candidate_id} уже проверен."
    return (f"Кандидат #{candidate_id} не добавлен: в ответе есть то, что бот говорить не может "
            f"({reason.removeprefix('forbidden: ')}). Пришлите /approve {candidate_id} <исправленный ответ>.")


# Commands shown in Telegram's "Menu" button; Uzbek is the default, ru/en follow the app language.
COMMANDS = {
    None: [("start", "Boshlash"), ("language", "Tilni o'zgartirish"), ("operator", "Operator bilan bog'lanish")],
    "ru": [("start", "Начать"), ("language", "Изменить язык"), ("operator", "Связаться с оператором")],
    "en": [("start", "Start"), ("language", "Change language"), ("operator", "Contact an operator")],
}


async def set_commands(bot: Bot) -> None:
    for code, commands in COMMANDS.items():
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in commands], language_code=code)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(settings.telegram_token)
    dp = Dispatcher()
    users = UserStore(settings.db_path)
    handoffs = HandoffStore(settings.db_path)
    feedback = FeedbackStore(settings.db_path)
    dp.include_router(TelegramSupportBot(settings, build_engine(settings, handoffs), users, handoffs, feedback).router)
    try:
        await set_commands(bot)
    except Exception as e:  # commands are a convenience; the bot works without them
        log.warning("set_my_commands failed: %s", e)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
