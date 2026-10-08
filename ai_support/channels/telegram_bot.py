"""Telegram adapter. All answer logic lives in SupportEngine; this file only moves messages.

Clients use inline buttons only; /start is the one command (Telegram needs it to open the bot).
On first contact the client picks a language and registers by sharing their phone number
(Telegram contact button; the only reply keyboard, because inline buttons cannot request a contact).
Unregistered clients are asked to register before the bot answers.

Who is speaking is always clear: a tapped quick question is repeated as "Your question", model
answers are signed by the assistant and operator replies by the support specialist. Once a
conversation reached a person, the client's messages go to that person (posted in the support chat as
replies to the escalation post), not to the model, until the conversation ends.

Escalations are posted to SUPPORT_CHAT_ID. An operator answers by replying to the escalation post (or
to a relayed client message), and the bot relays the reply to the client. Escalations and operator
replies are saved (ai_support/handoffs.py); an operator answer becomes a knowledge-base candidate
with Add / Edit and add / Reject buttons. /menu in the support chat (or /start in a support group)
opens the operator panel: candidates and the rating. The old text commands still work there.

Ratings (ai_support/feedback.py): a bot answer or a hand-off opens a conversation. It ends when the
client taps "End conversation", the operator does (button under the escalation post), or nobody writes
in it for SESSION_IDLE_MINUTES. Only then the client rates it once: the bot if it never reached a
person, otherwise the operator. On the same close the AI assesses the client from the whole
conversation and, if an operator had it, a post in the support chat asks the operator to assess the
client too (polite / calm / rude, a note). Assessments and levels never reach the client.

Every message of the chat (client, bot, operator, system events) is saved in ai_support/chatlog.py,
linked to the conversation, so support staff can read sessions as chats.

Once an admin added operators (ai_support/operators.py, admin bot) only they can answer clients.
An admin adds an operator by phone; registering here with that phone links their Telegram id.

Operator first: once operators are added, a new conversation goes to a free operator, mentioned in the
support-chat post. If nobody is free, or the operator stays silent for OPERATOR_WAIT_SECONDS, the AI
takes the conversation: it greets the client and answers what they asked meanwhile, or asks how it can
help. A late operator reply still reaches the client and takes the conversation back.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    InputMediaPhoto,
    MessageEntity,
    BotCommandScopeChat,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile,
    KeyboardButton,
    Message,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    URLInputFile,
    User as TgUser,
)

from ..botapi import telegram_call
from ..chatlog import BOT as LOG_BOT, CLIENT as LOG_CLIENT, OPERATOR as LOG_OPERATOR, SYSTEM as LOG_SYSTEM, ChatLog
from ..config import Settings
from ..engine import SupportEngine
from ..factory import build_engine, load_knowledge
from ..feedback import (
    BOT, OPERATOR, TIMEOUT, TONE_LABELS, TONES, Assessment, FeedbackStore, RatingError, RatingRequest, client_level,
    opens_conversation, render_assessment, render_rating,
)
from ..guides import Guide, GuideStore, guide_text
from ..handoffs import WAITING, WITH_AI, Candidate, Handoff, HandoffStore, ReviewError
from ..language import asks_for_operator, is_small_talk
from ..llm import Turn
from ..menu import (
    CHANGE_LANGUAGE_LABEL, CHANGE_PHONE_LABEL, CHOOSE_LANGUAGE, END_LABEL, LANGUAGE_CHOICES, MENU_LABEL, OPERATOR_LABEL,
    BACK_LABEL, CATEGORIES, SETTINGS_LABEL, category, match_menu,
)
from ..models import Audio, BotReply, Image, IncomingMessage, Lang, VideoAttachment
from ..operators import Operator, OperatorStore, pick_free
from ..prompt import build_system_prompt
from ..staff import StaffNotifier
from ..templates import t
from ..pii import mask_pii
from ..users import User, UserStore
from .media_group import MediaGroupCollector

log = logging.getLogger(__name__)

# Telegram bot API download limit is 20 MB; larger images are rejected before downloading.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
CHANNEL = "telegram"
# Telegram message limit is 4096 characters; candidate texts are shortened in lists.
PREVIEW_CHARS = 700
CLIENT_NOTE_HELP = "Нажмите «📝 Заметка о клиенте» под постом эскалации. Тон клиента — кнопками под постом."
OPERATOR_END_LABEL = "✅ Завершить разговор"
NOTE_LABEL = "📝 Заметка о клиенте"
TONE_BUTTONS = {"polite": "😊 Вежливо", "calm": "😐 Спокойно", "rude": "😠 Грубо"}
PANEL_TEXT = "Панель поддержки. Ответы клиентам — реплаем на пост эскалации."
REVIEW_HELP = "Добавьте ответ в базу знаний как есть, исправьте его (уберите детали конкретного клиента) или отклоните."
# Prompts the bot posts in the support chat; the operator answers by replying to them.
NOTE_PROMPT = "📝 Заметка о клиенте #{id}"
EDIT_PROMPT = "✏️ Исправленный ответ для кандидата #{id}"
PROMPT_RE = re.compile(r"^(📝 Заметка о клиенте|✏️ Исправленный ответ для кандидата) #(\d+)")
# What the chat log shows for a message without text.
MEDIA_PLACEHOLDER = {"photo": "[фото]", "voice": "[голосовое сообщение]", "audio": "[аудио]",
                     "document": "[файл]", "text": ""}
OPERATOR_LINKED = ("Вы подключены как оператор поддержки Xonsaroy Pay. Отвечайте клиентам реплаем на "
                   "посты бота в чате поддержки — ваши ответы будут доходить до клиентов.")
NEW_CONVERSATION_REASON = "новое обращение: сначала оператор"
NOT_OPERATOR = ("Ответ не отправлен клиенту: вас нет в списке операторов. "
                "Попросите администратора добавить вас в админ-боте по номеру телефона или по Telegram id: {id}. "
                "Если вас добавили по номеру, откройте этого бота в личных сообщениях, нажмите /start "
                "и поделитесь тем же номером.")


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"lang:{lang.value}")] for lang, label in LANGUAGE_CHOICES
    ])


def phone_keyboard(lang: Lang) -> ReplyKeyboardMarkup:
    """Registration: Telegram shares the client's own number only through a reply-keyboard contact button."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("share_phone", lang), request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def main_keyboard(lang: Lang) -> InlineKeyboardMarkup:
    """The client's menu: topics two per row, then operator and settings, "end conversation" last."""
    topics = [InlineKeyboardButton(text=c.label[lang], callback_data=f"cat:{c.id}") for c in CATEGORIES]
    return InlineKeyboardMarkup(inline_keyboard=[topics[i:i + 2] for i in range(0, len(topics), 2)] + [
        [InlineKeyboardButton(text=OPERATOR_LABEL[lang], callback_data="op"),
         InlineKeyboardButton(text=SETTINGS_LABEL[lang], callback_data="settings")],
        [InlineKeyboardButton(text=END_LABEL[lang], callback_data="end")],
    ])


# Before buttons replaced the reply keyboard, quick questions were also offered as a "mini menu".
quick_keyboard = main_keyboard


def category_keyboard(cat, lang: Lang, questions: list[tuple[str, str]] | None = None) -> InlineKeyboardMarkup:
    """A topic's questions, one per row so the full question fits, and "back" to the topics.

    `questions` are (id, button text) as the admin panel left them (ai_support/guides.py); by default the
    built-in ones.
    """
    if questions is None:
        questions = [(q.id, q.label[lang]) for q in cat.questions]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"q:{qid}")] for qid, label in questions
    ] + [[InlineKeyboardButton(text=BACK_LABEL[lang], callback_data="menu")]])


def settings_keyboard(lang: Lang) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=CHANGE_LANGUAGE_LABEL[lang], callback_data="settings:language")],
        [InlineKeyboardButton(text=CHANGE_PHONE_LABEL[lang], callback_data="settings:phone")],
        [InlineKeyboardButton(text=MENU_LABEL[lang], callback_data="menu")],
    ])


def end_keyboard(lang: Lang) -> InlineKeyboardMarkup:
    """End the conversation (then rate it) or go back to the menu: shown with "no conversation" and the like.

    Answers themselves carry no buttons, so the chat reads like one with a person.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=END_LABEL[lang], callback_data="end"),
        InlineKeyboardButton(text=MENU_LABEL[lang], callback_data="menu"),
    ]])


def rating_keyboard(request: RatingRequest, lang: Lang) -> InlineKeyboardMarkup:
    """Marks 1-5, plus "no answer" (operator only) and "could not help"."""
    stars = [InlineKeyboardButton(text=str(n), callback_data=f"rate:{request.id}:{n}") for n in range(1, 6)]
    other = [InlineKeyboardButton(text=t("rate_not_helped", lang), callback_data=f"rate:{request.id}:nohelp")]
    if request.target == OPERATOR:
        other.insert(0, InlineKeyboardButton(text=t("rate_no_answer", lang), callback_data=f"rate:{request.id}:none"))
    return InlineKeyboardMarkup(inline_keyboard=[stars, other])


def tone_keyboard() -> InlineKeyboardMarkup:
    """Under an escalation post: the operator's assessment of the client, a note, and ending the conversation."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=TONE_BUTTONS[tone], callback_data=f"ctone:{tone}") for tone in TONES],
        [InlineKeyboardButton(text=NOTE_LABEL, callback_data="hnote"),
         InlineKeyboardButton(text=OPERATOR_END_LABEL, callback_data="hend")],
    ])


def closed_tone_keyboard() -> InlineKeyboardMarkup:
    """After the conversation ended: the operator's assessment of the client and a note (no "end" any more)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=TONE_BUTTONS[tone], callback_data=f"ctone:{tone}") for tone in TONES],
        [InlineKeyboardButton(text=NOTE_LABEL, callback_data="hnote")],
    ])


def panel_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Панель", callback_data="panel:home")]])


def welcome_text(lang: Lang, saved: bool = False, greet: bool = False) -> str:
    """The menu screen: asks for the problem once. It greets only when nothing has greeted the client yet."""
    first = [t("language_saved", lang)] if saved else ([t("hello", lang)] if greet else [])
    return "\n\n".join(first + [t("welcome", lang), t("menu_hint", lang)])


def _bot_of(event):
    """The Bot an aiogram event is bound to, if any (test stand-ins have none)."""
    try:
        return event.bot
    except Exception:
        return None


def candidate_keyboard(candidate_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Добавить", callback_data=f"cand:approve:{candidate_id}"),
         InlineKeyboardButton(text="✏️ Исправить", callback_data=f"cand:edit:{candidate_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"cand:reject:{candidate_id}")],
    ])


def panel_keyboard() -> InlineKeyboardMarkup:
    """The operator panel in the support chat."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Кандидаты в базу знаний", callback_data="panel:candidates")],
        [InlineKeyboardButton(text="⭐ Рейтинг поддержки", callback_data="panel:rating")],
    ])


def video_input(video: VideoAttachment, uploaded: dict[str, str]):
    """What to pass to send_video: an uploaded copy's file_id, else the local file, else the URL."""
    file_id = uploaded.get(video.id) or video.file_ids.get(CHANNEL)
    if file_id:
        return file_id
    if video.path:
        return FSInputFile(video.path)
    if video.url:
        return URLInputFile(video.url)
    return None


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


def media_files(messages: list[Message]) -> list[str]:
    """Telegram file ids of the photos, voice messages, audio and files, for the admin panel's chat."""
    out = []
    for m in messages:
        media = m.photo[-1] if m.photo else (m.voice or m.audio or m.document)
        if media is not None:
            out.append(media.file_id)
    return out


def message_kind(message: Message) -> str:
    if message.photo:
        return "photo"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.document:
        return "document"
    return "text"


class TelegramSupportBot:
    def __init__(self, settings: Settings, engine: SupportEngine, users: UserStore | None = None,
                 handoffs: HandoffStore | None = None, feedback: FeedbackStore | None = None,
                 chatlog: ChatLog | None = None, operators: OperatorStore | None = None,
                 guides: GuideStore | None = None, staff: StaffNotifier | None = None):
        self.settings = settings
        # Tells operators and admins about clients in their chat with the admin bot (ADMIN_BOT_TOKEN).
        self.staff = staff
        self.engine = engine
        self.users = users or UserStore(":memory:")
        # Escalation posts in the support chat -> client, operator replies and KB candidates.
        self.handoffs = handoffs or HandoffStore(":memory:")
        # Client ratings of the bot/operators and assessments of clients (internal only).
        self.feedback = feedback or FeedbackStore(":memory:")
        # Every message of every client chat, for support staff to read later.
        self.chatlog = chatlog or ChatLog(":memory:")
        # Operators added by an admin; while the list is empty anyone in the support chat answers.
        self.operators = operators or OperatorStore(":memory:")
        # Answers to quick questions written in the admin panel: text, steps and screenshots.
        self.guides = guides or GuideStore(":memory:")
        self._albums: MediaGroupCollector[tuple[Message, Bot]] | None = None
        # Client chat -> (message id, is a menu) of the bot's latest message with inline buttons. Only that
        # message keeps buttons: older ones lose them, and a menu is edited in place instead of re-sent.
        self._buttons: dict[str, tuple[int, bool]] = {}
        # Instruction video id -> Telegram file_id of the copy uploaded by this bot.
        self._video_file_ids: dict[str, str] = {}
        # Client chat -> what the client sent while waiting for their operator: (message to answer, the
        # client's messages, text). If the AI takes over, it answers this. Lost on restart: then it just asks.
        self._pending: dict[str, list[tuple[Message, list[Message], str]]] = {}
        # Client chats that asked for a person before saying what the problem is: the AI tries to answer
        # their next question first and connects a specialist only if it cannot help. Lost on restart.
        self._ai_first: set[str] = set()
        self.router = Router()
        self._register()

    def _register(self) -> None:
        r = self.router
        support = self.settings.support_chat_id
        if support is not None:
            in_support = F.chat.id == support
            # Commands and prompt replies first: they are themselves replies in the support chat.
            r.message.register(self.on_panel, in_support, Command("menu", "panel"))
            if support < 0:
                r.message.register(self.on_panel, in_support, CommandStart())
            r.message.register(self.on_prompt_reply, in_support, self.is_prompt_reply)
            r.message.register(self.on_client_note, in_support, Command("client"))
            r.message.register(self.on_rating, in_support, Command("rating"))
            r.message.register(self.on_operator_end, in_support, Command("end"), self.is_handoff_reply)
            r.message.register(self.on_operator_reply, in_support, self.is_handoff_reply)
            r.message.register(self.on_candidates, in_support, Command("candidates"))
            r.message.register(self.on_approve, in_support, Command("approve"))
            r.message.register(self.on_reject, in_support, Command("reject"))
            in_support_cb = F.message.chat.id == support
            r.callback_query.register(self.on_client_tone, F.data.startswith("ctone:"), in_support_cb)
            r.callback_query.register(self.on_operator_end_button, F.data == "hend", in_support_cb)
            r.callback_query.register(self.on_note_button, F.data == "hnote", in_support_cb)
            r.callback_query.register(self.on_candidate_button, F.data.startswith("cand:"), in_support_cb)
            r.callback_query.register(self.on_panel_button, F.data.startswith("panel:"), in_support_cb)
            if support < 0:
                # A group: everything else there is operators talking. A positive id is a private
                # chat (e.g. your own id, to test alone): there you are also a client, so other
                # messages get normal answers.
                r.message.register(self.ignore, in_support)
        r.message.register(self.on_start, CommandStart())
        r.message.register(self.on_contact, F.chat.type == "private", F.contact)
        r.message.register(self.on_client_message, F.chat.type == "private")
        r.callback_query.register(self.on_language_chosen, F.data.startswith("lang:"))
        r.callback_query.register(self.on_change_language, F.data == "settings:language")
        r.callback_query.register(self.on_change_phone, F.data == "settings:phone")
        r.callback_query.register(self.on_settings_button, F.data == "settings")
        r.callback_query.register(self.on_menu_button, F.data == "menu")
        r.callback_query.register(self.on_operator_button, F.data == "op")
        r.callback_query.register(self.on_category, F.data.startswith("cat:"))
        r.callback_query.register(self.on_quick_question, F.data.startswith("q:"))
        r.callback_query.register(self.on_rate, F.data.startswith("rate:"))
        r.callback_query.register(self.on_end_button, F.data == "end")

    @property
    def handoff_ready(self) -> bool:
        """Whether a person can be reached: through the support chat, the admin bot or both."""
        return self.settings.support_chat_id is not None or self.staff is not None

    async def ignore(self, message: Message) -> None:
        return None

    def is_handoff_reply(self, message: Message) -> bool:
        """A reply to one of the bot's escalation posts (or a client message relayed under it)."""
        reply_to = message.reply_to_message
        return reply_to is not None and self.handoffs.find(str(message.chat.id), str(reply_to.message_id)) is not None

    def is_prompt_reply(self, message: Message) -> bool:
        """A reply to the bot's "write the note / the corrected answer" prompt."""
        reply_to = message.reply_to_message
        if reply_to is None or not getattr(getattr(reply_to, "from_user", None), "is_bot", False):
            return False
        return PROMPT_RE.match(reply_to.text or "") is not None

    # --- registration: language, then phone number -----------------------------------------------

    def register_user(self, message: Message) -> User:
        """Store the client (first contact) or refresh their details; keeps the chosen language and phone."""
        u = message.from_user
        return self.users.touch(
            CHANNEL, str(u.id if u else message.chat.id), str(message.chat.id),
            username=u.username if u else None, full_name=u.full_name if u else None,
            platform_lang=u.language_code if u else None,
        )

    def _callback_user(self, callback: CallbackQuery) -> User:
        user = self.users.get(CHANNEL, str(callback.from_user.id))
        if user is None:
            u = callback.from_user
            chat = callback.message.chat.id if callback.message is not None else u.id
            user = self.users.touch(CHANNEL, str(u.id), str(chat), u.username, u.full_name, u.language_code)
        return user

    # --- one set of buttons at a time ---------------------------------------------------------------

    async def _clear_buttons(self, chat_id: str, bot=None, keep: int | None = None) -> None:
        """Remove the buttons from the bot's previous message in this chat (unless it is `keep`)."""
        last = self._buttons.pop(chat_id, None)
        if last is None or last[0] == keep or bot is None:
            return
        try:
            await bot.edit_message_reply_markup(chat_id=int(chat_id), message_id=last[0], reply_markup=None)
        except Exception:  # already edited, deleted or too old: nothing to clean
            pass

    def _remember(self, chat_id: str, sent, menu: bool) -> None:
        message_id = getattr(sent, "message_id", None)
        if isinstance(message_id, int):
            self._buttons[chat_id] = (message_id, menu)

    async def _reply_buttons(self, message: Message, text: str, markup, menu: bool = False, bot=None) -> None:
        """Answer with inline buttons; the previous message's buttons disappear."""
        chat_id = str(message.chat.id)
        await self._clear_buttons(chat_id, bot or _bot_of(message))
        self._remember(chat_id, await message.answer(text, reply_markup=markup), menu)

    async def _push_buttons(self, bot: Bot, chat_id, text: str, markup, menu: bool = False,
                            track: bool = True) -> None:
        """Send to a client chat with inline buttons; the previous message's buttons disappear."""
        await self._clear_buttons(str(chat_id), bot)
        sent = await bot.send_message(int(chat_id), text, reply_markup=markup)
        if track:
            self._remember(str(chat_id), sent, menu)

    async def _show(self, callback: CallbackQuery, text: str, markup) -> None:
        """A menu screen: edit the menu the client tapped in place, like an app screen; otherwise send it."""
        message = callback.message
        chat_id = str(message.chat.id)
        message_id = getattr(message, "message_id", None)
        if message_id is not None and self._buttons.get(chat_id) == (message_id, True):
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except Exception:  # unchanged or too old to edit: send it anew
                pass
        try:  # the tapped message keeps no stale buttons
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await self._reply_buttons(message, text, markup, menu=True, bot=_bot_of(callback))

    async def _next_step(self, message: Message, user: User, start: bool = False) -> bool:
        """Ask for what registration still lacks; True if the client is fully registered.

        The language always comes before the phone number: on /start an unregistered client chooses it
        (again), then shares the number in that language.
        """
        if user.language is None or (start and not user.registered):
            # First visit: greet in Uzbek, Russian and English and ask for the language.
            await self._reply_buttons(message, CHOOSE_LANGUAGE, language_keyboard(), menu=True)
            return False
        if not user.registered:
            await self._ask_phone(message, user.lang)
            return False
        return True

    async def _ask_phone(self, message: Message, lang: Lang) -> None:
        await message.answer(t("register_ask", lang), reply_markup=phone_keyboard(lang))

    async def on_start(self, message: Message) -> None:
        user = self.register_user(message)
        if await self._next_step(message, user, start=True):
            # A returning client: the only greeting of this visit.
            await self._send_welcome(message, user.lang, greet=True)

    async def on_contact(self, message: Message) -> None:
        """The client shared a contact: their own number registers them."""
        user = self.register_user(message)
        contact = message.contact
        if user.language is None:
            await message.answer(CHOOSE_LANGUAGE, reply_markup=language_keyboard())
            return
        if message.from_user is None or contact.user_id != message.from_user.id or not contact.phone_number:
            # Someone else's contact: not a registration.
            await message.answer(t("register_own_number", user.lang), reply_markup=phone_keyboard(user.lang))
            return
        self.users.set_phone(CHANNEL, user.user_id, contact.phone_number)
        await message.answer(t("registered", user.lang), reply_markup=ReplyKeyboardRemove())
        # An admin may have added this number as an operator: now we know their Telegram id.
        operator = self.operators.link(contact.phone_number, user.user_id, message.from_user.username,
                                       message.from_user.full_name)
        if operator is not None and operator.active:
            await message.answer(OPERATOR_LINKED)
        await self._send_welcome(message, user.lang)

    async def on_change_language(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message is not None:
            await self._show(callback, CHOOSE_LANGUAGE, language_keyboard())

    async def on_change_phone(self, callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message is not None:
            await self._ask_phone(callback.message, self._callback_user(callback).lang)

    async def on_language_chosen(self, callback: CallbackQuery) -> None:
        try:
            lang = Lang(callback.data.split(":", 1)[1])
        except ValueError:
            await callback.answer()
            return
        user = self._callback_user(callback)
        self.users.set_language(CHANNEL, user.user_id, lang)
        self.engine.remember_language(user.chat_id, lang)
        await callback.answer(t("language_saved", lang))
        if callback.message is None:
            return
        if user.registered:
            # The language chooser turns into the menu, in the new language.
            await self._show(callback, welcome_text(lang, saved=True), main_keyboard(lang))
            return
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # message too old or already edited: the choice is saved anyway
            pass
        self._buttons.pop(str(callback.message.chat.id), None)
        await self._ask_phone(callback.message, lang)

    async def _send_welcome(self, message: Message, lang: Lang, saved: bool = False, greet: bool = False) -> None:
        await self._reply_buttons(message, welcome_text(lang, saved, greet), main_keyboard(lang), menu=True)

    # --- client buttons -----------------------------------------------------------------------------

    async def _registered_callback(self, callback: CallbackQuery) -> User | None:
        """The client behind a button, if registered; otherwise asks for the missing step."""
        await callback.answer()
        if callback.message is None:
            return None
        user = self._callback_user(callback)
        return user if await self._next_step(callback.message, user) else None

    async def on_menu_button(self, callback: CallbackQuery) -> None:
        user = await self._registered_callback(callback)
        if user is not None:
            await self._show(callback, t("main_menu", user.lang), main_keyboard(user.lang))

    async def on_category(self, callback: CallbackQuery) -> None:
        """A topic in the menu: the menu message turns into that topic's questions."""
        cat = category((callback.data or "").split(":", 1)[1])
        if cat is None:
            await callback.answer()
            return
        user = await self._registered_callback(callback)
        if user is not None:
            await self._show(callback, f"{cat.label[user.lang]}\n\n{t('pick_question', user.lang)}",
                             category_keyboard(cat, user.lang, self.guides.menu(cat.id, user.lang)))

    async def on_settings_button(self, callback: CallbackQuery) -> None:
        user = await self._registered_callback(callback)
        if user is not None:
            await self._show(callback, t("settings", user.lang), settings_keyboard(user.lang))

    async def on_operator_button(self, callback: CallbackQuery, bot: Bot) -> None:
        user = await self._registered_callback(callback)
        if user is not None:
            await self._to_operator(callback.message, bot, user, callback.from_user)

    async def on_quick_question(self, callback: CallbackQuery, bot: Bot) -> None:
        q = self.guides.question((callback.data or "").split(":", 1)[1])
        if q is None:
            await callback.answer()
            return
        user = await self._registered_callback(callback)
        if user is not None:
            await self._ask_quick(callback.message, bot, user, q, callback.from_user, tapped=True)

    async def _ask_quick(self, message: Message, bot: Bot, user: User, q, from_user=None, tapped: bool = False) -> None:
        """A quick question is answered as if the client typed it; the chat shows what was asked.

        Tapped in the menu, the menu itself turns into the question, so no extra message and no stale buttons.
        """
        lang = user.lang
        question = q.question[lang]
        asked = f"{t('your_question', lang)} {question}"
        chat_id = str(message.chat.id)
        message_id = getattr(message, "message_id", None)
        edited = False
        if tapped and message_id is not None and self._buttons.get(chat_id) == (message_id, True):
            try:
                await message.edit_text(asked, reply_markup=None)
                self._buttons.pop(chat_id, None)
                edited = True
            except Exception:  # too old to edit: say it in a new message
                pass
        if not edited:
            await self._clear_buttons(chat_id, bot)
            await message.answer(asked)
        if await self._with_operator(message, bot, [], question):
            return
        ai_first = chat_id in self._ai_first
        self._ai_first.discard(chat_id)
        found = self.guides.answer(q.id, lang)
        if found is not None:
            # The admin wrote this answer: no model and no wait for an operator, the client can still ask one.
            await self._send_guide(message, bot, q, *found, question, from_user)
            return
        if not ai_first and await self._operator_first(message, bot, [], question, lang, from_user):
            return
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        reply = await self.engine.handle(IncomingMessage(user_id=str(message.chat.id), text=question))
        await self._deliver(message, bot, reply, from_user)

    async def _send_guide(self, message: Message, bot: Bot, q, guide: Guide, lang: Lang, question: str,
                          from_user=None) -> None:
        """A quick question's guide: the text and steps, then the screenshots as one album."""
        reply = BotReply(guide_text(guide, lang), lang, topic=q.id, client_text=question)
        await self._deliver(message, bot, reply, from_user)
        await self._send_guide_images(message, guide)

    async def _send_guide_images(self, message: Message, guide: Guide) -> None:
        """Screenshots after the guide; a failed upload never breaks the answer already sent."""
        media = []
        for img in guide.images:
            if img.file_id:
                media.append((img.id, img.file_id))
                continue
            stored = self.guides.image(img.id)
            if stored is not None:
                ext = "png" if stored[3] == "image/png" else "jpg"
                media.append((img.id, BufferedInputFile(stored[1], filename=f"guide-{img.id}.{ext}")))
        if not media:
            return
        try:
            if len(media) == 1:
                sent = [await message.answer_photo(media[0][1])]
            else:
                sent = await message.answer_media_group([InputMediaPhoto(media=m) for _, m in media])
        except Exception as e:  # noqa: BLE001 - Telegram/network errors: the text is already sent
            log.warning("guide %s images not sent: %s", guide.qid, e)
            return
        for (image_id, _), m in zip(media, sent):
            photo = getattr(m, "photo", None)
            if photo:
                # Upload once: later clients get the same picture by id.
                self.guides.remember_file_id(image_id, photo[-1].file_id)

    async def _to_operator(self, message: Message, bot: Bot, user: User, from_user=None) -> None:
        chat_id = str(message.chat.id)
        conversation = self.feedback.conversation(CHANNEL, chat_id)
        if conversation is not None and conversation.handoff_id is not None and self.handoff_ready:
            # Already with a person: no second escalation; answer in the conversation's language.
            await message.answer(t("with_operator", _lang(conversation.language)))
            return
        if conversation is None and chat_id not in self._ai_first:
            # Nothing asked yet: the AI answers the question first, a specialist only if it cannot help.
            self._ai_first.add(chat_id)
            self._log(chat_id, LOG_SYSTEM, "Клиент попросил оператора: сначала отвечает AI")
            await self._clear_buttons(chat_id, bot)
            await message.answer(t("operator_ai_first", user.lang))
            return
        # The AI already answered and the client still wants a person (or asks a second time): connect them.
        self._ai_first.discard(chat_id)
        self._log(chat_id, LOG_SYSTEM, "Клиент попросил оператора")
        await self._deliver(message, bot, self.engine.handoff(chat_id, user.lang), from_user)

    # --- client messages ------------------------------------------------------------------------

    async def on_client_message(self, message: Message, bot: Bot) -> None:
        user = self.register_user(message)
        if not await self._next_step(message, user):
            return
        action = match_menu(message.text or "")
        if action is not None:
            # A button of the old reply keyboard, still on the client's screen.
            await self._on_menu(message, bot, user, action)
            return
        if (message.text and asks_for_operator(message.text) and not message.photo
                and not await self._with_operator(message, bot, [message], message.text)):
            # Typed "operator kerak": the same as the button.
            await self._to_operator(message, bot, user, message.from_user)
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
            await self._to_operator(message, bot, user)
        elif action.kind == "settings":
            await self._reply_buttons(message, t("settings", user.lang), settings_keyboard(user.lang), menu=True,
                                      bot=bot)
        elif action.kind == "end":
            await self._client_ends(bot, message.chat.id, user.lang)
        else:
            await self._ask_quick(message, bot, user, action.question)

    async def _answer(self, messages: list[Message], bot: Bot) -> None:
        """Answer one client message, or one album, as a single question."""
        first = messages[0]
        text = "\n".join(x for x in (message_text(m) for m in messages) if x)
        if await self._with_operator(first, bot, messages, text):
            return
        user = self.users.get(CHANNEL, str(first.from_user.id)) if getattr(first, "from_user", None) else None
        client_id = str(first.chat.id)
        ai_first = client_id in self._ai_first
        if not ai_first and await self._operator_first(first, bot, messages, text, user.lang if user else Lang.UZ_LATN,
                                                       client_kind=message_kind(first), files=media_files(messages)):
            return
        await bot.send_chat_action(first.chat.id, ChatAction.TYPING)
        msg = await self.to_incoming(messages, bot)
        msg.language = user.language if user else None
        reply = await self.engine.handle(msg)
        await self._deliver(first, bot, reply, client_kind=message_kind(first), files=media_files(messages))
        if opens_conversation(reply):
            self._ai_first.discard(client_id)

    async def _with_operator(self, first: Message, bot: Bot, messages: list[Message], text: str) -> bool:
        """While a person handles the conversation, the client's messages go to them, not to the model."""
        client_id = str(first.chat.id)
        conversation = self.feedback.conversation(CHANNEL, client_id)
        if not self.handoff_ready or conversation is None or conversation.handoff_id is None:
            return False
        handoff = self.handoffs.get(conversation.handoff_id)
        if handoff is None:
            return False
        self.handoffs.touch(handoff.id)
        if handoff.status == WAITING:
            # The operator has not answered yet: if the AI takes over, it answers this too.
            self._pending.setdefault(client_id, []).append((first, list(messages), text))
        else:
            # Someone answered (maybe from the admin bot or panel): nothing waits for the AI any more.
            self._pending.pop(client_id, None)
        kind = message_kind(messages[0]) if messages else "text"
        masked = mask_pii(text)
        self._log(client_id, LOG_CLIENT, masked or MEDIA_PLACEHOLDER[kind], kind, conversation.id, handoff.id,
                  files=media_files(messages))
        chat = handoff.support_chat_id
        if chat:
            posted = await bot.send_message(
                int(chat), f"💬 Клиент (эскалация #{handoff.id}): {masked or MEDIA_PLACEHOLDER[kind]}"[:4096],
                reply_to_message_id=int(handoff.support_message_id),
            )
            self.handoffs.link_post(handoff.id, chat, str(posted.message_id))
            for m in messages:
                if m.photo or m.voice or m.document or m.audio:
                    forwarded = await bot.forward_message(int(chat), m.chat.id, m.message_id)
                    self.handoffs.link_post(handoff.id, chat, str(forwarded.message_id))
        if self.staff is not None:
            who = self._client_name(first)
            await self.staff.notify(client_id, f"💬 {who}: {masked or MEDIA_PLACEHOLDER[kind]}",
                                    conversation.operator_id or handoff.assigned_id,
                                    await self._media(bot, messages))
        try:
            # A quiet "seen by support" mark instead of another message.
            await first.react([ReactionTypeEmoji(emoji="👀")])
        except Exception:
            pass
        return True

    def _client_name(self, message: Message) -> str:
        user = getattr(message, "from_user", None)
        if user is None:
            return f"Клиент {message.chat.id}"
        return user.full_name or (f"@{user.username}" if user.username else f"Клиент {message.chat.id}")

    async def _media(self, bot: Bot, messages: list[Message]) -> list[tuple[str, bytes]]:
        """The client's photos, voice messages and files, to pass to the staff through the admin bot."""
        out = []
        for m in messages:
            kind, obj = ("photo", m.photo[-1]) if m.photo else ("voice", m.voice) if m.voice else (
                "audio", m.audio) if m.audio else ("document", m.document) if m.document else (None, None)
            if obj is None:
                continue
            try:
                out.append((kind, await self._download(bot, obj)))
            except Exception as e:  # noqa: BLE001 - too large or gone: the text still reaches the staff
                log.warning("client media not passed to staff: %s", e)
        return out

    # --- operator first: a new conversation goes to a free operator, else (or after a silence) to the AI ----

    def operator_first_on(self) -> bool:
        return (self.settings.operator_first and self.handoff_ready
                and bool(self.operators.available()))

    def free_operator(self) -> Operator | None:
        operators = self.operators.available()
        if not operators:
            return None
        return pick_free(operators, self.handoffs.busy_operators(self.settings.operator_idle_minutes),
                         self.handoffs.last_assigned(), self.settings.operator_max_sessions)

    async def _operator_first(self, message: Message, bot: Bot, messages: list[Message], text: str, lang: Lang,
                              from_user=None, client_kind: str = "text", files: list[str] = ()) -> bool:
        """A new conversation: connect a free operator and wait for them. False if the AI should answer now."""
        client_id = str(message.chat.id)
        if not self.operator_first_on() or self.feedback.conversation(CHANNEL, client_id) is not None:
            return False
        operator = self.free_operator()
        if operator is None:
            # Everyone is busy: the AI greets the client and takes the conversation.
            self._log(client_id, LOG_SYSTEM, "Все операторы заняты — разговор ведёт AI")
            return await self._ai_greets(bot, client_id, lang, text, messages, kind=client_kind, files=files)
        reply = BotReply(t("connecting_operator", lang), lang, escalate=True,
                         escalation_reason=NEW_CONVERSATION_REASON, client_text=mask_pii(text))
        await self._deliver(message, bot, reply, from_user, client_kind, files, operator=operator,
                            wait=self.settings.operator_wait_seconds)
        self._pending[client_id] = [(message, list(messages), text)]
        return True

    async def _ai_greets(self, bot: Bot, client_id: str, lang: Lang, text: str, messages: list[Message],
                         log_client: bool = True, kind: str = "text", files: list[str] = ()) -> bool:
        """The AI takes the conversation and greets the client.

        With nothing to answer yet (no question, only a greeting) it also asks how it can help and returns
        True; otherwise the caller answers the question next.
        """
        conversation = self.feedback.bot_answered(CHANNEL, client_id, lang.value)
        question = any(image_source(m) or m.voice or m.audio or m.document for m in messages) or (
            bool(text.strip()) and not is_small_talk(text))
        if not question:
            if log_client and (text.strip() or kind != "text"):
                self._log(client_id, LOG_CLIENT, mask_pii(text) or MEDIA_PLACEHOLDER[kind], kind, conversation.id,
                          files=files)
            greeting = f"{t('ai_takeover', lang)}\n\n{t('ai_how_help', lang)}"
            await self._push_buttons(bot, client_id, greeting, main_keyboard(lang), menu=True)
            self._log(client_id, LOG_BOT, greeting, "text", conversation.id)
            return True
        await self._clear_buttons(client_id, bot)
        await bot.send_message(int(client_id), t("ai_takeover", lang))
        self._log(client_id, LOG_BOT, t("ai_takeover", lang), "text", conversation.id)
        return False

    async def expire_waits(self, bot: Bot, now=None) -> int:
        """Hand the conversations whose operator stayed silent to the AI; returns how many."""
        taken = 0
        for handoff in self.handoffs.due(now):
            if not self.handoffs.to_ai(handoff.id):
                continue
            taken += 1
            try:
                await self._ai_takes_over(bot, handoff)
            except Exception as e:  # noqa: BLE001 - one failed chat must not stop the others
                log.warning("AI takeover of hand-off %s failed: %s", handoff.id, e)
        return taken

    async def _ai_takes_over(self, bot: Bot, handoff: Handoff) -> None:
        client_id, lang = handoff.client_chat_id, _lang(handoff.language)
        conversation = self.feedback.release(handoff.id)
        pending = self._pending.pop(client_id, [])
        if conversation is None:
            return  # the client ended the conversation meanwhile
        who = handoff.assigned_name or "Оператор"
        self._log(client_id, LOG_SYSTEM, f"{who} не ответил за {self.settings.operator_wait_seconds} с — разговор ведёт AI",
                  "text", conversation.id, handoff.id)
        try:
            if handoff.support_chat_id:
                await bot.send_message(
                    int(handoff.support_chat_id),
                    f"⏱ {who} не ответил за {self.settings.operator_wait_seconds} с — клиенту (эскалация #{handoff.id}) "
                    "отвечает AI. Ответьте реплаем на пост, чтобы забрать разговор.",
                    reply_to_message_id=int(handoff.support_message_id))
            if self.staff is not None:
                await self.staff.notify(client_id, f"⏱ {who} не ответил за {self.settings.operator_wait_seconds} с — "
                                        "клиенту пока отвечает AI. Откройте диалог и напишите, чтобы забрать его.",
                                        handoff.assigned_id)
        except Exception as e:  # noqa: BLE001 - the client still gets the AI
            log.warning("timeout note not posted: %s", e)
        # After a restart the waiting messages are gone: then the AI just asks how it can help.
        reply_to = pending[0][0] if pending else None
        messages = [m for _, ms, _ in pending for m in ms]
        text = "\n".join(x for _, _, x in pending if x) if pending else ""
        if await self._ai_greets(bot, client_id, lang, text, messages, log_client=False) or reply_to is None:
            return
        await bot.send_chat_action(int(client_id), ChatAction.TYPING)
        msg = await self.to_incoming(messages, bot) if messages else IncomingMessage(user_id=client_id, text=text)
        msg.user_id, msg.text = client_id, text or msg.text
        self.engine.remember_language(client_id, lang)
        reply = await self.engine.handle(msg)
        await self._deliver(reply_to, bot, reply, log_client=False)

    async def watch_waits(self, bot: Bot, every: float = 5.0) -> None:
        """Runs with the bot: checks for silent operators every few seconds and for idle conversations
        (survives restarts via the DB)."""
        while True:
            await asyncio.sleep(every)
            try:
                await self.expire_waits(bot)
            except Exception as e:  # noqa: BLE001
                log.warning("operator wait check failed: %s", e)
            try:
                await self.expire_idle(bot)
            except Exception as e:  # noqa: BLE001
                log.warning("idle conversation check failed: %s", e)

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

    async def _deliver(self, message: Message, bot: Bot, reply: BotReply, user=None, client_kind: str = "text",
                       files: list[str] = (), operator: Operator | None = None, wait: int | None = None,
                       log_client: bool = True) -> None:
        opens = opens_conversation(reply)
        # Answers read like a support agent's chat message: no signature and no buttons under them.
        if reply.show_menu:
            await self._reply_buttons(message, reply.text, main_keyboard(reply.language), menu=True, bot=bot)
        else:
            if opens:
                await self._clear_buttons(str(message.chat.id), bot)
            await message.answer(reply.text)
        await self._send_videos(message, reply)
        client_id = str(message.chat.id)
        handoff_id = await self._escalate(message, bot, reply, user, operator, wait) if reply.escalate else None
        if handoff_id is not None:
            # The conversation reached a person: the operator is rated for it, not the bot.
            conversation = self.feedback.escalated(CHANNEL, client_id, handoff_id, reply.language.value, reply.topic)
        elif opens:
            conversation = self.feedback.bot_answered(CHANNEL, client_id, reply.language.value, reply.topic)
        else:
            conversation = self.feedback.conversation(CHANNEL, client_id)
        conversation_id = conversation.id if conversation else None
        if log_client and (reply.client_text or client_kind != "text"):
            self._log(client_id, LOG_CLIENT, reply.client_text or MEDIA_PLACEHOLDER[client_kind], client_kind,
                      conversation_id, handoff_id, files=files)
        self._log(client_id, LOG_BOT, reply.text, "text", conversation_id, handoff_id)
        if handoff_id is not None:
            self._log(client_id, LOG_SYSTEM, f"Передано специалисту: {reply.escalation_reason or '-'}", "text",
                      conversation_id, handoff_id)

    def _log(self, client_id: str, sender: str, text: str, kind: str = "text", conversation_id: int | None = None,
             handoff_id: int | None = None, operator_id: str | None = None, operator_name: str | None = None,
             files: list[str] = ()) -> None:
        if conversation_id is None and sender != LOG_SYSTEM:
            conversation = self.feedback.conversation(CHANNEL, client_id)
            conversation_id = conversation.id if conversation else None
        try:
            self.chatlog.add(CHANNEL, client_id, sender, text, kind, operator_id=operator_id,
                             operator_name=operator_name, handoff_id=handoff_id, conversation_id=conversation_id,
                             files=files)
        except Exception as e:  # the log must never break the conversation itself
            log.warning("chat log failed: %s", e)

    async def _send_videos(self, message: Message, reply: BotReply) -> None:
        """Instruction videos after the answer; a failed video never breaks the answer itself."""
        for video in reply.videos:
            media = video_input(video, self._video_file_ids)
            if media is None:
                continue
            try:
                sent = await message.answer_video(media, caption=video.title[:1024], supports_streaming=True)
            except Exception as e:  # noqa: BLE001 - Telegram/network errors: the text answer is already sent
                log.warning("video %s not sent: %s", video.id, e)
                continue
            if sent.video:
                # Upload once: later clients get the same file by id.
                self._video_file_ids[video.id] = sent.video.file_id

    async def _escalate(self, message: Message, bot: Bot, reply: BotReply, user=None, operator: Operator | None = None,
                        wait: int | None = None) -> int | None:
        """Hand the client to a person: post to the support chat and/or tell the staff in the admin bot.

        Returns the hand-off's id (None with neither SUPPORT_CHAT_ID nor ADMIN_BOT_TOKEN). It goes to
        `operator`, or to a free one if there is any; with `wait` the AI takes over after that many seconds
        of the operator's silence.
        """
        chat = self.settings.support_chat_id
        if not self.handoff_ready:
            log.warning("escalation without SUPPORT_CHAT_ID or ADMIN_BOT_TOKEN: chat=%s reason=%s", message.chat.id,
                        reply.escalation_reason)
            return None
        user = user or message.from_user
        who = f"@{user.username}" if user and user.username else (user.full_name if user else "?")
        client = self.users.get(CHANNEL, str(user.id)) if user else None
        client_id = str(message.chat.id)
        # Internal only: the AI's view of the client and the client's level, for the operator.
        assessment = await self.engine.assess_client(client_id)
        ai = render_assessment("ai", assessment) if assessment else "AI-оценка клиента: нет данных"
        # The level counts this case too (saved below, once the hand-off has an id).
        level = client_level(self.feedback.tones(CHANNEL, client_id) + ([assessment.tone] if assessment else []))
        summary = (
            f"🆘 Эскалация от {who} (chat {message.chat.id})\n"
            f"Телефон: {client.phone if client and client.phone else '-'}\n"
            f"Причина: {reply.escalation_reason or '-'}\n"
            f"Язык: {reply.language.value} · Тема: {reply.topic or '-'} · Экран: {reply.screen_id or '-'}\n"
            f"Уровень клиента: {level}\n\n"
            f"🙋 Клиент: {reply.client_text or '(изображение/голос)'}\n\n"
            f"🤖 Бот ответил: {reply.text}\n\n"
            f"{ai}"
        )
        operator = operator or self.free_operator()
        ask = ""
        if operator is not None:
            ask = (f"Новое обращение. Ответьте в течение {wait} с, иначе клиенту ответит AI." if wait
                   else "Обращение назначено вам.")
        if chat is not None:
            text = (f"{summary}\n\nОтветьте реплаем на это сообщение — бот перешлёт ответ клиенту от имени специалиста "
                    "поддержки. Новые сообщения клиента придут сюда же реплаями. Кнопки ниже: тон клиента, заметка, "
                    "завершение разговора.")
            entities = None
            if operator is not None:
                mention, entities = operator_mention(operator)
                text = f"{mention}, {ask[0].lower()}{ask[1:]}\n\n{text}"
            posted = await bot.send_message(chat, text[:4096], reply_markup=tone_keyboard(), entities=entities)
            where = (str(chat), str(posted.message_id))
        else:
            # Only the admin bot: the hand-off has no post; a unique key stands in for it.
            where = ("", f"dm:{client_id}:{time.time_ns()}")
        handoff_id = self.handoffs.open(CHANNEL, client_id, *where, reply, self.engine.recent_turns(client_id))
        if operator is not None:
            self.handoffs.assign(handoff_id, operator.user_id, operator.name, wait)
        if assessment is not None:
            self.feedback.assess(CHANNEL, client_id, "ai", assessment, handoff_id)
        if chat is not None and (message.photo or message.voice or message.document or message.audio):
            forwarded = await bot.forward_message(chat, message.chat.id, message.message_id)
            self.handoffs.link_post(handoff_id, str(chat), str(forwarded.message_id))
        if self.staff is not None:
            note = (f"{ask}\n\n" if ask else "") + summary + (
                "\n\nОткройте диалог и пишите прямо в этот бот: сообщения уйдут клиенту обычным текстом.")
            await self.staff.notify(client_id, note, operator.user_id if operator else None,
                                    await self._media(bot, [message]))
        return handoff_id

    # --- operator side --------------------------------------------------------------------------

    async def on_operator_reply(self, message: Message, bot: Bot) -> None:
        handoff = self.handoffs.find(str(message.chat.id), str(message.reply_to_message.message_id))
        text = message.text or message.caption
        if handoff is None or not text:
            return
        u = message.from_user
        # Admins (ADMIN_IDS) always reach the client; everyone else only once an admin added them.
        if not (u and u.id in self.settings.admin_ids) and not self.operators.may_answer(u.id if u else None):
            await message.answer(NOT_OPERATOR.format(id=u.id if u else "-"))
            return
        lang = _lang(handoff.language)
        await self._clear_buttons(handoff.client_chat_id, bot)
        await bot.send_message(int(handoff.client_chat_id), text)
        operator_id, operator_name = (str(u.id), u.full_name) if u else (None, None)
        # The conversation is this operator's now, also if they answered after the AI took it over.
        self.handoffs.claim(handoff.id, operator_id, operator_name)
        self._pending.pop(handoff.client_chat_id, None)
        if handoff.status == WITH_AI:
            self._log(handoff.client_chat_id, LOG_SYSTEM, f"{operator_name or 'Оператор'} забрал разговор у AI",
                      "text", None, handoff.id)
        conversation = self.feedback.operator_replied(CHANNEL, handoff.client_chat_id, handoff.id, lang.value,
                                                      operator_id, operator_name)
        self._log(handoff.client_chat_id, LOG_OPERATOR, text, "text", conversation.id, handoff.id, operator_id,
                  operator_name)
        candidate, created = self.handoffs.add_operator_reply(handoff.id, text, operator_id, operator_name)
        if candidate is not None and created:
            await message.answer(f"Ответ сохранён. Кандидат #{candidate.id} в базу знаний:\n\n"
                                 f"{candidate_text(candidate)}\n\n{REVIEW_HELP}",
                                 reply_markup=candidate_keyboard(candidate.id))

    async def on_panel(self, message: Message) -> None:
        await message.answer(PANEL_TEXT, reply_markup=panel_keyboard())

    async def on_panel_button(self, callback: CallbackQuery) -> None:
        """Panel buttons edit the panel message in place, like the admin bot; candidates come as their own messages."""
        await callback.answer()
        what = (callback.data or "").split(":", 1)[1]
        if what == "candidates":
            await self._send_candidates(callback.message)
            return
        if what == "rating":
            text, markup = render_rating(self.feedback)[:4096], panel_back_keyboard()
        else:
            text, markup = PANEL_TEXT, panel_keyboard()
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # unchanged or too old to edit: send it anew
            await callback.message.answer(text, reply_markup=markup)

    async def on_note_button(self, callback: CallbackQuery) -> None:
        handoff = self.handoffs.find(str(callback.message.chat.id), str(callback.message.message_id))
        if handoff is None:
            await callback.answer("Эскалация не найдена.")
            return
        await callback.answer()
        await callback.message.answer(
            f"{NOTE_PROMPT.format(id=handoff.id)}: ответьте на это сообщение — суть проблемы | предложения клиента.",
            reply_markup=ForceReply(),
        )

    async def on_candidate_button(self, callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await callback.answer()
            return
        action, candidate_id = parts[1], int(parts[2])
        reviewer = reviewer_name(callback)
        if action == "edit":
            await callback.answer()
            await callback.message.answer(
                f"{EDIT_PROMPT.format(id=candidate_id)}: ответьте на это сообщение исправленным текстом "
                "(без деталей конкретного клиента).",
                reply_markup=ForceReply(),
            )
            return
        try:
            if action == "approve":
                c = self.handoffs.approve(candidate_id, reviewer)
                self.reload_knowledge()
                done = f"Кандидат #{c.id} добавлен в базу знаний, бот использует его со следующего вопроса."
            elif action == "reject":
                c = self.handoffs.reject(candidate_id, reviewer)
                done = f"Кандидат #{c.id} отклонён."
            else:
                await callback.answer()
                return
        except ReviewError as e:
            await callback.answer()
            await callback.message.answer(review_error_text(candidate_id, e))
            return
        await callback.answer(done)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # too old to edit: the decision is saved anyway
            pass
        await callback.message.answer(done)

    async def on_prompt_reply(self, message: Message) -> None:
        """The operator answered the bot's prompt: a client note or a corrected KB answer."""
        match = PROMPT_RE.match(message.reply_to_message.text or "")
        body = (message.text or "").strip()
        target = int(match.group(2))
        if not body:
            await message.answer("Пустой ответ, ничего не сохранено.")
            return
        if match.group(1).startswith("📝"):
            handoff = self.handoffs.get(target)
            if handoff is None:
                await message.answer("Эскалация не найдена.")
                return
            problem, _, suggestions = body.partition("|")
            self.feedback.note(CHANNEL, handoff.client_chat_id, handoff.id, reviewer_name(message), problem, suggestions)
            await message.answer(f"Заметка о клиенте сохранена. Уровень клиента: "
                                 f"{self.feedback.level(CHANNEL, handoff.client_chat_id)}")
            return
        try:
            c = self.handoffs.approve(target, reviewer_name(message), body)
        except ReviewError as e:
            await message.answer(review_error_text(target, e))
            return
        self.reload_knowledge()
        await message.answer(f"Кандидат #{c.id} добавлен в базу знаний с вашим текстом.")

    # --- ending a conversation and rating it ----------------------------------------------------

    async def _end(self, bot: Bot, client_chat_id: int, by: str) -> bool:
        """Close the client's open conversation and ask them to rate it; False if nothing was open."""
        ended = self.feedback.end(CHANNEL, str(client_chat_id), by)
        if ended is None:
            return False
        conversation, request = ended
        if conversation.handoff_id is not None:
            self.handoffs.close_handoff(conversation.handoff_id)  # the operator is free for the next client
        self._pending.pop(str(client_chat_id), None)
        self._log(str(client_chat_id), LOG_SYSTEM,
                  {OPERATOR: "Разговор завершил специалист",
                   TIMEOUT: f"Разговор завершён автоматически: нет сообщений {self.settings.session_idle_minutes} мин"}
                  .get(by, "Разговор завершил клиент"), "text",
                  conversation.id, conversation.handoff_id)
        lang = _lang(conversation.language)
        text = t("rate_operator" if request.target == OPERATOR else "rate_bot", lang)
        if by in (OPERATOR, TIMEOUT):
            text = f"{t('ended_by_operator' if by == OPERATOR else 'ended_by_timeout', lang)}\n{text}"
        # The rating buttons stay until the client rates; the answer's "end / menu" buttons go away.
        await self._push_buttons(bot, client_chat_id, text, rating_keyboard(request, lang), track=False)
        await self._assess_ended(bot, conversation, by)
        self.engine.end_conversation(str(client_chat_id))
        return True

    async def _assess_ended(self, bot: Bot, conversation, by: str) -> None:
        """After the close: the AI assesses the client from the whole conversation, and the operator who had it
        is asked to assess the client too. For the support team only; the client never sees any of it."""
        client_id = conversation.client_id
        turns = [Turn("user" if m.sender == LOG_CLIENT else "assistant", m.text)
                 for m in self.chatlog.for_conversation(conversation.id) if m.sender != LOG_SYSTEM and m.text]
        assessment = await self.engine.assess_client(client_id, turns)
        if assessment is not None:
            self.feedback.assess(CHANNEL, client_id, "ai", assessment, conversation.handoff_id)
        handoff = self.handoffs.get(conversation.handoff_id) if conversation.handoff_id is not None else None
        if handoff is None or not handoff.support_chat_id:
            return  # the bot handled it alone (no operator to ask), or there is no support chat to ask in
        who = {OPERATOR: "специалист", TIMEOUT: f"автоматически, нет сообщений {self.settings.session_idle_minutes} мин"}
        ai = render_assessment("ai", assessment) if assessment else "AI-оценка клиента: нет данных"
        text = (f"🏁 Разговор с клиентом (эскалация #{handoff.id}) завершён — {who.get(by, 'клиент')}.\n"
                f"Оцените клиента: как он общался? При необходимости добавьте заметку.\n\n"
                f"{ai}\nУровень клиента: {self.feedback.level(CHANNEL, client_id)}")
        try:
            posted = await bot.send_message(int(handoff.support_chat_id), text[:4096],
                                            reply_markup=closed_tone_keyboard(),
                                            reply_to_message_id=int(handoff.support_message_id))
        except Exception as e:  # noqa: BLE001 - the conversation is closed and rated anyway
            log.warning("assessment request for hand-off %s not posted: %s", handoff.id, e)
            return
        # The tone and note buttons under this post work like the ones under the escalation post.
        self.handoffs.link_post(handoff.id, handoff.support_chat_id, str(posted.message_id))

    async def expire_idle(self, bot: Bot, now: datetime | None = None) -> int:
        """Close the conversations nobody wrote in for SESSION_IDLE_MINUTES; returns how many."""
        minutes = self.settings.session_idle_minutes
        if minutes <= 0:
            return 0
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=minutes)
        closed = 0
        for conversation in self.feedback.open_conversations(CHANNEL):
            if conversation.client_id in self._pending:
                continue  # still waiting for an operator: the operator wait decides first
            last = max(filter(None, (conversation.opened_at, self.chatlog.last_at(CHANNEL, conversation.client_id))))
            if datetime.fromisoformat(last) > cutoff:
                continue
            try:
                closed += await self._end(bot, int(conversation.client_id), TIMEOUT)
            except Exception as e:  # noqa: BLE001 - one failed chat must not stop the others
                log.warning("idle conversation %s not closed: %s", conversation.id, e)
        return closed

    async def _client_ends(self, bot: Bot, chat_id: int, lang: Lang) -> None:
        if not await self._end(bot, chat_id, "client"):
            await self._push_buttons(bot, chat_id, t("no_conversation", lang), main_keyboard(lang), menu=True)

    async def on_end_button(self, callback: CallbackQuery, bot: Bot) -> None:
        await callback.answer()
        if callback.message is None:
            return
        user = self.users.get(CHANNEL, str(callback.from_user.id))
        await self._client_ends(bot, callback.message.chat.id, user.lang if user else Lang.UZ_LATN)

    async def _operator_ends(self, bot: Bot, handoff) -> str:
        if handoff is None:
            return "Эскалация не найдена."
        if self.feedback.conversation_for_handoff(handoff.id) is None:
            return "Разговор уже завершён."
        await self._end(bot, int(handoff.client_chat_id), OPERATOR)
        return "Разговор завершён, клиента попросили его оценить."

    async def on_operator_end(self, message: Message, bot: Bot) -> None:
        """/end as a reply to an escalation post (the button under it does the same)."""
        handoff = self.handoffs.find(str(message.chat.id), str(message.reply_to_message.message_id))
        await message.answer(await self._operator_ends(bot, handoff))

    async def on_operator_end_button(self, callback: CallbackQuery, bot: Bot) -> None:
        handoff = self.handoffs.find(str(callback.message.chat.id), str(callback.message.message_id))
        await callback.answer(await self._operator_ends(bot, handoff))

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
            await callback.message.edit_text(f"{t('rate_thanks', lang)} {mark.get(rated.outcome, f'{rated.stars}/5')}")
        except Exception:  # message too old to edit: the rating is saved anyway
            pass
        if rated.target == BOT and rated.stars <= 2:
            await self._reply_buttons(callback.message, t("rate_bot_low", lang), main_keyboard(lang), menu=True,
                                      bot=bot)

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
        """/client <суть проблемы> | <предложения>, as a reply to an escalation post (the note button does the same)."""
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
        await self._send_candidates(message)

    async def _send_candidates(self, message: Message) -> None:
        pending = self.handoffs.candidates()
        if not pending:
            await message.answer("Новых кандидатов в базу знаний нет.")
            return
        await message.answer(f"Кандидаты в базу знаний: {len(pending)}. {REVIEW_HELP}")
        for c in pending:
            await message.answer(candidate_text(c)[:4096], reply_markup=candidate_keyboard(c.id))

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


def operator_mention(operator: Operator) -> tuple[str, list[MessageEntity] | None]:
    """How the support-chat post names the operator so Telegram notifies them (the mention starts the post)."""
    if operator.username:
        return f"@{operator.username}", None
    name = operator.name or operator.user_id
    length = len(name.encode("utf-16-le")) // 2
    return name, [MessageEntity(type="text_mention", offset=0, length=length,
                                user=TgUser(id=int(operator.user_id), is_bot=False, first_name=name))]


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


def reviewer_name(event: Message | CallbackQuery) -> str:
    u = event.from_user
    if u is None:
        return str(event.chat.id)
    return f"@{u.username}" if u.username else f"{u.full_name} ({u.id})"


def review_error_text(candidate_id: int, e: ReviewError) -> str:
    reason = str(e)
    if reason == "not_found":
        return f"Кандидата #{candidate_id} нет."
    if reason == "already_reviewed":
        return f"Кандидат #{candidate_id} уже проверен."
    return (f"Кандидат #{candidate_id} не добавлен: в ответе есть то, что бот говорить не может "
            f"({reason.removeprefix('forbidden: ')}). Пришлите /approve {candidate_id} <исправленный ответ>.")


# Clients use buttons; /start is the only command in Telegram's "Menu" (Uzbek default, ru/en by app language).
COMMANDS = {
    None: [("start", "Boshlash")],
    "ru": [("start", "Начать")],
    "en": [("start", "Start")],
}
# In the support chat: the operator panel.
SUPPORT_COMMANDS = [("menu", "Панель поддержки")]


async def set_commands(bot: Bot, support_chat_id: int | None = None) -> None:
    for code, commands in COMMANDS.items():
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in commands], language_code=code)
    if support_chat_id is not None:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in SUPPORT_COMMANDS],
                                  scope=BotCommandScopeChat(chat_id=support_chat_id))


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
    chatlog = ChatLog(settings.db_path)
    operators = OperatorStore(settings.db_path)
    guides = GuideStore(settings.db_path)
    # Staff hear about clients from the admin bot, so their traffic stays off this bot.
    staff = (StaffNotifier(telegram_call(settings.admin_bot_token), operators, settings.admin_ids)
             if settings.admin_bot_token else None)
    support = TelegramSupportBot(settings, build_engine(settings, handoffs), users, handoffs, feedback, chatlog,
                                 operators=operators, guides=guides, staff=staff)
    dp.include_router(support.router)
    try:
        await set_commands(bot, settings.support_chat_id)
    except Exception as e:  # commands are a convenience; the bot works without them
        log.warning("set_my_commands failed: %s", e)
    waits = asyncio.create_task(support.watch_waits(bot))
    try:
        await dp.start_polling(bot)
    finally:
        waits.cancel()


if __name__ == "__main__":
    asyncio.run(main())
