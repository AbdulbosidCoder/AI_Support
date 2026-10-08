"""Forbidden answers must be caught in every language; safe answers must pass."""
import pytest

from ai_support.guardrails import find_violations, restricted_request

FORBIDDEN = [
    # the example from the project brief
    "Pulingiz hozir 2 soat ichida qaytadi.",
    "Деньги вернутся в течение 2 часов.",
    "Мы вернём вам деньги, не переживайте.",
    "Возврат подтверждён, ожидайте.",
    "Your money will be refunded within 24 hours.",
    "We will refund the payment.",
    "Pulingiz albatta qaytadi.",
    "Пулингиз қайтади, хавотир олманг.",
    "Операция прошла успешно, деньги у получателя.",
    "Перевод успешно завершён.",
    "To'lov muvaffaqiyatli amalga oshdi.",
    "The payment was successful.",
    "Мы разблокировали вашу карту.",
    "Kartangizni blokdan chiqardik.",
    "Kartangiz blokdan chiqarildi.",
    "Картангиз блокдан чиқарилди.",
    "We have unblocked your account.",
    "Ваш лимит увеличен до 50 млн.",
    "Limit oshirildi.",
    "Платёж отменён.",
    "To'lov bekor qilindi.",
    "Антифрод срабатывает, когда больше 5 переводов в час.",
    "Советую вложить деньги в акции.",
    "Mablag' 3 kun ichida tushadi.",
    "Перевод зачислится через 30 минут.",
]

SAFE = [
    "Operatsiya holatiga ko'ra, u Pending holatida. To'lovni takroran amalga oshirmaslikni va operatsiyani tekshirish uchun yuborishni tavsiya qilamiz.",
    "Не повторяйте платёж. Пришлите дату, время и сумму, мы передадим операцию на проверку.",
    "Ilovada ko'rsatilganidek, karta xavfsizlik maqsadida vaqtincha cheklangan.",
    "Я не могу подтвердить возврат. Передаю обращение ответственному сотруднику.",
    "Please don't repeat the transfer; I am passing it to a specialist for review.",
    "Добавьте карту: «Kartalarim» → «Qo'shish». Код придёт в SMS.",
    "Qayta yuborish tugmasini taymer tugagandan keyin bosing.",
]


@pytest.mark.parametrize("text", FORBIDDEN)
def test_forbidden_caught(text):
    assert find_violations(text), text


@pytest.mark.parametrize("text", SAFE)
def test_safe_passes(text):
    assert not find_violations(text), find_violations(text)


@pytest.mark.parametrize("text,cat", [
    ("Верните мне деньги!", "refund"),
    ("Pulimni qaytarib bering", "refund"),
    ("Пулимни қайтариб беринг", "refund"),
    ("I want a refund", "refund"),
    ("Отмените платёж пожалуйста", "cancel"),
    ("to'lovni bekor qiling", "cancel"),
    ("Разблокируйте карту", "unblock"),
    ("kartamni blokdan chiqaring", "unblock"),
    ("please unblock my account", "unblock"),
    ("Увеличьте мне лимит", "limit_or_data_change"),
    ("limitni oshiring", "limit_or_data_change"),
    ("Почему меня заблокировал антифрод?", "antifraud_internals"),
    ("nega kartam bloklandi", "antifraud_internals"),
])
def test_restricted_requests(text, cat):
    assert restricted_request(text) == cat


def test_ordinary_question_not_restricted():
    assert restricted_request("Как добавить карту?") is None
    assert restricted_request("SMS kod kelmayapti") is None
