"""Telegram adapter. All answer logic lives in SupportEngine; this file only moves messages.

Every client is registered in the user store with the language they chose on /start;
the reply keyboard carries quick questions and settings (see ai_support/menu.py).

Escalations are posted to SUPPORT_CHAT_ID. An operator answers by replying to that post,
and the bot relays the reply to the client.
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
from ..factory import build_engine
from ..menu import (
    CHANGE_LANGUAGE_LABEL, CHOOSE_LANGUAGE, LANGUAGE_CHOICES, QUICK_QUESTIONS, match_menu, menu_rows, quick_question,
)
from ..models import Audio, BotReply, Image, IncomingMessage, Lang
from ..templates import t
from ..users import User, UserStore
from .media_group import MediaGroupCollector

log = logging.getLogger(__name__)

# Telegram bot API download limit is 20 MB; larger images are rejected before downloading.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
CHANNEL = "telegram"


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
    def __init__(self, settings: Settings, engine: SupportEngine, users: UserStore | None = None):
        self.settings = settings
        self.engine = engine
        self.users = users or UserStore(":memory:")
        # support-chat message id -> client chat id, for operator replies.
        self._handoffs: dict[int, int] = {}
        self._albums: MediaGroupCollector[tuple[Message, Bot]] | None = None
        self.router = Router()
        self._register()

    def _register(self) -> None:
        r = self.router
        support = self.settings.support_chat_id
        if support is not None:
            r.message.register(self.on_operator_reply, F.chat.id == support, self.is_handoff_reply)
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

    async def ignore(self, message: Message) -> None:
        return None

    def is_handoff_reply(self, message: Message) -> bool:
        """A reply to one of the bot's escalation posts."""
        return message.reply_to_message is not None and message.reply_to_message.message_id in self._handoffs

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
        await callback.message.answer(reply.text, reply_markup=quick_keyboard(reply.language) if reply.show_menu else None)
        if reply.escalate:
            await self._escalate(callback.message, bot, reply, callback.from_user)

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

    async def _deliver(self, message: Message, bot: Bot, reply: BotReply) -> None:
        await message.answer(reply.text, reply_markup=quick_keyboard(reply.language) if reply.show_menu else None)
        if reply.escalate:
            await self._escalate(message, bot, reply)

    async def _escalate(self, message: Message, bot: Bot, reply: BotReply, user=None) -> None:
        chat = self.settings.support_chat_id
        if chat is None:
            log.warning("escalation without SUPPORT_CHAT_ID: chat=%s reason=%s", message.chat.id, reply.escalation_reason)
            return
        user = user or message.from_user
        who = f"@{user.username}" if user and user.username else (user.full_name if user else "?")
        summary = (
            f"Эскалация от {who} (chat {message.chat.id})\n"
            f"Причина: {reply.escalation_reason or '-'}\n"
            f"Язык: {reply.language.value} · Тема: {reply.topic or '-'} · Экран: {reply.screen_id or '-'}\n\n"
            f"Клиент: {reply.client_text or '(изображение/голос)'}\n\n"
            f"Ответ бота: {reply.text}\n\n"
            "Ответьте реплаем на это сообщение, и бот перешлёт ответ клиенту."
        )
        posted = await bot.send_message(chat, summary)
        self._handoffs[posted.message_id] = message.chat.id
        if message.photo or message.voice or message.document or message.audio:
            await bot.forward_message(chat, message.chat.id, message.message_id)

    async def on_operator_reply(self, message: Message, bot: Bot) -> None:
        client = self._handoffs[message.reply_to_message.message_id]
        if not (message.text or message.caption):
            return
        await bot.send_message(client, message.text or message.caption)

    @staticmethod
    async def _download(bot: Bot, obj) -> bytes:
        buf = await bot.download(obj)
        return buf.read()


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
    dp.include_router(TelegramSupportBot(settings, build_engine(settings), users).router)
    try:
        await set_commands(bot)
    except Exception as e:  # commands are a convenience; the bot works without them
        log.warning("set_my_commands failed: %s", e)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
