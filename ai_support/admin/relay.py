"""Staff write to a client, or end the conversation, from the admin side (web panel or admin bot).

Everything reaches the client through the client bot (TELEGRAM_BOT_TOKEN) as a plain message, with no
signature or buttons, like a support agent's chat message. The reply is saved like an operator's reply in
the support chat, so the AI leaves the conversation to the person and their rating counts.
"""
from __future__ import annotations

import logging
import time

from ..botapi import Call
from ..channels.telegram_bot import _lang, rating_keyboard
from ..chatlog import SYSTEM
from ..feedback import OPERATOR
from ..models import BotReply
from ..templates import t
from .data import AdminData

log = logging.getLogger(__name__)


async def send_to_client(data: AdminData, client: Call, support_chat_id: int | None, target: dict, text: str,
                         staff_id: str, staff_name: str, photo: bytes | None = None, via: str = "админ-панели") -> int:
    """Send the staff member's message (and photo) to the client and save it; returns the session id.

    Raises whatever the Bot API raised if the client did not get it: then nothing is saved.
    """
    chat_id = int(target["client_id"])
    if photo is not None:
        await client("sendPhoto", {"chat_id": chat_id, "photo": photo, **({"caption": text[:1024]} if text else {})})
    else:
        await client("sendMessage", {"chat_id": chat_id, "text": text})
    saved = text or "[фото]"
    handoff_id = await support_copy(data, client, support_chat_id, target, saved, staff_name, via)
    return data.record_admin_reply(target, handoff_id, saved, staff_id, staff_name)


async def support_copy(data: AdminData, client: Call, support_chat_id: int | None, target: dict, text: str,
                       staff_name: str, via: str = "админ-панели") -> int | None:
    """Show the reply in the support chat (if there is one), and make sure the session has a hand-off.

    A session without a hand-off gets one: the client's next messages then go to the staff, not the AI.
    """
    handoff = target["handoff"]
    if handoff is not None and handoff.status == "closed":
        handoff = None  # an old case: this is a new one
    if handoff is not None and handoff.support_chat_id:
        try:
            posted = await client("sendMessage", {
                "chat_id": int(handoff.support_chat_id), "reply_to_message_id": int(handoff.support_message_id),
                "text": f"{staff_name} ответил клиенту из {via}:\n{text}"[:4096]})
            data.handoffs.link_post(handoff.id, handoff.support_chat_id, str(posted["message_id"]))
        except Exception as e:  # noqa: BLE001 - the client already has the reply; the copy is extra
            log.warning("support chat copy not posted: %s", e)
    if handoff is not None:
        return handoff.id
    where = ("", f"dm:{target['client_id']}:{time.time_ns()}")
    if support_chat_id is not None:
        try:
            posted = await client("sendMessage", {
                "chat_id": int(support_chat_id),
                "text": (f"{staff_name} написал клиенту {target['client_name']} из {via}"
                         + (f" (сессия #{target['session_id']})" if target["session_id"] else "") + f":\n{text}\n\n"
                         "Ответы клиента придут сюда; ответьте реплаем, чтобы продолжить.")[:4096]})
            where = (str(support_chat_id), str(posted["message_id"]))
        except Exception as e:  # noqa: BLE001 - still open the hand-off, so the answers reach the staff
            log.warning("support chat copy not posted: %s", e)
    note = BotReply(text, _lang(target["language"]), escalate=True, topic=target["topic"],
                    escalation_reason=f"сообщение из {via}", client_text=target["last_client_text"])
    return data.handoffs.open("telegram", target["client_id"], *where, note)


async def end_conversation(data: AdminData, client: Call, client_id: str, staff_name: str) -> bool:
    """The staff member ends the client's open conversation; the client is asked to rate it.

    False if nothing was open.
    """
    ended = data.feedback.end("telegram", client_id, OPERATOR)
    if ended is None:
        return False
    conversation, request = ended
    if conversation.handoff_id is not None:
        data.handoffs.close_handoff(conversation.handoff_id)
    data.chatlog.add("telegram", client_id, SYSTEM, f"Разговор завершил специалист ({staff_name})",
                     handoff_id=conversation.handoff_id, conversation_id=conversation.id)
    lang = _lang(conversation.language)
    text = f"{t('ended_by_operator', lang)}\n{t('rate_operator' if request.target == OPERATOR else 'rate_bot', lang)}"
    try:
        await client("sendMessage", {"chat_id": int(client_id), "text": text,
                                     "reply_markup": rating_keyboard(request, lang).model_dump(exclude_none=True)})
    except Exception as e:  # noqa: BLE001 - the conversation is closed anyway
        log.warning("rating request to %s not sent: %s", client_id, e)
    return True
