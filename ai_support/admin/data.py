"""What the admin bot and panel read: clients, sessions as chats, operators and their activity.

Reads the client bot's SQLite database (same DB_PATH, shared Docker volume). The admin side writes
the operator list and the quick-question guides (ai_support/guides.py). A session is a conversation
(ai_support/feedback.py); its chat is the client's logged messages after the previous conversation ended, up to this one's end.
Sessions from before the chat log existed are rebuilt from the saved hand-off and operator replies.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..chatlog import BOT, CLIENT, OPERATOR, SYSTEM, ChatLog
from ..feedback import FeedbackStore
from ..guides import Guide, GuideStore
from ..handoffs import HandoffStore
from ..menu import CATEGORIES, quick_question
from ..models import Lang
from ..operators import Operator, OperatorStore, is_phone
from ..users import UserStore


def _langs(values: dict) -> dict:
    return {(k.value if isinstance(k, Lang) else k): v for k, v in values.items()}


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
        self.guides = GuideStore(path)
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

    # --- quick questions and their guides ------------------------------------------------------

    def quick(self) -> list[dict]:
        """The client menu as the admin edits it: topics, their questions and what each one answers with."""
        guides = self.guides.all()
        out = []
        for cat in CATEGORIES:
            items = [self._quick_item(q.id, guides.get(q.id), q.label) for q in cat.questions]
            items += [self._quick_item(g.qid, g, None) for g in guides.values() if g.custom and g.category == cat.id]
            out.append({"id": cat.id, "label": _langs(cat.label), "questions": items})
        return out

    @staticmethod
    def _quick_item(qid: str, g: Guide | None, default: dict | None) -> dict:
        labels = {**_langs(default or {}), **_langs(g.labels if g else {})}
        return {
            "id": qid, "custom": default is None, "label": labels,
            "hidden": bool(g and g.hidden),
            "guide_languages": [l.value for l in Lang if g and g.has_answer(l)],
            "images": len(g.images) if g else 0,
            "updated_at": g.updated_at if g else None, "updated_by": g.updated_by if g else None,
        }

    def quick_guide(self, qid: str) -> dict | None:
        g = self.guides.get(qid)
        builtin = quick_question(qid)
        if g is None and builtin is None:
            return None
        cid = g.category if g else next(c.id for c in CATEGORIES if builtin in c.questions)
        return {
            "id": qid, "category": cid, "custom": builtin is None,
            "default_label": _langs(builtin.label) if builtin else {},
            "labels": _langs(g.labels) if g else {}, "texts": _langs(g.texts) if g else {},
            "steps": _langs(g.steps) if g else {}, "hidden": bool(g and g.hidden),
            "images": [{"id": i.id, "size": i.size, "content_type": i.content_type} for i in (g.images if g else [])],
            "updated_at": g.updated_at if g else None, "updated_by": g.updated_by if g else None,
        }

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
            "operators_active": count("SELECT COUNT(*) FROM operators WHERE active = 1 AND user_id IS NOT NULL"),
            "operators_pending": count("SELECT COUNT(*) FROM operators WHERE active = 1 AND user_id IS NULL"),
            "rating_bot": {"average": bot.average, "count": bot.count},
            "rating_operators": {"average": op.average, "count": op.count},
        }

    def stats(self, days: int = 14) -> dict:
        """The whole service: totals, sessions per day, topics, languages, ratings and client levels."""
        count = lambda sql, *a: self._one(sql, a)[0] or 0  # noqa: E731
        start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        per_day = {r["d"]: r for r in self._all(
            """SELECT substr(opened_at, 1, 10) AS d, COUNT(*) AS sessions, SUM(handoff_id IS NOT NULL) AS handoffs
               FROM conversations WHERE opened_at >= ? GROUP BY d""", (start.isoformat(),))}
        new_clients = {r["d"]: r["n"] for r in self._all(
            "SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS n FROM users WHERE created_at >= ? GROUP BY d",
            (start.isoformat(),))}
        daily = []
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            r = per_day.get(d)
            daily.append({"day": d, "sessions": r["sessions"] if r else 0,
                          "handoffs": (r["handoffs"] or 0) if r else 0, "clients": new_clients.get(d, 0)})
        sessions = count("SELECT COUNT(*) FROM conversations")
        escalated = count("SELECT COUNT(*) FROM conversations WHERE handoff_id IS NOT NULL")
        bot, op = self.feedback.score("bot"), self.feedback.score("operator")
        return {
            "clients": count("SELECT COUNT(*) FROM users"),
            "sessions": sessions,
            "sessions_closed": count("SELECT COUNT(*) FROM conversations WHERE status = 'closed'"),
            "escalated": escalated,
            "escalation_rate": round(100 * escalated / sessions) if sessions else 0,
            "messages": count("SELECT COUNT(*) FROM chat_messages"),
            "messages_by": _counts(self._all("SELECT sender AS k, COUNT(*) AS n FROM chat_messages GROUP BY sender")),
            "media": count("SELECT COUNT(*) FROM chat_messages WHERE sender = 'client' AND kind != 'text'"),
            "daily": daily,
            "topics": _top(self._all(
                """SELECT COALESCE(NULLIF(topic, ''), '—') AS k, COUNT(*) AS n FROM conversations
                   GROUP BY k ORDER BY n DESC LIMIT 10""")),
            "languages": _counts(self._all("SELECT language AS k, COUNT(*) AS n FROM conversations GROUP BY language")),
            "rating_bot": _score(bot),
            "rating_operators": _score(op),
            "levels": self.feedback.level_counts(),
            "tones": self.feedback.tone_counts(),
        }

    def client_stats(self, client_id: str, channel: str = "telegram") -> dict | None:
        """One client: who they are, their sessions, messages, topics, operators and ratings."""
        cols = self._user_columns()
        user = self._one(f"""SELECT chat_id, username, full_name, language, created_at,
                                    {"phone" if "phone" in cols else "NULL"} AS phone
                             FROM users WHERE channel = ? AND chat_id = ?""", (channel, client_id))
        sessions = self._one(
            """SELECT COUNT(*) AS total, SUM(status = 'open') AS open, SUM(handoff_id IS NOT NULL) AS escalated,
                      MIN(opened_at) AS first FROM conversations WHERE channel = ? AND client_id = ?""",
            (channel, client_id))
        if user is None and not sessions["total"]:
            return None
        where = (channel, client_id)
        ratings = self._all("""SELECT target, stars, outcome FROM rating_requests
                               WHERE channel = ? AND client_id = ? AND status = 'rated'""", where)
        level = self.feedback.level(channel, client_id)
        tones = self.feedback.tones(channel, client_id)
        last = self._one("SELECT MAX(created_at) FROM chat_messages WHERE channel = ? AND client_id = ?", where)[0]
        return {
            "client_id": client_id,
            "name": (user["full_name"] or user["username"] or client_id) if user else client_id,
            "username": user["username"] if user else None,
            "phone": user["phone"] if user else None,
            "language": user["language"] if user else None,
            "first_seen": (user["created_at"] if user else None) or sessions["first"],
            "last_message": last,
            "sessions": sessions["total"] or 0,
            "sessions_open": sessions["open"] or 0,
            "escalated": sessions["escalated"] or 0,
            "messages_by": _counts(self._all(
                "SELECT sender AS k, COUNT(*) AS n FROM chat_messages WHERE channel = ? AND client_id = ? GROUP BY sender",
                where)),
            "media": self._one("""SELECT COUNT(*) FROM chat_messages WHERE channel = ? AND client_id = ?
                                  AND sender = 'client' AND kind != 'text'""", where)[0],
            "topics": _top(self._all(
                """SELECT COALESCE(NULLIF(topic, ''), '—') AS k, COUNT(*) AS n FROM conversations
                   WHERE channel = ? AND client_id = ? GROUP BY k ORDER BY n DESC LIMIT 10""", where)),
            "operators": _top(self._all(
                """SELECT COALESCE(operator_name, operator_id) AS k, COUNT(*) AS n FROM conversations
                   WHERE channel = ? AND client_id = ? AND operator_id IS NOT NULL GROUP BY k ORDER BY n DESC""",
                where)),
            "ratings": len(ratings),
            "rating_average": round(sum(r["stars"] for r in ratings) / len(ratings), 1) if ratings else None,
            "ratings_by_stars": {str(st): sum(r["stars"] == st for r in ratings) for st in range(5, 0, -1)},
            "level": level.label,
            "tones": {t: tones.count(t) for t in sorted(set(tones))},
        }

    # --- operators ---------------------------------------------------------------------------

    def add_operator(self, value: str, name: str = "", username: str | None = None,
                     added_by: str | None = None, user_id: int | str | None = None) -> Operator:
        """Add an operator by phone number or Telegram id.

        By phone: if a client already registered with that number, they become the operator now;
        otherwise when they press /start in the client bot and share that number.
        """
        value = (value or "").strip()
        # A registered client's Telegram id is never mistaken for a local phone number.
        known_id = value.isdigit() and self._one("SELECT 1 FROM users WHERE user_id = ?", (value,)) is not None
        if known_id or not is_phone(value):
            return self.operators.add(value, name, username, added_by)
        op = self.operators.add_phone(value, name, added_by)
        if user_id:
            # Shared as a Telegram contact: its account is known, no need to wait for the registration.
            return self.operators.link(op.phone, user_id, username, name or None) or op
        row = self._one("SELECT user_id, username, full_name FROM users WHERE phone = ? ORDER BY updated_at DESC LIMIT 1",
                        (op.phone,)) if "phone" in self._user_columns() else None
        if row is not None:
            op = self.operators.link(op.phone, row["user_id"], row["username"], row["full_name"]) or op
        return op

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
        known = {o.key: o for o in self.operators.all()}
        # Busy: assigned to a client right now (a new conversation waits for them, or it is theirs).
        busy = self.handoffs.busy_operators()
        result = []
        for key in list(known) + [uid for uid in stats if uid not in known]:
            o: Operator | None = known.get(key)
            uid = o.user_id if o else key
            s, c, r = stats.get(uid), sessions.get(uid), ratings.get(uid)
            result.append({
                "key": key,
                "user_id": uid,
                "phone": o.phone if o else None,
                "linked": o.linked if o else True,
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
                "busy": busy.get(uid, 0) if uid else 0,
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
        h = self._one("SELECT status, assigned_name FROM handoffs WHERE id = ?", (r["handoff_id"],)) \
            if r["handoff_id"] is not None else None
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
            # Who the hand-off is assigned to and its state: waiting (for the operator), operator, ai, closed.
            "assigned_name": h["assigned_name"] if h else None,
            "handoff_status": h["status"] if h else None,
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
        messages = [_chat(m.__dict__) for m in self.chatlog.for_conversation(r["id"])]
        if not messages:
            messages = self._window(r["channel"], r["client_id"], prev, r["closed_at"])
        if not messages and r["handoff_id"] is not None:
            messages = self._from_handoff(r["handoff_id"])
        return {**self._session_row(r, self._client_names()), "messages": messages}

    def reply_target(self, session_id: int) -> dict | None:
        """Where an admin's reply to this session goes: the client, the hand-off and the last client question."""
        r = self._one("SELECT * FROM conversations WHERE id = ?", (session_id,))
        if r is None:
            return None
        names = self._client_names().get((r["channel"], r["client_id"]), {})
        last = self._one("""SELECT text FROM chat_messages WHERE channel = ? AND client_id = ? AND sender = 'client'
                            ORDER BY id DESC LIMIT 1""", (r["channel"], r["client_id"]))
        return {
            "session_id": r["id"], "channel": r["channel"], "client_id": r["client_id"],
            "client_name": names.get("full_name") or names.get("username") or r["client_id"], "language": r["language"], "topic": r["topic"] or "",
            "status": r["status"], "handoff": self.handoffs.get(r["handoff_id"]) if r["handoff_id"] else None,
            "last_client_text": last["text"] if last else "",
        }

    def record_admin_reply(self, target: dict, handoff_id: int | None, text: str, admin_id: str,
                           admin_name: str) -> int:
        """Save an admin's reply sent from the panel like an operator's; returns the session it belongs to."""
        channel, client_id = target["channel"], target["client_id"]
        if handoff_id is not None:
            # The conversation is with a person now: the AI does not take it over.
            self.handoffs.claim(handoff_id, admin_id, admin_name)
        conv = self.feedback.operator_replied(channel, client_id, handoff_id, target["language"], admin_id, admin_name)
        self.chatlog.add(channel, client_id, OPERATOR, text, operator_id=admin_id, operator_name=admin_name,
                         handoff_id=handoff_id, conversation_id=conv.id)
        if handoff_id is not None:
            self.handoffs.add_operator_reply(handoff_id, text, admin_id, admin_name)
        return conv.id

    def _window(self, channel: str, client_id: str, after: str | None, until: str | None) -> list[dict]:
        """Messages saved without a conversation id: the client's messages between two conversation ends."""
        where, args = ["channel = ?", "client_id = ?"], [channel, client_id]
        if after:
            where.append("created_at > ?")
            args.append(after)
        if until:
            where.append("created_at <= ?")
            args.append(until)
        rows = self._all(f"SELECT * FROM chat_messages WHERE {' AND '.join(where)} ORDER BY id LIMIT 500", args)
        return [_chat(dict(r)) for r in rows]

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
        out.append(_msg(SYSTEM, "Передано оператору", h["created_at"]))
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

    # --- the support menu in the admin bot ---------------------------------------------------------

    def is_staff(self, user_id: int, admin_ids) -> bool:
        """Admins and active operators with a known Telegram id may chat with clients from the admin bot."""
        return user_id in admin_ids or any(o.user_id == str(user_id) for o in self.operators.available())

    def support_queue(self, limit: int = 30) -> list[dict]:
        """Open conversations a person is handling or that wait for one, the waiting ones first."""
        rows = self._all("""SELECT c.*, h.status AS h_status, h.assigned_id, h.assigned_name FROM conversations c
                            JOIN handoffs h ON h.id = c.handoff_id
                            WHERE c.status = 'open' AND COALESCE(h.status, 'waiting') != 'closed'
                            ORDER BY c.id DESC LIMIT ?""",
                         (max(1, min(limit, 100)),))
        names = self._client_names()
        out = []
        for r in rows:
            client = names.get((r["channel"], r["client_id"]), {})
            last = self._one("""SELECT text, created_at FROM chat_messages WHERE channel = ? AND client_id = ?
                                AND sender = ? ORDER BY id DESC LIMIT 1""", (r["channel"], r["client_id"], CLIENT))
            out.append({
                "session_id": r["id"], "client_id": r["client_id"],
                "client_name": client.get("full_name") or client.get("username") or r["client_id"],
                # waiting: nobody answered yet; ai: the AI took over after a silence; operator: a person has it.
                "state": r["h_status"] or "waiting", "operator_id": r["operator_id"] or r["assigned_id"],
                "operator_name": r["operator_name"] or r["assigned_name"],
                "last_text": last["text"] if last else "", "last_at": last["created_at"] if last else r["opened_at"],
            })
        order = {"waiting": 0, "ai": 1, "operator": 2}
        return sorted(out, key=lambda x: (order.get(x["state"], 3), x["last_at"]))

    def client_target(self, client_id: str, channel: str = "telegram") -> dict | None:
        """Where a staff member's message to this client goes: their latest session (see reply_target)."""
        r = self._one("SELECT id FROM conversations WHERE channel = ? AND client_id = ? ORDER BY id DESC LIMIT 1",
                      (channel, client_id))
        if r is not None:
            return self.reply_target(r["id"])
        user = self._client_names().get((channel, client_id))
        if user is None:
            return None
        # A registered client with no conversation yet: the staff member starts one.
        registered = self.users.get(channel, client_id)
        return {"session_id": None, "channel": channel, "client_id": client_id,
                "client_name": user.get("full_name") or user.get("username") or client_id,
                "language": registered.lang.value if registered else Lang.UZ_LATN.value, "topic": "",
                "status": "closed", "handoff": None, "last_client_text": ""}

    def current_messages(self, client_id: str, limit: int = 15, channel: str = "telegram") -> tuple[list[dict], bool]:
        """The client's current session for the dialog in the admin bot: (latest messages oldest first, closed).

        A closed session stays closed: what the client wrote after it is a new session, and the dialog shows
        only that. With nothing new since the close, it shows the closed session (closed=True).
        """
        last = self._one("""SELECT id, closed_at FROM conversations WHERE channel = ? AND client_id = ?
                            AND closed_at IS NOT NULL ORDER BY closed_at DESC, id DESC LIMIT 1""", (channel, client_id))
        current = self.feedback.conversation(channel, client_id)
        rows = self._all(
            """SELECT * FROM chat_messages WHERE channel = ? AND client_id = ?
               AND (conversation_id = ? OR (conversation_id IS NULL AND created_at >= ?))
               ORDER BY id DESC LIMIT ?""",
            (channel, client_id, current.id if current else -1, last["closed_at"] if last else "", max(1, limit)))
        if not rows and current is None and last is not None:
            rows = self._all("SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                             (last["id"], max(1, limit)))
            return [_chat(dict(r)) for r in reversed(rows)], True
        return [_chat(dict(r)) for r in reversed(rows)], False

    def close(self) -> None:
        for store in (self.users, self.handoffs, self.feedback, self.chatlog, self.operators):
            store.close()
        self._db.close()


def _chat(m: dict) -> dict:
    """What the panel shows of a logged message."""
    out = {k: m[k] for k in ("id", "sender", "kind", "text", "operator_name", "created_at")}
    # Only the count: the panel loads each file through /api/media/<message id>/<n>.
    files = m.get("files")
    out["files"] = len(files) if isinstance(files, list) else len(json.loads(files)) if files else 0
    return out


def _counts(rows) -> dict:
    return {r["k"]: r["n"] for r in rows}


def _top(rows) -> list[dict]:
    return [{"name": r["k"], "count": r["n"]} for r in rows]


def _score(score) -> dict:
    return {"average": score.average, "count": score.count, "by_stars": {str(k): v for k, v in score.by_stars.items()},
            "no_answer": score.no_answer, "not_helped": score.not_helped}


def _msg(sender: str, text: str, at: str, operator_name: str | None = None) -> dict:
    return {"id": None, "sender": sender, "kind": "text", "text": text, "operator_name": operator_name,
            "created_at": at, "files": 0}


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
