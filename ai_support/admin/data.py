"""What the admin bot and panel read: clients, sessions as chats, operators and their activity.

Reads the client bot's SQLite database (same DB_PATH, shared Docker volume). The only thing the
admin side writes is the operator list. A session is a conversation (ai_support/feedback.py); its
chat is the client's logged messages after the previous conversation ended, up to this one's end.
Sessions from before the chat log existed are rebuilt from the saved hand-off and operator replies.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..chatlog import BOT, CLIENT, EVENT, OPERATOR, ChatLog
from ..feedback import FeedbackStore
from ..handoffs import HandoffStore
from ..operators import Operator, OperatorStore
from ..users import UserStore


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class AdminData:
    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("AdminData needs a database file shared with the client bot")
        # Opening every store creates any table the client bot has not created yet.
        self.users = UserStore(path)
        self.handoffs = HandoffStore(path)
        self.feedback = FeedbackStore(path)
        self.chatlog = ChatLog(path)
        self.operators = OperatorStore(path)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            # Two processes use the file: readers should not wait for the bot's writes.
            self._db.execute("PRAGMA journal_mode=WAL")

    def _all(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _user_columns(self) -> set[str]:
        return {r["name"] for r in self._all("PRAGMA table_info(users)")}

    # --- overview ----------------------------------------------------------------------------

    def overview(self) -> dict:
        today = _today()
        count = lambda sql, *a: self._one(sql, a)[0]  # noqa: E731
        bot, op = self.feedback.score("bot"), self.feedback.score("operator")
        return {
            "clients": count("SELECT COUNT(*) FROM users"),
            "clients_today": count("SELECT COUNT(*) FROM users WHERE created_at >= ?", today),
            "sessions_open": count("SELECT COUNT(*) FROM conversations WHERE status = 'open'"),
            "sessions_today": count("SELECT COUNT(*) FROM conversations WHERE opened_at >= ?", today),
            "handoffs_today": count("SELECT COUNT(*) FROM handoffs WHERE created_at >= ?", today),
            "waiting_operator": count(
                "SELECT COUNT(*) FROM conversations WHERE status = 'open' AND handoff_id IS NOT NULL "
                "AND operator_id IS NULL"),
            "messages_today": count("SELECT COUNT(*) FROM chat_messages WHERE created_at >= ?", today),
            "operators_active": count("SELECT COUNT(*) FROM operators WHERE active = 1"),
            "rating_bot": {"average": bot.average, "count": bot.count},
            "rating_operators": {"average": op.average, "count": op.count},
        }

    # --- operators ---------------------------------------------------------------------------

    def operator_list(self) -> list[dict]:
        """Every added operator, plus anyone who answered clients without being added, with activity."""
        stats = {r["operator_id"]: r for r in self._all(
            """SELECT operator_id, MAX(operator_name) AS name, COUNT(*) AS replies, MAX(created_at) AS last_reply
               FROM operator_replies WHERE operator_id IS NOT NULL GROUP BY operator_id""")}
        sessions = {r["operator_id"]: r for r in self._all(
            """SELECT operator_id, COUNT(*) AS total, SUM(status = 'open') AS open FROM conversations
               WHERE operator_id IS NOT NULL GROUP BY operator_id""")}
        ratings = {r["operator_id"]: r for r in self._all(
            """SELECT operator_id, AVG(stars) AS avg, COUNT(*) AS n FROM rating_requests
               WHERE status = 'rated' AND target = 'operator' AND operator_id IS NOT NULL GROUP BY operator_id""")}
        known = {o.user_id: o for o in self.operators.all()}
        result = []
        for uid in list(known) + [uid for uid in stats if uid not in known]:
            o: Operator | None = known.get(uid)
            s, c, r = stats.get(uid), sessions.get(uid), ratings.get(uid)
            result.append({
                "user_id": uid,
                "name": o.name if o else (s["name"] if s else uid),
                "username": o.username if o else None,
                "registered": o is not None,
                "active": o.active if o else False,
                "added_at": o.created_at if o else None,
                "replies": s["replies"] if s else 0,
                "last_reply": s["last_reply"] if s else None,
                "sessions": c["total"] if c else 0,
                "sessions_open": (c["open"] or 0) if c else 0,
                "rating": round(r["avg"], 1) if r else None,
                "ratings": r["n"] if r else 0,
            })
        return result

    # --- sessions ----------------------------------------------------------------------------

    def _client_names(self) -> dict[tuple[str, str], dict]:
        cols = self._user_columns()
        phone = "phone" if "phone" in cols else "NULL"
        rows = self._all(f"SELECT channel, chat_id, username, full_name, {phone} AS phone FROM users")
        return {(r["channel"], r["chat_id"]): dict(r) for r in rows}

    def sessions(self, operator_id: str | None = None, status: str | None = None, client_id: str | None = None,
                 limit: int = 100) -> list[dict]:
        where, args = [], []
        if operator_id:
            where.append("operator_id = ?")
            args.append(operator_id)
        if status in ("open", "closed"):
            where.append("status = ?")
            args.append(status)
        if client_id:
            where.append("client_id = ?")
            args.append(client_id)
        sql = "SELECT * FROM conversations" + (f" WHERE {' AND '.join(where)}" if where else "")
        rows = self._all(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 500))))
        names = self._client_names()
        return [self._session_row(r, names) for r in rows]

    def _session_row(self, r: sqlite3.Row, names: dict) -> dict:
        client = names.get((r["channel"], r["client_id"]), {})
        rating = None
        if r["closed_at"]:
            rr = self._one(
                """SELECT target, stars, outcome FROM rating_requests WHERE channel = ? AND client_id = ?
                   AND created_at >= ? AND status = 'rated' ORDER BY id LIMIT 1""",
                (r["channel"], r["client_id"], r["closed_at"]))
            rating = dict(rr) if rr else None
        return {
            "id": r["id"],
            "channel": r["channel"],
            "client_id": r["client_id"],
            "client_name": client.get("full_name") or client.get("username") or r["client_id"],
            "client_username": client.get("username"),
            "client_phone": client.get("phone"),
            "status": r["status"],
            "escalated": r["handoff_id"] is not None,
            "handoff_id": r["handoff_id"],
            "operator_id": r["operator_id"],
            "operator_name": r["operator_name"],
            "language": r["language"],
            "topic": r["topic"] or "",
            "opened_at": r["opened_at"],
            "closed_at": r["closed_at"],
            "closed_by": r["closed_by"],
            "rating": rating,
        }

    def session(self, session_id: int) -> dict | None:
        """One session with its messages, as a chat."""
        r = self._one("SELECT * FROM conversations WHERE id = ?", (session_id,))
        if r is None:
            return None
        prev = self._one(
            """SELECT MAX(closed_at) FROM conversations WHERE channel = ? AND client_id = ? AND id < ?
               AND closed_at IS NOT NULL""", (r["channel"], r["client_id"], r["id"]))[0]
        messages = [m.__dict__ for m in self.chatlog.messages(r["channel"], r["client_id"], prev, r["closed_at"])]
        if not messages and r["handoff_id"] is not None:
            messages = self._from_handoff(r["handoff_id"])
        return {**self._session_row(r, self._client_names()), "messages": messages}

    def _from_handoff(self, handoff_id: int) -> list[dict]:
        """A session saved before the chat log existed: the hand-off context and the operator replies."""
        h = self._one("SELECT * FROM handoffs WHERE id = ?", (handoff_id,))
        if h is None:
            return []
        out = []
        for turn in json.loads(h["context"] or "[]"):
            out.append(_msg(CLIENT if turn["role"] == "user" else BOT, turn["text"], h["created_at"]))
        if h["client_text"] and not any(m["text"] == h["client_text"] for m in out):
            out.append(_msg(CLIENT, h["client_text"], h["created_at"]))
        if h["bot_text"]:
            out.append(_msg(BOT, h["bot_text"], h["created_at"]))
        out.append(_msg(EVENT, "Передано оператору", h["created_at"]))
        for rep in self._all("SELECT * FROM operator_replies WHERE handoff_id = ? ORDER BY id", (handoff_id,)):
            out.append(_msg(OPERATOR, rep["text"], rep["created_at"], rep["operator_name"]))
        return out

    # --- clients -----------------------------------------------------------------------------

    def clients(self, query: str = "", limit: int = 100) -> list[dict]:
        cols = self._user_columns()
        phone = "u.phone" if "phone" in cols else "NULL"
        where, args = "", []
        if query.strip():
            like = f"%{query.strip()}%"
            where = "WHERE u.full_name LIKE ? OR u.username LIKE ? OR u.chat_id LIKE ?" + (
                f" OR {phone} LIKE ?" if phone != "NULL" else "")
            args = [like, like, like] + ([like] if phone != "NULL" else [])
        rows = self._all(
            f"""SELECT u.channel, u.user_id, u.chat_id, u.username, u.full_name, u.language, u.created_at,
                       {phone} AS phone,
                       (SELECT COUNT(*) FROM conversations c WHERE c.channel = u.channel AND c.client_id = u.chat_id)
                         AS sessions,
                       (SELECT MAX(created_at) FROM chat_messages m WHERE m.channel = u.channel
                         AND m.client_id = u.chat_id) AS last_message
                FROM users u {where}
                ORDER BY COALESCE(last_message, u.updated_at) DESC LIMIT ?""",
            (*args, max(1, min(limit, 500))))
        out = []
        for r in rows:
            item = dict(r)
            item["level"] = self.feedback.level(r["channel"], r["chat_id"]).label
            out.append(item)
        return out

    def close(self) -> None:
        for store in (self.users, self.handoffs, self.feedback, self.chatlog, self.operators):
            store.close()
        self._db.close()


def _msg(sender: str, text: str, at: str, operator_name: str | None = None) -> dict:
    return {"id": None, "sender": sender, "kind": "text", "text": text, "operator_name": operator_name,
            "created_at": at}


def render_overview(o: dict) -> str:
    """The overview as text for the admin bot."""
    def score(s: dict) -> str:
        return f"{s['average']}★ ({s['count']})" if s["count"] else "оценок нет"
    return (
        "📊 Сводка\n"
        f"Клиенты: {o['clients']} (новых сегодня: {o['clients_today']})\n"
        f"Сессии: открыто {o['sessions_open']}, сегодня {o['sessions_today']}\n"
        f"Передано операторам сегодня: {o['handoffs_today']} · ждут ответа: {o['waiting_operator']}\n"
        f"Сообщений сегодня: {o['messages_today']}\n"
        f"Активных операторов: {o['operators_active']}\n"
        f"Рейтинг бота: {score(o['rating_bot'])} · операторов: {score(o['rating_operators'])}"
    )
