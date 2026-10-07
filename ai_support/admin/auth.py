"""Check that a request to the admin panel comes from the admin bot's mini app, opened by an admin.

Telegram signs the mini app's launch data (initData) with the bot token; see
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

# Launch data older than this is refused: reopen the panel from the bot.
MAX_AGE_SECONDS = 24 * 3600


def telegram_user(init_data: str, bot_token: str, now: float | None = None,
                  max_age: int = MAX_AGE_SECONDS) -> dict | None:
    """The Telegram user from valid, fresh initData; None if the signature or age is wrong."""
    if not init_data or not bot_token:
        return None
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(fields.get("auth_date", "0"))
        user = json.loads(fields.get("user", "{}"))
    except ValueError:
        return None
    if (now if now is not None else time.time()) - auth_date > max_age:
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None


def sign(fields: dict[str, str], bot_token: str) -> str:
    """initData for `fields` signed like Telegram does (for tests and local checks)."""
    from urllib.parse import urlencode
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})
