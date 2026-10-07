"""Hand-offs to human support and what the bot learns from them (SQLite, independent of the channel).

Every escalation is stored with the client's question and the recent conversation; every operator
reply to it is stored too. The bot does not learn on its own: the first operator reply to a hand-off
creates a knowledge-base candidate, and only a candidate a person approved is added to the bot's
knowledge base (see KnowledgeBase.learned). Everything is saved with card numbers, PINFL, phones and
balances masked, and an answer that breaks a hard rule (promised refund or timeline, confirmed
success, unblocking, antifraud criteria...) can never be approved.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
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


class HandoffStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def open(self, channel: str, client_chat_id: str, support_chat_id: str, support_message_id: str,
             reply: BotReply, context: list[Turn] = ()) -> int:
        """Save an escalation posted to the support chat; returns its id."""
        turns = [{"role": t.role, "text": mask_pii(t.text)} for t in context]
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO handoffs (channel, client_chat_id, support_chat_id, support_message_id, language, topic,
                                         screen_id, reason, client_text, bot_text, context, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel, client_chat_id, support_chat_id, support_message_id, reply.language.value, reply.topic,
                 reply.screen_id, reply.escalation_reason, mask_pii(reply.client_text), mask_pii(reply.text),
                 json.dumps(turns, ensure_ascii=False), _now()),
            )
        return cur.lastrowid

    def find(self, support_chat_id: str, support_message_id: str) -> Handoff | None:
        """The hand-off an operator is replying to; survives restarts."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM handoffs WHERE support_chat_id = ? AND support_message_id = ?",
                (support_chat_id, support_message_id),
            ).fetchone()
        return _handoff(row) if row else None

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
                   row["client_text"] or "")


def _candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(row["id"], row["handoff_id"], row["language"], row["topic"] or "", row["question"],
                     row["answer"], row["status"])
