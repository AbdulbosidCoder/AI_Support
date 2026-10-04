"""Проверки базы знаний экранов: целостность и запрещённые ответы."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB = json.loads((ROOT / "data/knowledge_base/screens.json").read_text(encoding="utf-8"))

# Фразы, которые бот не должен произносить: обещания возврата/разблокировки/сроков,
# подтверждение успешности операции.
FORBIDDEN = [
    r"деньги вернутся", r"верн[её]м", r"возврат подтвержд", r"разблокиру[ею]м",
    r"сним[еу]м блок", r"операция (прошла )?успешн", r"в течение \d+ (час|минут)",
    r"pul(ingiz)? qaytadi", r"qaytarib beramiz", r"blokdan chiqaramiz", r"\d+ soat ichida",
]


def answer_texts(screen):
    yield screen.get("explanation_ru", "")
    yield screen.get("answer_uz", "")
    yield from screen.get("fix_steps_ru", [])


def test_images_exist():
    for s in KB["screens"]:
        assert (ROOT / s["image"]).is_file(), s["image"]
        for v in s.get("variants", []):
            assert (ROOT / v["image"]).is_file(), v["image"]


def test_ids_unique_and_required_fields():
    ids = [s["id"] for s in KB["screens"]]
    assert len(ids) == len(set(ids))
    for s in KB["screens"]:
        for key in ("section", "page_id", "state", "problem", "explanation_ru", "fix_steps_ru"):
            assert s.get(key), (s["id"], key)
        assert s["section"] in KB["sections"]
        if s["state"] == "error":
            assert s.get("error_text"), s["id"]


def test_no_forbidden_promises():
    for s in KB["screens"]:
        for text in answer_texts(s):
            for pat in FORBIDDEN:
                assert not re.search(pat, text, re.IGNORECASE), (s["id"], pat, text)


def test_no_unmasked_pii():
    phone = re.compile(r"\+?998[\s-]?\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")
    card = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")
    raw = json.dumps(KB, ensure_ascii=False)
    assert not phone.search(raw.replace("+998 00 000-00-00", ""))
    assert not card.search(raw.replace("0000 0000 0000 0000", ""))


def test_antifraud_screen_escalates_without_criteria():
    s = next(s for s in KB["screens"] if "antifraud" in s.get("related_scope", []))
    assert s["escalate"] is True
    assert any("Antifraud" in f for f in s.get("forbidden", []))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
