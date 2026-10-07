"""Support operators added by an admin (SQLite, independent of the channel).

There can be any number of operators. An admin adds one in the admin bot or the admin panel, usually
by phone number: the operator then opens the client bot, presses /start and registers by sharing their
phone, and the bot links that phone to their Telegram id (see `link`). An admin can also add an
operator directly by Telegram id.

While no linked operator is active, anyone in the support chat can answer clients (as before). Once at
least one is, only active linked operators' replies reach clients; the bot tells anyone else to ask an
admin to add them. An operator added by phone who has not registered yet changes nothing.

An operator is addressed by a key: the Telegram id once linked, otherwise the phone number (+digits).
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .users import normalize_phone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operators (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT UNIQUE,               -- Telegram user id; NULL until an operator added by phone registers
    phone      TEXT UNIQUE,               -- +digits, as the client bot saves it on registration
    name       TEXT NOT NULL,
    username   TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    added_by   TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    linked_at  TEXT
)
"""

# Phones are at least this many digits (Uzbekistan: 998 + 9); shorter input is a typo.
MIN_PHONE_DIGITS = 9


class OperatorError(Exception):
    pass


@dataclass
class Operator:
    user_id: str | None
    name: str
    username: str | None
    active: bool
    added_by: str | None
    created_at: str
    phone: str | None = None
    linked_at: str | None = None

    @property
    def key(self) -> str:
        return self.user_id or self.phone or ""

    @property
    def linked(self) -> bool:
        return self.user_id is not None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_phone(value: str) -> bool:
    """'+998 90 123-45-67' or '998901234567' is a phone; a Telegram id is shorter digits without '+'."""
    value = (value or "").strip()
    digits = "".join(ch for ch in value if ch.isdigit())
    if value.startswith("+"):
        return len(digits) >= MIN_PHONE_DIGITS
    return digits == value.replace(" ", "") and digits.startswith("998") and len(digits) == 12


class OperatorStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._migrate()
            self._db.execute(_SCHEMA)

    def _migrate(self) -> None:
        """The first version keyed operators by Telegram id only; move them into the new table."""
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(operators)")}
        if not cols or "phone" in cols:
            return
        self._db.execute("ALTER TABLE operators RENAME TO operators_v1")
        self._db.execute(_SCHEMA)
        self._db.execute(
            """INSERT INTO operators (user_id, name, username, active, added_by, created_at, updated_at, linked_at)
               SELECT user_id, name, username, active, added_by, created_at, updated_at, created_at FROM operators_v1""")
        self._db.execute("DROP TABLE operators_v1")

    def add(self, user_id: str | int, name: str, username: str | None = None, added_by: str | None = None) -> Operator:
        """Add an operator by Telegram id, or turn a removed one back on (name and username are refreshed)."""
        uid = str(user_id).strip()
        if not uid.isdigit():
            raise OperatorError("bad_id")
        username = (username or "").strip().lstrip("@") or None
        name = (name or "").strip() or (f"@{username}" if username else uid)
        now = _now()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO operators (user_id, name, username, active, added_by, created_at, updated_at, linked_at)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                   ON CONFLICT (user_id) DO UPDATE SET name = excluded.name,
                     username = COALESCE(excluded.username, operators.username), active = 1,
                     updated_at = excluded.updated_at""",
                (uid, name, username, added_by, now, now, now),
            )
        return self.get(uid)  # type: ignore[return-value]

    def add_phone(self, phone: str, name: str = "", added_by: str | None = None) -> Operator:
        """Add an operator by phone number; they become active for clients once they register in the bot."""
        number = normalize_phone(phone or "")
        if len(number) - 1 < MIN_PHONE_DIGITS:
            raise OperatorError("bad_phone")
        name = (name or "").strip() or number
        now = _now()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO operators (phone, name, active, added_by, created_at, updated_at)
                   VALUES (?, ?, 1, ?, ?, ?)
                   ON CONFLICT (phone) DO UPDATE SET name = excluded.name, active = 1,
                     updated_at = excluded.updated_at""",
                (number, name, added_by, now, now),
            )
        return self.get(number)  # type: ignore[return-value]

    def link(self, phone: str, user_id: str | int, username: str | None = None,
             full_name: str | None = None) -> Operator | None:
        """A person registered with this phone: if an admin added it, that operator is now this Telegram id.

        Returns the operator if this registration made someone an operator (or refreshed a link), else None.
        """
        number, uid = normalize_phone(phone or ""), str(user_id)
        if not number:
            return None
        now = _now()
        with self._lock, self._db:
            pending = self._db.execute("SELECT * FROM operators WHERE phone = ?", (number,)).fetchone()
            if pending is None or (pending["user_id"] and pending["user_id"] != uid):
                return None
            by_id = self._db.execute("SELECT * FROM operators WHERE user_id = ?", (uid,)).fetchone()
            if by_id is not None and by_id["id"] != pending["id"]:
                # Already added by Telegram id as well: keep that row and give it the phone.
                self._db.execute("DELETE FROM operators WHERE id = ?", (pending["id"],))
                self._db.execute("UPDATE operators SET phone = ?, active = MAX(active, ?), updated_at = ? WHERE id = ?",
                                 (number, pending["active"], now, by_id["id"]))
            else:
                self._db.execute(
                    """UPDATE operators SET user_id = ?, username = COALESCE(?, username),
                         name = CASE WHEN name = phone AND ? IS NOT NULL THEN ? ELSE name END,
                         linked_at = COALESCE(linked_at, ?), updated_at = ? WHERE id = ?""",
                    (uid, (username or "").lstrip("@") or None, full_name, full_name, now, now, pending["id"]))
        return self.get(uid)

    def set_active(self, key: str | int, active: bool) -> Operator:
        """Turn an operator off or on, by Telegram id or (not yet registered) phone."""
        column, value = self._where(key)
        with self._lock, self._db:
            cur = self._db.execute(f"UPDATE operators SET active = ?, updated_at = ? WHERE {column} = ?",
                                   (int(active), _now(), value))
        if not cur.rowcount:
            raise OperatorError("not_found")
        return self.get(key)  # type: ignore[return-value]

    def get(self, key: str | int) -> Operator | None:
        column, value = self._where(key)
        with self._lock:
            row = self._db.execute(f"SELECT * FROM operators WHERE {column} = ?", (value,)).fetchone()
        return _operator(row) if row else None

    @staticmethod
    def _where(key: str | int) -> tuple[str, str]:
        key = str(key).strip()
        return ("phone", normalize_phone(key)) if key.startswith("+") else ("user_id", key)

    def all(self) -> list[Operator]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM operators ORDER BY active DESC, created_at").fetchall()
        return [_operator(r) for r in rows]

    def may_answer(self, user_id: str | int | None) -> bool:
        """Whether this person's reply in the support chat goes to the client."""
        with self._lock:
            any_active = self._db.execute(
                "SELECT 1 FROM operators WHERE active = 1 AND user_id IS NOT NULL LIMIT 1").fetchone()
            if any_active is None:
                return True  # no linked operator yet: everyone in the support chat answers, as before
            if user_id is None:
                return False
            row = self._db.execute("SELECT 1 FROM operators WHERE user_id = ? AND active = 1",
                                   (str(user_id),)).fetchone()
        return row is not None

    def close(self) -> None:
        self._db.close()


def _operator(row: sqlite3.Row) -> Operator:
    return Operator(row["user_id"], row["name"], row["username"], bool(row["active"]), row["added_by"],
                    row["created_at"], row["phone"], row["linked_at"])
