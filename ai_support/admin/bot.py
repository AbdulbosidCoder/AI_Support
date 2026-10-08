"""The admin bot: a separate Telegram bot for admins (ADMIN_IDS) only.

Everything is on inline buttons: open the web panel (a Telegram mini app with sessions shown as
chats), the overview, the operator list (turn an operator off or on) and adding an operator by a
forwarded message or "<id> <name>". Anyone else is told their Telegram id, so it can be added to
ADMIN_IDS.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from ..config import Settings
from ..operators import OperatorError, to_phone
from .data import AdminData, render_overview

log = logging.getLogger(__name__)

ADD_HELP = ("Чтобы добавить оператора, отправьте его номер телефона и имя:\n"
            "<code>+998901234567 Имя Фамилия</code> (можно и <code>90 123 45 67 Имя</code>)\n"
            "или поделитесь его контактом.\n\n"
            "Оператор открывает клиентский бот, нажимает /start и при регистрации делится этим номером — "
            "бот сам запомнит его Telegram id. Если он уже зарегистрирован в боте, он станет оператором сразу.\n\n"
            "Можно и по Telegram id: <code>123456789 Имя</code> или переслать сюда его сообщение.")
PANEL_LABEL = "🖥 Открыть панель"


def menu_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    rows = []
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


class AdminBot:
    def __init__(self, settings: Settings, data: AdminData):
        self.settings = settings
        self.data = data
        # Admins who tapped "Add operator" and whose next message is the operator.
        self._adding: set[int] = set()
        self.router = Router()
        admins = F.from_user.id.in_(settings.admin_ids)
        r = self.router
        r.message.register(self.on_start, CommandStart(), admins)
        r.message.register(self.on_start, Command("menu"), admins)
        r.message.register(self.on_add_command, Command("add_operator"), admins)
        r.message.register(self.on_message, admins, F.chat.type == "private")
        r.callback_query.register(self.on_callback, F.data.startswith("a:"), admins)
        r.message.register(self.on_stranger, F.chat.type == "private")
        r.callback_query.register(self.on_stranger_callback)

    async def on_stranger(self, message: Message) -> None:
        uid = message.from_user.id if message.from_user else message.chat.id
        await message.answer(f"Это админ-бот поддержки Xonsaroy Pay, доступ только у администраторов.\n"
                             f"Ваш Telegram id: <code>{uid}</code>", parse_mode="HTML")

    async def on_stranger_callback(self, callback: CallbackQuery) -> None:
        await callback.answer("Нет доступа")

    async def on_start(self, message: Message) -> None:
        self._adding.discard(message.from_user.id)
        hint = "" if self.settings.admin_url else "\n\n⚠ ADMIN_DOMAIN не задан: веб-панель недоступна."
        await message.answer(f"{render_overview(self.data.overview())}{hint}",
                             reply_markup=menu_keyboard(self.settings))

    async def on_add_command(self, message: Message) -> None:
        body = (message.text or "").partition(" ")[2]
        if not body.strip():
            self._adding.add(message.from_user.id)
            await message.answer(ADD_HELP, parse_mode="HTML")
            return
        await self._add_from_text(message, body)

    async def on_message(self, message: Message) -> None:
        uid = message.from_user.id
        if uid not in self._adding:
            await message.answer("Выберите действие:", reply_markup=menu_keyboard(self.settings))
            return
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
