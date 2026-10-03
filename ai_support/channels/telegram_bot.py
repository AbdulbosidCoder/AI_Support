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

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class TelegramSupportBot:
    def __init__(self, settings: Settings, engine: SupportEngine):
        self.settings = settings
        self.engine = engine
        # support-chat message id -> client chat id, for operator replies.
        self._handoffs: dict[int, int] = {}
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
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        msg = IncomingMessage(user_id=str(message.chat.id), text=message.text or message.caption or "")
        if message.photo:
            msg.images.append(Image(await self._download(bot, message.photo[-1]), "image/jpeg"))
        elif message.document and (message.document.mime_type or "") in ("image/jpeg", "image/png", "image/webp"):
            if (message.document.file_size or 0) <= MAX_IMAGE_BYTES:
                msg.images.append(Image(await self._download(bot, message.document), message.document.mime_type))
        elif message.voice:
            msg.audio = Audio(await self._download(bot, message.voice), message.voice.mime_type or "audio/ogg", "voice.ogg")
        elif message.audio:
            msg.audio = Audio(await self._download(bot, message.audio), message.audio.mime_type or "audio/mpeg",
                              message.audio.file_name or "audio.mp3")
        reply = await self.engine.handle(msg)
        await self._deliver(message, bot, reply)

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
