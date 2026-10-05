"""Quick-question menu and language choice, independent of the channel.

A quick question is answered exactly as if the client had typed it: it goes through the
engine, the knowledge base and the guardrails. Channels only render the buttons.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Lang

# Order in which languages are offered on the first start.
LANGUAGE_CHOICES: list[tuple[Lang, str]] = [
    (Lang.UZ_LATN, "🇺🇿 O'zbekcha"),
    (Lang.UZ_CYRL, "🇺🇿 Ўзбекча"),
    (Lang.RU, "🇷🇺 Русский"),
    (Lang.EN, "🇬🇧 English"),
]

# Shown before the client has chosen a language, so it greets in all three main languages.
CHOOSE_LANGUAGE = (
    "Assalomu alaykum! Men Xonsaroy Pay yordamchisiman. Iltimos, tilni tanlang.\n\n"
    "Здравствуйте! Я помощник Xonsaroy Pay. Пожалуйста, выберите язык.\n\n"
    "Hello! I am the Xonsaroy Pay assistant. Please choose your language."
)


@dataclass(frozen=True)
class QuickQuestion:
    id: str
    label: dict[Lang, str]
    question: dict[Lang, str]


QUICK_QUESTIONS: list[QuickQuestion] = [
    QuickQuestion(
        "add_card",
        {Lang.UZ_LATN: "💳 Karta qo'shish", Lang.UZ_CYRL: "💳 Карта қўшиш",
         Lang.RU: "💳 Добавить карту", Lang.EN: "💳 Add a card"},
        {Lang.UZ_LATN: "Kartani qanday qo'shaman?", Lang.UZ_CYRL: "Картани қандай қўшаман?",
         Lang.RU: "Как добавить карту?", Lang.EN: "How do I add a card?"},
    ),
    QuickQuestion(
        "sms_code",
        {Lang.UZ_LATN: "📩 SMS-kod kelmayapti", Lang.UZ_CYRL: "📩 SMS-код келмаяпти",
         Lang.RU: "📩 Не приходит SMS-код", Lang.EN: "📩 No SMS code"},
        {Lang.UZ_LATN: "SMS-kod kelmayapti, nima qilay?", Lang.UZ_CYRL: "SMS-код келмаяпти, нима қилай?",
         Lang.RU: "Не приходит SMS-код, что делать?", Lang.EN: "I don't receive the SMS code, what should I do?"},
    ),
    QuickQuestion(
        "payment_problem",
        {Lang.UZ_LATN: "💸 To'lov o'tmadi", Lang.UZ_CYRL: "💸 Тўлов ўтмади",
         Lang.RU: "💸 Платёж не прошёл", Lang.EN: "💸 Payment failed"},
        {Lang.UZ_LATN: "To'lov o'tmadi yoki Pending holatida qoldi. Nima qilishim kerak?",
         Lang.UZ_CYRL: "Тўлов ўтмади ёки Pending ҳолатида қолди. Нима қилишим керак?",
         Lang.RU: "Платёж не прошёл или завис в статусе Pending. Что делать?",
         Lang.EN: "My payment failed or is stuck in Pending. What should I do?"},
    ),
    QuickQuestion(
        "transfer_not_received",
        {Lang.UZ_LATN: "🔄 O'tkazma kelmadi", Lang.UZ_CYRL: "🔄 Ўтказма келмади",
         Lang.RU: "🔄 Перевод не дошёл", Lang.EN: "🔄 Transfer not received"},
        {Lang.UZ_LATN: "Kartadan kartaga pul o'tkazdim, lekin qabul qiluvchi olmadi. Nima qilish kerak?",
         Lang.UZ_CYRL: "Картадан картага пул ўтказдим, лекин қабул қилувчи олмади. Нима қилиш керак?",
         Lang.RU: "Я сделал перевод с карты на карту, но получатель не получил деньги. Что делать?",
         Lang.EN: "I made a card-to-card transfer but the recipient didn't get it. What should I do?"},
    ),
    QuickQuestion(
        "apartment_debt",
        {Lang.UZ_LATN: "🏠 Kvartira qarzi", Lang.UZ_CYRL: "🏠 Квартира қарзи",
         Lang.RU: "🏠 Долг за квартиру", Lang.EN: "🏠 Apartment debt"},
        {Lang.UZ_LATN: "Kvartira qarzini qanday tekshirib, to'layman?",
         Lang.UZ_CYRL: "Квартира қарзини қандай текшириб, тўлайман?",
         Lang.RU: "Как проверить и оплатить задолженность за квартиру?",
         Lang.EN: "How do I check and pay my apartment debt?"},
    ),
    QuickQuestion(
        "history",
        {Lang.UZ_LATN: "📜 Operatsiyalar tarixi", Lang.UZ_CYRL: "📜 Операциялар тарихи",
         Lang.RU: "📜 История операций", Lang.EN: "📜 Transaction history"},
        {Lang.UZ_LATN: "Operatsiyalar tarixini qayerdan topaman?", Lang.UZ_CYRL: "Операциялар тарихини қаердан топаман?",
         Lang.RU: "Где найти историю операций?", Lang.EN: "Where can I find my transaction history?"},
    ),
]

OPERATOR_LABEL = {Lang.UZ_LATN: "👨‍💼 Operator", Lang.UZ_CYRL: "👨‍💼 Оператор",
                  Lang.RU: "👨‍💼 Оператор", Lang.EN: "👨‍💼 Operator"}
SETTINGS_LABEL = {Lang.UZ_LATN: "⚙️ Sozlamalar", Lang.UZ_CYRL: "⚙️ Созламалар",
                  Lang.RU: "⚙️ Настройки", Lang.EN: "⚙️ Settings"}
CHANGE_LANGUAGE_LABEL = {Lang.UZ_LATN: "🌐 Tilni o'zgartirish", Lang.UZ_CYRL: "🌐 Тилни ўзгартириш",
                         Lang.RU: "🌐 Изменить язык", Lang.EN: "🌐 Change language"}


@dataclass(frozen=True)
class MenuAction:
    kind: str  # "question" | "operator" | "settings"
    question: QuickQuestion | None = None


def menu_rows(lang: Lang) -> list[list[str]]:
    """Main menu button labels, two per row; the last row is operator and settings."""
    labels = [q.label[lang] for q in QUICK_QUESTIONS]
    rows = [labels[i:i + 2] for i in range(0, len(labels), 2)]
    rows.append([OPERATOR_LABEL[lang], SETTINGS_LABEL[lang]])
    return rows


def quick_question(qid: str) -> QuickQuestion | None:
    return next((q for q in QUICK_QUESTIONS if q.id == qid), None)


def match_menu(text: str) -> MenuAction | None:
    """The menu button a text came from, in any language (an old keyboard may still be on screen)."""
    text = (text or "").strip()
    if not text:
        return None
    if text in OPERATOR_LABEL.values():
        return MenuAction("operator")
    if text in SETTINGS_LABEL.values():
        return MenuAction("settings")
    for q in QUICK_QUESTIONS:
        if text in q.label.values():
            return MenuAction("question", q)
    return None
