"""The admin bot: a separate Telegram bot for the support team.

Admins (ADMIN_IDS) get everything on inline buttons: open the web panel (a Telegram mini app with sessions
shown as chats), the overview, the operator list (turn an operator off or on) and adding an operator by a
forwarded message or "<id> <name>".

Admins and active operators get the "Support" menu: the clients waiting for a person or being answered by
one. Opening a client starts a dialog: whatever the staff member writes (text or a photo) reaches the
client through the client bot as a plain message, as from a support agent. New clients and their messages
come here too (ai_support/staff.py), so the support team never has to use the client bot.

Anyone else is told their Telegram id; an operator added by phone shares their number here to be linked.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from ..botapi import Call, telegram_call
from ..chatlog import BOT, CLIENT, OPERATOR, SYSTEM
from ..config import Settings
from ..operators import OperatorError, to_phone
from .data import AdminData, render_overview
from .relay import end_conversation, send_to_client

log = logging.getLogger(__name__)

ADD_HELP = ("Чтобы добавить оператора, отправьте его номер телефона и имя:\n"
            "<code>+998901234567 Имя Фамилия</code> (можно и <code>90 123 45 67 Имя</code>)\n"
            "или поделитесь его контактом.\n\n"
            "Оператор открывает клиентский бот, нажимает /start и при регистрации делится этим номером — "
            "бот сам запомнит его Telegram id. Если он уже зарегистрирован в боте, он станет оператором сразу.\n\n"
            "Можно и по Telegram id: <code>123456789 Имя</code> или переслать сюда его сообщение.")
PANEL_LABEL = "🖥 Открыть панель"
SUPPORT_LABEL = "🎧 Поддержка"
SHARE_PHONE_LABEL = "📱 Я оператор: поделиться номером"
STATE = {"waiting": "🔴 ждёт ответа", "ai": "🟡 отвечает AI", "operator": "🟢"}
DIALOG_HELP = ("Пишите сюда: каждое сообщение (текст или фото) уйдёт клиенту обычным сообщением, "
               "как от сотрудника поддержки. Клиент не видит, что вы пишете через бота.")
MAX_TEXT = 4000


def menu_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=SUPPORT_LABEL, callback_data="a:sup")]]
    if settings.admin_url:
        rows.append([InlineKeyboardButton(text=PANEL_LABEL, web_app=WebAppInfo(url=settings.admin_url))])
    rows += [
        [InlineKeyboardButton(text="📊 Сводка", callback_data="a:stats"),
         InlineKeyboardButton(text="💬 Сессии", callback_data="a:sessions")],
        [InlineKeyboardButton(text="👥 Операторы", callback_data="a:ops"),
         InlineKeyboardButton(text="➕ Добавить оператора", callback_data="a:add")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard(settings: Settings, extra: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    if settings.admin_url:
        rows.append([InlineKeyboardButton(text=PANEL_LABEL, web_app=WebAppInfo(url=settings.admin_url))])
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="a:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_operator(text: str) -> tuple[str, str] | None:
    """'+998 90 123 45 67 Name' -> ('+998901234567', 'Name'); '123456 Name' -> ('123456', 'Name').

    The phone may be written with spaces or dashes; None if the text starts with neither a phone nor an id.
    """
    text = (text or "").strip()
    i = 0
    while i < len(text) and (text[i].isdigit() or text[i] in "+ -()"):
        i += 1
    head, name = text[:i].strip(), text[i:].strip()
    digits = "".join(ch for ch in head if ch.isdigit())
    if not digits:
        return None
    phone = to_phone(head)
    if phone is not None:
        # 9 plain digits may also be a Telegram id: AdminData.add_operator checks the registered clients.
        return (head if head.isdigit() and len(head) == 9 else phone), name
    first, _, rest = head.partition(" ")
    if not first.isdigit():
        return None
    return first, f"{rest} {name}".strip()


def operators_text(data: AdminData) -> str:
    ops = data.operator_list()
    if not ops:
        return ("Операторов пока нет. Пока список пуст, отвечать клиентам может любой участник чата поддержки; "
                "после добавления первого оператора — только операторы из списка.")
    lines = ["👥 Операторы"]
    for o in ops:
        state = "✅" if o["active"] else ("⛔" if o["registered"] else "❔ не добавлен")
        if o["registered"] and not o["linked"]:
            state += " ⏳ ждём регистрации в боте"
        rating = f"{o['rating']}★ ({o['ratings']})" if o["rating"] is not None else "оценок нет"
        ids = " · ".join(x for x in (o["phone"], f"id {o['user_id']}" if o["user_id"] else None) if x)
        lines.append(f"{state} {o['name']} · {ids}\n"
                     f"   ответов {o['replies']}, сессий {o['sessions']} (открыто {o['sessions_open']}), {rating}")
    return "\n".join(lines)


def operators_keyboard(settings: Settings, data: AdminData) -> InlineKeyboardMarkup:
    toggles = []
    for o in data.operator_list():
        if o["registered"]:
            action, label = ("off", "⛔ Отключить") if o["active"] else ("on", "✅ Включить")
        else:
            action, label = "on", "➕ Добавить"
        toggles.append([InlineKeyboardButton(text=f"{label}: {o['name']}"[:60],
                                             callback_data=f"a:op:{o['key']}:{action}")])
    return back_keyboard(settings, toggles)


def sessions_text(data: AdminData, limit: int = 10) -> str:
    rows = data.sessions(limit=limit)
    if not rows:
        return "Сессий пока нет."
    lines = [f"💬 Последние сессии ({len(rows)}). Переписку целиком смотрите в панели."]
    for s in rows:
        who = s["operator_name"] or ("ждёт оператора" if s["escalated"] else "бот")
        state = "🟢" if s["status"] == "open" else "⚪"
        lines.append(f"{state} #{s['id']} {s['client_name']} · {who} · {s['topic'] or '-'} · {s['opened_at'][:16]}")
    return "\n".join(lines)


def support_view(data: AdminData, admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    """The support menu: who waits for a person, and who is being answered by whom."""
    queue = data.support_queue()
    rows = []
    for c in queue:
        state = STATE.get(c["state"], "")
        if c["state"] == "operator":
            state += f" {c['operator_name'] or 'специалист'}"
        rows.append([InlineKeyboardButton(text=f"{c['client_name']} · {state}"[:60],
                                          callback_data=f"a:chat:{c['client_id']}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="a:sup")])
    if admin:
        rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="a:menu")])
    if not queue:
        text = ("🎧 Поддержка\n\nСейчас никто не ждёт специалиста. На все вопросы отвечает AI; если он не может "
                "помочь, клиент появится здесь, а вам придёт сообщение.")
    else:
        waiting = sum(c["state"] != "operator" for c in queue)
        text = (f"🎧 Поддержка: {len(queue)} клиент(ов), ждут ответа: {waiting}.\n"
                "Откройте клиента, чтобы увидеть переписку и ответить.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def dialog_text(data: AdminData, client_id: str) -> str:
    target = data.client_target(client_id)
    name = target["client_name"] if target else client_id
    messages, closed = data.current_messages(client_id)
    lines = [f"💬 {name}" + (" · разговор завершён" if closed else ""), ""]
    for m in messages:
        body = (m["text"] or ("[файл]" if m["files"] else "")).strip()
        if len(body) > 300:
            body = body[:300] + "…"
        who = {CLIENT: "👤 Клиент", BOT: "🤖 AI", OPERATOR: f"🎧 {m['operator_name'] or 'Специалист'}",
               SYSTEM: "·"}.get(m["sender"], m["sender"])
        lines.append(f"{who}: {body}" if m["sender"] != SYSTEM else f"· {body}")
    if len(lines) == 2:
        lines.append("Сообщений пока нет.")
    lines += ["", "Если написать клиенту, начнётся новый разговор. " + DIALOG_HELP if closed else DIALOG_HELP]
    text = "\n".join(lines)
    return text if len(text) <= 4096 else "…" + text[-4000:]


def dialog_keyboard(client_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Завершить разговор", callback_data=f"a:close:{client_id}")],
        [InlineKeyboardButton(text="⬅️ К списку клиентов", callback_data="a:sup")],
    ])


class AdminBot:
    def __init__(self, settings: Settings, data: AdminData, client: Call | None = None):
        self.settings = settings
        self.data = data
        # The client bot's Bot API: staff messages reach clients through it.
        self.client = client or telegram_call(settings.telegram_token)
        # Admins who tapped "Add operator" and whose next message is the operator.
        self._adding: set[int] = set()
        # Staff member -> the client they are writing to.
        self._dialog: dict[int, str] = {}
        self.router = Router()
        admins = F.from_user.id.in_(settings.admin_ids)
        staff = self.is_staff
        r = self.router
        r.message.register(self.on_start, CommandStart(), staff)
        r.message.register(self.on_start, Command("menu"), staff)
        r.message.register(self.on_support_command, Command("support"), staff)
        r.message.register(self.on_add_command, Command("add_operator"), admins)
        r.message.register(self.on_message, staff, F.chat.type == "private")
        r.callback_query.register(self.on_callback, F.data.startswith("a:"), staff)
        r.message.register(self.on_stranger_contact, F.chat.type == "private", F.contact)
        r.message.register(self.on_stranger, F.chat.type == "private")
        r.callback_query.register(self.on_stranger_callback)

    def is_staff(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return user is not None and self.data.is_staff(user.id, self.settings.admin_ids)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.settings.admin_ids

    def staff_name(self, user) -> str:
        op = next((o for o in self.data.operators.available() if o.user_id == str(user.id)), None)
        return (op.name if op and op.name and op.name != op.phone else None) or user.full_name or "Специалист"

    async def on_stranger(self, message: Message) -> None:
        uid = message.from_user.id if message.from_user else message.chat.id
        await message.answer(
            f"Это бот команды поддержки Xonsaroy Pay. Ваш Telegram id: <code>{uid}</code>\n\n"
            "Если администратор добавил вас оператором по номеру телефона, нажмите кнопку ниже и поделитесь им.",
            parse_mode="HTML", reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=SHARE_PHONE_LABEL, request_contact=True)]], resize_keyboard=True,
                one_time_keyboard=True))

    async def on_stranger_contact(self, message: Message) -> None:
        """An operator added by phone shares their own number: from now on they work here."""
        c, user = message.contact, message.from_user
        if user is None or c.user_id != user.id:
            await message.answer("Поделитесь своим номером кнопкой ниже, а не чужим контактом.")
            return
        op = self.data.operators.link(c.phone_number, user.id, user.username, user.full_name)
        if op is None or not op.active:
            await message.answer("Этого номера нет в списке операторов. Попросите администратора добавить вас.",
                                 reply_markup=ReplyKeyboardRemove())
            return
        log.info("operator %s linked in the admin bot", user.id)
        await message.answer(f"✅ Готово, {op.name}: вы оператор поддержки. Новые клиенты будут приходить сюда.",
                             reply_markup=ReplyKeyboardRemove())
        await self._send_support(message, user.id)

    async def on_stranger_callback(self, callback: CallbackQuery) -> None:
        await callback.answer("Нет доступа")

    async def on_start(self, message: Message) -> None:
        uid = message.from_user.id
        self._adding.discard(uid)
        self._dialog.pop(uid, None)
        if not self.is_admin(uid):
            await self._send_support(message, uid)
            return
        hint = "" if self.settings.admin_url else "\n\n⚠ ADMIN_DOMAIN не задан: веб-панель недоступна."
        await message.answer(f"{render_overview(self.data.overview())}{hint}",
                             reply_markup=menu_keyboard(self.settings))

    async def on_support_command(self, message: Message) -> None:
        self._dialog.pop(message.from_user.id, None)
        await self._send_support(message, message.from_user.id)

    async def _send_support(self, message: Message, uid: int) -> None:
        text, markup = support_view(self.data, self.is_admin(uid))
        await message.answer(text, reply_markup=markup)

    # --- the dialog with a client ---------------------------------------------------------------------

    async def _open_dialog(self, message: Message, uid: int, client_id: str) -> None:
        if self.data.client_target(client_id) is None:
            await message.answer("Клиент не найден.")
            return
        self._dialog[uid] = client_id
        await message.answer(dialog_text(self.data, client_id), reply_markup=dialog_keyboard(client_id))

    async def _relay(self, message: Message, client_id: str) -> None:
        """The staff member's message goes to the client as a plain message."""
        user = message.from_user
        target = self.data.client_target(client_id)
        if target is None:
            await message.answer("Клиент не найден.")
            return
        text = (message.text or message.caption or "").strip()
        photo = None
        if message.photo:
            photo = await self.download(message)
        elif not text:
            await message.answer("Клиенту можно отправить текст или фото.")
            return
        if len(text) > (1024 if photo is not None else MAX_TEXT):
            await message.answer("Слишком длинное сообщение: разделите его на части.")
            return
        try:
            await send_to_client(self.data, self.client, self.settings.support_chat_id, target, text,
                                 str(user.id), self.staff_name(user), photo, via="админ-бота")
        except Exception as e:  # noqa: BLE001 - blocked by the client, no token, network: nothing was saved
            log.warning("staff %s message to %s not sent: %s", user.id, client_id, e)
            await message.answer(f"⚠ Клиент не получил сообщение: {e}")
            return
        log.info("staff %s wrote to client %s from the admin bot", user.id, client_id)
        try:
            await message.react([ReactionTypeEmoji(emoji="👌")])  # a quiet "delivered"
        except Exception:  # noqa: BLE001, S110 - only a mark
            pass

    async def download(self, message: Message) -> bytes:
        data = await message.bot.download(message.photo[-1])
        return data.read()

    async def on_add_command(self, message: Message) -> None:
        body = (message.text or "").partition(" ")[2]
        if not body.strip():
            self._adding.add(message.from_user.id)
            await message.answer(ADD_HELP, parse_mode="HTML")
            return
        await self._add_from_text(message, body)

    async def on_message(self, message: Message) -> None:
        uid = message.from_user.id
        if uid in self._adding and self.is_admin(uid):
            await self._add_message(message)
        elif uid in self._dialog:
            await self._relay(message, self._dialog[uid])
        elif self.is_admin(uid):
            await message.answer("Выберите действие:", reply_markup=menu_keyboard(self.settings))
        else:
            await self._send_support(message, uid)

    async def _add_message(self, message: Message) -> None:
        if message.contact is not None:
            c = message.contact
            name = " ".join(x for x in (c.first_name, c.last_name) if x)
            # A contact from Telegram carries the account id: the operator is linked at once.
            await self._add(message, c.phone_number, name, None, user_id=c.user_id)
            return
        origin = message.forward_origin
        if origin is not None:
            user = getattr(origin, "sender_user", None)
            if user is None:
                await message.answer("У этого пользователя скрыт аккаунт при пересылке. "
                                     "Отправьте его id и имя: <code>123456789 Имя</code>", parse_mode="HTML")
                return
            await self._add(message, str(user.id), user.full_name, user.username)
            return
        await self._add_from_text(message, message.text or "")

    async def _add_from_text(self, message: Message, text: str) -> None:
        parsed = parse_operator(text)
        if parsed is None:
            await message.answer(ADD_HELP, parse_mode="HTML")
            return
        await self._add(message, parsed[0], parsed[1], None)

    async def _add(self, message: Message, value: str, name: str, username: str | None,
                   user_id: int | None = None) -> None:
        try:
            op = self.data.add_operator(value, name, username, added_by=str(message.from_user.id), user_id=user_id)
        except OperatorError:
            await message.answer(ADD_HELP, parse_mode="HTML")
            return
        self._adding.discard(message.from_user.id)
        log.info("operator %s added by admin %s", op.key, message.from_user.id)
        if op.linked:
            text = (f"✅ Оператор добавлен: {op.name} (id {op.user_id}).\n"
                    "Теперь его ответы в чате поддержки доходят до клиентов.")
        else:
            text = (f"✅ Оператор добавлен: {op.name} ({op.phone}).\n"
                    "Пусть откроет клиентский бот, нажмёт /start и при регистрации поделится этим номером: "
                    "бот привяжет его Telegram id, и его ответы начнут доходить до клиентов.")
        await message.answer(text, reply_markup=back_keyboard(self.settings))

    async def on_callback(self, callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        message = callback.message
        if message is None:
            await callback.answer()
            return
        uid = callback.from_user.id
        if action in ("sup", "chat", "close"):
            await self._support_callback(callback, message, uid, parts)
            return
        if not self.is_admin(uid):
            await callback.answer("Нет доступа")
            return
        self._dialog.pop(uid, None)
        if action == "op" and len(parts) == 4:
            try:
                if parts[3] == "on" and self.data.operators.get(parts[2]) is None:
                    known = next((o for o in self.data.operator_list() if o["key"] == parts[2]), None)
                    self.data.add_operator(parts[2], known["name"] if known else "",
                                           added_by=str(callback.from_user.id))
                else:
                    self.data.operators.set_active(parts[2], parts[3] == "on")
            except OperatorError:
                await callback.answer("Оператор не найден")
                return
            await callback.answer("Сохранено")
            action = "ops"
        else:
            await callback.answer()
        if action == "stats":
            text, markup = render_overview(self.data.overview()), back_keyboard(self.settings)
        elif action == "ops":
            text, markup = operators_text(self.data), operators_keyboard(self.settings, self.data)
        elif action == "sessions":
            text, markup = sessions_text(self.data), back_keyboard(self.settings)
        elif action == "add":
            self._adding.add(callback.from_user.id)
            await message.answer(ADD_HELP, parse_mode="HTML")
            return
        else:
            text, markup = render_overview(self.data.overview()), menu_keyboard(self.settings)
        await _edit_or_send(message, text, markup)

    async def _support_callback(self, callback: CallbackQuery, message: Message, uid: int, parts: list[str]) -> None:
        action, client_id = parts[1], ":".join(parts[2:])
        if action == "chat" and client_id:
            await callback.answer()
            # Sent anew: the button may be under a notification about the client, which should stay.
            await self._open_dialog(message, uid, client_id)
            return
        if action == "close" and client_id:
            ended = await end_conversation(self.data, self.client, client_id, self.staff_name(callback.from_user))
            await callback.answer("Разговор завершён, клиента попросили оценить его" if ended
                                  else "Разговор уже завершён")
            for staff, current in list(self._dialog.items()):
                if current == client_id:
                    del self._dialog[staff]
        else:
            await callback.answer()
            self._dialog.pop(uid, None)
        text, markup = support_view(self.data, self.is_admin(uid))
        await _edit_or_send(message, text, markup)


async def _edit_or_send(message: Message, text: str, markup) -> None:
    try:
        await message.edit_text(text[:4096], reply_markup=markup)
    except Exception:  # unchanged or too old to edit: send it anew
        await message.answer(text[:4096], reply_markup=markup)


async def set_panel_button(bot: Bot, settings: Settings) -> None:
    """The "Panel" button next to the message field, for each admin."""
    if not settings.admin_url:
        return
    for admin in settings.admin_ids:
        try:
            await bot.set_chat_menu_button(chat_id=admin, menu_button=MenuButtonWebApp(
                text="Панель", web_app=WebAppInfo(url=settings.admin_url)))
        except Exception as e:  # the admin has not started the bot yet
            log.info("menu button for admin %s not set: %s", admin, e)
