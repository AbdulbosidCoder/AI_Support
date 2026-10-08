"""Read-only CRM access to the chats: tokens, embed links, masking, IP allow-list, rate limit, audit."""
import asyncio
import json
import time

from aiohttp.test_utils import TestClient, TestServer

from ai_support.admin.auth import sign
from ai_support.admin.crm import CrmConfig, CrmError, CrmStore, RateLimit, main, setup
from ai_support.admin.data import AdminData
from ai_support.admin.web import create_app
from ai_support.chatlog import ChatLog
from ai_support.feedback import FeedbackStore
from ai_support.users import UserStore

TOKEN = "123:ABC"
ADMIN = 42


def admin_init_data(user_id=ADMIN):
    return sign({"auth_date": str(int(time.time())), "query_id": "q",
                 "user": json.dumps({"id": user_id, "first_name": "Admin"})}, TOKEN)


def seed(db):
    """Two clients with one session each; client 7 sent a photo and a card number."""
    users, feedback, log = UserStore(db), FeedbackStore(db), ChatLog(db)
    users.touch("telegram", "7", "7", "anvar", "Anvar")
    users.set_phone("telegram", "7", "+998901112233")
    users.touch("telegram", "8", "8", "bobur", "Bobur")
    c7 = feedback.bot_answered("telegram", "7", "uz_latn", "cards")
    log.add("telegram", "7", "client", "Karta 8600 1234 5678 9012 qo'shilmayapti", conversation_id=c7.id)
    photo = log.add("telegram", "7", "client", "[фото]", kind="photo", files=["AgACfile"], conversation_id=c7.id)
    log.add("telegram", "7", "bot", "Javob", conversation_id=c7.id)
    c8 = feedback.bot_answered("telegram", "8", "ru", "payments")
    log.add("telegram", "8", "client", "Платёж завис", conversation_id=c8.id)
    return c7.id, c8.id, photo


def make_app(db, config=None, fetch=None):
    async def default_fetch(file_id):
        return b"PNGDATA", "image/png"
    store = CrmStore(db)
    app = create_app(AdminData(db), TOKEN, frozenset({ADMIN}), fetch=fetch or default_fetch)
    setup(app, store, config or CrmConfig(public_url="https://admin.example.uz/"))
    return app, store


def call(app, *requests):
    """Send (method, path, headers, body) in order; returns [(status, body, headers)].

    A request may also be a function of the results so far that returns such a tuple.
    """
    async def run():
        out = []
        async with TestClient(TestServer(app)) as client:
            for req in requests:
                method, path, headers, body = req(out) if callable(req) else req
                res = await client.request(method, path, headers=headers or {}, json=body)
                is_json = res.content_type == "application/json"
                out.append((res.status, await res.json() if is_json else await res.read(), res.headers))
        return out
    return asyncio.run(run())


def bearer(key, **extra):
    return {"Authorization": f"Bearer {key}", **extra}


# --- tokens ----------------------------------------------------------------------------------------

def test_token_is_shown_once_and_stored_only_as_hash(tmp_path):
    db = tmp_path / "bot.sqlite3"
    store = CrmStore(db)
    token, value = store.create("Support CRM")
    assert value.startswith("crm_") and len(value) > 40
    raw = db.read_bytes()
    assert value.encode() not in raw  # only the hash is in the database
    assert store.access(value).token.id == token.id
    assert store.access(value + "x") is None and store.access("crm_guess") is None
    assert "token" not in token.public() and "token_hash" not in token.public()


def test_revoked_or_expired_token_and_its_links_stop_working(tmp_path):
    store = CrmStore(tmp_path / "bot.sqlite3")
    token, value = store.create("CRM")
    link, _ = store.ticket(token, 60)
    assert store.access(link) is not None
    store.revoke(token.id)
    assert store.access(value) is None and store.access(link) is None
    assert not store.get(token.id).active
    short, _ = store.create("Short", days=1)
    assert store.access(store.ticket(short, 60)[0], now=time.time() + 2 * 3600) is None  # link expired


def test_embed_link_cannot_be_forged_or_widened(tmp_path):
    store = CrmStore(tmp_path / "bot.sqlite3")
    token, _ = store.create("CRM")
    link, _ = store.ticket(token, 60, session_id=5)
    body, sig = link[len("crmt_"):].split(".")
    forged_body = body[:-2] + ("AA" if body[-2:] != "AA" else "BB")
    assert store.access(f"crmt_{forged_body}.{sig}") is None
    assert store.access(f"crmt_{body}.") is None
    assert store.access(link).session_id == 5
    # Another database has another signing secret.
    assert CrmStore(tmp_path / "other.sqlite3").access(link) is None


def test_bad_token_settings_refused(tmp_path):
    store = CrmStore(tmp_path / "bot.sqlite3")
    for kwargs in ({"name": ""}, {"name": "x", "allowed_ips": "not-an-ip"}, {"name": "x", "scopes": "write"},
                   {"name": "x", "days": 0}):
        try:
            store.create(**kwargs)
        except CrmError:
            continue
        raise AssertionError(kwargs)
    token, _ = store.create("x", allowed_ips="203.0.113.0/24, 198.51.100.7", scopes="media")
    assert token.allowed_ips == ("203.0.113.0/24", "198.51.100.7/32") and token.scopes == ("media",)
    assert token.allows_ip("203.0.113.9") and token.allows_ip("198.51.100.7")
    assert not token.allows_ip("198.51.100.8") and not token.allows_ip(None)


def test_rate_limit_window():
    limit = RateLimit(2)
    assert limit.allow(1, now=0) and limit.allow(1, now=1)
    assert not limit.allow(1, now=2)
    assert limit.allow(2, now=2)  # per key
    assert limit.allow(1, now=61)


# --- API -------------------------------------------------------------------------------------------

def test_api_lists_and_shows_sessions_masked_and_read_only(tmp_path):
    db = tmp_path / "bot.sqlite3"
    c7, c8, photo = seed(db)
    app, store = make_app(db)
    _, value = store.create("CRM")
    (s1, listed, h1), (s2, chat, _), (s3, img, _), (s4, *_), (s5, *_), (s6, *_) = call(
        app, ("GET", "/crm/v1/sessions", bearer(value), None),
        ("GET", f"/crm/v1/sessions/{c7}", bearer(value), None),
        ("GET", f"/crm/v1/media/{photo}/0", bearer(value), None),
        ("POST", f"/api/sessions/{c7}/reply", bearer(value), {"text": "hi"}),
        ("DELETE", "/api/operators/1", bearer(value), None),
        ("POST", f"/crm/v1/sessions/{c7}", bearer(value), None))
    assert s1 == 200 and [s["id"] for s in listed["sessions"]] == [c8, c7]
    assert h1["Cache-Control"] == "no-store" and h1["X-Content-Type-Options"] == "nosniff"
    anvar = listed["sessions"][1]
    assert anvar["client_phone"] == "+*** ** *** ** 33"  # masked without the "pii" scope
    assert s2 == 200 and "1234 5678" not in json.dumps(chat) and "AgACfile" not in json.dumps(chat)
    assert [m["files"] for m in chat["messages"]] == [0, 1, 0]
    assert all(m["media"] == [] for m in chat["messages"])
    assert s3 == 403  # photos need the "media" scope
    assert s4 == 401 and s5 == 401  # a CRM token opens nothing of the admin API
    assert s6 == 405  # nothing to write


def test_media_and_phone_with_scopes(tmp_path):
    db = tmp_path / "bot.sqlite3"
    c7, _, photo = seed(db)
    app, store = make_app(db)
    _, value = store.create("CRM", scopes="media,pii")
    (_, chat, _), (status, img, headers) = call(
        app, ("GET", f"/crm/v1/sessions/{c7}", bearer(value), None),
        ("GET", f"/crm/v1/media/{photo}/0", bearer(value), None))
    assert chat["client_phone"] == "+998901112233"
    assert chat["messages"][1]["media"] == [f"/crm/v1/media/{photo}/0"]
    assert status == 200 and img == b"PNGDATA" and headers["Cache-Control"] == "no-store"


def test_unknown_key_refused_and_audited(tmp_path):
    db = tmp_path / "bot.sqlite3"
    seed(db)
    app, store = make_app(db)
    token, value = store.create("CRM")
    results = call(app, ("GET", "/crm/v1/sessions", None, None),
                   ("GET", "/crm/v1/sessions", bearer("crm_wrong"), None),
                   ("GET", "/crm/v1/sessions", {"X-Telegram-Init-Data": admin_init_data()}, None),
                   ("GET", "/crm/v1/sessions", bearer(value), None))
    assert [r[0] for r in results] == [401, 401, 401, 200]
    log = store.audit_log()
    assert [e["status"] for e in log] == [200, 401, 401, 401]
    assert log[0]["token_id"] == token.id and store.get(token.id).last_used_at


def test_ip_allow_list_uses_cloudflare_address(tmp_path):
    db = tmp_path / "bot.sqlite3"
    seed(db)
    app, store = make_app(db)
    _, value = store.create("CRM", allowed_ips="203.0.113.0/24")
    ok, other, missing = call(
        app, ("GET", "/crm/v1/sessions", bearer(value, **{"CF-Connecting-IP": "203.0.113.10"}), None),
        ("GET", "/crm/v1/sessions", bearer(value, **{"CF-Connecting-IP": "198.51.100.1"}), None),
        ("GET", "/crm/v1/sessions", bearer(value), None))  # direct call from 127.0.0.1
    assert ok[0] == 200 and other[0] == 403 and missing[0] == 403
    app2, store2 = make_app(tmp_path / "bot.sqlite3", CrmConfig(trust_proxy=False))
    _, v2 = store2.create("CRM2", allowed_ips="203.0.113.0/24")
    (status, *_), = call(app2, ("GET", "/crm/v1/sessions", bearer(v2, **{"CF-Connecting-IP": "203.0.113.10"}), None))
    assert status == 403  # the header is ignored when the proxy is not trusted


def test_embed_link_works_from_staff_browsers_outside_the_allow_list(tmp_path):
    db = tmp_path / "bot.sqlite3"
    seed(db)
    app, store = make_app(db)
    _, value = store.create("CRM", allowed_ips="203.0.113.10")
    server = bearer(value, **{"CF-Connecting-IP": "203.0.113.10"})
    made, from_staff, token_from_staff = call(
        app, ("POST", "/crm/v1/embed-links", server, {}),
        lambda out: ("GET", "/crm/v1/sessions",
                     bearer(out[0][1]["url"].split("#key=")[1], **{"CF-Connecting-IP": "198.51.100.1"}), None),
        ("GET", "/crm/v1/sessions", bearer(value, **{"CF-Connecting-IP": "198.51.100.1"}), None))
    assert made[0] == 200 and from_staff[0] == 200 and token_from_staff[0] == 403


def test_plain_http_and_rate_limit_refused(tmp_path):
    db = tmp_path / "bot.sqlite3"
    seed(db)
    app, store = make_app(db, CrmConfig(rate_per_minute=2))
    _, value = store.create("CRM")
    results = call(app, ("GET", "/crm/v1/me", bearer(value, **{"X-Forwarded-Proto": "http"}), None),
                   ("GET", "/crm/v1/me", bearer(value), None), ("GET", "/crm/v1/me", bearer(value), None),
                   ("GET", "/crm/v1/me", bearer(value), None))
    assert [r[0] for r in results] == [403, 200, 200, 429]
    assert results[3][2]["Retry-After"] == "60"


def test_guessing_keys_is_rate_limited_by_address(tmp_path):
    db = tmp_path / "bot.sqlite3"
    app, store = make_app(db, CrmConfig(rate_per_minute=3))
    results = call(app, *[("GET", "/crm/v1/me", bearer(f"crm_guess{i}"), None) for i in range(5)])
    assert [r[0] for r in results] == [401, 401, 401, 429, 429]
    assert len(store.audit_log()) == 3  # refused guesses do not flood the log


# --- embed links and page --------------------------------------------------------------------------

def test_embed_link_for_one_session_sees_only_it(tmp_path):
    db = tmp_path / "bot.sqlite3"
    c7, c8, photo = seed(db)
    app, store = make_app(db)
    _, value = store.create("CRM", scopes="media")
    def link(out):
        return bearer(out[0][1]["url"].split("#key=")[1])

    (status, made, _), me, listed, own, other, own_photo, more = call(
        app, ("POST", "/crm/v1/embed-links", bearer(value), {"session_id": c7, "minutes": 30}),
        lambda out: ("GET", "/crm/v1/me", link(out), None), lambda out: ("GET", "/crm/v1/sessions", link(out), None),
        lambda out: ("GET", f"/crm/v1/sessions/{c7}", link(out), None),
        lambda out: ("GET", f"/crm/v1/sessions/{c8}", link(out), None),
        lambda out: ("GET", f"/crm/v1/media/{photo}/0", link(out), None),
        lambda out: ("POST", "/crm/v1/embed-links", link(out), {}))
    assert status == 200 and made["url"].startswith("https://admin.example.uz/crm/#key=crmt_")
    assert me[1]["session_id"] == c7
    assert [s["id"] for s in listed[1]["sessions"]] == [c7]
    assert own[0] == 200 and other[0] == 404 and own_photo[0] == 200
    assert more[0] == 403  # a link cannot make links


def test_embed_link_for_other_client_cannot_open_photo(tmp_path):
    db = tmp_path / "bot.sqlite3"
    c7, c8, photo = seed(db)
    app, store = make_app(db)
    token, _ = store.create("CRM", scopes="media")
    link, _ = store.ticket(token, 60, client_id="8")
    (status, *_), (listed_status, listed, _) = call(
        app, ("GET", f"/crm/v1/media/{photo}/0", bearer(link), None),
        ("GET", "/crm/v1/sessions?client=7", bearer(link), None))
    assert status == 404
    assert listed_status == 200 and [s["id"] for s in listed["sessions"]] == [c8]


def test_embed_link_minutes_checked(tmp_path):
    app, store = make_app(tmp_path / "bot.sqlite3")
    _, value = store.create("CRM")
    results = call(app, ("POST", "/crm/v1/embed-links", bearer(value), {"minutes": 0}),
                   ("POST", "/crm/v1/embed-links", bearer(value), {"minutes": 100000}),
                   ("POST", "/crm/v1/embed-links", bearer(value), {"session_id": "x"}))
    assert [r[0] for r in results] == [400, 400, 400]


def test_page_is_framed_only_by_the_crm(tmp_path):
    app, _ = make_app(tmp_path / "bot.sqlite3", CrmConfig(frame_ancestors=("https://crm.example.uz",)))
    (status, body, headers), (js_status, js, js_headers), (bad, *_) = call(
        app, ("GET", "/crm/", None, None), ("GET", "/crm/crm.js", None, None), ("GET", "/crm/index.html", None, None))
    assert status == 200 and b"crm.js" in body
    assert "frame-ancestors https://crm.example.uz" in headers["Content-Security-Policy"]
    assert "X-Frame-Options" not in headers and headers["Referrer-Policy"] == "no-referrer"
    assert js_status == 200 and b"sessionStorage" in js
    assert bad == 404
    closed, _ = make_app(tmp_path / "other.sqlite3")
    (_, _, h), = call(closed, ("GET", "/crm/", None, None))
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"] and h["X-Frame-Options"] == "DENY"


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("CRM_FRAME_ANCESTORS", "https://crm.example.uz http://insecure.example, https://b.uz")
    monkeypatch.setenv("CRM_RATE_PER_MINUTE", "30")
    monkeypatch.setenv("CRM_TRUST_PROXY", "0")
    c = CrmConfig.from_env("https://admin.example.uz/")
    assert c.frame_ancestors == ("https://crm.example.uz", "https://b.uz")  # https only
    assert c.rate_per_minute == 30 and not c.trust_proxy


# --- managed by admins -----------------------------------------------------------------------------

def test_admins_create_list_and_revoke_tokens(tmp_path):
    db = tmp_path / "bot.sqlite3"
    seed(db)
    app, store = make_app(db)
    admin = {"X-Telegram-Init-Data": admin_init_data()}
    stranger = {"X-Telegram-Init-Data": admin_init_data(7)}
    def key(out):
        return bearer(out[0][1]["token"], **{"CF-Connecting-IP": "203.0.113.4"})

    created, listed, denied, used, revoked, after, audit, bad = call(
        app, ("POST", "/api/crm/tokens", admin, {"name": "Bitrix", "allowed_ips": ["203.0.113.0/24"], "days": 365}),
        ("GET", "/api/crm/tokens", admin, None), ("POST", "/api/crm/tokens", stranger, {"name": "x"}),
        lambda out: ("GET", "/crm/v1/me", key(out), None),
        lambda out: ("DELETE", f"/api/crm/tokens/{out[0][1]['id']}", admin, None),
        lambda out: ("GET", "/crm/v1/me", key(out), None),
        lambda out: ("GET", f"/api/crm/tokens/{out[0][1]['id']}/audit", admin, None),
        ("POST", "/api/crm/tokens", admin, {"name": "x", "scopes": ["write"]}))
    assert created[0] == 200 and created[1]["token"].startswith("crm_")
    assert "42 Admin" in created[1]["created_by"] and created[1]["expires_at"]
    assert listed[1][0]["name"] == "Bitrix" and "token" not in listed[1][0]
    assert denied[0] == 403
    assert used[0] == 200 and revoked[1]["active"] is False and after[0] == 401
    assert [e["status"] for e in audit[1]] == [200] and audit[1][0]["ip"] == "203.0.113.4"
    assert bad == (400, {"error": "bad_scope"}, bad[2])


def test_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.sqlite3"))
    main(["create", "CRM", "--ips", "10.0.0.0/8", "--scopes", "media"])
    value = capsys.readouterr().out.strip().splitlines()[-1]
    assert value.startswith("crm_")
    main(["list"])
    out = capsys.readouterr().out
    assert "CRM [active]" in out and "10.0.0.0/8" in out and value not in out
    main(["revoke", "1"])
    main(["list"])
    assert "[revoked]" in capsys.readouterr().out
