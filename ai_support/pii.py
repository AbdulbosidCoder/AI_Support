"""Masking of personal data in text: card numbers, PINFL, phone numbers, balances."""
from __future__ import annotations

import re

# 16-digit card number, optionally grouped by 4.
_CARD = re.compile(r"(?<!\d)(\d{4})[ -]?(\d{4})[ -]?(\d{4})[ -]?(\d{4})(?!\d)")
# PINFL (JShShIR): 14 digits.
_PINFL = re.compile(r"(?<!\d)\d{14}(?!\d)")
# Uzbek phone numbers: +998 XX XXX XX XX with optional separators.
_PHONE = re.compile(r"(?:\+?998)[ -]?\(?\d{2}\)?[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}(?!\d)")
# Balance/amount with a currency word: "1 250 000 so'm", "500000 сум", "100 UZS".
_BALANCE = re.compile(
    r"(?i)(баланс\w*|balans\w*|qoldiq\w*|қолдиқ\w*|balance)(\s*[:\-–]?\s*)(\d[\d  .,]*\d|\d)"
)


def mask_pii(text: str) -> str:
    if not text:
        return text
    text = _CARD.sub(lambda m: f"{m.group(1)} **** **** {m.group(4)}", text)
    text = _PHONE.sub("+998 ** *** ** **", text)
    text = _PINFL.sub("**************", text)
    text = _BALANCE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return text


def contains_pii(text: str) -> bool:
    return bool(text) and any(p.search(text) for p in (_CARD, _PINFL, _PHONE, _BALANCE))
