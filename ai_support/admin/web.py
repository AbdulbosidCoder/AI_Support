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

from ..botapi import Call, telegram_call
from ..guides import MAX_IMAGE_BYTES, GuideError
from ..operators import OperatorError
from .auth import telegram_user
from .data import AdminData
from .relay import send_to_client

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
DATA = web.AppKey("data", AdminData)
TOKEN = web.AppKey("token", str)
ADMINS = web.AppKey("admins", frozenset)
# file id -> (bytes, content type): how the panel gets a client's photo or voice message from Telegram.
Fetch = Callable[[str], Awaitable[tuple[bytes, str]]]
FETCH = web.AppKey("fetch", object)
CACHE = web.AppKey("cache", OrderedDict)
# The client bot's Bot API (botapi.Call), to answer a client from the panel.
TELEGRAM = web.AppKey("telegram", object)
SUPPORT_CHAT = web.AppKey("support_chat", object)
MAX_REPLY = 4000
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
    admins = {str(a) for a in request.app[ADMINS]}
    # An admin who answered from the panel shows up here by their replies; they need no adding.
    return _json([{**o, "admin": str(o["user_id"]) in admins} for o in request.app[DATA].operator_list()])


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


async def edit_operator(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "bad_json"}, 400)
    try:
        op = request.app[DATA].operators.update(request.match_info["key"], body.get("name"), body.get("phone"))
    except OperatorError as e:
        return _json({"error": str(e)}, 404 if str(e) == "not_found" else 400)
    log.info("operator %s edited by admin %s", op.key, request[ADMIN_USER]["id"])
    return _json(_operator(op))


async def delete_operator(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    try:
        request.app[DATA].operators.delete(key)
    except OperatorError as e:
        return _json({"error": str(e)}, 404)
    log.info("operator %s deleted by admin %s", key, request[ADMIN_USER]["id"])
    return _json({"ok": True})


# --- quick questions and their guides ------------------------------------------------------------

def _guide_error(e: GuideError) -> web.Response:
    if e.code == "forbidden":
        found = [{"category": v.category, "fragment": v.fragment, "where": w} for v, w in zip(e.violations, e.where)]
        return _json({"error": "forbidden", "violations": found}, 400)
    return _json({"error": e.code}, 404 if e.code == "not_found" else 400)


async def quick(request: web.Request) -> web.Response:
    return _json({"categories": request.app[DATA].quick()})


async def quick_guide(request: web.Request) -> web.Response:
    guide = request.app[DATA].quick_guide(request.match_info["qid"])
    return _json(guide) if guide is not None else _json({"error": "not_found"}, 404)


async def save_quick(request: web.Request) -> web.Response:
    """Create a question (POST /api/quick) or save a question's guide (PUT /api/quick/{qid}).

    Nothing is saved if any text holds something the bot must never say (ai_support/guardrails.py).
    """
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "bad_json"}, 400)
    if not isinstance(body, dict):
        return _json({"error": "bad_json"}, 400)
    data, qid = request.app[DATA], request.match_info.get("qid")
    current = data.quick_guide(qid) if qid else None
    if qid and current is None:
        return _json({"error": "not_found"}, 404)
    cid = current["category"] if current else str(body.get("category") or "")
    try:
        g = data.guides.save(qid, cid, body.get("labels"), body.get("texts"), body.get("steps"),
                             bool(body.get("hidden")), by=_admin_name(request[ADMIN_USER]))
    except GuideError as e:
        return _guide_error(e)
    log.info("quick question %s saved by admin %s", g.qid, request[ADMIN_USER]["id"])
    return _json(data.quick_guide(g.qid))


async def delete_quick(request: web.Request) -> web.Response:
    qid = request.match_info["qid"]
    try:
        request.app[DATA].guides.delete(qid)
    except GuideError as e:
        return _guide_error(e)
    log.info("quick question %s guide deleted by admin %s", qid, request[ADMIN_USER]["id"])
    return _json({"ok": True})


async def add_quick_image(request: web.Request) -> web.Response:
    """A screenshot for a guide, sent as multipart form field "file"."""
    qid = request.match_info["qid"]
    try:
        form = await request.post()
    except (ValueError, web.HTTPRequestEntityTooLarge):
        return _json({"error": "image_too_large"}, 400)
    upload = form.get("file")
    if upload is None or not hasattr(upload, "file"):
        return _json({"error": "no_file"}, 400)
    try:
        request.app[DATA].guides.add_image(qid, upload.file.read(MAX_IMAGE_BYTES + 1))
    except GuideError as e:
        return _guide_error(e)
    return _json(request.app[DATA].quick_guide(qid))


async def quick_image(request: web.Request) -> web.Response:
    stored = request.app[DATA].guides.image(_int(request.match_info["id"], 0))
    if stored is None or stored[0] != request.match_info["qid"]:
        return _json({"error": "not_found"}, 404)
    return web.Response(body=stored[1], content_type=stored[3], headers={"Cache-Control": "private, max-age=3600"})


async def delete_quick_image(request: web.Request) -> web.Response:
    qid = request.match_info["qid"]
    try:
        request.app[DATA].guides.delete_image(qid, _int(request.match_info["id"], 0))
    except GuideError as e:
        return _guide_error(e)
    return _json(request.app[DATA].quick_guide(qid))


async def move_quick_image(request: web.Request) -> web.Response:
    qid = request.match_info["qid"]
    try:
        body = await request.json()
        delta = -1 if int(body.get("delta", 0)) < 0 else 1
    except (ValueError, TypeError, AttributeError):
        return _json({"error": "bad_json"}, 400)
    try:
        request.app[DATA].guides.move_image(qid, _int(request.match_info["id"], 0), delta)
    except GuideError as e:
        return _guide_error(e)
    return _json(request.app[DATA].quick_guide(qid))


def _admin_name(user: dict) -> str:
    return " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "Админ"


async def reply(request: web.Request) -> web.Response:
    """The admin answers the client of a session from the panel; it reaches the client like an operator's reply."""
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "bad_json"}, 400)
    text = str(body.get("text") or "").strip()
    if not text or len(text) > MAX_REPLY:
        return _json({"error": "bad_text"}, 400)
    sid = request.match_info["id"]
    data = request.app[DATA]
    target = data.reply_target(int(sid)) if sid.isdigit() else None
    if target is None:
        return _json({"error": "not_found"}, 404)
    if target["channel"] != "telegram":
        return _json({"error": "unsupported_channel"}, 400)
    admin = request[ADMIN_USER]
    admin_id, name = str(admin["id"]), _admin_name(admin)
    try:
        session_id = await send_to_client(data, request.app[TELEGRAM], request.app[SUPPORT_CHAT], target, text,
                                          admin_id, name)
    except Exception as e:  # blocked by the client, no token, network: nothing was sent, nothing is saved
        log.warning("admin reply to %s not sent: %s", target["client_id"], e)
        return _json({"error": "not_sent", "detail": str(e)}, 502)
    log.info("admin %s replied to client %s (session %s)", admin_id, target["client_id"], session_id)
    return _json({"ok": True, "session_id": session_id})


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
               fetch: Fetch | None = None, support_chat_id: int | None = None,
               telegram: Call | None = None) -> web.Application:
    """bot_token checks the admin's launch data; client_bot_token downloads what clients sent."""
    # Screenshots for guides are uploaded through the panel: a little over one image's limit.
    app = web.Application(middlewares=[admin_only], client_max_size=MAX_IMAGE_BYTES + 256 * 1024)
    app[DATA], app[TOKEN], app[ADMINS] = data, bot_token, frozenset(admin_ids)
    app[FETCH], app[CACHE] = fetch or telegram_fetch(client_bot_token), OrderedDict()
    app[TELEGRAM], app[SUPPORT_CHAT] = telegram or telegram_call(client_bot_token), support_chat_id
    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/me", me)
    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/operators", operators)
    app.router.add_post("/api/operators", add_operator)
    app.router.add_post("/api/operators/{key}/active", set_operator_active)
    app.router.add_patch("/api/operators/{key}", edit_operator)
    app.router.add_delete("/api/operators/{key}", delete_operator)
    app.router.add_post("/api/sessions/{id}/reply", reply)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get("/api/sessions/{id}", session)
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/clients", clients)
    app.router.add_get("/api/clients/{id}/stats", client_stats)
    app.router.add_get("/api/media/{id}/{n}", media)
    app.router.add_get("/api/quick", quick)
    app.router.add_post("/api/quick", save_quick)
    app.router.add_get("/api/quick/{qid}", quick_guide)
    app.router.add_put("/api/quick/{qid}", save_quick)
    app.router.add_delete("/api/quick/{qid}", delete_quick)
    app.router.add_post("/api/quick/{qid}/images", add_quick_image)
    app.router.add_get("/api/quick/{qid}/images/{id}", quick_image)
    app.router.add_delete("/api/quick/{qid}/images/{id}", delete_quick_image)
    app.router.add_post("/api/quick/{qid}/images/{id}/move", move_quick_image)
    return app
