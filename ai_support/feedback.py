"""Ratings after a case and assessments of clients (SQLite, independent of the channel).

Two directions:
- The client rates the conversation once it ends: the client taps "End conversation" (button or command)
  or the operator ends it. The bot is rated if the conversation never reached a person, otherwise the
  operator. 1-5 stars, or "did not answer" / "could not help" (both count as 1 star).
- The operator and the AI assess the client from the conversation: what the problem was, what the
  client suggested, and how they talked (polite / calm / rude). From these the client gets a level.

Everything here is internal. Levels and assessments are shown only in the support chat; the bot never
shows them to clients and never changes answers, limits or access because of them (the bot makes no
decisions about access or limits at all). The support rating is anonymous: it has no client ids.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import BotReply
from .pii import mask_pii

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rating_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    target        TEXT NOT NULL,          -- 'bot' | 'operator'
    handoff_id    INTEGER,
    operator_id   TEXT,
    operator_name TEXT,
    language      TEXT NOT NULL,
    topic         TEXT,
    prompt_ref    TEXT,                   -- unused; kept so older databases still match
    status        TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'rated' | 'expired'
    stars         INTEGER,
    outcome       TEXT,                   -- 'stars' | 'no_answer' | 'not_helped'
    created_at    TEXT NOT NULL,
    rated_at      TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
    handoff_id    INTEGER,                -- set once the conversation reached a person
    operator_id   TEXT,
    operator_name TEXT,
    language      TEXT NOT NULL,
    topic         TEXT,
    opened_at     TEXT NOT NULL,
    closed_at     TEXT,
    closed_by     TEXT                    -- 'client' | 'operator'
);
CREATE TABLE IF NOT EXISTS client_assessments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    handoff_id  INTEGER,
    source      TEXT NOT NULL,            -- 'ai' | 'operator'
    assessor    TEXT,
    tone        TEXT,                     -- 'polite' | 'calm' | 'rude'
    problem     TEXT,
    suggestions TEXT,
    created_at  TEXT NOT NULL
);
"""

BOT, OPERATOR = "bot", "operator"
OPEN, RATED, EXPIRED, CLOSED = "open", "rated", "expired", "closed"
NO_ANSWER, NOT_HELPED = "no_answer", "not_helped"
TONES = ("polite", "calm", "rude")
TONE_LABELS = {"polite": "вежливо", "calm": "спокойно", "rude": "грубо"}


class RatingError(Exception):
    pass


@dataclass
class RatingRequest:
    id: int
    channel: str
    client_id: str
    target: str
    handoff_id: int | None
    operator_name: str | None
    language: str
    prompt_ref: str | None
    status: str
    stars: int | None
    outcome: str | None


@dataclass
class Conversation:
    id: int
    channel: str
    client_id: str
    handoff_id: int | None
    operator_id: str | None
    operator_name: str | None
    language: str
    topic: str

    @property
    def target(self) -> str:
        """Who the client rates when it ends: the operator once a person took the case, else the bot."""
        return OPERATOR if self.handoff_id is not None else BOT


@dataclass
class Assessment:
    tone: str
    problem: str = ""
    suggestions: str = ""


@dataclass
class Level:
    code: str   # 'A' | 'B' | 'C' | 'new'
    label: str
    assessed: int  # cases the level is based on

    def __str__(self) -> str:
        return f"{self.label} (оценок: {self.assessed})" if self.assessed else self.label


@dataclass
class Score:
    """App-store style rating of one target."""
    count: int
    average: float
    by_stars: dict[int, int]
    no_answer: int
    not_helped: int


def opens_conversation(reply: BotReply) -> bool:
    """A real answer or a hand-off: there is now something to rate. Greetings and fixed errors are not."""
    return reply.escalate or (bool(reply.topic) and reply.topic != "greeting" and not reply.show_menu)


def parse_rating(value: str) -> tuple[int, str] | None:
    """Button value -> (stars, outcome): '1'..'5', 'none' (did not answer), 'nohelp' (could not help)."""
    if value in {"1", "2", "3", "4", "5"}:
        return int(value), "stars"
    if value == "none":
        return 1, NO_ANSWER
    if value == "nohelp":
        return 1, NOT_HELPED
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FeedbackStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    # --- conversations: opened by an answer or a hand-off, ended by the client or the operator --------

    def conversation(self, channel: str, client_id: str) -> Conversation | None:
        """The client's open conversation, if any."""
        with self._lock:
            row = self._open(channel, client_id)
        return _conversation(row) if row else None

    def conversation_for_handoff(self, handoff_id: int) -> Conversation | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE handoff_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
                (handoff_id, OPEN)).fetchone()
        return _conversation(row) if row else None

    def bot_answered(self, channel: str, client_id: str, language: str, topic: str = "") -> Conversation:
        """The bot answered: open a conversation, or keep the open one (a hand-off stays a hand-off)."""
        with self._lock, self._db:
            row = self._open(channel, client_id)
            if row is None:
                return _conversation(self._insert(channel, client_id, language, topic))
            self._db.execute("UPDATE conversations SET language = ?, topic = COALESCE(NULLIF(?, ''), topic) WHERE id = ?",
                             (language, topic, row["id"]))
            return _conversation(self._db.execute("SELECT * FROM conversations WHERE id = ?", (row["id"],)).fetchone())

    def escalated(self, channel: str, client_id: str, handoff_id: int, language: str, topic: str = "") -> Conversation:
        """The conversation reached a person: from now on the operator is rated for it, not the bot."""
        with self._lock, self._db:
            row = self._open(channel, client_id) or self._insert(channel, client_id, language, topic)
            self._db.execute("UPDATE conversations SET handoff_id = ?, language = ? WHERE id = ?",
                             (handoff_id, language, row["id"]))
            return _conversation(self._db.execute("SELECT * FROM conversations WHERE id = ?", (row["id"],)).fetchone())

    def release(self, handoff_id: int) -> Conversation | None:
        """The operator did not answer in time: the open conversation goes back to the bot (rated as the bot's)."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE handoff_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
                (handoff_id, OPEN)).fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE conversations SET handoff_id = NULL WHERE id = ?", (row["id"],))
            return _conversation(self._db.execute("SELECT * FROM conversations WHERE id = ?", (row["id"],)).fetchone())

    def operator_replied(self, channel: str, client_id: str, handoff_id: int, language: str,
                         operator_id: str | None, operator_name: str | None) -> Conversation:
        """Remember who answered; a reply after the conversation ended opens it again."""
        with self._lock, self._db:
            row = self._open(channel, client_id) or self._insert(channel, client_id, language, "")
            self._db.execute(
                "UPDATE conversations SET handoff_id = ?, operator_id = ?, operator_name = ? WHERE id = ?",
                (handoff_id, operator_id, operator_name, row["id"]))
            return _conversation(self._db.execute("SELECT * FROM conversations WHERE id = ?", (row["id"],)).fetchone())

    def end(self, channel: str, client_id: str, by: str) -> tuple[Conversation, RatingRequest] | None:
        """Close the client's open conversation and open its one rating request; None if nothing is open."""
        with self._lock, self._db:
            row = self._open(channel, client_id)
            if row is None:
                return None
            self._db.execute("UPDATE conversations SET status = ?, closed_at = ?, closed_by = ? WHERE id = ?",
                             (CLOSED, _now(), by, row["id"]))
            c = _conversation(row)
            cur = self._db.execute(
                """INSERT INTO rating_requests (channel, client_id, target, handoff_id, operator_id, operator_name,
                                                language, topic, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel, client_id, c.target, c.handoff_id, c.operator_id, c.operator_name, c.language, c.topic,
                 _now()),
            )
            req = self._db.execute("SELECT * FROM rating_requests WHERE id = ?", (cur.lastrowid,)).fetchone()
        return c, _request(req)

    def _open(self, channel: str, client_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM conversations WHERE channel = ? AND client_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (channel, client_id, OPEN)).fetchone()

    def _insert(self, channel: str, client_id: str, language: str, topic: str) -> sqlite3.Row:
        cur = self._db.execute(
            "INSERT INTO conversations (channel, client_id, language, topic, opened_at) VALUES (?, ?, ?, ?, ?)",
            (channel, client_id, language, topic, _now()))
        return self._db.execute("SELECT * FROM conversations WHERE id = ?", (cur.lastrowid,)).fetchone()

    # --- the client rates the ended conversation --------------------------------------------------

    def request(self, request_id: int) -> RatingRequest | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM rating_requests WHERE id = ?", (request_id,)).fetchone()
        return _request(row) if row else None

    def rate(self, request_id: int, client_id: str, value: str) -> RatingRequest:
        """Save the client's rating; the client may change it."""
        parsed = parse_rating(value)
        r = self.request(request_id)
        if r is None or r.client_id != client_id or parsed is None:
            raise RatingError("not_found")
        if r.status == EXPIRED:
            raise RatingError("expired")
        stars, outcome = parsed
        with self._lock, self._db:
            self._db.execute(
                "UPDATE rating_requests SET status = ?, stars = ?, outcome = ?, rated_at = ? WHERE id = ?",
                (RATED, stars, outcome, _now(), request_id),
            )
        return self.request(request_id)  # type: ignore[return-value]

    # --- the operator and the AI assess the client ------------------------------------------------

    def assess(self, channel: str, client_id: str, source: str, a: Assessment, handoff_id: int | None = None,
               assessor: str | None = None) -> None:
        if a.tone not in TONES:
            raise ValueError(f"unknown tone {a.tone!r}")
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO client_assessments (channel, client_id, handoff_id, source, assessor, tone, problem,
                                                   suggestions, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel, client_id, handoff_id, source, assessor, a.tone, mask_pii(a.problem.strip()),
                 mask_pii(a.suggestions.strip()), _now()),
            )

    def note(self, channel: str, client_id: str, handoff_id: int, assessor: str, problem: str,
             suggestions: str = "") -> None:
        """The operator's words about the case; keeps the operator's tone for it if they already gave one."""
        with self._lock:
            row = self._db.execute(
                "SELECT tone FROM client_assessments WHERE handoff_id = ? AND source = ? ORDER BY id DESC LIMIT 1",
                (handoff_id, OPERATOR)).fetchone()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO client_assessments (channel, client_id, handoff_id, source, assessor, tone, problem,
                                                   suggestions, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel, client_id, handoff_id, OPERATOR, assessor, row["tone"] if row else None,
                 mask_pii(problem.strip()), mask_pii(suggestions.strip()), _now()),
            )

    def tones(self, channel: str, client_id: str) -> list[str]:
        """One tone per case: the operator's if they gave one, else the AI's; bot-only cases count each once."""
        with self._lock:
            rows = self._db.execute(
                """SELECT handoff_id, source, tone, id FROM client_assessments
                   WHERE channel = ? AND client_id = ? AND tone IS NOT NULL ORDER BY id""",
                (channel, client_id)).fetchall()
        per_case: dict[object, tuple[int, str]] = {}
        for r in rows:
            key = r["handoff_id"] if r["handoff_id"] is not None else f"ai-{r['id']}"
            rank = 1 if r["source"] == OPERATOR else 0
            if key not in per_case or rank >= per_case[key][0]:
                per_case[key] = (rank, r["tone"])
        return [tone for _, tone in per_case.values()]

    def level(self, channel: str, client_id: str) -> Level:
        return client_level(self.tones(channel, client_id))

    # --- anonymous summaries for the support chat -------------------------------------------------

    def score(self, target: str | None = None) -> Score:
        where, args = ("status = ?", [RATED]) if target is None else ("status = ? AND target = ?", [RATED, target])
        with self._lock:
            rows = self._db.execute(f"SELECT stars, outcome FROM rating_requests WHERE {where}", args).fetchall()
        by_stars = {s: 0 for s in range(5, 0, -1)}
        for r in rows:
            by_stars[r["stars"]] += 1
        count = len(rows)
        return Score(count, round(sum(r["stars"] for r in rows) / count, 1) if count else 0.0, by_stars,
                     sum(r["outcome"] == NO_ANSWER for r in rows), sum(r["outcome"] == NOT_HELPED for r in rows))

    def operator_scores(self) -> list[tuple[str, float, int]]:
        """(operator, average stars, ratings) for operators that were rated, best first."""
        with self._lock:
            rows = self._db.execute(
                """SELECT COALESCE(operator_name, operator_id, '?') AS who, AVG(stars) AS avg, COUNT(*) AS n
                   FROM rating_requests WHERE status = ? AND target = ? GROUP BY who ORDER BY avg DESC, n DESC""",
                (RATED, OPERATOR)).fetchall()
        return [(r["who"], round(r["avg"], 1), r["n"]) for r in rows]

    def level_counts(self) -> dict[str, int]:
        """How many clients are at each level; anonymous."""
        with self._lock:
            clients = self._db.execute("SELECT DISTINCT channel, client_id FROM client_assessments").fetchall()
        counts = {code: 0 for code in ("A", "B", "C")}
        for c in clients:
            lvl = self.level(c["channel"], c["client_id"])
            if lvl.code in counts:
                counts[lvl.code] += 1
        return counts

    def tone_counts(self) -> dict[str, int]:
        with self._lock:
            clients = self._db.execute("SELECT DISTINCT channel, client_id FROM client_assessments").fetchall()
        counts = {tone: 0 for tone in TONES}
        for c in clients:
            for tone in self.tones(c["channel"], c["client_id"]):
                counts[tone] += 1
        return counts

    def close(self) -> None:
        self._db.close()


LEVELS = {
    "A": "A · вежливый",
    "B": "B · обычный",
    "C": "C · требует внимания",
    "new": "новый, оценок ещё нет",
}


def client_level(tones: list[str]) -> Level:
    """A: polite in most cases and never rude (2+ cases). C: rude twice or in a third of cases. B: the rest."""
    n = len(tones)
    if not n:
        return Level("new", LEVELS["new"], 0)
    rude, polite = tones.count("rude"), tones.count("polite")
    if rude >= 2 or rude * 3 >= n:
        code = "C"
    elif n >= 2 and not rude and polite * 2 >= n:
        code = "A"
    else:
        code = "B"
    return Level(code, LEVELS[code], n)


def _stars(avg: float) -> str:
    full = int(avg + 0.5)
    return "★" * full + "☆" * (5 - full)


def _bar(n: int, total: int, width: int = 10) -> str:
    filled = round(width * n / total) if total else 0
    return "█" * filled + "·" * (width - filled)


def render_score(title: str, s: Score) -> str:
    if not s.count:
        return f"{title}: оценок пока нет"
    lines = [f"{title}: {s.average} {_stars(s.average)} · {s.count} оценок"]
    lines += [f"{star}★ {_bar(s.by_stars[star], s.count)} {s.by_stars[star]}" for star in range(5, 0, -1)]
    lines.append(f"Не ответили: {s.no_answer} · Не смогли помочь: {s.not_helped} (считаются как 1★)")
    return "\n".join(lines)


def render_rating(store: FeedbackStore) -> str:
    """The anonymous rating for the support chat, in the style of an app store rating."""
    parts = [
        "📊 Анонимный рейтинг поддержки (оценки клиентов)",
        render_score("Вся поддержка", store.score()),
        render_score("🤖 AI-бот", store.score(BOT)),
        render_score("👨‍💼 Операторы", store.score(OPERATOR)),
    ]
    ops = store.operator_scores()
    if ops:
        parts.append("По операторам:\n" + "\n".join(f"{who}: {avg}★ ({n})" for who, avg, n in ops))
    levels = store.level_counts()
    tones = store.tone_counts()
    parts.append(
        "👥 Клиенты (анонимно, по оценкам операторов и AI)\n"
        + " · ".join(f"{LEVELS[c]}: {levels[c]}" for c in ("A", "B", "C"))
        + "\nТон в обращениях: " + " · ".join(f"{TONE_LABELS[t]}: {tones[t]}" for t in TONES)
    )
    return "\n\n".join(parts)


def render_assessment(source: str, a: Assessment, level: Level | None = None) -> str:
    who = "AI" if source == "ai" else "Оператор"
    lines = [f"{who}-оценка клиента · тон: {TONE_LABELS.get(a.tone, '-')}"]
    if a.problem:
        lines.append(f"Суть проблемы: {mask_pii(a.problem)}")
    if a.suggestions:
        lines.append(f"Предложения: {mask_pii(a.suggestions)}")
    if level is not None:
        lines.append(f"Уровень клиента: {level}")
    return "\n".join(lines)


def _conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(row["id"], row["channel"], row["client_id"], row["handoff_id"], row["operator_id"],
                        row["operator_name"], row["language"], row["topic"] or "")


def _request(row: sqlite3.Row) -> RatingRequest:
    return RatingRequest(row["id"], row["channel"], row["client_id"], row["target"], row["handoff_id"],
                         row["operator_name"], row["language"], row["prompt_ref"], row["status"], row["stars"],
                         row["outcome"])
