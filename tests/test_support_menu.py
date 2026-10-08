"""The support menu in the admin bot: staff chat with clients there, never through the client bot."""
import asyncio
from types import SimpleNamespace as NS

from fakes import FakeLLM, FakeSTT, answer

from ai_support.admin.bot import AdminBot, dialog_text, support_view
from ai_support.admin.data import AdminData
from ai_support.admin.web import create_app
from ai_support.channels.telegram_bot import TelegramSupportBot
from ai_support.chatlog import ChatLog
from ai_support.config import Settings
from ai_support.engine import SupportEngine
from ai_support.feedback import FeedbackStore
from ai_support.handoffs import HandoffStore
from ai_support.models import BotReply, Lang
from ai_support.operators import OperatorStore
from ai_support.prompt import RULES
from ai_support.staff import StaffNotifier
from ai_support.users import UserStore

ADMIN = 42


class FakeTelegram:
    """A Bot API stand-in: records (method, payload)."""

    def __init__(self, fail_for=()):
        self.calls, self.fail_for, self.next_id = [], set(fail_for), 500

    async def __call__(self, method, payload):
        if payload.get("chat_id") in self.fail_for:
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        self.calls.append((method, payload))
        self.next_id += 1
        return {"message_id": self.next_id}

    def to(self, chat_id):
        return [p for _, p in self.calls if p.get("chat_id") == chat_id]


# --- the client bot tells the staff through the admin bot ---------------------------------------

class ClientChat:
    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None, **_):
        self.sent.append((text, reply_markup))
        return NS(message_id=len(self.sent))

    async def send_message(self, chat, text, **_):
        self.sent.append((text, None))
        return NS(message_id=len(self.sent))

    async def send_chat_action(self, *_):
        pass


def client_msg(chat, text, uid=7):
    async def react(*_):
        pass
    return NS(text=text, caption=None, photo=None, document=None, reply_to_message=None, voice=None, audio=None,
              media_group_id=None, chat=NS(id=uid), message_id=1, answer=chat.answer, react=react,
              from_user=NS(id=uid, username="anvar", full_name="Anvar", language_code="uz"))


def client_bot(llm, admin_api, operators=None):
    users = UserStore(":memory:")
    users.touch("telegram", "7", "7", "anvar", "Anvar", "uz")
    users.set_language("telegram", "7", Lang.UZ_LATN)
    users.set_phone("telegram", "7", "998901234567")
    operators = operators or OperatorStore(":memory:")
    staff = StaffNotifier(admin_api, operators, {ADMIN})
    # No SUPPORT_CHAT_ID: the admin bot is the only way to the staff.
    return TelegramSupportBot(Settings(), SupportEngine(llm, FakeSTT()), users, operators=operators, staff=staff)


def test_escalation_without_support_chat_reaches_staff_in_the_admin_bot():
    llm = FakeLLM(answer("Mutaxassisga uzatyapman.", "uz_latn", escalate=True, reason="refund"))
    admin_api, chat = FakeTelegram(), ClientChat()
    bot = client_bot(llm, admin_api)
    asyncio.run(bot.on_client_message(client_msg(chat, "Pulimni qaytaring"), chat))
    conversation = bot.feedback.conversation("telegram", "7")
    handoff = bot.handoffs.get(conversation.handoff_id)
    assert handoff.support_chat_id == "" and handoff.support_message_id.startswith("dm:7:")
    (note,) = admin_api.to(ADMIN)
    assert "Pulimni qaytaring" in note["text"] and "refund" in note["text"]
    assert note["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "a:chat:7"
    # The client sees a plain message: no signature and no buttons.
    assert chat.sent[-1][1] is None and not chat.sent[-1][0].startswith("Yordamchi")
    # The client's next message goes to the staff, not to the model.
    asyncio.run(bot.on_client_message(client_msg(chat, "Hali ham kelmadi"), chat))
    assert len(llm.calls) == 1 and "Hali ham kelmadi" in admin_api.to(ADMIN)[-1]["text"]


def test_client_messages_go_to_the_operator_handling_them():
    llm = FakeLLM(answer("Uzatyapman.", "uz_latn", escalate=True, reason="x"))
    admin_api, chat, operators = FakeTelegram(), ClientChat(), OperatorStore(":memory:")
    operators.add(10, "Ali")
    operators.add(11, "Vali")
    bot = client_bot(llm, admin_api, operators)
    asyncio.run(bot.on_client_message(client_msg(chat, "Karta bloklandi"), chat))
    handoff = bot.handoffs.get(bot.feedback.conversation("telegram", "7").handoff_id)
    bot.feedback.operator_replied("telegram", "7", handoff.id, "uz_latn", "11", "Vali")
    admin_api.calls.clear()
    asyncio.run(bot.on_client_message(client_msg(chat, "Rahmat, kutaman"), chat))
    assert [p["chat_id"] for _, p in admin_api.calls] == [11]


def test_staff_notifier_falls_back_to_admins():
    operators = OperatorStore(":memory:")
    api = FakeTelegram()
    assert StaffNotifier(api, operators, {ADMIN}).recipients() == [ADMIN]  # no operators yet
    operators.add(10, "Ali")
    notifier = StaffNotifier(FakeTelegram(fail_for={10}), operators, {ADMIN})
    assert notifier.recipients() == [10]
    # Ali never opened the admin bot: the admin gets it instead of nobody.
    assert asyncio.run(notifier.notify("7", "Yangi mijoz")) == 1
    assert [p["chat_id"] for _, p in notifier.call.calls] == [ADMIN]


def test_staff_notifier_passes_client_photos():
    api = FakeTelegram()
    asyncio.run(StaffNotifier(api, OperatorStore(":memory:"), {ADMIN}).notify("7", "Skrinshot",
                                                                                media=[("photo", b"jpg")]))
    assert [m for m, _ in api.calls] == ["sendPhoto", "sendMessage"] and api.calls[0][1]["photo"] == b"jpg"


# --- the support menu and the dialog in the admin bot ----------------------------------------------

def waiting_client(db):
    users, handoffs, feedback = UserStore(db), HandoffStore(db), FeedbackStore(db)
    users.touch("telegram", "7", "7", "anvar", "Anvar")
    reply = BotReply("Mutaxassisga uzatyapman.", Lang.UZ_LATN, escalate=True, topic="refund",
                     client_text="Pulimni qaytaring")
    hid = handoffs.open("telegram", "7", "", "dm:7:1", reply)
    conv = feedback.escalated("telegram", "7", hid, "uz_latn", "refund")
    ChatLog(db).add("telegram", "7", "client", "Pulimni qaytaring", conversation_id=conv.id, handoff_id=hid)
    return hid, conv


class StaffChat:
    def __init__(self):
        self.answers, self.reactions = [], []

    def message(self, user_id, text=None, contact=None):
        async def answer(text, **kw):
            self.answers.append((text, kw.get("reply_markup")))

        async def react(reaction):
            self.reactions.append(reaction)

        async def edit_text(text, **kw):
            self.answers.append((text, kw.get("reply_markup")))
        return NS(text=text, caption=None, photo=None, contact=contact, forward_origin=None,
                  chat=NS(id=user_id, type="private"), answer=answer, react=react, edit_text=edit_text,
                  from_user=NS(id=user_id, username=None, full_name=f"Staff {user_id}"))

    def callback(self, user_id, data):
        answered = []

        async def answer(text=None, **_):
            answered.append(text)
        return NS(data=data, from_user=NS(id=user_id, username=None, full_name=f"Staff {user_id}"),
                  message=self.message(user_id), answer=answer), answered


def admin_bot(db, client=None):
    data = AdminData(db)
    return AdminBot(Settings(admin_ids=frozenset({ADMIN})), data, client=client or FakeTelegram()), data


def test_support_menu_lists_waiting_clients(tmp_path):
    db = tmp_path / "bot.sqlite3"
    waiting_client(db)
    _, data = admin_bot(db)
    text, kb = support_view(data, admin=False)
    assert "ждут ответа: 1" in text
    assert kb.inline_keyboard[0][0].callback_data == "a:chat:7" and "Anvar" in kb.inline_keyboard[0][0].text
    assert all(row[0].callback_data != "a:menu" for row in kb.inline_keyboard)  # operators see only support
    assert "Pulimni qaytaring" in dialog_text(data, "7")


def test_operator_answers_a_client_from_the_admin_bot(tmp_path):
    db = tmp_path / "bot.sqlite3"
    hid, conv = waiting_client(db)
    OperatorStore(db).add(10, "Ali")
    client = FakeTelegram()
    bot, _ = admin_bot(db, client)
    assert bot.is_staff(NS(from_user=NS(id=10))) and not bot.is_staff(NS(from_user=NS(id=11)))
    staff = StaffChat()
    cb, _ = staff.callback(10, "a:chat:7")
    asyncio.run(bot.on_callback(cb))
    assert bot._dialog[10] == "7" and "Pulimni qaytaring" in staff.answers[-1][0]
    asyncio.run(bot.on_message(staff.message(10, "Assalomu alaykum, tekshiryapman")))
    # The client gets a plain message from the client bot: no name, no buttons.
    assert client.calls == [("sendMessage", {"chat_id": 7, "text": "Assalomu alaykum, tekshiryapman"})]
    assert HandoffStore(db).get(hid).status == "operator"  # the AI no longer takes it over
    assert FeedbackStore(db).conversation("telegram", "7").operator_name == "Ali"
    assert ChatLog(db).for_conversation(conv.id)[-1].text == "Assalomu alaykum, tekshiryapman"
    assert staff.reactions  # a quiet "delivered" mark
    # Ending the conversation asks the client to rate it.
    cb, answered = staff.callback(10, "a:close:7")
    asyncio.run(bot.on_callback(cb))
    assert FeedbackStore(db).conversation("telegram", "7") is None and 10 not in bot._dialog
    rating = client.calls[-1][1]
    assert rating["chat_id"] == 7 and rating["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("rate:")
    assert answered and "завершён" in answered[0]


def test_operator_cannot_use_admin_actions(tmp_path):
    db = tmp_path / "bot.sqlite3"
    OperatorStore(db).add(10, "Ali")
    bot, _ = admin_bot(db)
    staff = StaffChat()
    cb, answered = staff.callback(10, "a:ops")
    asyncio.run(bot.on_callback(cb))
    assert answered == ["Нет доступа"]


def test_message_from_staff_not_sent_is_not_saved(tmp_path):
    db = tmp_path / "bot.sqlite3"
    hid, _ = waiting_client(db)
    bot, _ = admin_bot(db, FakeTelegram(fail_for={7}))
    staff = StaffChat()
    bot._dialog[ADMIN] = "7"
    asyncio.run(bot.on_message(staff.message(ADMIN, "Salom")))
    assert "не получил" in staff.answers[-1][0] and HandoffStore(db).operator_replies(hid) == []


def test_operator_added_by_phone_links_in_the_admin_bot(tmp_path):
    db = tmp_path / "bot.sqlite3"
    OperatorStore(db).add_phone("+998901234567", "Dilnoza")
    bot, data = admin_bot(db)
    staff = StaffChat()
    other = NS(phone_number="+998901234567", user_id=99)
    asyncio.run(bot.on_stranger_contact(staff.message(20, contact=other)))
    assert not data.is_staff(20, bot.settings.admin_ids)  # someone else's contact
    own = NS(phone_number="998901234567", user_id=20)
    asyncio.run(bot.on_stranger_contact(staff.message(20, contact=own)))
    assert data.is_staff(20, bot.settings.admin_ids) and "Dilnoza" in staff.answers[-2][0]


def test_panel_reply_without_support_chat_opens_a_handoff_for_the_admin_bot(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from test_admin import TOKEN, init_data
    db = tmp_path / "bot.sqlite3"
    feedback = FeedbackStore(db)
    conv = feedback.bot_answered("telegram", "8", "ru", "cards")
    tg = FakeTelegram()
    app = create_app(AdminData(db), TOKEN, frozenset({ADMIN}), telegram=tg)

    async def go():
        async with TestClient(TestServer(app)) as c:
            r = await c.post(f"/api/sessions/{conv.id}/reply", json={"text": "Проверим"},
                             headers={"X-Telegram-Init-Data": init_data()})
            return r.status
    assert asyncio.run(go()) == 200
    assert tg.calls == [("sendMessage", {"chat_id": 8, "text": "Проверим"})]
    handoff = HandoffStore(db).get(FeedbackStore(db).conversation("telegram", "8").handoff_id)
    assert handoff.support_chat_id == "" and handoff.client_chat_id == "8"


def test_prompt_keeps_the_human_tone_and_never_claims_to_be_human():
    assert "сотрудник службы поддержки" in RULES
    assert "Никогда не упоминай искусственный интеллект" in RULES
    assert "никогда не утверждай, что ты человек" in RULES
