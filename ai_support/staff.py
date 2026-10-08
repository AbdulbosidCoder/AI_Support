"""Telling support staff about clients through the admin bot, not the client bot.

The client bot (in its own process) calls the Bot API as the admin bot (ADMIN_BOT_TOKEN): a new hand-off
and every client message while a person handles the conversation reach the operator's (or admin's)
private chat with the admin bot, with a button that opens the dialog there. Staff traffic then never
goes through the client bot, so a busy support team cannot push it into Telegram's rate limits.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from .botapi import Call
from .operators import OperatorStore

log = logging.getLogger(__name__)

# Media kind -> (Bot API method, file field). Anything else is sent as a document.
MEDIA = {"photo": ("sendPhoto", "photo"), "voice": ("sendVoice", "voice"), "audio": ("sendAudio", "audio"),
         "document": ("sendDocument", "document")}


def open_button(client_id: str) -> dict:
    return {"inline_keyboard": [[{"text": "💬 Открыть диалог", "callback_data": f"a:chat:{client_id}"}]]}


class StaffNotifier:
    def __init__(self, call: Call, operators: OperatorStore, admin_ids: Iterable[int]):
        self.call = call
        self.operators = operators
        self.admin_ids = sorted(admin_ids)

    def recipients(self, operator_id: str | None = None) -> list[int]:
        """The person handling the client, else every available operator, else the admins."""
        if operator_id and operator_id.lstrip("-").isdigit():
            return [int(operator_id)]
        ops = [int(o.user_id) for o in self.operators.available() if o.user_id and o.user_id.isdigit()]
        return ops or list(self.admin_ids)

    async def notify(self, client_id: str, text: str, operator_id: str | None = None,
                     media: list[tuple[str, bytes]] = ()) -> int:
        """Send `text` (and the client's media) to the staff; returns to how many people it was delivered.

        If none of the intended people can be reached (they never opened the admin bot), the admins get it.
        """
        sent = await self._send(self.recipients(operator_id), client_id, text, media)
        if sent == 0 and self.admin_ids:
            sent = await self._send([a for a in self.admin_ids if str(a) != operator_id], client_id, text, media)
        return sent

    async def _send(self, ids: list[int], client_id: str, text: str, media: list[tuple[str, bytes]]) -> int:
        sent = 0
        for chat_id in ids:
            try:
                for kind, data in media:
                    method, field = MEDIA.get(kind, MEDIA["document"])
                    await self.call(method, {"chat_id": chat_id, field: data})
                await self.call("sendMessage", {"chat_id": chat_id, "text": text[:4096],
                                                "reply_markup": open_button(client_id)})
                sent += 1
            except Exception as e:  # noqa: BLE001 - not started the admin bot, blocked it, network
                log.warning("staff %s not notified about client %s: %s", chat_id, client_id, e)
        return sent
