"""Quick-question menu and language choice, independent of the channel.

The menu is a tree: topics first (registration, cards, payments...), then the questions of the
chosen topic, each written in full as the client would ask it. A quick question is answered exactly
as if the client had typed it: it goes through the engine, the knowledge base and the guardrails,
unless an admin wrote a guide for it in the panel (ai_support/guides.py), which is then sent instead.
Admins can also rename, hide or add questions there. Channels only render the buttons.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Lang

# Order in which languages are offered on the first start.
LANGUAGE_CHOICES: list[tuple[Lang, str]] = [
    (Lang.UZ_LATN, "O'zbekcha (lotin)"),
    (Lang.UZ_CYRL, "Ўзбекча (кирилл)"),
    (Lang.RU, "Русский"),
    (Lang.EN, "English"),
]

# Shown before the client has chosen a language, so it greets in all three main languages.
CHOOSE_LANGUAGE = (
    "Assalomu alaykum! Xonsaroy Pay qo'llab-quvvatlash xizmati. Iltimos, tilni tanlang.\n\n"
    "Здравствуйте! Это поддержка Xonsaroy Pay. Пожалуйста, выберите язык.\n\n"
    "Hello! This is Xonsaroy Pay support. Please choose your language."
)


@dataclass(frozen=True)
class QuickQuestion:
    id: str
    # The button: the client's question in full, in the client's words.
    label: dict[Lang, str]
    # What is asked of the engine; the label itself unless the question needs more detail.
    question: dict[Lang, str]


@dataclass(frozen=True)
class Category:
    """A topic in the main menu; tapping it shows its questions."""
    id: str
    label: dict[Lang, str]
    questions: list[QuickQuestion]


def _q(qid: str, uz: str, cyrl: str, ru: str, en: str, question: dict[Lang, str] | None = None) -> QuickQuestion:
    label = {Lang.UZ_LATN: uz, Lang.UZ_CYRL: cyrl, Lang.RU: ru, Lang.EN: en}
    return QuickQuestion(qid, label, question or label)


def _label(uz: str, cyrl: str, ru: str, en: str) -> dict[Lang, str]:
    return {Lang.UZ_LATN: uz, Lang.UZ_CYRL: cyrl, Lang.RU: ru, Lang.EN: en}


CATEGORIES: list[Category] = [
    Category("registration", _label("Ro'yxatdan o'tish va kirish", "Рўйхатдан ўтиш ва кириш",
                                    "Регистрация и вход", "Registration and login"), [
        _q("registration", "Ilovada qanday ro'yxatdan o'taman?", "Иловада қандай рўйхатдан ўтаман?",
           "Как зарегистрироваться в приложении?", "How do I register in the app?",
           {Lang.UZ_LATN: "Xonsaroy Pay ilovasida qanday ro'yxatdan o'taman va identifikatsiyadan o'taman?",
            Lang.UZ_CYRL: "Xonsaroy Pay иловасида қандай рўйхатдан ўтаман ва идентификациядан ўтаман?",
            Lang.RU: "Как зарегистрироваться в приложении Xonsaroy Pay и пройти идентификацию?",
            Lang.EN: "How do I register in the Xonsaroy Pay app and pass identification?"}),
        _q("registration_problem", "Ro'yxatdan o'tishda muammo bo'ldi", "Рўйхатдан ўтишда муаммо бўлди",
           "Проблема при регистрации", "I have a problem registering"),
        _q("sms_code", "SMS-kod kelmayapti, nima qilay?", "SMS-код келмаяпти, нима қилай?",
           "Не приходит SMS-код, что делать?", "I don't receive the SMS code, what should I do?"),
        _q("myid", "MyID identifikatsiyadan o'ta olmayapman", "MyID идентификациядан ўта олмаяпман",
           "Не могу пройти идентификацию MyID", "I can't pass MyID identification"),
        _q("cannot_login", "Ilovaga kira olmayapman", "Иловага кира олмаяпман",
           "Не могу войти в приложение", "I can't log in to the app"),
        _q("vpn_on_login", "Kirishda VPN haqida xabar chiqyapti", "Киришда VPN ҳақида хабар чиқяпти",
           "При входе пишет, что включён VPN", "The app says VPN is on when I log in"),
    ]),
    Category("cards", _label("Kartalar", "Карталар", "Карты", "Cards"), [
        _q("add_card", "Kartani qanday qo'shaman?", "Картани қандай қўшаман?",
           "Как добавить карту?", "How do I add a card?"),
        _q("card_sms", "Karta qo'shishda SMS kelmayapti", "Карта қўшишда SMS келмаяпти",
           "Не приходит SMS при добавлении карты", "No SMS when I add a card"),
        _q("card_blocked", "Kartam vaqtincha bloklangan deb chiqyapti", "Картам вақтинча блокланган деб чиқяпти",
           "Пишет, что карта временно заблокирована", "It says my card is temporarily blocked"),
        _q("delete_card", "Kartani qanday o'chiraman?", "Картани қандай ўчираман?",
           "Как удалить карту?", "How do I delete a card?"),
    ]),
    Category("payments", _label("To'lovlar", "Тўловлар", "Платежи", "Payments"), [
        _q("how_to_pay", "Xizmat uchun qanday to'layman?", "Хизмат учун қандай тўлайман?",
           "Как оплатить услугу?", "How do I pay for a service?"),
        _q("payment_problem", "To'lov o'tmadi yoki Pending holatida", "Тўлов ўтмади ёки Pending ҳолатида",
           "Платёж не прошёл или завис в Pending", "My payment failed or is stuck in Pending",
           {Lang.UZ_LATN: "To'lov o'tmadi yoki Pending holatida qoldi. Nima qilishim kerak?",
            Lang.UZ_CYRL: "Тўлов ўтмади ёки Pending ҳолатида қолди. Нима қилишим керак?",
            Lang.RU: "Платёж не прошёл или завис в статусе Pending. Что делать?",
            Lang.EN: "My payment failed or is stuck in Pending. What should I do?"}),
        _q("failed_debited", "To'lov o'tmadi, lekin pul yechildi", "Тўлов ўтмади, лекин пул ечилди",
           "Платёж не прошёл, но деньги списались", "The payment failed but money was debited"),
        _q("double_payment", "Bitta xizmatga ikki marta to'lab qo'ydim", "Битта хизматга икки марта тўлаб қўйдим",
           "Я дважды оплатил одну услугу", "I paid for one service twice"),
        _q("history", "Operatsiyalar tarixini qayerdan topaman?", "Операциялар тарихини қаердан топаман?",
           "Где найти историю операций?", "Where can I find my transaction history?"),
    ]),
    Category("transfers", _label("O'tkazmalar", "Ўтказмалар", "Переводы", "Transfers"), [
        _q("how_to_transfer", "Kartadan kartaga qanday o'tkazaman?", "Картадан картага қандай ўтказаман?",
           "Как перевести с карты на карту?", "How do I make a card-to-card transfer?"),
        _q("transfer_not_received", "O'tkazma qabul qiluvchiga kelmadi", "Ўтказма қабул қилувчига келмади",
           "Перевод не дошёл до получателя", "The recipient didn't get my transfer",
           {Lang.UZ_LATN: "Kartadan kartaga pul o'tkazdim, lekin qabul qiluvchi olmadi. Nima qilish kerak?",
            Lang.UZ_CYRL: "Картадан картага пул ўтказдим, лекин қабул қилувчи олмади. Нима қилиш керак?",
            Lang.RU: "Я сделал перевод с карты на карту, но получатель не получил деньги. Что делать?",
            Lang.EN: "I made a card-to-card transfer but the recipient didn't get it. What should I do?"}),
        _q("wrong_recipient", "Pulni noto'g'ri kartaga o'tkazib yubordim", "Пулни нотўғри картага ўтказиб юбордим",
           "Я перевёл деньги не на ту карту", "I sent money to the wrong card"),
    ]),
    Category("apartment", _label("Kvartira to'lovi", "Квартира тўлови", "Оплата квартиры",
                                 "Apartment payments"), [
        _q("add_contract", "Shartnomani ilovaga qanday qo'shaman?", "Шартномани иловага қандай қўшаман?",
           "Как добавить договор в приложение?", "How do I add my contract to the app?"),
        _q("apartment_debt", "Kvartira qarzini qanday tekshirib, to'layman?", "Квартира қарзини қандай текшириб, тўлайман?",
           "Как проверить и оплатить долг за квартиру?", "How do I check and pay my apartment debt?"),
        _q("debt_not_updated", "To'ladim, lekin qarz yangilanmadi", "Тўладим, лекин қарз янгиланмади",
           "Оплатил, но долг не обновился", "I paid but the debt didn't update"),
        _q("schedule_receipt", "To'lov jadvali va kvitansiya qayerda?", "Тўлов жадвали ва квитанция қаерда?",
           "Где график платежей и квитанция?", "Where are my payment schedule and receipt?"),
    ]),
    Category("security", _label("Xavfsizlik va cheklovlar", "Хавфсизлик ва чекловлар",
                                "Безопасность и ограничения", "Security and restrictions"), [
        _q("restricted", "Hisobimda cheklov borligi haqida xabar chiqdi", "Ҳисобимда чеклов борлиги ҳақида хабар чиқди",
           "Пишет, что на аккаунте ограничение", "It says my account has a restriction"),
        _q("suspicious", "Men qilmagan operatsiyani ko'ryapman", "Мен қилмаган операцияни кўряпман",
           "Вижу операцию, которую я не делал", "I see an operation I didn't make"),
    ]),
    Category("technical", _label("Texnik muammolar", "Техник муаммолар", "Технические проблемы",
                                 "Technical problems"), [
        _q("server_error", "«Server Error» yoki cheksiz yuklanish", "«Server Error» ёки чексиз юкланиш",
           "«Server Error» или бесконечная загрузка", "«Server Error» or endless loading"),
        _q("app_not_opening", "Ilova ochilmayapti", "Илова очилмаяпти",
           "Приложение не открывается", "The app won't open"),
    ]),
    Category("profile", _label("Profil va sozlamalar", "Профил ва созламалар", "Профиль и настройки",
                               "Profile and settings"), [
        _q("app_settings", "Ilova tili va sozlamalarini qanday o'zgartiraman?",
           "Илова тили ва созламаларини қандай ўзгартираман?",
           "Как изменить язык и настройки приложения?", "How do I change the app language and settings?"),
    ]),
]

# A simple emoji in front of a topic or question button, where one picture says it at a glance. Only
# buttons carry them: the question as echoed in the chat and sent to the engine stays plain text, and
# messages to clients have no emoji. Questions added in the admin panel get none.
ICONS: dict[str, str] = {
    # topics
    "registration": "📝", "cards": "💳", "payments": "💸", "transfers": "🔄", "apartment": "🏠",
    "security": "🛡", "technical": "🛠", "profile": "👤",
    # questions ("registration" is both a topic and its first question)
    "registration_problem": "⚠️", "sms_code": "💬", "myid": "🪪", "cannot_login": "🔐",
    "vpn_on_login": "🌐", "add_card": "➕", "card_sms": "💬", "card_blocked": "🔒", "delete_card": "🗑",
    "how_to_pay": "💸", "payment_problem": "⏳", "failed_debited": "❗", "double_payment": "🔁",
    "history": "📜", "how_to_transfer": "🔄", "transfer_not_received": "📭", "wrong_recipient": "↩️",
    "add_contract": "📄", "apartment_debt": "🏠", "debt_not_updated": "🧾", "schedule_receipt": "📅",
    "restricted": "🚫", "suspicious": "🕵️", "server_error": "🛠", "app_not_opening": "📵",
    "app_settings": "⚙️",
}


def button_label(item_id: str, text: str) -> str:
    """A topic's or question's button text with its emoji, if it has one (and the text lacks it)."""
    icon = ICONS.get(item_id)
    return f"{icon} {text}" if icon and not text.startswith(icon) else text


# Every question of every category, for lookups by id.
QUICK_QUESTIONS: list[QuickQuestion] = [q for c in CATEGORIES for q in c.questions]

OPERATOR_LABEL = {Lang.UZ_LATN: "Operator", Lang.UZ_CYRL: "Оператор",
                  Lang.RU: "Оператор", Lang.EN: "Operator"}
SETTINGS_LABEL = {Lang.UZ_LATN: "Sozlamalar", Lang.UZ_CYRL: "Созламалар",
                  Lang.RU: "Настройки", Lang.EN: "Settings"}
END_LABEL = {Lang.UZ_LATN: "Suhbatni yakunlash", Lang.UZ_CYRL: "Суҳбатни якунлаш",
             Lang.RU: "Завершить разговор", Lang.EN: "End conversation"}
# Paging through a topic's questions when there are more than fit one screen (QUESTIONS_PER_PAGE).
QUESTIONS_PER_PAGE = 5
PREV_PAGE_LABEL = {Lang.UZ_LATN: "‹ Oldingi", Lang.UZ_CYRL: "‹ Олдинги", Lang.RU: "‹ Предыдущие",
                   Lang.EN: "‹ Previous"}
NEXT_PAGE_LABEL = {Lang.UZ_LATN: "Keyingi ›", Lang.UZ_CYRL: "Кейинги ›", Lang.RU: "Следующие ›", Lang.EN: "Next ›"}
BACK_LABEL = {Lang.UZ_LATN: "Orqaga", Lang.UZ_CYRL: "Орқага", Lang.RU: "Назад", Lang.EN: "Back"}
MENU_LABEL = {Lang.UZ_LATN: "Menyu", Lang.UZ_CYRL: "Меню", Lang.RU: "Меню", Lang.EN: "Menu"}
CHANGE_PHONE_LABEL = {Lang.UZ_LATN: "Raqamni yangilash", Lang.UZ_CYRL: "Рақамни янгилаш",
                      Lang.RU: "Обновить номер", Lang.EN: "Update phone number"}
CHANGE_LANGUAGE_LABEL = {Lang.UZ_LATN: "Tilni o'zgartirish", Lang.UZ_CYRL: "Тилни ўзгартириш",
                         Lang.RU: "Изменить язык", Lang.EN: "Change language"}


@dataclass(frozen=True)
class MenuAction:
    kind: str  # "question" | "operator" | "settings" | "end" | "menu"
    question: QuickQuestion | None = None


def menu_rows(lang: Lang) -> list[list[str]]:
    """Main menu button labels: topics two per row; then operator and settings, and "end conversation" last."""
    labels = [c.label[lang] for c in CATEGORIES]
    rows = [labels[i:i + 2] for i in range(0, len(labels), 2)]
    rows.append([OPERATOR_LABEL[lang], SETTINGS_LABEL[lang]])
    rows.append([END_LABEL[lang]])
    return rows


def category(cid: str) -> Category | None:
    return next((c for c in CATEGORIES if c.id == cid), None)


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
    if text in END_LABEL.values():
        return MenuAction("end")
    if text in MENU_LABEL.values():
        return MenuAction("menu")
    for q in QUICK_QUESTIONS:
        if text in q.label.values():
            return MenuAction("question", q)
    return None
