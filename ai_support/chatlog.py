"""Every message of a client's support chat (SQLite), independent of the channel.

The client's messages, the bot's answers, the operator's replies and system events (hand-off,
end of the conversation) are saved in order, so support staff can later read a session as a
chat (admin web page). A session is a `conversations` row (ai_support/feedback.py); messages
sent outside a conversation (greetings, registration) have no conversation_id.
Client text is PII-masked, like everything else the bot keeps.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .pii import mask_pii

CLIENT, BOT, OPERATOR, SYSTEM = "client", "bot", "operator", "system"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel         TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    conversation_id INTEGER,                -- conversations.id; NULL outside a conversation
    handoff_id      INTEGER,                -- handoffs.id once the conversation reached a person
    sender          TEXT NOT NULL,          -- 'client' | 'bot' | 'operator' | 'system'
    operator_id     TEXT,
    operator_name   TEXT,
    kind            TEXT NOT NULL DEFAULT 'text',  -- 'text' | 'photo' | 'voice' | 'audio' | 'document'
    text            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_client ON chat_messages (channel, client_id, id);
CREATE INDEX IF NOT EXISTS chat_messages_conversation ON chat_messages (conversation_id, id);
"""


@dataclass
class ChatMessage:
    id: int
    channel: str
    client_id: str
    conversation_id: int | None
    handoff_id: int | None
    sender: str
    operator_id: str | None
    operator_name: str | None
    kind: str
    text: str
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

    def add(self, channel: str, client_id: str, sender: str, text: str, kind: str = "text",
            conversation_id: int | None = None, handoff_id: int | None = None,
            operator_id: str | None = None, operator_name: str | None = None) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO chat_messages (channel, client_id, conversation_id, handoff_id, sender, operator_id,
                                              operator_name, kind, text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel, client_id, conversation_id, handoff_id, sender, operator_id, operator_name, kind,
                 mask_pii(text or ""), _now()),
            )
        return cur.lastrowid

    def for_conversation(self, conversation_id: int) -> list[ChatMessage]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id",
                                    (conversation_id,)).fetchall()
        return [ChatMessage(**dict(r)) for r in rows]

    def for_client(self, channel: str, client_id: str, limit: int = 200) -> list[ChatMessage]:
        """The client's latest messages, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM chat_messages WHERE channel = ? AND client_id = ? ORDER BY id DESC LIMIT ?",
                (channel, client_id, limit)).fetchall()
        return [ChatMessage(**dict(r)) for r in reversed(rows)]

    def close(self) -> None:
        self._db.close()
