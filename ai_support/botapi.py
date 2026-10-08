"""A minimal Telegram Bot API client for calls made as another bot than the one running.

The admin side writes to clients as the client bot (TELEGRAM_BOT_TOKEN), and the client bot tells
operators about new clients as the admin bot (ADMIN_BOT_TOKEN), so operators never have to use the
client bot and the two bots' traffic stays apart.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from aiohttp import ClientSession, ClientTimeout, FormData

# (method, payload) -> result. A payload value of bytes is uploaded as a file (multipart).
Call = Callable[[str, dict], Awaitable[dict]]


FILENAMES = {"photo": "photo.jpg", "voice": "voice.ogg", "audio": "audio.mp3", "document": "file"}


class TelegramError(Exception):
    pass


def telegram_call(bot_token: str) -> Call:
    """Call a Bot API method as the bot with this token."""
    async def call(method: str, payload: dict) -> dict:
        if not bot_token:
            raise TelegramError("no_token")
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        async with ClientSession(timeout=ClientTimeout(total=30)) as http:
            if any(isinstance(v, bytes) for v in payload.values()):
                form = FormData()
                for key, value in payload.items():
                    if isinstance(value, bytes):
                        form.add_field(key, value, filename=FILENAMES.get(key, key))
                    elif value is not None:
                        form.add_field(key, value if isinstance(value, str) else json.dumps(value))
                request = http.post(url, data=form)
            else:
                request = http.post(url, json=payload)
            async with request as res:
                body = await res.json()
        if not body.get("ok"):
            raise TelegramError(body.get("description") or "telegram_error")
        return body["result"]

    return call
