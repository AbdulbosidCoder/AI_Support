"""Кейсы колл-центра («Колл-центр кейсы.pdf») в базе знаний и запрещённые ответы по ним."""
import re

import pytest

from ai_support.config import Settings
from ai_support.guardrails import find_violations, restricted_request
from ai_support.knowledge import KnowledgeBase
from ai_support.prompt import build_system_prompt

KB = KnowledgeBase.load(Settings().kb_dir)
TOPICS = {f'{t["section"]}/{t["id"]}': t for t in KB.topics}

# номер кейса в PDF -> тема БЗ, которая его покрывает
CASES = {
    1: "apartment/sms_not_received",
    2: "registration/pinfl_phone_mismatch",
    3: "registration/vpn_on_login",
    4: "technical/server_error",
    5: "cards/phone_mismatch",
    6: "registration/myid",
    7: "technical/server_error_on_open",
    8: "antifraud/registration_block",
    9: "apartment/debt_not_updated",
    10: "apartment/payment_status",
    11: "payments/status_pending",
    12: "payments/status_failed_debited",
    13: "payments/double_payment",
    14: "registration/cannot_login",
    15: "antifraud/suspicious_operation",
    16: "antifraud/device_or_sim_restriction",
    17: "antifraud/device_or_sim_restriction",
    18: "antifraud/device_or_sim_restriction",
    19: "antifraud/registration_block",
    20: "antifraud/device_or_sim_restriction",
    21: "antifraud/device_or_sim_restriction",
    22: "payments/cancel_successful",
}

ANTIFRAUD = ["antifraud/registration_block", "antifraud/device_or_sim_restriction", "antifraud/suspicious_operation"]
# Внутренние признаки Antifraud из кейсов: в ответах бота их быть не должно.
ANTIFRAUD_INTERNALS = re.compile(
    r"клон|другом устройстве|нескольких устройств|несоответстви|лимит|критери|параметр|признак|чужими данными",
    re.IGNORECASE)
TIMELINE = re.compile(r"\b(день|дня|дней|сутк\w*|завтра|час\w{0,2}|минут\w*)\b", re.IGNORECASE)


def texts(topic):
    yield topic["title"]
    yield from topic["answer_steps"]


def test_every_case_is_covered():
    for case, tid in CASES.items():
        assert tid in TOPICS, (case, tid)
        assert "call_center_cases" in TOPICS[tid]["source"], (case, tid)


def test_ids_unique():
    ids = KB.topic_ids()
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("tid", sorted(set(CASES.values())))
def test_case_answers_have_no_forbidden_phrases_or_timelines(tid):
    for text in texts(TOPICS[tid]):
        assert not find_violations(text), (tid, text)
        assert not TIMELINE.search(text), (tid, text)


@pytest.mark.parametrize("tid", ANTIFRAUD)
def test_antifraud_escalates_and_hides_internals(tid):
    t = TOPICS[tid]
    assert t["escalate_if"].startswith("Всегда")
    assert t.get("forbidden")
    for text in texts(t):
        assert not ANTIFRAUD_INTERNALS.search(text), (tid, text)


@pytest.mark.parametrize("tid", [
    "payments/status_pending", "payments/status_failed_debited", "payments/double_payment",
    "payments/cancel_successful", "apartment/debt_not_updated", "apartment/payment_status",
])
def test_money_cases_escalate_and_forbid_confirmation(tid):
    t = TOPICS[tid]
    assert t["escalate_if"] and t.get("forbidden"), tid


def test_debt_department_phones_listed():
    steps = " ".join(TOPICS["apartment/debt_not_updated"]["answer_steps"])
    for complex_, phone in [("Orzular", "90 033 14 00"), ("Afsona", "77 122 14 00"), ("Ocean", "90 061 14 00"),
                            ("Shirin hayot", "77 353 14 00"), ("Vooha Residence", "77 003 14 00")]:
        assert f"{complex_}" in steps and phone in steps


def test_server_error_does_not_promise_restore_time():
    t = TOPICS["technical/server_error"]
    assert any("время восстановления" in f for f in t["forbidden"])


def test_prompt_renders_topic_restrictions():
    p = build_system_prompt(KB)
    for tid in set(CASES.values()):
        assert f"topic={tid}" in p
    assert "Нельзя: Не называть причины, правила, критерии и лимиты системы безопасности" in p


@pytest.mark.parametrize("text", [
    "Ваша SIM-карта была клонирована, поэтому доступ ограничен.",
    "Ваш телефон клонирован.",
    "Система обнаружила клонирование устройства.",
    "Обнаружено клонирование вашего устройства.",
    "Sizning SIM kartangiz klonlangan.",
    "Сизнинг телефонингиз клонланган.",
    "Your SIM was cloned.",
])
def test_confirming_cloning_is_forbidden(text):
    assert find_violations(text), text


@pytest.mark.parametrize("text", [
    "Доступ ограничен системой безопасности. Передаю обращение специалистам.",
    "Операция находится в обработке. Мы проверим её статус и дополнительно сообщим о результате.",
    "Возможны временные технические затруднения. Пожалуйста, попробуйте повторить операцию позже.",
])
def test_operator_phrases_from_cases_are_allowed(text):
    assert not find_violations(text), find_violations(text)


@pytest.mark.parametrize("text,cat", [
    ("Отмените успешную операцию, я передумал", "cancel"),
    ("Я дважды оплатил, верните деньги", "refund"),
    ("Почему меня заблокировал антифрод?", "antifraud_internals"),
])
def test_case_requests_go_to_human(text, cat):
    assert restricted_request(text) == cat
