"""The admin panel: a Telegram mini app served over HTTP (published through a Cloudflare Tunnel).

The page itself is static; every /api request carries the mini app's signed launch data in the
X-Telegram-Init-Data header and is answered only for users listed in ADMIN_IDS.
Photos and voice messages from clients are fetched from Telegram with the client bot's token
(TELEGRAM_BOT_TOKEN) when the chat is opened; nothing is copied to disk.
"""
from __future__ import annotations

import json
import logging
import mimetypes
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from ..operators import OperatorError
from .auth import telegram_user
from .data import AdminData

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
DATA = web.AppKey("data", AdminData)
TOKEN = web.AppKey("token", str)
ADMINS = web.AppKey("admins", frozenset)
# file id -> (bytes, content type): how the panel gets a client's photo or voice message from Telegram.
Fetch = Callable[[str], Awaitable[tuple[bytes, str]]]
FETCH = web.AppKey("fetch", object)
CACHE = web.AppKey("cache", OrderedDict)
CACHE_SIZE = 64
MAX_FILE = 20 * 1024 * 1024  # what the Bot API lets a bot download
# The admin who sent the request (the Telegram user from the launch data).
ADMIN_USER = web.RequestKey("admin", dict) if hasattr(web, "RequestKey") else "admin"


def _json(payload, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


@web.middleware
async def admin_only(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    user = telegram_user(request.headers.get("X-Telegram-Init-Data", ""), request.app[TOKEN])
    if user is None:
        return _json({"error": "unauthorized"}, 401)
    if user["id"] not in request.app[ADMINS]:
        return _json({"error": "forbidden", "user_id": user["id"]}, 403)
    request[ADMIN_USER] = user
    return await handler(request)


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def overview(request: web.Request) -> web.Response:
    return _json(request.app[DATA].overview())


async def stats(request: web.Request) -> web.Response:
    days = _int(request.query.get("days"), 14)
    return _json(request.app[DATA].stats(max(1, min(days, 90))))


async def client_stats(request: web.Request) -> web.Response:
    found = request.app[DATA].client_stats(request.match_info["id"])
    return _json(found) if found else _json({"error": "not_found"}, 404)


async def operators(request: web.Request) -> web.Response:
    return _json(request.app[DATA].operator_list())


async def add_operator(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "bad_json"}, 400)
    admin = request[ADMIN_USER]
    try:
        # "phone" (preferred) or "user_id": the operator registers in the client bot with that phone.
        value = str(body.get("phone") or body.get("user_id") or "")
        op = request.app[DATA].add_operator(value, str(body.get("name", "")), body.get("username") or None,
                                            added_by=str(admin["id"]))
    except OperatorError as e:
        return _json({"error": str(e)}, 400)
    log.info("operator %s added by admin %s", op.key, admin["id"])
    return _json(_operator(op))


async def set_operator_active(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "bad_json"}, 400)
    try:
        op = request.app[DATA].operators.set_active(request.match_info["key"], bool(body.get("active")))
    except OperatorError as e:
        return _json({"error": str(e)}, 404)
    return _json(_operator(op))


def _operator(op) -> dict:
    return {**op.__dict__, "key": op.key, "linked": op.linked}


def _int(value: str | None, default: int) -> int:
    return int(value) if value and value.isdigit() else default


async def sessions(request: web.Request) -> web.Response:
    q = request.query
    return _json(request.app[DATA].sessions(q.get("operator") or None, q.get("status") or None,
                                            q.get("client") or None, _int(q.get("limit"), 100)))


async def session(request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    found = request.app[DATA].session(int(sid)) if sid.isdigit() else None
    return _json(found) if found else _json({"error": "not_found"}, 404)


async def clients(request: web.Request) -> web.Response:
    q = request.query
    return _json(request.app[DATA].clients(q.get("q", ""), _int(q.get("limit"), 100)))


class MediaError(Exception):
    pass


def telegram_fetch(bot_token: str) -> Fetch:
    """Download a file the client sent to the client bot, by its file id."""
    api = f"https://api.telegram.org/bot{bot_token}"
    files = f"https://api.telegram.org/file/bot{bot_token}"

    async def fetch(file_id: str) -> tuple[bytes, str]:
        if not bot_token:
            raise MediaError("no_token")
        async with ClientSession(timeout=ClientTimeout(total=30)) as http:
            async with http.get(f"{api}/getFile", params={"file_id": file_id}) as res:
                body = await res.json()
            path = (body.get("result") or {}).get("file_path")
            if not body.get("ok") or not path:
                raise MediaError("not_found")
            async with http.get(f"{files}/{path}") as res:
                if res.status != 200:
                    raise MediaError("not_found")
                data = await res.read()
        return data, _content_type(path)

    return fetch


def _content_type(path: str) -> str:
    if path.endswith((".oga", ".ogg")):
        return "audio/ogg"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


async def media(request: web.Request) -> web.Response:
    """One photo, voice message or file of a logged client message."""
    mid, n = request.match_info["id"], request.match_info["n"]
    files = request.app[DATA].chatlog.files(int(mid)) if mid.isdigit() else []
    if not n.isdigit() or int(n) >= len(files):
        return _json({"error": "not_found"}, 404)
    file_id = files[int(n)]
    cache = request.app[CACHE]
    if file_id in cache:
        cache.move_to_end(file_id)
        data, ctype = cache[file_id]
    else:
        try:
            data, ctype = await request.app[FETCH](file_id)
        except MediaError as e:
            return _json({"error": str(e)}, 404)
        except Exception as e:  # Telegram or the network: the chat itself still shows
            log.warning("media %s not fetched: %s", file_id, e)
            return _json({"error": "unavailable"}, 502)
        if len(data) <= MAX_FILE:
            cache[file_id] = (data, ctype)
            while len(cache) > CACHE_SIZE:
                cache.popitem(last=False)
    return web.Response(body=data, content_type=ctype, headers={"Cache-Control": "private, max-age=86400"})


async def me(request: web.Request) -> web.Response:
    return _json(request[ADMIN_USER])


def create_app(data: AdminData, bot_token: str, admin_ids: frozenset[int], client_bot_token: str = "",
               fetch: Fetch | None = None) -> web.Application:
    """bot_token checks the admin's launch data; client_bot_token downloads what clients sent."""
    app = web.Application(middlewares=[admin_only])
    app[DATA], app[TOKEN], app[ADMINS] = data, bot_token, frozenset(admin_ids)
    app[FETCH], app[CACHE] = fetch or telegram_fetch(client_bot_token), OrderedDict()
    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/me", me)
    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/operators", operators)
    app.router.add_post("/api/operators", add_operator)
    app.router.add_post("/api/operators/{key}/active", set_operator_active)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get("/api/sessions/{id}", session)
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/clients", clients)
    app.router.add_get("/api/clients/{id}/stats", client_stats)
    app.router.add_get("/api/media/{id}/{n}", media)
    return app
