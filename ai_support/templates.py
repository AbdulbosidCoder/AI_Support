"""Fixed replies used without the model: hand-off, errors, PII reminder, greetings, menu."""
from __future__ import annotations

from .models import Lang

_T: dict[str, dict[Lang, str]] = {
    "welcome": {
        Lang.UZ_LATN: "Qanday muammo yuz berdi? Yozing, ovozli xabar yoki skrinshot yuboring.",
        Lang.UZ_CYRL: "Қандай муаммо юз берди? Ёзинг, овозли хабар ёки скриншот юборинг.",
        Lang.RU: "Какая у вас проблема? Напишите, отправьте голосовое или скриншот.",
        Lang.EN: "What problem are you facing? Write it, send a voice message or a screenshot.",
    },
    "ask_problem": {
        Lang.UZ_LATN: "Assalomu alaykum! Qanday muammo yuz berdi?",
        Lang.UZ_CYRL: "Ассалому алайкум! Қандай муаммо юз берди?",
        Lang.RU: "Здравствуйте! Какая у вас проблема?",
        Lang.EN: "Hello! What problem are you facing?",
    },
    "menu_hint": {
        Lang.UZ_LATN: "Yoki tayyor savollardan birini tanlang:",
        Lang.UZ_CYRL: "Ёки тайёр саволлардан бирини танланг:",
        Lang.RU: "Или выберите готовый вопрос:",
        Lang.EN: "Or pick a common question:",
    },
    "language_saved": {
        Lang.UZ_LATN: "Til saqlandi: O'zbekcha.",
        Lang.UZ_CYRL: "Тил сақланди: Ўзбекча.",
        Lang.RU: "Язык сохранён: Русский.",
        Lang.EN: "Language saved: English.",
    },
    "settings": {
        Lang.UZ_LATN: "Sozlamalar. Kerakli bandni tanlang.",
        Lang.UZ_CYRL: "Созламалар. Керакли бандни танланг.",
        Lang.RU: "Настройки. Выберите нужный пункт.",
        Lang.EN: "Settings. Choose an option.",
    },
    "handoff": {
        Lang.UZ_LATN: "Murojaatingiz qo'llab-quvvatlash xodimiga yuborildi. U shu chatda javob beradi.",
        Lang.UZ_CYRL: "Мурожаатингиз қўллаб-қувватлаш ходимига юборилди. У шу чатда жавоб беради.",
        Lang.RU: "Ваше обращение передано специалисту поддержки. Он ответит вам в этом чате.",
        Lang.EN: "Your request has been passed to a support specialist. They will reply in this chat.",
    },
    "guardrail": {
        Lang.UZ_LATN: "Bu masala bo'yicha men o'zim tasdiqlay olmayman. Murojaatingizni mas'ul xodimga tekshirish uchun yuboraman.",
        Lang.UZ_CYRL: "Бу масала бўйича мен ўзим тасдиқлай олмайман. Мурожаатингизни масъул ходимга текшириш учун юбораман.",
        Lang.RU: "Я не могу сам подтвердить это. Передаю ваше обращение ответственному сотруднику для проверки.",
        Lang.EN: "I can't confirm this myself. I am passing your request to a responsible specialist for review.",
    },
    "error": {
        Lang.UZ_LATN: "Hozir xabaringizni qayta ishlay olmadim. Murojaatingizni qo'llab-quvvatlash xodimiga yuboraman.",
        Lang.UZ_CYRL: "Ҳозир хабарингизни қайта ишлай олмадим. Мурожаатингизни қўллаб-қувватлаш ходимига юбораман.",
        Lang.RU: "Сейчас не получилось обработать сообщение. Передаю обращение специалисту поддержки.",
        Lang.EN: "I couldn't process your message right now. I am passing it to a support specialist.",
    },
    "pii_reminder": {
        Lang.UZ_LATN: "Xavfsizlik uchun: karta raqami, JShShIR (PINFL), SMS-kod va balansni yubormang. Agar ular skrinshotda bo'lsa, yuborishdan oldin yopib qo'ying.",
        Lang.UZ_CYRL: "Хавфсизлик учун: карта рақами, ЖШШИР (PINFL), SMS-код ва балансни юборманг. Агар улар скриншотда бўлса, юборишдан олдин ёпиб қўйинг.",
        Lang.RU: "Для вашей безопасности: не присылайте номер карты, ПИНФЛ, SMS-коды и баланс. Если они видны на скриншоте, закройте их перед отправкой.",
        Lang.EN: "For your safety: please don't send card numbers, PINFL, SMS codes or balances. If they are visible on a screenshot, cover them before sending.",
    },
    "voice_unavailable": {
        Lang.UZ_LATN: "Ovozli xabarni hozir tanib bo'lmadi. Iltimos, muammoni matn bilan yozing.",
        Lang.UZ_CYRL: "Овозли хабарни ҳозир таниб бўлмади. Илтимос, муаммони матн билан ёзинг.",
        Lang.RU: "Не удалось распознать голосовое сообщение. Пожалуйста, напишите проблему текстом.",
        Lang.EN: "I couldn't recognise the voice message. Please describe the problem in text.",
    },
    "image_unsupported": {
        Lang.UZ_LATN: "Rasmni ochib bo'lmadi. Iltimos, skrinshotni JPG yoki PNG ko'rinishida yuboring yoki muammoni matn bilan yozing.",
        Lang.UZ_CYRL: "Расмни очиб бўлмади. Илтимос, скриншотни JPG ёки PNG кўринишида юборинг ёки муаммони матн билан ёзинг.",
        Lang.RU: "Не удалось открыть изображение. Пришлите скриншот в формате JPG или PNG или опишите проблему текстом.",
        Lang.EN: "I couldn't open the image. Please send the screenshot as JPG or PNG, or describe the problem in text.",
    },
    "empty": {
        Lang.UZ_LATN: "Muammoingizni yozing yoki ilova skrinshotini yuboring.",
        Lang.UZ_CYRL: "Муаммоингизни ёзинг ёки илова скриншотини юборинг.",
        Lang.RU: "Опишите проблему или отправьте скриншот приложения.",
        Lang.EN: "Please describe the problem or send a screenshot of the app.",
    },
}


def t(key: str, lang: Lang) -> str:
    return _T[key][lang]
