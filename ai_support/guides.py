"""Guides for quick questions, written by an admin in the panel (SQLite, independent of the channel).

A guide is the prepared answer to a quick question: a text, numbered solution steps and screenshots.
When a client taps a question that has a guide in their language, the bot sends the guide instead of
asking the model. Questions without a guide keep going through the engine (ai_support/menu.py).

Admins can also add their own questions to a topic of the menu, rename or hide the built-in ones.
A question added in the panel always has a guide; its id starts with "c" (built-in ids never do).

Every text is checked against the forbidden answers of ai_support/guardrails.py before it is saved:
a guide must not promise refunds or timelines, confirm payments, unblock or explain antifraud rules.

Screenshots are stored in the database itself, so the admin panel and the bot (two processes sharing
the file) see the same thing; the bot remembers the Telegram file_id after the first upload.
"""
from __future__ import annotations

import io
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image as PILImage, UnidentifiedImageError

from .guardrails import Violation, find_violations
from .menu import CATEGORIES, QuickQuestion, category, quick_question
from .models import Lang
from .templates import t

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quick_guides (
    qid        TEXT PRIMARY KEY,          -- a built-in question id, or "c<n>" for one added in the panel
    category   TEXT NOT NULL,
    custom     INTEGER NOT NULL DEFAULT 0,
    hidden     INTEGER NOT NULL DEFAULT 0, -- not shown in the menu
    labels     TEXT NOT NULL DEFAULT '{}', -- language -> button text (built-in: overrides the default)
    texts      TEXT NOT NULL DEFAULT '{}', -- language -> explanation
    steps      TEXT NOT NULL DEFAULT '{}', -- language -> [step, ...]
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS quick_guide_images (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    qid          TEXT NOT NULL,
    position     INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    data         BLOB NOT NULL,
    file_id      TEXT,                     -- Telegram file_id once the bot has sent it
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quick_guide_images_qid ON quick_guide_images (qid, position);
"""

# Telegram sends at most 10 photos in one album; a photo is at most 10 MB.
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_STEPS = 15
MAX_LABEL = 120
# A Telegram message is at most 4096 characters; leave room for the signature and the heading.
MAX_TEXT = 3500
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png"}
# A custom question shown to a client whose language it was not written in falls back to these.
FALLBACK = [Lang.RU, Lang.UZ_LATN, Lang.UZ_CYRL, Lang.EN]


class GuideError(Exception):
    def __init__(self, code: str, violations: list[Violation] | None = None, where: list[str] | None = None):
        super().__init__(code)
        self.code = code
        self.violations = violations or []
        self.where = where or []


@dataclass
class GuideImage:
    id: int
    content_type: str
    size: int
    file_id: str | None = None


@dataclass
class Guide:
    qid: str
    category: str
    custom: bool
    hidden: bool = False
    labels: dict[Lang, str] = field(default_factory=dict)
    texts: dict[Lang, str] = field(default_factory=dict)
    steps: dict[Lang, list[str]] = field(default_factory=dict)
    images: list[GuideImage] = field(default_factory=list)
    updated_at: str | None = None
    updated_by: str | None = None

    def has_answer(self, lang: Lang) -> bool:
        return bool(self.texts.get(lang) or self.steps.get(lang))

    def answer_lang(self, lang: Lang) -> Lang | None:
        """The language to answer a client in: theirs, or for a custom question any language it has."""
        if self.has_answer(lang):
            return lang
        if self.custom:
            return next((l for l in FALLBACK if self.has_answer(l)), None)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _by_lang(raw: str | None) -> dict[Lang, object]:
    out = {}
    for key, value in json.loads(raw or "{}").items():
        try:
            out[Lang(key)] = value
        except ValueError:
            continue
    return out


def _clean(values, kind: str) -> dict[Lang, object]:
    """Panel input -> {Lang: text} (or {Lang: [steps]}); empty languages are dropped."""
    out = {}
    for key, value in (values or {}).items():
        try:
            lang = Lang(key)
        except ValueError:
            raise GuideError("bad_language") from None
        if kind == "steps":
            if not isinstance(value, list):
                raise GuideError("bad_steps")
            steps = [str(s).strip() for s in value if str(s).strip()]
            if len(steps) > MAX_STEPS:
                raise GuideError("too_many_steps")
            if steps:
                out[lang] = steps
        else:
            text = str(value or "").strip()
            if len(text) > (MAX_LABEL if kind == "labels" else MAX_TEXT):
                raise GuideError(f"too_long_{kind}")
            if text:
                out[lang] = text
    return out


def check_texts(labels: dict, texts: dict, steps: dict) -> None:
    """Raise GuideError('forbidden') if anything an admin wrote is something the bot must never say."""
    found, where = [], []
    parts = [(f"labels.{l.value}", v) for l, v in labels.items()] + [(f"texts.{l.value}", v) for l, v in texts.items()]
    parts += [(f"steps.{l.value}.{i + 1}", s) for l, v in steps.items() for i, s in enumerate(v)]
    for place, text in parts:
        for v in find_violations(text):
            found.append(v)
            where.append(place)
    if found:
        raise GuideError("forbidden", found, where)


def check_image(data: bytes) -> str:
    """The content type of an uploaded screenshot, or GuideError if it is not one Telegram can send."""
    if not data:
        raise GuideError("empty_image")
    if len(data) > MAX_IMAGE_BYTES:
        raise GuideError("image_too_large")
    try:
        img = PILImage.open(io.BytesIO(data))
        img.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise GuideError("bad_image") from None
    if img.format not in IMAGE_TYPES:
        raise GuideError("bad_image")
    return IMAGE_TYPES[img.format]


def guide_text(guide: Guide, lang: Lang) -> str:
    """What the client reads: the explanation, then the numbered steps."""
    parts = []
    if guide.texts.get(lang):
        parts.append(guide.texts[lang])
    steps = guide.steps.get(lang) or []
    if steps:
        parts.append(t("guide_steps", lang) + "\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
    return "\n\n".join(parts)


class GuideStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    # --- reading ---------------------------------------------------------------------------------

    def _images(self, qid: str) -> list[GuideImage]:
        rows = self._db.execute(
            "SELECT id, content_type, LENGTH(data) AS size, file_id FROM quick_guide_images "
            "WHERE qid = ? ORDER BY position, id", (qid,)).fetchall()
        return [GuideImage(r["id"], r["content_type"], r["size"], r["file_id"]) for r in rows]

    def _guide(self, row: sqlite3.Row) -> Guide:
        return Guide(row["qid"], row["category"], bool(row["custom"]), bool(row["hidden"]),
                     _by_lang(row["labels"]), _by_lang(row["texts"]), _by_lang(row["steps"]),
                     self._images(row["qid"]), row["updated_at"], row["updated_by"])

    def get(self, qid: str) -> Guide | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM quick_guides WHERE qid = ?", (qid,)).fetchone()
            return self._guide(row) if row else None

    def all(self) -> dict[str, Guide]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM quick_guides ORDER BY created_at, qid").fetchall()
            return {r["qid"]: self._guide(r) for r in rows}

    def image(self, image_id: int) -> tuple[str, bytes, str | None, str] | None:
        """(qid, bytes, file_id, content type) of a screenshot."""
        with self._lock:
            r = self._db.execute("SELECT qid, data, file_id, content_type FROM quick_guide_images WHERE id = ?",
                                 (image_id,)).fetchone()
        return (r["qid"], bytes(r["data"]), r["file_id"], r["content_type"]) if r else None

    # --- the client's menu -----------------------------------------------------------------------

    def menu(self, cid: str, lang: Lang) -> list[tuple[str, str]]:
        """(question id, button text) of a topic in the client's language: built-in, then added ones."""
        cat = category(cid)
        if cat is None:
            return []
        guides = self.all()
        out = []
        for q in cat.questions:
            g = guides.get(q.id)
            if g is not None and g.hidden:
                continue
            out.append((q.id, (g.labels.get(lang) if g else None) or q.label[lang]))
        for g in guides.values():
            if g.custom and g.category == cid and not g.hidden:
                label = g.labels.get(lang) or next((g.labels[l] for l in FALLBACK if g.labels.get(l)), "")
                if label:
                    out.append((g.qid, label))
        return out

    def question(self, qid: str) -> QuickQuestion | None:
        """A tapped question: built-in, or one added in the panel (its labels are the question)."""
        q = quick_question(qid)
        if q is not None:
            return q
        g = self.get(qid)
        if g is None or not g.custom:
            return None
        fallback = next((g.labels[l] for l in FALLBACK if g.labels.get(l)), qid)
        label = {lang: g.labels.get(lang) or fallback for lang in Lang}
        return QuickQuestion(qid, label, label)

    def answer(self, qid: str, lang: Lang) -> tuple[Guide, Lang] | None:
        """The guide to send for a question and the language to send it in, or None to ask the model."""
        g = self.get(qid)
        if g is None:
            return None
        answer_lang = g.answer_lang(lang)
        return (g, answer_lang) if answer_lang is not None else None

    # --- writing (admin panel) -------------------------------------------------------------------

    def save(self, qid: str | None, cid: str, labels=None, texts=None, steps=None, hidden: bool = False,
             by: str | None = None) -> Guide:
        """Create a question (qid None) or update a guide. Texts with forbidden phrases are refused."""
        if category(cid) is None:
            raise GuideError("bad_category")
        labels, texts, steps = _clean(labels, "labels"), _clean(texts, "texts"), _clean(steps, "steps")
        check_texts(labels, texts, steps)
        builtin = quick_question(qid) if qid else None
        if builtin is not None:
            cid = next(c.id for c in CATEGORIES if builtin in c.questions)  # a built-in stays in its topic
        else:
            if not labels:
                raise GuideError("no_label")
            if not texts and not steps:
                raise GuideError("no_answer")
        now = _now()
        with self._lock, self._db:
            if qid is None:
                last = self._db.execute(
                    "SELECT MAX(CAST(SUBSTR(qid, 2) AS INTEGER)) FROM quick_guides WHERE custom = 1").fetchone()[0]
                qid = f"c{(last or 0) + 1}"
            elif builtin is None and not self._db.execute(
                    "SELECT 1 FROM quick_guides WHERE qid = ? AND custom = 1", (qid,)).fetchone():
                raise GuideError("not_found")
            dump = lambda d: json.dumps({k.value: v for k, v in d.items()}, ensure_ascii=False)  # noqa: E731
            self._db.execute(
                """INSERT INTO quick_guides (qid, category, custom, hidden, labels, texts, steps, created_at,
                                             updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (qid) DO UPDATE SET category = excluded.category, hidden = excluded.hidden,
                     labels = excluded.labels, texts = excluded.texts, steps = excluded.steps,
                     updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
                (qid, cid, int(builtin is None), int(hidden), dump(labels), dump(texts), dump(steps), now, now, by))
        return self.get(qid)  # type: ignore[return-value]

    def delete(self, qid: str) -> None:
        """Remove a guide: a built-in question goes back to the model's answer, an added one disappears."""
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM quick_guides WHERE qid = ?", (qid,))
            self._db.execute("DELETE FROM quick_guide_images WHERE qid = ?", (qid,))
        if not cur.rowcount:
            raise GuideError("not_found")

    def add_image(self, qid: str, data: bytes) -> GuideImage:
        ctype = check_image(data)
        with self._lock, self._db:
            if not self._db.execute("SELECT 1 FROM quick_guides WHERE qid = ?", (qid,)).fetchone():
                builtin = quick_question(qid)
                if builtin is None:
                    raise GuideError("not_found")
                # Screenshots before any text: the built-in question gets an empty guide to hold them.
                cid = next(c.id for c in CATEGORIES if builtin in c.questions)
                self._db.execute("INSERT INTO quick_guides (qid, category, created_at, updated_at) VALUES (?, ?, ?, ?)",
                                 (qid, cid, _now(), _now()))
            count, last = self._db.execute(
                "SELECT COUNT(*), COALESCE(MAX(position), 0) FROM quick_guide_images WHERE qid = ?", (qid,)).fetchone()
            if count >= MAX_IMAGES:
                raise GuideError("too_many_images")
            cur = self._db.execute(
                "INSERT INTO quick_guide_images (qid, position, content_type, data, created_at) VALUES (?, ?, ?, ?, ?)",
                (qid, last + 1, ctype, data, _now()))
        return GuideImage(cur.lastrowid, ctype, len(data))

    def delete_image(self, qid: str, image_id: int) -> None:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM quick_guide_images WHERE id = ? AND qid = ?", (image_id, qid))
        if not cur.rowcount:
            raise GuideError("not_found")

    def move_image(self, qid: str, image_id: int, delta: int) -> None:
        """Move a screenshot one place earlier (-1) or later (+1) in the album."""
        with self._lock, self._db:
            ids = [r[0] for r in self._db.execute(
                "SELECT id FROM quick_guide_images WHERE qid = ? ORDER BY position, id", (qid,))]
            if image_id not in ids:
                raise GuideError("not_found")
            i = ids.index(image_id)
            j = max(0, min(len(ids) - 1, i + delta))
            ids[i], ids[j] = ids[j], ids[i]
            for pos, iid in enumerate(ids, 1):
                self._db.execute("UPDATE quick_guide_images SET position = ? WHERE id = ?", (pos, iid))

    def remember_file_id(self, image_id: int, file_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE quick_guide_images SET file_id = ? WHERE id = ?", (file_id, image_id))
