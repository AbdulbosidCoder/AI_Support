"""Support operators added by an admin (SQLite, independent of the channel).

Admins add operators in the admin bot or the admin panel. While the list is empty, anyone in the
support chat can answer clients (as before). Once at least one operator is active, only active
operators' replies reach clients; the bot tells anyone else to ask an admin to add them.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operators (
    user_id    TEXT PRIMARY KEY,          -- Telegram user id
    name       TEXT NOT NULL,
    username   TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    added_by   TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class OperatorError(Exception):
    pass


@dataclass
class Operator:
    user_id: str
    name: str
    username: str | None
    active: bool
    added_by: str | None
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OperatorStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(_SCHEMA)

    def add(self, user_id: str | int, name: str, username: str | None = None, added_by: str | None = None) -> Operator:
        """Add an operator, or turn a removed one back on (name and username are refreshed)."""
        uid = str(user_id).strip()
        if not uid.lstrip("-").isdigit():
            raise OperatorError("bad_id")
        username = (username or "").strip().lstrip("@") or None
        name = (name or "").strip() or (f"@{username}" if username else uid)
        now = _now()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO operators (user_id, name, username, active, added_by, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT (user_id) DO UPDATE SET name = excluded.name,
                     username = COALESCE(excluded.username, operators.username), active = 1,
                     updated_at = excluded.updated_at""",
                (uid, name, username, added_by, now, now),
            )
        return self.get(uid)  # type: ignore[return-value]

    def set_active(self, user_id: str | int, active: bool) -> Operator:
        uid = str(user_id)
        with self._lock, self._db:
            cur = self._db.execute("UPDATE operators SET active = ?, updated_at = ? WHERE user_id = ?",
                                   (int(active), _now(), uid))
        if not cur.rowcount:
            raise OperatorError("not_found")
        return self.get(uid)  # type: ignore[return-value]

    def get(self, user_id: str | int) -> Operator | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM operators WHERE user_id = ?", (str(user_id),)).fetchone()
        return _operator(row) if row else None

    def all(self) -> list[Operator]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM operators ORDER BY active DESC, created_at").fetchall()
        return [_operator(r) for r in rows]

    def may_answer(self, user_id: str | int | None) -> bool:
        """Whether this person's reply in the support chat goes to the client."""
        with self._lock:
            any_active = self._db.execute("SELECT 1 FROM operators WHERE active = 1 LIMIT 1").fetchone()
            if any_active is None:
                return True  # no list yet: everyone in the support chat answers, as before
            if user_id is None:
                return False
            row = self._db.execute("SELECT 1 FROM operators WHERE user_id = ? AND active = 1",
                                   (str(user_id),)).fetchone()
        return row is not None

    def close(self) -> None:
        self._db.close()


def _operator(row: sqlite3.Row) -> Operator:
    return Operator(row["user_id"], row["name"], row["username"], bool(row["active"]), row["added_by"],
                    row["created_at"])
