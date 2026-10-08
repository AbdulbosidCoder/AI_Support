import asyncio
import json
import time
from types import SimpleNamespace as NS

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from ai_support.admin.auth import sign
from ai_support.admin.data import AdminData
from ai_support.admin.web import create_app
from ai_support.channels.telegram_bot import TelegramSupportBot
from ai_support.chatlog import ChatLog
from ai_support.config import Settings
from ai_support.engine import SupportEngine
from ai_support.guides import GuideError, GuideStore, guide_text
from ai_support.menu import quick_question
from ai_support.models import Lang
from ai_support.templates import t
from ai_support.users import UserStore
from fakes import FakeLLM, FakeSTT, answer, png

TOKEN = "123:ABC"
ADMIN = 42

GUIDE = {"texts": {"ru": "Карта добавляется в разделе «Карты»."},
         "steps": {"ru": ["Откройте «Карты».", "Нажмите «Добавить карту».", "Введите код из SMS."]}}


# --- store -----------------------------------------------------------------------------------

def test_guide_for_a_builtin_question_in_the_clients_language():
    store = GuideStore(":memory:")
    g = store.save("add_card", "cards", texts=GUIDE["texts"], steps=GUIDE["steps"], by="Admin")
    assert not g.custom and g.updated_by == "Admin"
    found, lang = store.answer("add_card", Lang.RU)
    assert lang == Lang.RU and guide_text(found, Lang.RU) == (
        "Карта добавляется в разделе «Карты».\n\n" + t("guide_steps", Lang.RU)
        + "\n1. Откройте «Карты».\n2. Нажмите «Добавить карту».\n3. Введите код из SMS.")
    # No guide in Uzbek: an Uzbek client still gets the model's answer.
    assert store.answer("add_card", Lang.UZ_LATN) is None


def test_builtin_question_stays_in_its_topic():
    store = GuideStore(":memory:")
    assert store.save("add_card", "payments", texts=GUIDE["texts"]).category == "cards"


def test_admin_adds_a_question_to_a_topic():
    store = GuideStore(":memory:")
    g = store.save(None, "cards", labels={"ru": "Как привязать Humo?", "uz_latn": "Humo kartani qanday qo'shaman?"},
                   texts={"ru": "Так же, как Uzcard."})
    assert g.custom and g.qid == "c1"
    assert store.menu("cards", Lang.RU)[-1] == ("c1", "Как привязать Humo?")
    # No English label or text: an English client sees and gets the Russian ones.
    assert store.menu("cards", Lang.EN)[-1] == ("c1", "Как привязать Humo?")
    assert store.answer("c1", Lang.EN)[1] == Lang.RU
    assert store.question("c1").label[Lang.UZ_LATN] == "Humo kartani qanday qo'shaman?"
    assert store.save(None, "cards", labels={"ru": "Ещё"}, texts={"ru": "Да"}).qid == "c2"


def test_a_new_question_needs_a_label_and_an_answer():
    store = GuideStore(":memory:")
    for labels, texts, code in [({}, {"ru": "x"}, "no_label"), ({"ru": "Вопрос"}, {}, "no_answer")]:
        with pytest.raises(GuideError) as e:
            store.save(None, "cards", labels=labels, texts=texts)
        assert e.value.code == code
    with pytest.raises(GuideError):
        store.save(None, "nope", labels={"ru": "Q"}, texts={"ru": "A"})


def test_rename_or_hide_a_builtin_question():
    store = GuideStore(":memory:")
    store.save("add_card", "cards", labels={"ru": "Добавить карту"})
    assert ("add_card", "Добавить карту") in store.menu("cards", Lang.RU)
    assert store.answer("add_card", Lang.RU) is None  # renamed only: the model still answers
    store.save("delete_card", "cards", hidden=True)
    assert "delete_card" not in [qid for qid, _ in store.menu("cards", Lang.RU)]


@pytest.mark.parametrize("field,value", [
    ("texts", {"ru": "Деньги вернутся в течение 3 дней."}),
    ("steps", {"ru": ["Подождите.", "Мы разблокируем карту."]}),
    ("labels", {"ru": "Возврат подтверждён"}),
])
def test_forbidden_phrases_are_never_saved(field, value):
    store = GuideStore(":memory:")
    with pytest.raises(GuideError) as e:
        store.save("add_card", "cards", **{field: value})
    assert e.value.code == "forbidden" and e.value.violations and e.value.where[0].startswith(field)
    assert store.get("add_card") is None


def test_screenshots_are_checked_ordered_and_removed():
    store = GuideStore(":memory:")
    a = store.add_image("add_card", png())  # a built-in question without a guide yet can hold screenshots
    b = store.add_image("add_card", png(color=(1, 2, 3)))
    assert [i.id for i in store.get("add_card").images] == [a.id, b.id] and a.content_type == "image/png"
    store.move_image("add_card", b.id, -1)
    assert [i.id for i in store.get("add_card").images] == [b.id, a.id]
    store.delete_image("add_card", b.id)
    assert [i.id for i in store.get("add_card").images] == [a.id]
    for data in (b"", b"not an image", b"GIF89a" + b"\0" * 10):
        with pytest.raises(GuideError):
            store.add_image("add_card", data)
    with pytest.raises(GuideError):
        store.add_image("c9", png())


def test_deleting_a_guide_removes_its_screenshots():
    store = GuideStore(":memory:")
    store.save("add_card", "cards", texts=GUIDE["texts"])
    img = store.add_image("add_card", png())
    store.delete("add_card")
    assert store.get("add_card") is None and store.image(img.id) is None


# --- client bot ------------------------------------------------------------------------------

class Chat:
    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None, **_):
        self.sent.append(("text", text, reply_markup))

    async def answer_photo(self, photo, **_):
        self.sent.append(("photo", photo))
        return NS(photo=[NS(file_id="small"), NS(file_id="PHOTO1")])

    async def answer_media_group(self, media, **_):
        self.sent.append(("album", [m.media for m in media]))
        return [NS(photo=[NS(file_id=f"ALBUM{i}")]) for i in range(len(media))]

    async def send_chat_action(self, *_):
        pass


def callback(chat, data, uid=42):
    async def noop(*_, **__):
        pass
    return NS(data=data, from_user=NS(id=uid, username="ali", full_name="Ali", language_code="ru"),
              message=NS(chat=NS(id=uid), answer=chat.answer, answer_photo=chat.answer_photo,
                         answer_media_group=chat.answer_media_group, edit_reply_markup=noop),
              answer=noop)


def client_bot(llm=None):
    users = UserStore(":memory:")
    users.touch("telegram", "42", "42", "ali", "Ali", "ru")
    users.set_language("telegram", "42", Lang.RU)
    users.set_phone("telegram", "42", "998901234567")
    guides, chatlog = GuideStore(":memory:"), ChatLog(":memory:")
    bot = TelegramSupportBot(Settings(), SupportEngine(llm or FakeLLM(), FakeSTT()), users, chatlog=chatlog,
                             guides=guides)
    return bot, guides, chatlog


def test_tapped_question_with_a_guide_gets_the_guide_and_screenshots():
    llm = FakeLLM()
    bot, guides, chatlog = client_bot(llm)
    guides.save("add_card", "cards", texts=GUIDE["texts"], steps=GUIDE["steps"])
    first, second = guides.add_image("add_card", png()), guides.add_image("add_card", png(color=(9, 9, 9)))
    chat = Chat()
    asyncio.run(bot.on_quick_question(callback(chat, "q:add_card"), chat))
    assert not llm.calls  # the admin's answer, not the model's
    asked, (_, text, kb), (kind, album) = chat.sent
    assert asked[1] == f"{t('your_question', Lang.RU)} {quick_question('add_card').question[Lang.RU]}"
    assert text == f"{t('assistant_name', Lang.RU)}:\n{guide_text(guides.get('add_card'), Lang.RU)}"
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["end", "menu"]
    assert kind == "album" and len(album) == 2 and album[0].data == guides.image(first.id)[1]
    # Uploaded once: the next client gets the same photos by file_id.
    assert [i.file_id for i in guides.get("add_card").images] == ["ALBUM0", "ALBUM1"]
    chat2 = Chat()
    asyncio.run(bot.on_quick_question(callback(chat2, "q:add_card"), chat2))
    assert chat2.sent[-1] == ("album", ["ALBUM0", "ALBUM1"])
    assert [(m.sender, m.text) for m in chatlog.for_client("telegram", "42")][:2] == [
        ("client", quick_question("add_card").question[Lang.RU]), ("bot", guide_text(guides.get("add_card"), Lang.RU))]
    assert second.id


def test_single_screenshot_is_sent_as_a_photo():
    bot, guides, _ = client_bot()
    guides.save("add_card", "cards", texts=GUIDE["texts"])
    guides.add_image("add_card", png())
    chat = Chat()
    asyncio.run(bot.on_quick_question(callback(chat, "q:add_card"), chat))
    assert chat.sent[-1][0] == "photo" and guides.get("add_card").images[0].file_id == "PHOTO1"


def test_question_without_a_guide_in_the_clients_language_asks_the_model():
    llm = FakeLLM(answer("Откройте раздел «Карты».", "ru", topic="add_card"))
    bot, guides, _ = client_bot(llm)
    guides.save("add_card", "cards", texts={"uz_latn": "Kartalar bo'limini oching."})
    chat = Chat()
    asyncio.run(bot.on_quick_question(callback(chat, "q:add_card"), chat))
    assert llm.calls and chat.sent[-1][1].endswith("Откройте раздел «Карты».")


def test_added_question_is_in_the_menu_and_answered_with_its_guide():
    llm = FakeLLM()
    bot, guides, _ = client_bot(llm)
    g = guides.save(None, "cards", labels={"ru": "Как привязать Humo?"}, texts={"ru": "Так же, как Uzcard."})
    chat = Chat()

    async def show(cb, text, markup, **_):
        chat.sent.append(("menu", text, markup))
    bot._show = show
    asyncio.run(bot.on_category(callback(chat, "cat:cards")))
    rows = chat.sent[-1][2].inline_keyboard
    assert [rows[-2][0].text, rows[-2][0].callback_data] == ["Как привязать Humo?", f"q:{g.qid}"]
    asyncio.run(bot.on_quick_question(callback(chat, f"q:{g.qid}"), chat))
    assert not llm.calls and chat.sent[-1][1].endswith("Так же, как Uzcard.")


def test_hidden_question_is_not_in_the_menu():
    bot, guides, _ = client_bot()
    guides.save("delete_card", "cards", hidden=True)
    chat = Chat()

    async def show(cb, text, markup, **_):
        chat.sent.append(("menu", text, markup))
    bot._show = show
    asyncio.run(bot.on_category(callback(chat, "cat:cards")))
    assert "q:delete_card" not in [r[0].callback_data for r in chat.sent[-1][2].inline_keyboard]


# --- admin panel API -------------------------------------------------------------------------

def init_data(user_id=ADMIN):
    return sign({"auth_date": str(int(time.time())), "query_id": "q",
                 "user": json.dumps({"id": user_id, "first_name": "Admin"})}, TOKEN)


def api(app, *requests):
    """(method, path, body) as the admin; a body of bytes is uploaded as the multipart field "file"."""
    async def run():
        out = []
        async with TestClient(TestServer(app)) as client:
            for method, path, body in requests:
                headers = {"X-Telegram-Init-Data": init_data()}
                if isinstance(body, bytes):
                    form = aiohttp.FormData()
                    form.add_field("file", body, filename="shot.png", content_type="image/png")
                    res = await client.request(method, path, headers=headers, data=form)
                else:
                    res = await client.request(method, path, headers=headers, json=body)
                is_json = res.content_type == "application/json"
                out.append((res.status, await res.json() if is_json else await res.read()))
        return out
    return asyncio.run(run())


def test_admin_edits_a_guide_from_the_panel(tmp_path):
    app = create_app(AdminData(tmp_path / "db.sqlite3"), TOKEN, frozenset({ADMIN}))
    shot = png()
    (s1, menu), (s2, saved), (s3, with_image), (s4, image), (s5, listed) = api(
        app,
        ("GET", "/api/quick", None),
        ("PUT", "/api/quick/add_card", GUIDE),
        ("POST", "/api/quick/add_card/images", shot),
        ("GET", "/api/quick/add_card/images/1", None),
        ("GET", "/api/quick", None),
    )
    assert s1 == 200 and menu["categories"][1]["id"] == "cards"
    card = next(q for q in menu["categories"][1]["questions"] if q["id"] == "add_card")
    assert card["label"]["ru"] == "Как добавить карту?" and card["guide_languages"] == [] and not card["custom"]
    assert s2 == 200 and saved["steps"]["ru"][0] == "Откройте «Карты»." and saved["updated_by"] == "Admin"
    assert s3 == 200 and with_image["images"][0]["content_type"] == "image/png"
    assert s4 == 200 and image == shot
    card = next(q for q in listed["categories"][1]["questions"] if q["id"] == "add_card")
    assert card["guide_languages"] == ["ru"] and card["images"] == 1


def test_panel_refuses_forbidden_text_and_saves_nothing(tmp_path):
    data = AdminData(tmp_path / "db.sqlite3")
    app = create_app(data, TOKEN, frozenset({ADMIN}))
    [(status, body)] = api(app, ("PUT", "/api/quick/payment_problem",
                                 {"texts": {"ru": "Не волнуйтесь, деньги вернутся в течение 24 часов."}}))
    assert status == 400 and body["error"] == "forbidden"
    assert {v["category"] for v in body["violations"]} >= {"promised_refund", "promised_timeline"}
    assert body["violations"][0]["where"] == "texts.ru"
    assert data.guides.get("payment_problem") is None


def test_panel_adds_and_deletes_a_question(tmp_path):
    data = AdminData(tmp_path / "db.sqlite3")
    app = create_app(data, TOKEN, frozenset({ADMIN}))
    new = {"category": "cards", "labels": {"ru": "Как привязать Humo?"}, "texts": {"ru": "Так же, как Uzcard."}}
    (s1, created), (s2, bad), (s3, _), (s4, gone), (s5, missing) = api(
        app,
        ("POST", "/api/quick", new),
        ("POST", "/api/quick", {"category": "cards", "labels": {"ru": "Пусто"}}),
        ("DELETE", "/api/quick/c1", None),
        ("GET", "/api/quick/c1", None),
        ("PUT", "/api/quick/nope", GUIDE),
    )
    assert s1 == 200 and created["id"] == "c1" and created["custom"] and created["category"] == "cards"
    assert s2 == 400 and bad["error"] == "no_answer"
    assert s3 == 200 and s4 == 404 and s5 == 404


def test_panel_refuses_a_file_that_is_not_a_screenshot(tmp_path):
    app = create_app(AdminData(tmp_path / "db.sqlite3"), TOKEN, frozenset({ADMIN}))
    [(status, body)] = api(app, ("POST", "/api/quick/add_card/images", b"%PDF-1.4"))
    assert status == 400 and body["error"] == "bad_image"
