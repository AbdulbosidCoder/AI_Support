"""Hard rules checked in code, independent of the model.

1. Restricted requests (refund, cancellation, unblocking, limit/data change, antifraud
   internals) always go to a human.
2. Forbidden answers (promised refunds/timelines, confirmed success, unblocking, antifraud
   criteria) are never sent: the answer is replaced with a safe hand-off.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_I = re.IGNORECASE

# --- client asks for something only staff can decide -----------------------------------------
RESTRICTED_REQUESTS: dict[str, list[str]] = {
    "refund": [
        r"верн(и|ите|уть)\w*\s+(мне\s+)?(деньг|средств|плат)", r"возврат", r"refund", r"money back",
        r"pul(im|imni|ni)?\s+qaytar", r"qaytarib\s+ber", r"пул(им|имни|ни)?\s+қайтар", r"қайтариб\s+бер",
    ],
    "cancel": [
        r"отмен(и|ите|ить)\w*\s+(\w+\s+)?(плат|перев|операц)", r"cancel\s+(the\s+|my\s+)?(payment|transfer|transaction)",
        r"(to'lov|o'tkazma|operatsiya)\w*\s+bekor\s+qil", r"(тўлов|ўтказма|операция)\w*\s+бекор\s+қил",
    ],
    "unblock": [
        r"разблокир", r"сним\w*\s+блок", r"unblock", r"unlock\s+(my\s+)?(card|account)",
        r"blokdan\s+chiqar", r"блокдан\s+чиқар",
    ],
    "limit_or_data_change": [
        r"(увелич|повыс|измен|сним)\w*\s+(мне\s+)?лимит", r"(increase|raise|change|remove)\s+(my\s+|the\s+)?limit",
        r"limit\w*\s+(oshir|o'zgartir|olib\s+tashla)", r"лимит\w*\s+(оширин|ўзгартир)",
        r"(измен|поменя)\w*\s+(мои\s+)?(данн|пинфл|фио)", r"(change|update)\s+my\s+(personal\s+)?(data|pinfl|name)",
        r"(ma'lumot|pinfl|jshshir)\w*\s+o'zgartir",
    ],
    "antifraud_internals": [
        r"(почему|за что).{0,30}(заблокир|антифрод|antifraud)", r"(критери|правил|алгоритм)\w*\s+(антифрод|antifraud|блокир)",
        r"(why|reason).{0,30}(blocked|antifraud)", r"nega.{0,30}(bloklan|antifrod|antifraud)",
        r"нега.{0,30}(блоклан|антифрод)",
    ],
}

# --- things the bot must never say ------------------------------------------------------------
_NUM_TIME_RU = r"\d+\s*(минут|час|дн|сут|рабоч)"
_NUM_TIME_UZ = r"\d+\s*(daqiqa|soat|kun|иш\s+куни|дақиқа|соат|кун)"
_NUM_TIME_EN = r"\d+\s*(minute|hour|day|business day)"
FORBIDDEN_ANSWERS: dict[str, list[str]] = {
    "promised_refund": [
        r"деньги\s+(вам\s+)?(вернутся|будут\s+возвращены|поступят\s+обратно)", r"мы\s+вернём", r"мы\s+вернем",
        r"возврат\s+(подтвержд|оформл|одобр)", r"средства\s+(уже\s+)?возвращ",
        r"pul(ingiz)?\s+(albatta\s+)?qaytadi", r"pul(ingiz)?\s+qaytarildi", r"qaytarib\s+beramiz", r"mablag'\w*\s+qaytarildi",
        r"пул(ингиз)?\s+қайтади", r"қайтариб\s+берамиз",
        r"(your\s+)?money\s+will\s+be\s+(returned|refunded)", r"refund\s+(is\s+|has\s+been\s+)?(confirmed|approved|processed)",
        r"we\s+will\s+refund",
    ],
    "promised_timeline": [
        rf"(в\s+течение|через|в\s+пределах)\s+{_NUM_TIME_RU}", rf"{_NUM_TIME_UZ}\w*\s+(ichida|davomida|ичида)",
        rf"(within|in)\s+{_NUM_TIME_EN}",
    ],
    "confirmed_success": [
        r"(операция|платёж|платеж|перевод)\s+(успешно\s+)?(прош(ёл|ел|ла)\s+успешно|успешно\s+(заверш|выполн|проведен))",
        r"(operatsiya|to'lov|o'tkazma)\w*\s+muvaffaqiyatli\s+(yakunlandi|amalga\s+oshdi|o'tdi)",
        r"(операция|тўлов|ўтказма)\w*\s+муваффақиятли\s+(якунланди|амалга\s+ошди|ўтди)",
        r"(payment|transfer|transaction)\s+(was|is|has\s+been)\s+(completed\s+)?successful",
    ],
    "unblocked": [
        r"(мы\s+)?(разблокировал|разблокируем)", r"блокировк\w+\s+снят", r"blokdan\s+chiqard(ik|im)", r"blokdan\s+chiqaramiz",
        r"блокдан\s+чиқардик", r"(we\s+)?(have\s+)?unblocked", r"we\s+will\s+unblock",
    ],
    "changed_limit_or_data": [
        r"лимит\s+(увеличен|повышен|изменён|изменен)", r"limit\s+(oshirildi|o'zgartirildi)",
        r"limit\s+(has\s+been\s+|was\s+)?(increased|raised|changed)", r"(данные|пинфл)\s+(изменены|обновлены)",
        r"платёж\s+отменён|платеж\s+отменен|to'lov\s+bekor\s+qilindi|payment\s+(has\s+been\s+|was\s+)?cancel+ed",
    ],
    "antifraud_criteria": [
        r"(антифрод|antifraud|antifrod|антифрод)\w*\s+(срабатывает|блокирует|reacts|triggers|ishlaydi|ишлайди)[\s,]+(когда|если|при|when|if|agar|агар)",
        # any antifraud statement with a numeric threshold
        r"(антифрод|antifraud|antifrod)\w*.{0,60}(больше|более|свыше|more\s+than|over|dan\s+ortiq|дан\s+ортиқ)\s*\d+",
        r"(критери|правил)\w*\s+антифрод\w*\s*:", r"antifraud\s+(rules|criteria)\s*(are|:)",
        # confirming a specific security event (e.g. a cloned device or SIM) reveals what antifraud detected
        r"(sim|сим|устройств|телефон)\S*(\s+\S+){0,2}?\s+клонирован", r"(обнаруж|выявл)\S*\s+клонирован",
        r"(sim|telefon|qurilma)\S*(\s+\S+)?\s+klonlangan", r"(сим|телефон|қурилма)\S*(\s+\S+)?\s+клонланган",
        r"(sim|device|phone)\S*\s+(was\s+|has\s+been\s+)?cloned",
    ],
    "financial_advice": [
        r"(советую|рекомендую)\s+(вложить|инвестир|купить\s+акци|взять\s+кредит)",
        r"(i\s+)?recommend\s+(investing|buying\s+stocks|taking\s+a\s+loan)",
        r"(sarmoya\s+kiriting|kredit\s+oling)",
    ],
}



def _norm(text: str) -> str:
    return text.replace("ʻ", "'").replace("’", "'").replace("`", "'").replace("ё", "е").replace("Ё", "Е")


_COMPILED_REQ = {k: [re.compile(_norm(p), _I) for p in v] for k, v in RESTRICTED_REQUESTS.items()}
_COMPILED_ANS = {k: [re.compile(_norm(p), _I) for p in v] for k, v in FORBIDDEN_ANSWERS.items()}


def restricted_request(text: str) -> str | None:
    """Return the restricted category the client asks for, or None."""
    text = _norm(text or "")
    for cat, pats in _COMPILED_REQ.items():
        if any(p.search(text) for p in pats):
            return cat
    return None


@dataclass
class Violation:
    category: str
    fragment: str


def find_violations(answer: str) -> list[Violation]:
    answer = _norm(answer or "")
    out = []
    for cat, pats in _COMPILED_ANS.items():
        for p in pats:
            m = p.search(answer)
            if m:
                out.append(Violation(cat, m.group(0)))
                break
    return out
