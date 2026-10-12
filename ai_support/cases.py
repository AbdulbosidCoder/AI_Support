"""Support cases written by an admin in the panel (SQLite, independent of the channel).

A case is an instruction for the bot, in the same shape as the call-center cases the team gave us
(«Колл-центр кейсы.pdf»): a topic of the support scope, the client's situation, and what to do about
it: the answer steps, what must not be said, and when to hand the conversation to a person.
Unlike a quick-question guide (ai_support/guides.py), a case is not sent to the client as it is: it goes
into the model's knowledge base, and the model answers by it whenever a client describes that situation.

The admin panel and the client bot are two processes sharing the database file: every change bumps
a version number, and the bot rebuilds its knowledge base when it sees a new one (see version()).

Every text the bot may say is checked against the forbidden answers of ai_support/guardrails.py before
it is saved. A case about antifraud always goes to a person, and its causes are never explained.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .guardrails import Violation, find_violations

_SCHEMA = """
CREATE TABLE IF NOT EXISTS support_cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    section     TEXT NOT NULL,            -- a topic of the support scope, see SECTIONS
    title       TEXT NOT NULL,            -- the client's situation
    steps       TEXT NOT NULL DEFAULT '[]', -- what the bot does / answers, in order
    forbidden   TEXT NOT NULL DEFAULT '[]', -- what the bot must not say or do in this case
    escalate    TEXT NOT NULL DEFAULT 'if', -- 'never' | 'if' | 'always'
    escalate_if TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS support_cases_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO support_cases_version (id, version) VALUES (1, 0);
"""

# The topics of the support scope, as in the knowledge base (faq.json "section").
SECTIONS: dict[str, str] = {
    "registration": "Регистрация и вход",
    "cards": "Карты",
    "payments": "Платежи",
    "transfers": "P2P-переводы",
    "apartment": "Оплата квартирной задолженности",
    "antifraud": "Antifraud и ограничения безопасности",
    "technical": "Технические проблемы",
    "profile": "Профиль и настройки",
}
ESCALATE = ("never", "if", "always")
MAX_TITLE = 300
MAX_STEP = 1000
MAX_STEPS = 15
MAX_FORBIDDEN = 10
# Said about every antifraud case, whatever the admin wrote: the causes stay internal.
ANTIFRAUD_FORBIDDEN = "Не называть причины, правила, критерии и лимиты системы безопасности."


class CaseError(Exception):
    def __init__(self, code: str, violations: list[Violation] | None = None, where: list[str] | None = None):
        super().__init__(code)
        self.code = code
        self.violations = violations or []
        self.where = where or []


@dataclass
class Case:
    id: int
    section: str
    title: str
    steps: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    escalate: str = "if"
    escalate_if: str = ""
    active: bool = True
    created_at: str = ""
    updated_at: str = ""
    updated_by: str | None = None

    @property
    def topic(self) -> str:
        """The case's topic id, as the model names it in its answers."""
        return f"{self.section}/case{self.id}"

    def as_topic(self) -> dict:
        """The case in the shape of a knowledge base topic (faq.json)."""
        forbidden = list(self.forbidden)
        if self.section == "antifraud" and ANTIFRAUD_FORBIDDEN not in forbidden:
            forbidden.insert(0, ANTIFRAUD_FORBIDDEN)
        escalate_if = {"always": "Всегда.", "never": ""}.get(self.escalate, self.escalate_if)
        if self.escalate == "always" and self.escalate_if:
            escalate_if = f"Всегда. {self.escalate_if}"
        return {"section": self.section, "id": f"case{self.id}", "title": self.title, "answer_steps": list(self.steps),
                "forbidden": forbidden, "escalate_if": escalate_if, "source": "admin_panel"}

    def to_dict(self) -> dict:
        return {"id": self.id, "topic": self.topic, "section": self.section, "section_label": SECTIONS[self.section],
                "title": self.title, "steps": self.steps, "forbidden": self.forbidden, "escalate": self.escalate,
                "escalate_if": self.escalate_if, "active": self.active, "created_at": self.created_at,
                "updated_at": self.updated_at, "updated_by": self.updated_by}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, limit: int, code: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise CaseError(code)
    return text


def _list(values, limit: int, code: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise CaseError("bad_list")
    items = [_text(v, MAX_STEP, "too_long_step") for v in values]
    items = [x for x in items if x]
    if len(items) > limit:
        raise CaseError(code)
    return items


def check_texts(title: str, steps: list[str], escalate_if: str) -> None:
    """Raise CaseError('forbidden') if anything the bot would say by this case breaks a hard rule.

    The "must not" list is not checked: it names exactly the things the bot must not say.
    """
    found, where = [], []
    parts = [("title", title), ("escalate_if", escalate_if)] + [(f"steps.{i + 1}", s) for i, s in enumerate(steps)]
    for place, text in parts:
        for v in find_violations(text):
            found.append(v)
            where.append(place)
    if found:
        raise CaseError("forbidden", found, where)


class CaseStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    @staticmethod
    def _case(r: sqlite3.Row) -> Case:
        return Case(r["id"], r["section"], r["title"], json.loads(r["steps"] or "[]"), json.loads(r["forbidden"] or "[]"),
                    r["escalate"], r["escalate_if"], bool(r["active"]), r["created_at"], r["updated_at"], r["updated_by"])

    def version(self) -> int:
        """Grows with every change: the bot compares it to rebuild its knowledge base."""
        with self._lock:
            return self._db.execute("SELECT version FROM support_cases_version WHERE id = 1").fetchone()[0]

    def get(self, case_id: int) -> Case | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM support_cases WHERE id = ?", (case_id,)).fetchone()
        return self._case(r) if r else None

    def all(self) -> list[Case]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM support_cases ORDER BY id").fetchall()
        return [self._case(r) for r in rows]

    def topics(self) -> list[dict]:
        """The active cases as knowledge base topics, for the model's prompt."""
        return [c.as_topic() for c in self.all() if c.active and c.section in SECTIONS]

    def save(self, case_id: int | None, section: str, title: str, steps=None, forbidden=None, escalate: str = "if",
             escalate_if: str = "", active: bool = True, by: str | None = None) -> Case:
        """Create a case (case_id None) or update one. Texts with forbidden phrases are refused."""
        if section not in SECTIONS:
            raise CaseError("bad_section")
        if escalate not in ESCALATE:
            raise CaseError("bad_escalate")
        title = _text(title, MAX_TITLE, "too_long_title")
        steps = _list(steps, MAX_STEPS, "too_many_steps")
        forbidden = _list(forbidden, MAX_FORBIDDEN, "too_many_forbidden")
        escalate_if = _text(escalate_if, MAX_STEP, "too_long_step")
        if not title:
            raise CaseError("no_title")
        if not steps:
            raise CaseError("no_steps")
        if section == "antifraud":
            escalate = "always"  # antifraud restrictions are checked by a person (hard rule)
        if escalate == "if" and not escalate_if:
            raise CaseError("no_escalate_if")
        check_texts(title, steps, escalate_if)
        now = _now()
        dump = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731
        with self._lock, self._db:
            if case_id is None:
                cur = self._db.execute(
                    """INSERT INTO support_cases (section, title, steps, forbidden, escalate, escalate_if, active,
                                                  created_at, updated_at, updated_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (section, title, dump(steps), dump(forbidden), escalate, escalate_if, int(active), now, now, by))
                case_id = cur.lastrowid
            else:
                cur = self._db.execute(
                    """UPDATE support_cases SET section = ?, title = ?, steps = ?, forbidden = ?, escalate = ?,
                         escalate_if = ?, active = ?, updated_at = ?, updated_by = ? WHERE id = ?""",
                    (section, title, dump(steps), dump(forbidden), escalate, escalate_if, int(active), now, by, case_id))
                if not cur.rowcount:
                    raise CaseError("not_found")
            self._bump()
        return self.get(case_id)  # type: ignore[return-value]

    def delete(self, case_id: int) -> None:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM support_cases WHERE id = ?", (case_id,))
            if not cur.rowcount:
                raise CaseError("not_found")
            self._bump()

    def _bump(self) -> None:
        self._db.execute("UPDATE support_cases_version SET version = version + 1 WHERE id = 1")

    def close(self) -> None:
        self._db.close()
