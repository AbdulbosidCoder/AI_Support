"""Read-only access to the AI Support chats for the support team's CRM, by API token.

The CRM never gets the admins' Telegram access. An admin issues it a token (`crm_...`); only the
token's SHA-256 hash is stored, so a copy of the database does not leak it. A token can be revoked,
can expire, and can be limited to a list of IP addresses or networks. Every request is rate limited
and written to an audit log.

Two ways in, both read-only (nothing can be sent to a client or changed):
- API: the CRM's server calls GET /crm/v1/... with "Authorization: Bearer crm_...".
- Embedded page: the CRM's server asks POST /crm/v1/embed-links for a short-lived signed link
  (`crmt_...`, default 1 hour, optionally for one session or client) and shows it in an iframe.
  The key travels in the URL fragment (#key=...), which browsers never send to a server or in a Referer.
  Only the sites in CRM_FRAME_ANCESTORS may frame the page.

Card numbers, PINFL, phones and balances are masked in what the CRM gets; client photos, voice
messages and files are shown only to tokens with the "media" scope, unmasked phones only with "pii".

Manage tokens from the admin panel API (/api/crm/tokens) or the command line:
    python -m ai_support.admin.crm create "Support CRM" [--ips 203.0.113.0/24] [--days 365] [--scopes media]
    python -m ai_support.admin.crm list
    python -m ai_support.admin.crm revoke <id>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

from ..pii import mask_pii
from .web import ADMIN_USER, DATA, STATIC, _admin_name, _int, _json, media

log = logging.getLogger(__name__)

TOKEN_PREFIX = "crm_"
TICKET_PREFIX = "crmt_"
SCOPES = frozenset({"media", "pii"})
DEFAULT_TICKET_MINUTES = 60
MAX_TICKET_MINUTES = 8 * 60
AUDIT_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,   -- sha256 of the token; the token itself is shown once
    prefix       TEXT NOT NULL,          -- first characters, to tell tokens apart in lists
    scopes       TEXT NOT NULL DEFAULT '',
    allowed_ips  TEXT NOT NULL DEFAULT '',  -- comma separated addresses or networks; empty = any
    created_at   TEXT NOT NULL,
    created_by   TEXT,
    expires_at   TEXT,
    revoked_at   TEXT,
    last_used_at TEXT,
    last_ip      TEXT
);
CREATE TABLE IF NOT EXISTS crm_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id   INTEGER,
    method     TEXT NOT NULL,
    path       TEXT NOT NULL,
    ip         TEXT,
    status     INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_audit_token ON crm_audit (token_id, created_at);
CREATE TABLE IF NOT EXISTS crm_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class CrmError(Exception):
    pass


def parse_networks(value: str | list | None) -> list[str]:
    """'203.0.113.5, 10.0.0.0/8' -> normalised networks; CrmError on anything that is not one."""
    items = value if isinstance(value, list) else str(value or "").replace(";", ",").split(",")
    out = []
    for item in (str(i).strip() for i in items):
        if not item:
            continue
        try:
            out.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError:
            raise CrmError("bad_ip") from None
    return out


def parse_scopes(value: str | list | None) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").replace(";", ",").split(",")
    scopes = sorted({str(i).strip() for i in items if str(i).strip()})
    if any(s not in SCOPES for s in scopes):
        raise CrmError("bad_scope")
    return scopes


@dataclass(frozen=True)
class CrmToken:
    id: int
    name: str
    prefix: str
    scopes: tuple[str, ...]
    allowed_ips: tuple[str, ...]
    created_at: str
    created_by: str | None
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None
    last_ip: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > _iso(_now()))

    def allows_ip(self, ip: str | None) -> bool:
        if not self.allowed_ips:
            return True
        try:
            addr = ipaddress.ip_address(ip or "")
        except ValueError:
            return False
        return any(addr in ipaddress.ip_network(n) for n in self.allowed_ips)

    def public(self) -> dict:
        return {**self.__dict__, "scopes": list(self.scopes), "allowed_ips": list(self.allowed_ips),
                "active": self.active}


def _token(r: sqlite3.Row) -> CrmToken:
    return CrmToken(r["id"], r["name"], r["prefix"], tuple(s for s in r["scopes"].split(",") if s),
                    tuple(n for n in r["allowed_ips"].split(",") if n), r["created_at"], r["created_by"],
                    r["expires_at"], r["revoked_at"], r["last_used_at"], r["last_ip"])


@dataclass(frozen=True)
class Access:
    """Who is asking: the token, and what an embed link narrows it to."""
    token: CrmToken
    session_id: int | None = None
    client_id: str | None = None
    link: bool = False  # an embed link, opened in a staff member's browser

    def has(self, scope: str) -> bool:
        return scope in self.token.scopes


class CrmStore:
    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            row = self._db.execute("SELECT value FROM crm_meta WHERE key = 'ticket_secret'").fetchone()
            if row is None:
                # Signs embed links; kept in the database so links survive a restart.
                self._db.execute("INSERT INTO crm_meta VALUES ('ticket_secret', ?)", (secrets.token_hex(32),))
                row = self._db.execute("SELECT value FROM crm_meta WHERE key = 'ticket_secret'").fetchone()
        self._secret = bytes.fromhex(row["value"])

    # --- tokens ----------------------------------------------------------------------------------

    def create(self, name: str, allowed_ips=None, scopes=None, days: int | None = None,
               created_by: str | None = None) -> tuple[CrmToken, str]:
        """A new token and its secret value; the value is not stored and cannot be shown again."""
        name = (name or "").strip()[:100]
        if not name:
            raise CrmError("bad_name")
        nets, scope_list = parse_networks(allowed_ips), parse_scopes(scopes)
        if days is not None and not 1 <= int(days) <= 3650:
            raise CrmError("bad_days")
        value = TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires = _iso(_now() + timedelta(days=int(days))) if days else None
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO crm_tokens (name, token_hash, prefix, scopes, allowed_ips, created_at, created_by,
                   expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, _hash(value), value[:10], ",".join(scope_list), ",".join(nets), _iso(_now()), created_by,
                 expires))
        return self.get(cur.lastrowid), value

    def get(self, token_id: int) -> CrmToken | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM crm_tokens WHERE id = ?", (token_id,)).fetchone()
        return _token(r) if r else None

    def list(self) -> list[CrmToken]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM crm_tokens ORDER BY id DESC").fetchall()
        return [_token(r) for r in rows]

    def revoke(self, token_id: int) -> CrmToken:
        """Revoke at once: the token and every embed link made with it stop working."""
        with self._lock, self._db:
            self._db.execute("UPDATE crm_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                             (_iso(_now()), token_id))
        found = self.get(token_id)
        if found is None:
            raise CrmError("not_found")
        return found

    def by_value(self, value: str) -> CrmToken | None:
        if not value.startswith(TOKEN_PREFIX):
            return None
        with self._lock:
            r = self._db.execute("SELECT * FROM crm_tokens WHERE token_hash = ?", (_hash(value),)).fetchone()
        return _token(r) if r else None

    def touch(self, token_id: int, ip: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE crm_tokens SET last_used_at = ?, last_ip = ? WHERE id = ?",
                             (_iso(_now()), ip, token_id))

    # --- embed links -----------------------------------------------------------------------------

    def ticket(self, token: CrmToken, minutes: int = DEFAULT_TICKET_MINUTES, session_id: int | None = None,
               client_id: str | None = None) -> tuple[str, str]:
        """A signed, short-lived key for the embedded page; it dies with its token."""
        minutes = max(1, min(int(minutes), MAX_TICKET_MINUTES))
        exp = int(time.time()) + minutes * 60
        if token.expires_at:
            exp = min(exp, int(datetime.strptime(token.expires_at, "%Y-%m-%dT%H:%M:%SZ")
                               .replace(tzinfo=timezone.utc).timestamp()))
        payload = {"t": token.id, "exp": exp}
        if session_id is not None:
            payload["s"] = int(session_id)
        if client_id:
            payload["c"] = str(client_id)
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        return f"{TICKET_PREFIX}{body}.{sig}", _iso(datetime.fromtimestamp(exp, timezone.utc))

    def access(self, key: str, now: float | None = None) -> Access | None:
        """The access a token or an embed link gives; None if it is unknown, revoked, expired or forged."""
        if key.startswith(TICKET_PREFIX):
            body, _, sig = key[len(TICKET_PREFIX):].partition(".")
            expected = _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
            if not sig or not hmac.compare_digest(expected, sig):
                return None
            try:
                payload = json.loads(_unb64(body))
            except ValueError:
                return None
            if (now if now is not None else time.time()) > payload.get("exp", 0):
                return None
            token = self.get(int(payload.get("t", 0)))
            if token is None or not token.active:
                return None
            return Access(token, payload.get("s"), payload.get("c"), link=True)
        token = self.by_value(key)
        return Access(token) if token is not None and token.active else None

    # --- audit -----------------------------------------------------------------------------------

    def audit(self, token_id: int | None, method: str, path: str, ip: str | None, status: int) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO crm_audit (token_id, method, path, ip, status, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (token_id, method, path[:300], ip, status, _iso(_now())))

    def audit_log(self, token_id: int | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM crm_audit", []
        if token_id is not None:
            sql += " WHERE token_id = ?"
            args.append(token_id)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 1000)))).fetchall()
        return [dict(r) for r in rows]

    def prune_audit(self, days: int = AUDIT_DAYS) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM crm_audit WHERE created_at < ?", (_iso(_now() - timedelta(days=days)),))

    def close(self) -> None:
        self._db.close()


class RateLimit:
    """At most `per_minute` requests per key (a token, or the address of a caller without one) in 60 seconds."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[object, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            return True


@dataclass(frozen=True)
class CrmConfig:
    # Sites allowed to show the chats page in an iframe, e.g. "https://crm.example.uz". Empty: nobody.
    frame_ancestors: tuple[str, ...] = ()
    rate_per_minute: int = 120
    # Behind the Cloudflare Tunnel the caller's address is in CF-Connecting-IP; the admin port is not
    # published outside Docker, so the header cannot be forged by going around the tunnel.
    trust_proxy: bool = True
    public_url: str = ""

    @classmethod
    def from_env(cls, public_url: str = "") -> "CrmConfig":
        ancestors = tuple(a.strip() for a in os.getenv("CRM_FRAME_ANCESTORS", "").replace(",", " ").split()
                          if a.strip().startswith("https://"))
        return cls(frame_ancestors=ancestors,
                   rate_per_minute=max(1, int(os.getenv("CRM_RATE_PER_MINUTE", cls.rate_per_minute))),
                   trust_proxy=os.getenv("CRM_TRUST_PROXY", "1").strip().lower() not in ("0", "false", "no", "off"),
                   public_url=public_url)


STORE = web.AppKey("crm_store", CrmStore)
CONFIG = web.AppKey("crm_config", CrmConfig)
LIMIT = web.AppKey("crm_limit", RateLimit)
ACCESS = web.RequestKey("crm_access", Access) if hasattr(web, "RequestKey") else "crm_access"

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def client_ip(request: web.Request, trust_proxy: bool) -> str | None:
    if trust_proxy:
        forwarded = request.headers.get("CF-Connecting-IP", "").strip()
        if forwarded:
            return forwarded
    return request.remote


@web.middleware
async def crm_only(request: web.Request, handler):
    """Every /crm/v1/ request needs a live token or embed link, from an allowed address, within the rate limit."""
    if not request.path.startswith("/crm/v1/"):
        return await handler(request)
    store, config, limit = request.app[STORE], request.app[CONFIG], request.app[LIMIT]
    ip = client_ip(request, config.trust_proxy)
    token_id = None
    if request.headers.get("X-Forwarded-Proto", "https").lower() != "https":
        response = _json({"error": "https_required"}, 403)
    else:
        auth = request.headers.get("Authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        access = store.access(key) if key else None
        if access is None:
            if not limit.allow(("ip", ip)):
                # Someone guessing keys: refuse without filling the audit log.
                response = _json({"error": "rate_limited"}, 429)
                response.headers.update({**_SECURITY_HEADERS, "Retry-After": "60"})
                return response
            log.warning("CRM request without a valid key from %s: %s", ip, request.path)
            response = _json({"error": "unauthorized"}, 401)
        else:
            token_id = access.token.id
            # The allow-list guards the token (the CRM's server); an embed link is opened from staff
            # browsers anywhere, so it is limited by its short life and by its token instead.
            if not access.link and not access.token.allows_ip(ip):
                response = _json({"error": "forbidden_ip"}, 403)
            elif not limit.allow(token_id):
                response = _json({"error": "rate_limited"}, 429)
                response.headers["Retry-After"] = "60"
            else:
                request[ACCESS] = access
                store.touch(token_id, ip)
                try:
                    response = await handler(request)
                except web.HTTPException as e:
                    response = _json({"error": e.reason}, e.status)
    response.headers.update(_SECURITY_HEADERS)
    store.audit(token_id, request.method, request.path_qs, ip, response.status)
    return response


# --- what the CRM sees -------------------------------------------------------------------------------

def _mask_phone(phone: str | None) -> str | None:
    digits = "".join(c for c in phone or "" if c.isdigit())
    return f"+*** ** *** ** {digits[-2:]}" if len(digits) >= 4 else (None if not phone else "***")


def _session(s: dict, access: Access) -> dict:
    out = {k: v for k, v in s.items() if k != "messages"}
    if not access.has("pii"):
        out["client_phone"] = _mask_phone(out.get("client_phone"))
    if "messages" in s:
        out["messages"] = [_message(m, s["id"], access) for m in s["messages"]]
    return out


def _message(m: dict, session_id: int, access: Access) -> dict:
    out = {**m, "text": m["text"] if access.has("pii") else mask_pii(m["text"] or "")}
    files = out.pop("files", 0) or 0
    out["files"] = files
    # The CRM opens a file by this path; without the "media" scope it only learns that a file was sent.
    out["media"] = [f"/crm/v1/media/{m['id']}/{n}" for n in range(files)] if access.has("media") and m["id"] else []
    return out


def _allowed(session: dict, access: Access) -> bool:
    if access.session_id is not None and session["id"] != access.session_id:
        return False
    return access.client_id is None or session["client_id"] == access.client_id


async def sessions(request: web.Request) -> web.Response:
    access, q = request[ACCESS], request.query
    status = q.get("status") or None
    client = access.client_id or q.get("client") or None
    rows = request.app[DATA].sessions(q.get("operator") or None, status, client, _int(q.get("limit"), 50))
    since = _int(q.get("since_id"), 0)
    rows = [s for s in rows if s["id"] > since and _allowed(s, access)]
    return _json({"sessions": [_session(s, access) for s in rows]})


async def session(request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    found = request.app[DATA].session(int(sid)) if sid.isdigit() else None
    if found is None or not _allowed(found, request[ACCESS]):
        return _json({"error": "not_found"}, 404)
    return _json(_session(found, request[ACCESS]))


async def crm_media(request: web.Request) -> web.Response:
    access = request[ACCESS]
    if not access.has("media"):
        return _json({"error": "forbidden_scope"}, 403)
    if access.session_id is not None or access.client_id is not None:
        # An embed link for one session or client opens only that client's files.
        data, mid = request.app[DATA], request.match_info["id"]
        msg = data._one("SELECT client_id FROM chat_messages WHERE id = ?", (int(mid),)) if mid.isdigit() else None
        allowed = access.client_id
        if access.session_id is not None:
            conv = data._one("SELECT client_id FROM conversations WHERE id = ?", (access.session_id,))
            allowed = conv["client_id"] if conv else None
        if msg is None or msg["client_id"] != allowed:
            return _json({"error": "not_found"}, 404)
    response = await media(request)
    response.headers["Cache-Control"] = "no-store"
    return response


async def me(request: web.Request) -> web.Response:
    access = request[ACCESS]
    return _json({"name": access.token.name, "scopes": list(access.token.scopes),
                  "session_id": access.session_id, "client_id": access.client_id})


async def embed_link(request: web.Request) -> web.Response:
    """A link for an iframe in the CRM; only the CRM's server (with the token itself) can ask for one."""
    access = request[ACCESS]
    if access.link:
        return _json({"error": "forbidden"}, 403)  # an embed link cannot make more links
    try:
        body = await request.json() if request.can_read_body else {}
    except ValueError:
        return _json({"error": "bad_json"}, 400)
    if not isinstance(body, dict):
        return _json({"error": "bad_json"}, 400)
    sid, cid = body.get("session_id"), body.get("client_id")
    if sid is not None and not str(sid).isdigit():
        return _json({"error": "bad_session_id"}, 400)
    minutes = body.get("minutes", DEFAULT_TICKET_MINUTES)
    if not isinstance(minutes, int) or not 1 <= minutes <= MAX_TICKET_MINUTES:
        return _json({"error": "bad_minutes"}, 400)
    key, expires = request.app[STORE].ticket(access.token, minutes, int(sid) if sid is not None else None,
                                            str(cid) if cid else None)
    base = request.app[CONFIG].public_url.rstrip("/")
    return _json({"url": f"{base}/crm/#key={key}", "expires_at": expires})


async def page(request: web.Request) -> web.Response:
    """The read-only chats page for an iframe; its key comes in the URL fragment."""
    ancestors = " ".join(request.app[CONFIG].frame_ancestors) or "'none'"
    headers = {
        **_SECURITY_HEADERS,
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
            f"media-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
            f"frame-ancestors {ancestors}"),
    }
    if not request.app[CONFIG].frame_ancestors:
        headers["X-Frame-Options"] = "DENY"
    name = request.match_info.get("file", "crm.html")
    if name not in ("crm.html", "crm.js", "crm.css"):
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC / name, headers=headers)


# --- token management for admins (inside the Telegram-authenticated /api/) -------------------------

async def list_tokens(request: web.Request) -> web.Response:
    return _json([t.public() for t in request.app[STORE].list()])


async def create_token(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "bad_json"}, 400)
    if not isinstance(body, dict):
        return _json({"error": "bad_json"}, 400)
    admin = request[ADMIN_USER]
    days = body.get("days")
    try:
        token, value = request.app[STORE].create(str(body.get("name") or ""), body.get("allowed_ips"),
                                                 body.get("scopes"), int(days) if days else None,
                                                 created_by=f"{admin['id']} {_admin_name(admin)}")
    except (CrmError, ValueError) as e:
        return _json({"error": str(e) if isinstance(e, CrmError) else "bad_days"}, 400)
    log.info("CRM token %s (%s) created by admin %s", token.id, token.name, admin["id"])
    # The only time the value is shown.
    return _json({**token.public(), "token": value})


async def revoke_token(request: web.Request) -> web.Response:
    try:
        token = request.app[STORE].revoke(_int(request.match_info["id"], 0))
    except CrmError:
        return _json({"error": "not_found"}, 404)
    log.info("CRM token %s revoked by admin %s", token.id, request[ADMIN_USER]["id"])
    return _json(token.public())


async def token_audit(request: web.Request) -> web.Response:
    return _json(request.app[STORE].audit_log(_int(request.match_info["id"], 0), _int(request.query.get("limit"), 100)))


def setup(app: web.Application, store: CrmStore, config: CrmConfig | None = None) -> web.Application:
    """Add the CRM routes to the admin panel app (before it starts)."""
    config = config or CrmConfig()
    app[STORE], app[CONFIG], app[LIMIT] = store, config, RateLimit(config.rate_per_minute)
    app.middlewares.append(crm_only)
    app.router.add_get("/crm/", page)
    app.router.add_get("/crm/{file:crm\\.(?:html|js|css)}", page)
    app.router.add_get("/crm/v1/me", me)
    app.router.add_get("/crm/v1/sessions", sessions)
    app.router.add_get("/crm/v1/sessions/{id}", session)
    app.router.add_get("/crm/v1/media/{id}/{n}", crm_media)
    app.router.add_post("/crm/v1/embed-links", embed_link)
    app.router.add_get("/api/crm/tokens", list_tokens)
    app.router.add_post("/api/crm/tokens", create_token)
    app.router.add_delete("/api/crm/tokens/{id}", revoke_token)
    app.router.add_get("/api/crm/tokens/{id}/audit", token_audit)
    return app


# --- command line ------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    from ..config import Settings
    parser = argparse.ArgumentParser(prog="python -m ai_support.admin.crm", description="CRM access tokens")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="issue a token (shown once)")
    c.add_argument("name")
    c.add_argument("--ips", default="", help="allowed addresses or networks, comma separated")
    c.add_argument("--days", type=int, default=None, help="expires after this many days")
    c.add_argument("--scopes", default="", help="media (client photos/voice), pii (unmasked phones)")
    sub.add_parser("list", help="all tokens")
    r = sub.add_parser("revoke", help="revoke a token at once")
    r.add_argument("id", type=int)
    args = parser.parse_args(argv)
    store = CrmStore(Settings.from_env().db_path)
    try:
        if args.cmd == "create":
            token, value = store.create(args.name, args.ips, args.scopes, args.days, created_by="cli")
            print(f"Token #{token.id} for {token.name}. Save it now, it is not shown again:\n{value}")
        elif args.cmd == "list":
            for t in store.list():
                state = "active" if t.active else ("revoked" if t.revoked_at else "expired")
                print(f"#{t.id} {t.prefix}… {t.name} [{state}] scopes={','.join(t.scopes) or '-'} "
                      f"ips={','.join(t.allowed_ips) or 'any'} last_used={t.last_used_at or '-'}")
        else:
            store.revoke(args.id)
            print(f"Token #{args.id} revoked")
    except CrmError as e:
        raise SystemExit(f"error: {e}") from None
    finally:
        store.close()


if __name__ == "__main__":
    main()
