"""Hand-offs to human support and what the bot learns from them (SQLite, independent of the channel).

Every escalation is stored with the client's question and the recent conversation; every operator
reply to it is stored too. The bot does not learn on its own: the first operator reply to a hand-off
creates a knowledge-base candidate, and only a candidate a person approved is added to the bot's
knowledge base (see KnowledgeBase.learned). Everything is saved with card numbers, PINFL, phones and
balances masked, and an answer that breaks a hard rule (promised refund or timeline, confirmed
success, unblocking, antifraud criteria...) can never be approved.

A hand-off can be assigned to one operator. A new conversation goes to a free operator first and
waits for them (status "waiting", until `wait_until`); if nobody answers in time the AI takes it
over (status "ai") and the operator is free again, though a later reply still takes it back
(status "operator"). An operator is busy while assigned to a hand-off that is waiting or theirs,
not closed and not idle (see `busy_operators`).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import guardrails
from .llm import Turn
from .models import BotReply
from .pii import mask_pii

_SCHEMA = """
CREATE TABLE IF NOT EXISTS handoffs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    channel            TEXT NOT NULL,
    client_chat_id     TEXT NOT NULL,
    support_chat_id    TEXT NOT NULL,
    support_message_id TEXT NOT NULL,
    language           TEXT NOT NULL,
    topic              TEXT,
    screen_id          TEXT,
    reason             TEXT,
    client_text        TEXT,
    bot_text           TEXT,
    context            TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE (support_chat_id, support_message_id)
);
-- Other posts in the support chat that belong to a hand-off (the client's later messages relayed there):
-- an operator may reply to any of them.
CREATE TABLE IF NOT EXISTS handoff_posts (
    support_chat_id    TEXT NOT NULL,
    support_message_id TEXT NOT NULL,
    handoff_id         INTEGER NOT NULL REFERENCES handoffs (id),
    PRIMARY KEY (support_chat_id, support_message_id)
);
CREATE TABLE IF NOT EXISTS operator_replies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id    INTEGER NOT NULL REFERENCES handoffs (id),
    operator_id   TEXT,
    operator_name TEXT,
    text          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_candidates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id  INTEGER NOT NULL UNIQUE REFERENCES handoffs (id),
    language    TEXT NOT NULL,
    topic       TEXT,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at  TEXT NOT NULL
);
"""

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
# Hand-off states; NULL in databases from before assignment existed (treated as unassigned).
WAITING, WITH_OPERATOR, WITH_AI, CLOSED = "waiting", "operator", "ai", "closed"
# Columns added after the first version; existing databases get them on start.
_ASSIGNMENT_COLUMNS = {
    "status": "TEXT",
    "assigned_id": "TEXT",          # operator's Telegram id
    "assigned_name": "TEXT",
    "wait_until": "TEXT",           # set only while a new conversation waits for its operator
    "last_activity": "TEXT",
    "closed_at": "TEXT",
}


class ReviewError(Exception):
    pass


@dataclass
class Handoff:
    id: int
    channel: str
    client_chat_id: str
    language: str
    topic: str
    client_text: str
    support_chat_id: str = ""
    support_message_id: str = ""
    status: str | None = None
    assigned_id: str | None = None
    assigned_name: str | None = None
    wait_until: str | None = None


@dataclass
class Candidate:
    id: int
    handoff_id: int
    language: str
    topic: str
    question: str
    answer: str
    status: str

    def violations(self) -> list[guardrails.Violation]:
        return guardrails.find_violations(self.answer)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(when: datetime | None) -> str:
    return (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


class HandoffStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(handoffs)")}
            for name, kind in _ASSIGNMENT_COLUMNS.items():
                if name not in cols:
                    self._db.execute(f"ALTER TABLE handoffs ADD COLUMN {name} {kind}")

    def open(self, channel: str, client_chat_id: str, support_chat_id: str, support_message_id: str,
             reply: BotReply, context: list[Turn] = ()) -> int:
        """Save an escalation posted to the support chat; returns its id."""
        turns = [{"role": t.role, "text": mask_pii(t.text)} for t in context]
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO handoffs (channel, client_chat_id, support_chat_id, support_message_id, language, topic,
                                         screen_id, reason, client_text, bot_text, context, created_at, last_activity)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel, client_chat_id, support_chat_id, support_message_id, reply.language.value, reply.topic,
                 reply.screen_id, reply.escalation_reason, mask_pii(reply.client_text), mask_pii(reply.text),
                 json.dumps(turns, ensure_ascii=False), _now(), _now()),
            )
        return cur.lastrowid

    def find(self, support_chat_id: str, support_message_id: str) -> Handoff | None:
        """The hand-off an operator is replying to (its post or a linked one); survives restarts."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM handoffs WHERE support_chat_id = ? AND support_message_id = ?",
                (support_chat_id, support_message_id),
            ).fetchone() or self._db.execute(
                """SELECT h.* FROM handoff_posts p JOIN handoffs h ON h.id = p.handoff_id
                   WHERE p.support_chat_id = ? AND p.support_message_id = ?""",
                (support_chat_id, support_message_id),
            ).fetchone()
        return _handoff(row) if row else None

    def get(self, handoff_id: int) -> Handoff | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        return _handoff(row) if row else None

    # --- assignment: which operator has the hand-off ----------------------------------------------

    def assign(self, handoff_id: int, operator_id: str, operator_name: str, wait_seconds: int | None = None,
               now: datetime | None = None) -> None:
        """Give the hand-off to an operator; with `wait_seconds` the AI takes it over if they stay silent."""
        now = now or datetime.now(timezone.utc)
        until = _iso(now + timedelta(seconds=wait_seconds)) if wait_seconds is not None else None
        with self._lock, self._db:
            self._db.execute(
                """UPDATE handoffs SET status = ?, assigned_id = ?, assigned_name = ?, wait_until = ?,
                     last_activity = ? WHERE id = ?""",
                (WAITING, operator_id, operator_name, until, _iso(now), handoff_id))

    def claim(self, handoff_id: int, operator_id: str | None, operator_name: str | None) -> None:
        """An operator answered: the hand-off is theirs now, even if the AI had taken it over."""
        with self._lock, self._db:
            self._db.execute(
                """UPDATE handoffs SET status = ?, assigned_id = COALESCE(?, assigned_id),
                     assigned_name = COALESCE(?, assigned_name), wait_until = NULL, last_activity = ?,
                     closed_at = NULL WHERE id = ?""",
                (WITH_OPERATOR, operator_id, operator_name, _now(), handoff_id))

    def touch(self, handoff_id: int) -> None:
        """The client wrote in the hand-off: the operator is still busy with it."""
        with self._lock, self._db:
            self._db.execute("UPDATE handoffs SET last_activity = ? WHERE id = ?", (_now(), handoff_id))

    def due(self, now: datetime | None = None) -> list[Handoff]:
        """Hand-offs whose operator did not answer in time."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM handoffs WHERE status = ? AND wait_until IS NOT NULL AND wait_until <= ? ORDER BY id",
                (WAITING, _iso(now))).fetchall()
        return [_handoff(r) for r in rows]

    def to_ai(self, handoff_id: int) -> bool:
        """The AI takes over a hand-off still waiting for its operator; False if the operator got there first."""
        with self._lock, self._db:
            cur = self._db.execute("UPDATE handoffs SET status = ?, wait_until = NULL WHERE id = ? AND status = ?",
                                   (WITH_AI, handoff_id, WAITING))
        return cur.rowcount == 1

    def close_handoff(self, handoff_id: int) -> None:
        """The conversation ended: the hand-off no longer keeps its operator busy."""
        with self._lock, self._db:
            self._db.execute("UPDATE handoffs SET status = ?, wait_until = NULL, closed_at = ? WHERE id = ?",
                             (CLOSED, _now(), handoff_id))

    def busy_operators(self, idle_minutes: int = 30, now: datetime | None = None) -> dict[str, int]:
        """Operator id -> hand-offs they have now. A hand-off nobody wrote in for `idle_minutes` does not count."""
        since = _iso((now or datetime.now(timezone.utc)) - timedelta(minutes=idle_minutes))
        with self._lock:
            rows = self._db.execute(
                """SELECT assigned_id, COUNT(*) AS n FROM handoffs
                   WHERE status IN (?, ?) AND assigned_id IS NOT NULL AND closed_at IS NULL AND last_activity >= ?
                   GROUP BY assigned_id""", (WAITING, WITH_OPERATOR, since)).fetchall()
        return {r["assigned_id"]: r["n"] for r in rows}

    def last_assigned(self) -> dict[str, str]:
        """Operator id -> when they last got a hand-off, so new ones go round the free operators in turn."""
        with self._lock:
            rows = self._db.execute(
                "SELECT assigned_id, MAX(id) AS last FROM handoffs WHERE assigned_id IS NOT NULL GROUP BY assigned_id"
            ).fetchall()
        return {r["assigned_id"]: r["last"] for r in rows}

    def link_post(self, handoff_id: int, support_chat_id: str, support_message_id: str) -> None:
        """Another support-chat post of this hand-off: a reply to it reaches the same client."""
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO handoff_posts VALUES (?, ?, ?)",
                             (support_chat_id, support_message_id, handoff_id))

    def add_operator_reply(self, handoff_id: int, text: str, operator_id: str | None = None,
                           operator_name: str | None = None) -> tuple[Candidate | None, bool]:
        """Save the operator's answer and fold it into the hand-off's candidate.

        Returns the candidate (None if there is no question to learn from) and whether it was just created.
        Replies after a candidate was reviewed are saved but do not change it.
        """
        text = mask_pii(text.strip())
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO operator_replies (handoff_id, operator_id, operator_name, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (handoff_id, operator_id, operator_name, text, _now()),
            )
            h = self._db.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
            existing = self._db.execute("SELECT * FROM kb_candidates WHERE handoff_id = ?", (handoff_id,)).fetchone()
            if existing is not None:
                if existing["status"] == PENDING:
                    self._db.execute("UPDATE kb_candidates SET answer = ? WHERE id = ?",
                                     (f'{existing["answer"]}\n{text}', existing["id"]))
                row = self._db.execute("SELECT * FROM kb_candidates WHERE id = ?", (existing["id"],)).fetchone()
                return _candidate(row), False
            question = _question(h)
            if not question:
                return None, False
            cur = self._db.execute(
                "INSERT INTO kb_candidates (handoff_id, language, topic, question, answer, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (handoff_id, h["language"], h["topic"], question, text, _now()),
            )
            row = self._db.execute("SELECT * FROM kb_candidates WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _candidate(row), True

    def operator_replies(self, handoff_id: int) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT text FROM operator_replies WHERE handoff_id = ? ORDER BY id", (handoff_id,)
            ).fetchall()
        return [r["text"] for r in rows]

    def candidate(self, candidate_id: int) -> Candidate | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM kb_candidates WHERE id = ?", (candidate_id,)).fetchone()
        return _candidate(row) if row else None

    def candidates(self, status: str = PENDING, limit: int = 10) -> list[Candidate]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM kb_candidates WHERE status = ? ORDER BY id LIMIT ?", (status, limit)
            ).fetchall()
        return [_candidate(r) for r in rows]

    def approve(self, candidate_id: int, reviewer: str, answer: str | None = None) -> Candidate:
        """A person checked the answer; `answer` replaces the operator's wording (e.g. without case details)."""
        c = self.candidate(candidate_id)
        if c is None:
            raise ReviewError("not_found")
        if c.status != PENDING:
            raise ReviewError("already_reviewed")
        final = mask_pii(answer.strip()) if answer and answer.strip() else c.answer
        violations = guardrails.find_violations(final)
        if violations:
            raise ReviewError("forbidden: " + ", ".join(v.category for v in violations))
        with self._lock, self._db:
            self._db.execute(
                "UPDATE kb_candidates SET status = ?, answer = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (APPROVED, final, reviewer, _now(), candidate_id),
            )
        return self.candidate(candidate_id)  # type: ignore[return-value]

    def reject(self, candidate_id: int, reviewer: str) -> Candidate:
        c = self.candidate(candidate_id)
        if c is None:
            raise ReviewError("not_found")
        if c.status != PENDING:
            raise ReviewError("already_reviewed")
        with self._lock, self._db:
            self._db.execute(
                "UPDATE kb_candidates SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (REJECTED, reviewer, _now(), candidate_id),
            )
        return self.candidate(candidate_id)  # type: ignore[return-value]

    def learned(self) -> list[dict]:
        """Approved answers for the knowledge base, oldest first (stable for prompt caching)."""
        return [
            {"id": c.id, "language": c.language, "topic": c.topic, "question": c.question, "answer": c.answer}
            for c in self.candidates(APPROVED, limit=-1)
        ]

    def close(self) -> None:
        self._db.close()


def _question(h: sqlite3.Row) -> str:
    """What the client asked: their message, or (for "call an operator") their last message before it."""
    if h["client_text"]:
        return h["client_text"]
    for turn in reversed(json.loads(h["context"])):
        if turn["role"] == "user" and turn["text"].strip():
            return turn["text"]
    return ""


def _handoff(row: sqlite3.Row) -> Handoff:
    return Handoff(row["id"], row["channel"], row["client_chat_id"], row["language"], row["topic"] or "",
                   row["client_text"] or "", row["support_chat_id"], row["support_message_id"], row["status"],
                   row["assigned_id"], row["assigned_name"], row["wait_until"])


def _candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(row["id"], row["handoff_id"], row["language"], row["topic"] or "", row["question"],
                     row["answer"], row["status"])
