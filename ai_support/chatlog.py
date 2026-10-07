"""Every message of every client session, for the admin panel (SQLite, independent of the channel).

The client bot logs what the client wrote, what the bot answered, what an operator replied, and
events such as a hand-off or the end of a conversation. A session is a conversation from
ai_support/feedback.py: its messages are the client's messages after the previous conversation
ended, up to the moment this one ended (or now, if it is still open). Card numbers, PINFL, phones
and balances are masked before saving, like everywhere else.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .pii import mask_pii

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    sender        TEXT NOT NULL,          -- 'client' | 'bot' | 'operator' | 'event'
    kind          TEXT NOT NULL DEFAULT 'text',   -- 'text' | 'photo' | 'voice' | 'document'
    text          TEXT NOT NULL DEFAULT '',
    operator_id   TEXT,
    operator_name TEXT,
    handoff_id    INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_client ON chat_messages (channel, client_id, created_at);
"""

CLIENT, BOT, OPERATOR, EVENT = "client", "bot", "operator", "event"


@dataclass
class ChatMessage:
    id: int
    sender: str
    kind: str
    text: str
    operator_name: str | None
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatLog:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def add(self, channel: str, client_id: str, sender: str, text: str = "", kind: str = "text",
            operator_id: str | None = None, operator_name: str | None = None, handoff_id: int | None = None) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO chat_messages (channel, client_id, sender, kind, text, operator_id, operator_name,
                                             handoff_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel, client_id, sender, kind, mask_pii((text or "").strip()), operator_id, operator_name,
                 handoff_id, _now()),
            )
        return cur.lastrowid

    def messages(self, channel: str, client_id: str, after: str | None = None, until: str | None = None,
                 limit: int = 500) -> list[ChatMessage]:
        """The client's messages in (after, until], oldest first."""
        where, args = ["channel = ?", "client_id = ?"], [channel, client_id]
        if after:
            where.append("created_at > ?")
            args.append(after)
        if until:
            where.append("created_at <= ?")
            args.append(until)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM chat_messages WHERE {' AND '.join(where)} ORDER BY id LIMIT ?", (*args, limit)
            ).fetchall()
        return [ChatMessage(r["id"], r["sender"], r["kind"], r["text"], r["operator_name"], r["created_at"])
                for r in rows]

    def close(self) -> None:
        self._db.close()
