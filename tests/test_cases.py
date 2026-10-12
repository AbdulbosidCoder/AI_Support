"""Cases added in the admin panel: saved only without forbidden answers, followed by the bot at once."""
import asyncio
import json
import time
from types import SimpleNamespace as NS

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ai_support.admin.auth import sign
from ai_support.admin.data import AdminData
from ai_support.admin.web import create_app
from ai_support.cases import ANTIFRAUD_FORBIDDEN, CaseError, CaseStore
from ai_support.channels.telegram_bot import TelegramSupportBot
from ai_support.config import Settings
from ai_support.engine import SupportEngine
from ai_support.factory import always_escalate, load_knowledge
from ai_support.knowledge import KnowledgeBase
from ai_support.models import IncomingMessage
from ai_support.prompt import build_system_prompt, reply_schema
from ai_support.users import UserStore
from fakes import FakeLLM, FakeSTT, answer

SMS = dict(section="apartment", title="Не приходит SMS-код при оплате квартиры",
           steps=["Проверьте баланс SIM-карты.", "Проверьте папку «Спам» или поиск по номеру 3700."],
           forbidden=["Не обещать, что SMS придёт в определённое время."],
           escalate="if", escalate_if="SMS так и не пришло после этих шагов.")


def test_save_update_delete_bump_the_version():
    store = CaseStore(":memory:")
    v0 = store.version()
    c = store.save(None, **SMS, by="Admin")
    assert c.id == 1 and c.topic == "apartment/case1" and c.updated_by == "Admin" and store.version() == v0 + 1
    c = store.save(c.id, **{**SMS, "title": "SMS-код не приходит"})
    assert store.get(1).title == "SMS-код не приходит" and store.version() == v0 + 2
    store.delete(1)
    assert store.get(1) is None and store.all() == [] and store.version() == v0 + 3
    with pytest.raises(CaseError):
        store.delete(1)
    with pytest.raises(CaseError):
        store.save(5, **SMS)


@pytest.mark.parametrize("change,code", [
    ({"title": "  "}, "no_title"),
    ({"steps": ["", " "]}, "no_steps"),
    ({"escalate_if": ""}, "no_escalate_if"),
    ({"section": "crypto"}, "bad_section"),
    ({"escalate": "sometimes"}, "bad_escalate"),
    ({"steps": "один шаг"}, "bad_list"),
    ({"steps": ["шаг"] * 16}, "too_many_steps"),
])
def test_incomplete_cases_are_refused(change, code):
    with pytest.raises(CaseError) as e:
        CaseStore(":memory:").save(None, **{**SMS, **change})
    assert e.value.code == code


@pytest.mark.parametrize("field,value", [
    ("steps", ["Деньги вернутся в течение 24 часов."]),
    ("steps", ["Скажите клиенту: платёж прошёл успешно."]),
    ("steps", ["Мы разблокировали ваш аккаунт."]),
    ("steps", ["Pulingiz albatta qaytadi."]),
    ("steps", ["Объясните: антифрод срабатывает, когда SIM-карта стоит в другом телефоне."]),
    ("steps", ["Лимит увеличен, можно повторить перевод."]),
    ("title", "Возврат подтверждён, клиент спрашивает когда"),
    ("escalate_if", "Если деньги не вернутся через 2 часа"),
])
def test_forbidden_answers_are_never_saved(field, value):
    store = CaseStore(":memory:")
    with pytest.raises(CaseError) as e:
        store.save(None, **{**SMS, field: value})
    assert e.value.code == "forbidden" and e.value.violations and e.value.where[0].split(".")[0] == field
    assert store.all() == [] and store.version() == 0


def test_the_must_not_list_may_name_what_is_forbidden():
    c = CaseStore(":memory:").save(None, **{**SMS, "forbidden": ["Не говорить «деньги вернутся через 2 часа»."]})
    assert c.forbidden == ["Не говорить «деньги вернутся через 2 часа»."]


def test_antifraud_case_always_goes_to_a_person_and_hides_causes():
    c = CaseStore(":memory:").save(None, "antifraud", "Клиент не может войти после смены телефона",
                                   ["Объясните, что доступ ограничен системой безопасности.",
                                    "Передайте обращение специалисту."], escalate="never")
    assert c.escalate == "always"
    topic = c.as_topic()
    assert topic["escalate_if"].startswith("Всегда") and topic["forbidden"][0] == ANTIFRAUD_FORBIDDEN


# --- the bot's knowledge base ----------------------------------------------------------------

def kb_with(store):
    return KnowledgeBase.load(Settings().kb_dir).with_cases(store.topics())


def test_active_cases_go_into_the_prompt_and_the_topic_list():
    store = CaseStore(":memory:")
    store.save(None, **SMS)
    store.save(None, **{**SMS, "title": "Выключенный кейс"}, active=False)
    kb = kb_with(store)
    prompt = build_system_prompt(kb)
    assert "## Кейсы, добавленные администраторами поддержки" in prompt
    assert "### topic=apartment/case1: Не приходит SMS-код при оплате квартиры" in prompt
    assert "Нельзя: Не обещать, что SMS придёт в определённое время." in prompt
    assert "Эскалация, если: SMS так и не пришло после этих шагов." in prompt
    assert "Выключенный кейс" not in prompt
    enum = reply_schema(kb)["properties"]["topic"]["enum"]
    assert "apartment/case1" in enum and "apartment/case2" not in enum
    # Hard rules come before any case.
    assert prompt.index("Жёсткие правила") < prompt.index("apartment/case1")


def test_a_forbidden_text_put_in_the_database_directly_never_reaches_the_prompt():
    store = CaseStore(":memory:")
    store.save(None, **SMS)
    store._db.execute("UPDATE support_cases SET steps = ?", (json.dumps(["Деньги вернутся через 2 часа."]),))
    assert "case1" not in KnowledgeBase.load(Settings().kb_dir).with_cases(store.topics()).render()


def test_always_escalate_covers_builtin_antifraud_and_case_topics():
    store = CaseStore(":memory:")
    store.save(None, **{**SMS, "escalate": "always"})
    store.save(None, **{**SMS, "escalate": "never"})
    topics = always_escalate(load_knowledge(Settings(), cases=store))
    assert {"apartment/case1", "antifraud/registration_block"} <= topics and "apartment/case2" not in topics


def test_engine_hands_an_always_case_to_a_person_even_if_the_model_did_not():
    llm = FakeLLM(answer("Проверьте баланс SIM-карты.", topic="apartment/case1"))
    engine = SupportEngine(llm, FakeSTT())
    engine.set_always_escalate({"apartment/case1"})
    reply = asyncio.run(engine.handle(IncomingMessage("7", "SMS не приходит")))
    assert reply.escalate and "apartment/case1" in reply.escalation_reason


# --- the client bot follows a case saved in the panel (another process) -------------------------

def test_client_bot_reloads_its_knowledge_when_a_case_is_saved(tmp_path):
    db = tmp_path / "db.sqlite3"
    llm = FakeLLM()
    users = UserStore(":memory:")
    bot = TelegramSupportBot(Settings(), SupportEngine(llm, FakeSTT()), users, cases=CaseStore(db))

    async def ask():
        return await bot._handle(NS(), IncomingMessage("42", "SMS kod kelmayapti"))

    asyncio.run(ask())
    assert llm.system_prompts == []  # nothing changed: no rebuild
    CaseStore(db).save(None, **SMS)  # the admin panel's own connection
    asyncio.run(ask())
    assert len(llm.system_prompts) == 1 and "apartment/case1" in llm.system_prompts[0]
    assert "apartment/case1" in llm.schemas[0]["properties"]["topic"]["enum"]
    asyncio.run(ask())
    assert len(llm.system_prompts) == 1


# --- admin panel API -------------------------------------------------------------------------

TOKEN, ADMIN = "123:ABC", 555


def init_data():
    return sign({"auth_date": str(int(time.time())), "query_id": "q",
                 "user": json.dumps({"id": ADMIN, "first_name": "Admin"})}, TOKEN)


def api(app, *requests):
    async def run():
        out = []
        async with TestClient(TestServer(app)) as client:
            for method, path, body in requests:
                res = await client.request(method, path, headers={"X-Telegram-Init-Data": init_data()}, json=body)
                out.append((res.status, await res.json()))
        return out
    return asyncio.run(run())


def test_panel_adds_edits_and_deletes_a_case(tmp_path):
    data = AdminData(tmp_path / "db.sqlite3")
    app = create_app(data, TOKEN, frozenset({ADMIN}))
    (s1, listed), (s2, created), (s3, edited), (s4, after), (s5, _), (s6, gone) = api(
        app,
        ("GET", "/api/cases", None),
        ("POST", "/api/cases", SMS),
        ("PUT", "/api/cases/1", {**SMS, "active": False}),
        ("GET", "/api/cases", None),
        ("DELETE", "/api/cases/1", None),
        ("DELETE", "/api/cases/1", None),
    )
    assert s1 == 200 and listed["cases"] == [] and {s["id"] for s in listed["sections"]} >= {"apartment", "antifraud"}
    # The call-center cases already in the knowledge base are shown read-only.
    assert len(listed["builtin"]) >= 15 and any(c["topic"] == "payments/status_pending" for c in listed["builtin"])
    assert s2 == 200 and created["id"] == 1 and created["updated_by"] == "Admin" and created["active"]
    assert s3 == 200 and not edited["active"]
    assert s4 == 200 and after["cases"][0]["title"] == SMS["title"]
    assert s5 == 200 and s6 == 404 and data.cases.all() == []


def test_panel_refuses_a_forbidden_case_and_saves_nothing(tmp_path):
    data = AdminData(tmp_path / "db.sqlite3")
    app = create_app(data, TOKEN, frozenset({ADMIN}))
    [(status, body), (s2, bad)] = api(
        app,
        ("POST", "/api/cases", {**SMS, "steps": ["Скажите, что деньги вернутся в течение 3 дней."]}),
        ("POST", "/api/cases", {**SMS, "title": ""}),
    )
    assert status == 400 and body["error"] == "forbidden"
    assert {v["category"] for v in body["violations"]} >= {"promised_refund", "promised_timeline"}
    assert body["violations"][0]["where"] == "steps.1"
    assert s2 == 400 and bad["error"] == "no_title"
    assert data.cases.all() == []
