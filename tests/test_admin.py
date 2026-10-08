import asyncio
import json
import time
from types import SimpleNamespace as NS

from aiohttp.test_utils import TestClient, TestServer

from ai_support.admin.auth import sign, telegram_user
from ai_support.admin.bot import AdminBot, operators_text, parse_operator, sessions_text
from ai_support.admin.data import AdminData, render_overview
from ai_support.admin.web import create_app
from ai_support.channels.telegram_bot import NOT_OPERATOR, TelegramSupportBot
from ai_support.chatlog import ChatLog
from ai_support.config import Settings, parse_ids
from ai_support.engine import SupportEngine
from ai_support.feedback import FeedbackStore
from ai_support.handoffs import HandoffStore
from ai_support.models import BotReply, Lang
from ai_support.operators import OperatorError, OperatorStore, to_phone
from ai_support.users import UserStore
from fakes import FakeLLM, FakeSTT

TOKEN = "123:ABC"
ADMIN = 42


def init_data(user_id=ADMIN, token=TOKEN, auth_date=None):
    return sign({"auth_date": str(auth_date or int(time.time())), "query_id": "q",
                 "user": json.dumps({"id": user_id, "first_name": "Admin"})}, token)


# --- settings ---------------------------------------------------------------------------------

def test_admin_ids_and_url():
    assert parse_ids("1, 2;3, x,") == {1, 2, 3}
    assert Settings(admin_domain="admin.example.com").admin_url == "https://admin.example.com/"
    assert Settings(admin_domain="https://admin.example.com/").admin_url == "https://admin.example.com/"
    assert Settings().admin_url == ""


# --- mini app launch data --------------------------------------------------------------------

def test_valid_init_data_gives_user():
    assert telegram_user(init_data(), TOKEN)["id"] == ADMIN


def test_tampered_or_foreign_init_data_rejected():
    data = init_data()
    assert telegram_user(data.replace("Admin", "Hacker"), TOKEN) is None
    assert telegram_user(init_data(token="999:OTHER"), TOKEN) is None
    assert telegram_user("", TOKEN) is None
    assert telegram_user("user=%7B%22id%22%3A42%7D", TOKEN) is None  # no hash


def test_old_init_data_rejected():
    assert telegram_user(init_data(auth_date=int(time.time()) - 3 * 24 * 3600), TOKEN) is None


# --- operators -------------------------------------------------------------------------------

def test_everyone_answers_until_an_operator_is_added():
    ops = OperatorStore(":memory:")
    assert ops.may_answer(1) and ops.may_answer(None)
    ops.add(10, "Ali", "ali")
    assert ops.may_answer(10) and not ops.may_answer(11) and not ops.may_answer(None)
    ops.set_active(10, False)
    assert ops.may_answer(11)  # nobody active: back to the old behaviour


def test_operator_add_validates_and_reactivates():
    ops = OperatorStore(":memory:")
    try:
        ops.add("abc", "x")
        raise AssertionError("bad id accepted")
    except OperatorError:
        pass
    ops.add(5, "", "@vali")
    assert ops.get(5).name == "@vali" and ops.get(5).username == "vali"
    ops.set_active(5, False)
    assert ops.add(5, "Vali").active


# --- sessions as chats -----------------------------------------------------------------------

def stores(path):
    return UserStore(path), HandoffStore(path), FeedbackStore(path), ChatLog(path)


def test_session_chat_splits_by_conversation(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users, handoffs, feedback, log = stores(db)
    users.touch("telegram", "7", "7", "client7", "Client Seven")
    log.add("telegram", "7", "client", "Karta qo'shilmayapti, 8600 1234 5678 9012")
    log.add("telegram", "7", "bot", "Javob")
    feedback.bot_answered("telegram", "7", "uz_latn", "cards")
    feedback.end("telegram", "7", "client")
    log.add("telegram", "7", "system", "Клиент завершил разговор")
    data = AdminData(db)
    with data._lock:
        data._db.execute("UPDATE conversations SET closed_at = '2000-01-01T00:00:00+00:00'")
        data._db.execute("UPDATE chat_messages SET created_at = '1999-12-31T00:00:00+00:00'")
        data._db.commit()
    log.add("telegram", "7", "client", "Yana savol")
    feedback.bot_answered("telegram", "7", "uz_latn", "payments")

    sessions = data.sessions()
    assert [s["topic"] for s in sessions] == ["payments", "cards"]
    assert sessions[0]["client_name"] == "Client Seven" and sessions[0]["status"] == "open"
    old = data.session(sessions[1]["id"])
    assert [m["sender"] for m in old["messages"]] == ["client", "bot", "system"]
    assert "1234" not in old["messages"][0]["text"]  # card number masked
    new = data.session(sessions[0]["id"])
    assert [m["text"] for m in new["messages"]] == ["Yana savol"]
    assert data.sessions(client_id="7", status="closed")[0]["id"] == sessions[1]["id"]


def test_session_before_chat_log_rebuilt_from_handoff(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users, handoffs, feedback, log = stores(db)
    reply = BotReply("Operatorga uzatdim", Lang.UZ_LATN, escalate=True, topic="refund", client_text="Pulni qaytaring")
    hid = handoffs.open("telegram", "7", "-100", "55", reply)
    feedback.escalated("telegram", "7", hid, "uz_latn", "refund")
    handoffs.add_operator_reply(hid, "Tekshiryapmiz", "10", "Ali")
    feedback.operator_replied("telegram", "7", hid, "uz_latn", "10", "Ali")
    data = AdminData(db)
    s = data.session(data.sessions()[0]["id"])
    assert [m["sender"] for m in s["messages"]] == ["client", "bot", "system", "operator"]
    assert s["operator_name"] == "Ali"
    ops = data.operator_list()
    assert ops[0]["user_id"] == "10" and not ops[0]["registered"] and ops[0]["replies"] == 1
    assert "не добавлен" in operators_text(data)
    assert "#" in sessions_text(data)
    assert "Клиенты: 0" in render_overview(data.overview())


def test_clients_search_and_phone_when_present(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users, *_ = stores(db)
    users.touch("telegram", "7", "7", "anvar", "Anvar")
    users.touch("telegram", "8", "8", "bobur", "Bobur")
    data = AdminData(db)
    assert {c["chat_id"] for c in data.clients()} == {"7", "8"}
    assert [c["chat_id"] for c in data.clients("anv")] == ["7"]
    users.set_phone("telegram", "8", "+998901112233")
    assert [c["phone"] for c in data.clients("90111")] == ["+998901112233"]


# --- web API ---------------------------------------------------------------------------------

def api(app, *requests):
    """Send (method, path, user_id, body) requests in order; returns [(status, body)]."""
    async def run():
        out = []
        async with TestClient(TestServer(app)) as client:
            for method, path, user_id, body in requests:
                headers = {"X-Telegram-Init-Data": init_data(user_id)} if user_id else {}
                res = await client.request(method, path, headers=headers, json=body)
                is_json = res.content_type == "application/json"
                out.append((res.status, await res.json() if is_json else await res.text()))
        return out
    return asyncio.run(run())


def test_api_only_for_admins(tmp_path):
    app = create_app(AdminData(tmp_path / "db.sqlite3"), TOKEN, frozenset({ADMIN}))
    unsigned, stranger, admin, page = api(app, ("GET", "/api/overview", None, None),
                                          ("GET", "/api/overview", 7, None),
                                          ("GET", "/api/overview", ADMIN, None),
                                          ("GET", "/", None, None))
    assert unsigned[0] == 401
    assert stranger[0] == 403 and stranger[1]["user_id"] == 7
    assert admin[0] == 200 and admin[1]["clients"] == 0
    assert page[0] == 200 and "telegram-web-app.js" in page[1]


def test_api_adds_and_disables_operators(tmp_path):
    app = create_app(AdminData(tmp_path / "db.sqlite3"), TOKEN, frozenset({ADMIN}))
    added, bad, by_phone, phone_off, off, missing, listed, no_session = api(
        app,
        ("POST", "/api/operators", ADMIN, {"user_id": "10", "name": "Ali"}),
        ("POST", "/api/operators", ADMIN, {"user_id": "x"}),
        ("POST", "/api/operators", ADMIN, {"phone": "+998 90 555 55 55", "name": "Vali"}),
        ("POST", "/api/operators/%2B998905555555/active", ADMIN, {"active": False}),
        ("POST", "/api/operators/10/active", ADMIN, {"active": False}),
        ("POST", "/api/operators/99/active", ADMIN, {"active": True}),
        ("GET", "/api/operators", ADMIN, None),
        ("GET", "/api/sessions/123", ADMIN, None),
    )
    assert added[0] == 200 and added[1]["active"] and added[1]["added_by"] == str(ADMIN)
    assert bad[0] == 400
    assert by_phone[0] == 200 and by_phone[1]["key"] == "+998905555555" and not by_phone[1]["linked"]
    assert phone_off[0] == 200 and not phone_off[1]["active"]
    assert off[0] == 200 and not off[1]["active"]
    assert missing[0] == 404
    assert sorted(o["key"] for o in listed[1]) == ["+998905555555", "10"]
    assert no_session[0] == 404


# --- admin bot -------------------------------------------------------------------------------

def test_admin_bot_builds_and_parses_operator(tmp_path):
    bot = AdminBot(Settings(admin_ids=frozenset({ADMIN}), admin_domain="a.example.com"),
                   AdminData(tmp_path / "db.sqlite3"))
    assert bot.router.message.handlers and bot.router.callback_query.handlers
    assert parse_operator("123 Ali Valiyev") == ("123", "Ali Valiyev")
    assert parse_operator("123") == ("123", "")
    assert parse_operator("Ali 123") is None
    assert parse_operator("+998 90 123-45-67 Dilnoza Karimova") == ("+998901234567", "Dilnoza Karimova")
    assert parse_operator("998901234567 Dilnoza") == ("+998901234567", "Dilnoza")


def test_many_operators_by_phone_and_linking(tmp_path):
    from ai_support.operators import is_phone
    assert is_phone("+998 90 123 45 67") and is_phone("998901234567")
    assert not is_phone("123456789") and not is_phone("+12")
    ops = OperatorStore(":memory:")
    ops.add_phone("+998901111111", "A")
    ops.add_phone("+998902222222", "B")
    try:
        ops.add_phone("+12", "x")
        raise AssertionError("short phone accepted")
    except OperatorError:
        pass
    assert ops.may_answer(5)  # nobody linked yet
    assert ops.link("998901111111", 10, "a_user", "Ali") is not None
    assert ops.link("+998901111111", 11) is None  # number already belongs to another Telegram id
    assert ops.link("+998903333333", 12) is None  # not an operator's number
    assert ops.may_answer(10) and not ops.may_answer(11)
    assert [o.key for o in ops.all()] == ["10", "+998902222222"]
    ops.set_active("+998902222222", False)
    assert not ops.get("+998902222222").active


def test_operator_added_by_id_then_phone_is_one_row():
    ops = OperatorStore(":memory:")
    ops.add(10, "Ali")
    ops.add_phone("+998901111111", "Ali")
    ops.link("+998901111111", 10)
    assert [(o.user_id, o.phone) for o in ops.all()] == [("10", "+998901111111")]


def test_old_operator_table_is_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "bot.sqlite3"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE operators (user_id TEXT PRIMARY KEY, name TEXT NOT NULL, username TEXT,
                   active INTEGER NOT NULL DEFAULT 1, added_by TEXT, created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL)""")
    con.execute("INSERT INTO operators VALUES ('10', 'Ali', NULL, 1, '42', '2026-10-07', '2026-10-07')")
    con.commit()
    con.close()
    op = OperatorStore(db).get("10")
    assert op.name == "Ali" and op.linked and op.phone is None


def test_admin_adds_registered_client_by_phone_at_once(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users = UserStore(db)
    users.touch("telegram", "77", "77", "dilnoza", "Dilnoza")
    users.set_phone("telegram", "77", "+998 90 123 45 67")
    data = AdminData(db)
    op = data.add_operator("+998901234567", "", added_by="42")
    assert op.user_id == "77" and op.name == "Dilnoza" and op.linked
    pending = data.add_operator("+998909999999", "Vali")
    assert not pending.linked
    listed = {o["key"]: o for o in data.operator_list()}
    assert listed["+998909999999"]["registered"] and not listed["+998909999999"]["linked"]
    assert "ждём регистрации" in operators_text(data)
    assert data.overview()["operators_pending"] == 1


# --- client bot: logging and operator list ---------------------------------------------------

class FakeTgBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return NS(message_id=len(self.sent))

    async def send_chat_action(self, *a, **kw):
        pass


class FakeMessage:
    def __init__(self, text, chat_id, user_id, reply_to=None):
        self.text, self.caption = text, None
        self.chat = NS(id=chat_id, type="private" if chat_id > 0 else "supergroup")
        self.from_user = NS(id=user_id, username=None, full_name=f"User {user_id}", language_code="ru")
        self.reply_to_message = reply_to
        self.photo = self.voice = self.document = self.audio = None
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


def support_bot():
    settings = Settings(support_chat_id=-100)
    bot = TelegramSupportBot(settings, SupportEngine(FakeLLM(), FakeSTT()))
    reply = BotReply("Operatorga uzatdim", Lang.UZ_LATN, escalate=True, topic="refund")
    hid = bot.handoffs.open("telegram", "7", "-100", "55", reply)
    return bot, hid


def test_only_listed_operators_reach_the_client():
    bot, _ = support_bot()
    bot.operators.add(10, "Ali")
    tg = FakeTgBot()
    stranger = FakeMessage("Salom", -100, 11, reply_to=NS(message_id=55))
    asyncio.run(bot.on_operator_reply(stranger, tg))
    assert tg.sent == [] and stranger.answers == [NOT_OPERATOR.format(id=11)]
    operator = FakeMessage("Tekshiryapmiz", -100, 10, reply_to=NS(message_id=55))
    asyncio.run(bot.on_operator_reply(operator, tg))
    client, text = tg.sent[0]
    assert client == 7 and text == "Tekshiryapmiz"  # a plain message, as from a person


def test_session_prefers_messages_saved_with_conversation_id(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users, handoffs, feedback, log = stores(db)
    conv = feedback.bot_answered("telegram", "7", "ru", "cards")
    log.add("telegram", "7", "client", "без id")
    log.add("telegram", "7", "client", "с id", conversation_id=conv.id)
    data = AdminData(db)
    assert [m["text"] for m in data.session(conv.id)["messages"]] == ["с id"]
    assert [m.text for m in log.for_client("telegram", "7")] == ["без id", "с id"]


def test_client_photo_is_loaded_from_telegram_once(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users, handoffs, feedback, log = stores(db)
    conv = feedback.bot_answered("telegram", "7", "ru", "payments")
    photo = log.add("telegram", "7", "client", "[фото]", kind="photo", files=["AgACfile"], conversation_id=conv.id)
    calls = []

    async def fetch(file_id):
        calls.append(file_id)
        return b"PNGDATA", "image/png"

    app = create_app(AdminData(db), TOKEN, frozenset({ADMIN}), fetch=fetch)
    chat, first, again, missing, stranger = api(
        app, ("GET", f"/api/sessions/{conv.id}", ADMIN, None), ("GET", f"/api/media/{photo}/0", ADMIN, None),
        ("GET", f"/api/media/{photo}/0", ADMIN, None), ("GET", f"/api/media/{photo}/1", ADMIN, None),
        ("GET", f"/api/media/{photo}/0", 7, None))
    message = chat[1]["messages"][0]
    assert message["files"] == 1 and "AgACfile" not in json.dumps(chat[1])  # file ids stay on the server
    assert first == (200, "PNGDATA") and again == (200, "PNGDATA") and calls == ["AgACfile"]
    assert missing[0] == 404 and stranger[0] == 403


def test_photo_without_client_bot_token_is_not_found(tmp_path):
    db = tmp_path / "bot.sqlite3"
    photo = ChatLog(db).add("telegram", "7", "client", "[фото]", kind="photo", files=["AgACfile"])
    app = create_app(AdminData(db), TOKEN, frozenset({ADMIN}))
    (status, body), = api(app, ("GET", f"/api/media/{photo}/0", ADMIN, None))
    assert status == 404 and body["error"] == "no_token"


def test_overall_and_per_client_statistics(tmp_path):
    from ai_support.feedback import Assessment
    db = tmp_path / "bot.sqlite3"
    users, handoffs, feedback, log = stores(db)
    users.touch("telegram", "7", "7", "anvar", "Anvar")
    users.touch("telegram", "8", "8", "bobur", "Bobur")
    conv = feedback.bot_answered("telegram", "7", "uz_latn", "cards")
    log.add("telegram", "7", "client", "Karta", conversation_id=conv.id)
    log.add("telegram", "7", "client", "[фото]", kind="photo", files=["f"], conversation_id=conv.id)
    log.add("telegram", "7", "bot", "Javob", conversation_id=conv.id)
    _, req = feedback.end("telegram", "7", "client")
    feedback.rate(req.id, "7", "4")
    reply = BotReply("Operatorga", Lang.UZ_LATN, escalate=True, topic="payments", client_text="Pul")
    hid = handoffs.open("telegram", "7", "-100", "55", reply)
    feedback.escalated("telegram", "7", hid, "uz_latn", "payments")
    feedback.operator_replied("telegram", "7", hid, "uz_latn", "10", "Ali")
    feedback.assess("telegram", "7", "operator", Assessment("polite"), handoff_id=hid)
    feedback.bot_answered("telegram", "8", "ru", "cards")
    data = AdminData(db)

    st = data.stats(days=7)
    assert (st["clients"], st["sessions"], st["escalated"], st["escalation_rate"]) == (2, 3, 1, 33)
    assert st["topics"][0] == {"name": "cards", "count": 2} and st["media"] == 1
    assert len(st["daily"]) == 7 and st["daily"][-1]["sessions"] == 3 and st["daily"][-1]["handoffs"] == 1
    assert st["rating_bot"]["count"] == 1 and st["rating_bot"]["by_stars"]["4"] == 1
    assert st["languages"] == {"uz_latn": 2, "ru": 1} and st["messages_by"] == {"client": 2, "bot": 1}

    c = data.client_stats("7")
    assert (c["name"], c["sessions"], c["sessions_open"], c["escalated"]) == ("Anvar", 2, 1, 1)
    assert c["messages_by"] == {"client": 2, "bot": 1} and c["media"] == 1
    assert c["operators"] == [{"name": "Ali", "count": 1}] and c["rating_average"] == 4.0
    assert c["tones"] == {"polite": 1} and {t["name"] for t in c["topics"]} == {"cards", "payments"}
    assert data.client_stats("999") is None

    app = create_app(data, TOKEN, frozenset({ADMIN}))
    overall, one, missing, stranger = api(app, ("GET", "/api/stats?days=3", ADMIN, None),
                                          ("GET", "/api/clients/8/stats", ADMIN, None),
                                          ("GET", "/api/clients/999/stats", ADMIN, None),
                                          ("GET", "/api/stats", 7, None))
    assert overall[0] == 200 and len(overall[1]["daily"]) == 3
    assert one[0] == 200 and one[1]["name"] == "Bobur" and one[1]["operators"] == []
    assert missing[0] == 404 and stranger[0] == 403


def test_panel_shows_who_a_new_conversation_waits_for_and_busy_operators(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users, handoffs, feedback, log = stores(db)
    OperatorStore(db).add("10", "Dilnoza")
    reply = BotReply("Ulayapman", Lang.UZ_LATN, escalate=True, client_text="Salom")
    hid = handoffs.open("telegram", "7", "-100", "55", reply)
    handoffs.assign(hid, "10", "Dilnoza", 60)
    feedback.escalated("telegram", "7", hid, "uz_latn")
    data = AdminData(db)
    s = data.sessions()[0]
    assert (s["assigned_name"], s["handoff_status"]) == ("Dilnoza", "waiting")
    assert data.operator_list()[0]["busy"] == 1
    handoffs.close_handoff(hid)
    assert data.operator_list()[0]["busy"] == 0


def test_operator_store_edits_and_deletes():
    ops = OperatorStore(":memory:")
    ops.add_phone("+998901234567", "Ali")
    ops.add(5, "Vali")
    op = ops.update("+998901234567", name="Ali Valiyev", phone="+998 90 765 43 21")
    assert (op.name, op.phone, op.key) == ("Ali Valiyev", "+998907654321", "+998907654321")
    assert ops.get("+998901234567") is None
    assert ops.update("5", phone="+998901111111").phone == "+998901111111"
    for bad in ({"phone": "+998 90 765 43 21"}, {"phone": "12"}, {"name": " "}):
        try:
            ops.update("5", **bad)
        except OperatorError as e:
            assert str(e) in ("phone_taken", "bad_phone", "bad_name")
        else:
            raise AssertionError(bad)
    ops.delete("5")
    assert ops.get("5") is None and [o.name for o in ops.all()] == ["Ali Valiyev"]
    try:
        ops.delete("5")
    except OperatorError as e:
        assert str(e) == "not_found"


def test_operators_edited_and_deleted_from_panel(tmp_path):
    data = AdminData(tmp_path / "db.sqlite3")
    data.add_operator("+998901234567", "Ali")
    app = create_app(data, TOKEN, frozenset({ADMIN}))
    edited, taken, deleted, gone, stranger = api(
        app, ("PATCH", "/api/operators/+998901234567", ADMIN, {"name": "Ali V", "phone": "+998907654321"}),
        ("PATCH", "/api/operators/+998907654321", ADMIN, {"phone": "1"}),
        ("DELETE", "/api/operators/+998907654321", ADMIN, None),
        ("DELETE", "/api/operators/+998907654321", ADMIN, None),
        ("DELETE", "/api/operators/+998907654321", 7, None))
    assert edited[0] == 200 and edited[1]["name"] == "Ali V" and edited[1]["key"] == "+998907654321"
    assert taken == (400, {"error": "bad_phone"})
    assert deleted == (200, {"ok": True}) and gone[0] == 404 and stranger[0] == 403
    assert data.operator_list() == []


class FakeTelegram:
    def __init__(self, fail=False):
        self.calls, self.fail, self.next_id = [], fail, 500

    async def __call__(self, method, payload):
        if self.fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.calls.append((method, payload))
        self.next_id += 1
        return {"message_id": self.next_id}


def test_admin_answers_a_session_from_the_panel(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users, handoffs, feedback, log = stores(db)
    users.touch("telegram", "7", "7", "anvar", "Anvar")
    reply = BotReply("Operatorga uzatdim", Lang.UZ_LATN, escalate=True, topic="refund", client_text="Pul qaytmadi")
    hid = handoffs.open("telegram", "7", "-100", "55", reply)
    handoffs.assign(hid, "10", "Ali", wait_seconds=60)
    conv = feedback.escalated("telegram", "7", hid, "uz_latn", "refund")
    tg = FakeTelegram()
    app = create_app(AdminData(db), TOKEN, frozenset({ADMIN}), support_chat_id=-100, telegram=tg)
    sent, empty = api(app, ("POST", f"/api/sessions/{conv.id}/reply", ADMIN, {"text": "Tekshiryapmiz"}),
                      ("POST", f"/api/sessions/{conv.id}/reply", ADMIN, {"text": "  "}))
    assert sent == (200, {"ok": True, "session_id": conv.id}) and empty[0] == 400
    (m1, to_client), (m2, copy) = tg.calls
    assert to_client == {"chat_id": 7, "text": "Tekshiryapmiz"}  # no signature, no buttons
    assert copy["chat_id"] == -100 and copy["reply_to_message_id"] == 55 and "Tekshiryapmiz" in copy["text"]
    # The operator's replies to the copy reach the same client; the AI no longer takes the case over.
    assert handoffs.find("-100", "502").id == hid and handoffs.due() == []
    data = AdminData(db)
    msgs = data.session(conv.id)["messages"]
    assert (msgs[-1]["sender"], msgs[-1]["text"], msgs[-1]["operator_name"]) == ("operator", "Tekshiryapmiz", "Admin")
    assert handoffs.operator_replies(hid) == ["Tekshiryapmiz"]


def test_admin_reply_to_bot_session_opens_a_handoff(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users, handoffs, feedback, log = stores(db)
    conv = feedback.bot_answered("telegram", "8", "ru", "cards")
    log.add("telegram", "8", "client", "Карта не добавляется", conversation_id=conv.id)
    tg = FakeTelegram()
    app = create_app(AdminData(db), TOKEN, frozenset({ADMIN}), support_chat_id=-100, telegram=tg)
    (status, body), = api(app, ("POST", f"/api/sessions/{conv.id}/reply", ADMIN, {"text": "Проверим"}))
    assert status == 200 and body["session_id"] == conv.id
    assert tg.calls[0][1]["text"] == "Проверим"
    h = handoffs.find("-100", "502")
    assert h is not None and h.client_chat_id == "8" and h.client_text == "Карта не добавляется"
    # The client's next messages go to the support chat: the conversation has the hand-off now.
    assert feedback.conversation("telegram", "8").handoff_id == h.id


def test_admin_reply_not_saved_when_telegram_refuses(tmp_path):
    db = tmp_path / "bot.sqlite3"
    users, handoffs, feedback, log = stores(db)
    conv = feedback.bot_answered("telegram", "8", "ru", "cards")
    app = create_app(AdminData(db), TOKEN, frozenset({ADMIN}), telegram=FakeTelegram(fail=True))
    missing, refused = api(app, ("POST", "/api/sessions/999/reply", ADMIN, {"text": "x"}),
                           ("POST", f"/api/sessions/{conv.id}/reply", ADMIN, {"text": "Проверим"}))
    assert missing[0] == 404 and refused[0] == 502 and refused[1]["error"] == "not_sent"
    assert log.for_client("telegram", "8") == []


def test_admin_answers_clients_without_being_an_operator():
    bot, _ = support_bot()
    bot.settings = Settings(support_chat_id=-100, admin_ids=frozenset({12}))
    bot.operators.add(10, "Ali")
    tg = FakeTgBot()
    admin = FakeMessage("Tekshiryapmiz", -100, 12, reply_to=NS(message_id=55))
    asyncio.run(bot.on_operator_reply(admin, tg))
    assert tg.sent and str(tg.sent[0][0]) == "7" and admin.answers == []


def test_local_phone_formats_are_phones():
    for value in ("90 123 45 67", "(90) 123-45-67", "90-123-45-67", "901234567", "+998 90 123 45 67", "998901234567"):
        assert to_phone(value) == "+998901234567", value
    for value in ("123456789", "10", "1234567890", "@ali", "Ali", ""):
        assert to_phone(value) is None, value
    assert parse_operator("90 123 45 67 Vali") == ("+998901234567", "Vali")
    assert parse_operator("901234567 Vali") == ("901234567", "Vali")  # may be an id: add_operator decides


def test_operator_added_by_local_number_links_a_registered_client(tmp_path):
    data = AdminData(tmp_path / "db.sqlite3")
    data.users.touch("telegram", "555", "555", "vali", "Vali Valiyev")
    data.users.set_phone("telegram", "555", "+998 90 123 45 67")
    op = data.add_operator("90 123 45 67", "")
    assert op.linked and op.user_id == "555" and op.phone == "+998901234567"
    assert data.operators.may_answer(555)


def test_nine_digit_id_of_a_registered_client_stays_an_id(tmp_path):
    data = AdminData(tmp_path / "db.sqlite3")
    data.users.touch("telegram", "901234567", "901234567", "ali", "Ali")
    op = data.add_operator("901234567", "Ali")
    assert op.user_id == "901234567" and op.phone is None
    assert data.add_operator("931112233", "Vali").phone == "+998931112233"


def test_operator_from_a_shared_contact_is_linked_at_once(tmp_path):
    data = AdminData(tmp_path / "db.sqlite3")
    op = data.add_operator("998901234567", "Vali", user_id=777)
    assert op.linked and op.user_id == "777" and data.operators.may_answer(777)
