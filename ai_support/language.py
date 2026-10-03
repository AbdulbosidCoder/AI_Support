"""Heuristic language detection: Uzbek (Latin/Cyrillic), Russian, English.

Used for template replies (errors, guardrail fallbacks) where no model output is available.
"""
from __future__ import annotations

import re

from .models import Lang

_CYR = re.compile(r"[а-яёўқғҳА-ЯЁЎҚҒҲ]")
_LAT = re.compile(r"[a-zA-Z]")
_UZ_CYR_LETTERS = re.compile(r"[ўқғҳЎҚҒҲ]")
_UZ_CYR_WORDS = {
    "ва", "учун", "билан", "керак", "эмас", "йўқ", "нима", "қандай", "пул", "қилиш", "бўлди",
    "бор", "менинг", "кирит", "ёрдам", "илтимос", "салом", "тўлов", "ўтказма", "картам",
}
_UZ_LAT_WORDS = {
    "va", "uchun", "bilan", "kerak", "emas", "yo'q", "yoq", "nima", "qanday", "pul", "qilish",
    "bo'ldi", "boldi", "bor", "mening", "menga", "yordam", "iltimos", "salom", "assalomu", "to'lov",
    "tolov", "o'tkazma", "otkazma", "kartam", "karta", "qaytarish", "nega", "ishlamayapti", "kod",
    "kelmadi", "ilova", "xato", "pulim", "yechildi", "qilib", "bermayapti", "kirolmayapman",
}
_EN_WORDS = {
    "the", "is", "my", "i", "and", "to", "not", "money", "card", "payment", "transfer", "please",
    "why", "how", "app", "error", "help", "can't", "cannot", "hello", "hi", "account", "what",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'ʻ’`]+", text.lower().replace("ʻ", "'").replace("’", "'").replace("`", "'"))


def detect_language(text: str, default: Lang = Lang.UZ_LATN) -> Lang:
    if not text or not text.strip():
        return default
    cyr = len(_CYR.findall(text))
    lat = len(_LAT.findall(text))
    words = _words(text)
    if cyr > lat:
        if _UZ_CYR_LETTERS.search(text) or sum(w in _UZ_CYR_WORDS for w in words) >= 1:
            return Lang.UZ_CYRL
        return Lang.RU
    if lat == 0:
        return default
    uz = sum(w in _UZ_LAT_WORDS for w in words) + text.count("o'") + text.count("g'")
    en = sum(w in _EN_WORDS for w in words)
    if en > uz:
        return Lang.EN
    return Lang.UZ_LATN
