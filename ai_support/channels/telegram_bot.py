"""Telegram adapter. All answer logic lives in SupportEngine; this file only moves messages.

Escalations are posted to SUPPORT_CHAT_ID. An operator answers by replying to that post,
and the bot relays the reply to the client.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from ..config import Settings
from ..engine import SupportEngine
from ..factory import build_engine
from ..models import Audio, BotReply, Image, IncomingMessage
from .media_group import MediaGroupCollector

log = logging.getLogger(__name__)

# Telegram bot API download limit is 20 MB; larger images are rejected before downloading.
MAX_IMAGE_BYTES = 20 * 1024 * 1024


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
    def __init__(self, settings: Settings, engine: SupportEngine):
        self.settings = settings
        self.engine = engine
        # support-chat message id -> client chat id, for operator replies.
        self._handoffs: dict[int, int] = {}
        self._albums: MediaGroupCollector[tuple[Message, Bot]] | None = None
        self.router = Router()
        self._register()

    def _register(self) -> None:
        r = self.router
        support = self.settings.support_chat_id
        if support is not None:
            r.message.register(self.on_operator_reply, F.chat.id == support, F.reply_to_message)
            r.message.register(self.ignore, F.chat.id == support)
        r.message.register(self.on_start, CommandStart())
        r.message.register(self.on_operator_command, Command("operator"))
        r.message.register(self.on_client_message, F.chat.type == "private")

    async def ignore(self, message: Message) -> None:
        return None

    async def on_start(self, message: Message) -> None:
        hint = (message.from_user.language_code or "") if message.from_user else ""
        hint = {"ru": "здравствуйте", "en": "hello"}.get(hint, "")
        await message.answer(self.engine.welcome(str(message.chat.id), hint))

    async def on_operator_command(self, message: Message, bot: Bot) -> None:
        await self._deliver(message, bot, self.engine.handoff(str(message.chat.id)))

    async def on_client_message(self, message: Message, bot: Bot) -> None:
        if message.media_group_id:
            if self._albums is None:
                self._albums = MediaGroupCollector(self._on_album)
            self._albums.add(message.media_group_id, (message, bot))
            return
        await self._answer([message], bot)

    async def _on_album(self, items: list[tuple[Message, Bot]]) -> None:
        await self._answer([m for m, _ in items], items[0][1])

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
        await message.answer(reply.text)
        if reply.escalate:
            await self._escalate(message, bot, reply)

    async def _escalate(self, message: Message, bot: Bot, reply: BotReply) -> None:
        chat = self.settings.support_chat_id
        if chat is None:
            log.warning("escalation without SUPPORT_CHAT_ID: chat=%s reason=%s", message.chat.id, reply.escalation_reason)
            return
        user = message.from_user
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
        client = self._handoffs.get(message.reply_to_message.message_id)
        if client is None or not (message.text or message.caption):
            return
        await bot.send_message(client, message.text or message.caption)

    @staticmethod
    async def _download(bot: Bot, obj) -> bytes:
        buf = await bot.download(obj)
        return buf.read()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(settings.telegram_token)
    dp = Dispatcher()
    dp.include_router(TelegramSupportBot(settings, build_engine(settings)).router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
