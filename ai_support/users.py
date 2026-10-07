"""Registered clients and their chosen language (SQLite), independent of the channel.

Every client who opens the bot is stored here. The chosen language drives menus and fixed
replies now and is meant for notifications later (v2), so the chat id to reach the client
is kept too. Registration: the client shares their phone number (Telegram contact) once;
the number is kept to improve client service later and is shown to support staff only.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import Lang

DEFAULT_LANGUAGE = Lang.UZ_LATN

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    channel       TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    username      TEXT,
    full_name     TEXT,
    platform_lang TEXT,
    language      TEXT,
    phone         TEXT,
    registered_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (channel, user_id)
)
"""
# Columns added after the first release; older databases get them on start.
_ADDED_COLUMNS = {"phone": "TEXT", "registered_at": "TEXT"}


@dataclass
class User:
    channel: str
    user_id: str
    chat_id: str
    username: str | None = None
    full_name: str | None = None
    # Language reported by the client's app/device; only a hint.
    platform_lang: str | None = None
    # Language the client chose; None until they pick one.
    language: Lang | None = None
    # Phone number the client shared on registration (digits with a leading +); None until registered.
    phone: str | None = None
    registered_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def lang(self) -> Lang:
        return self.language or DEFAULT_LANGUAGE

    @property
    def registered(self) -> bool:
        return bool(self.phone)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UserStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(_SCHEMA)
            have = {r["name"] for r in self._db.execute("PRAGMA table_info(users)")}
            for name, kind in _ADDED_COLUMNS.items():
                if name not in have:
                    self._db.execute(f"ALTER TABLE users ADD COLUMN {name} {kind}")

    def get(self, channel: str, user_id: str) -> User | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM users WHERE channel = ? AND user_id = ?", (channel, user_id)
            ).fetchone()
        return _user(row) if row else None

    def touch(self, channel: str, user_id: str, chat_id: str, username: str | None = None,
              full_name: str | None = None, platform_lang: str | None = None) -> User:
        """Register the client, or refresh their contact details; the chosen language is kept."""
        now = _now()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO users (channel, user_id, chat_id, username, full_name, platform_lang, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (channel, user_id) DO UPDATE SET
                     chat_id = excluded.chat_id, username = excluded.username, full_name = excluded.full_name,
                     platform_lang = excluded.platform_lang, updated_at = excluded.updated_at""",
                (channel, user_id, chat_id, username, full_name, platform_lang, now, now),
            )
        return self.get(channel, user_id)  # type: ignore[return-value]

    def set_language(self, channel: str, user_id: str, lang: Lang) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE users SET language = ?, updated_at = ? WHERE channel = ? AND user_id = ?",
                (lang.value, _now(), channel, user_id),
            )

    def set_phone(self, channel: str, user_id: str, phone: str) -> None:
        """Registration: save the client's phone number (normalised to + and digits)."""
        now = _now()
        with self._lock, self._db:
            self._db.execute(
                """UPDATE users SET phone = ?, registered_at = COALESCE(registered_at, ?), updated_at = ?
                   WHERE channel = ? AND user_id = ?""",
                (normalize_phone(phone), now, now, channel, user_id),
            )

    def by_language(self, channel: str, lang: Lang) -> list[User]:
        """Clients to notify in a given language (v2 notifications); not-yet-chosen counts as the default."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM users WHERE channel = ? AND COALESCE(language, ?) = ? ORDER BY created_at",
                (channel, DEFAULT_LANGUAGE.value, lang.value),
            ).fetchall()
        return [_user(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def close(self) -> None:
        self._db.close()


def normalize_phone(phone: str) -> str:
    """Telegram sends numbers with or without "+"; keep "+" and digits only."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"+{digits}" if digits else ""


def _user(row: sqlite3.Row) -> User:
    lang = row["language"]
    return User(
        channel=row["channel"], user_id=row["user_id"], chat_id=row["chat_id"], username=row["username"],
        full_name=row["full_name"], platform_lang=row["platform_lang"],
        language=Lang(lang) if lang else None, phone=row["phone"], registered_at=row["registered_at"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )
