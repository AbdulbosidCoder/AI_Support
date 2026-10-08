"""Run the admin bot and its web panel: `python -m ai_support.admin`."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiohttp import web

from ..config import Settings
from .bot import AdminBot, set_panel_button
from .data import AdminData
from .web import create_app

log = logging.getLogger("ai_support.admin")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    if not settings.admin_bot_token:
        # Not an error: the admin side is optional, and the container should not restart in a loop.
        log.warning("ADMIN_BOT_TOKEN is not set: admin bot and panel are off")
        return
    if not settings.admin_ids:
        log.warning("ADMIN_IDS is empty: nobody can use the admin bot; it replies with each user's id")
    if not settings.admin_domain:
        log.warning("ADMIN_DOMAIN is not set: the bot works, but the web panel button is hidden")
    data = AdminData(settings.db_path)
    runner = web.AppRunner(create_app(data, settings.admin_bot_token, settings.admin_ids,
                                       settings.telegram_token, support_chat_id=settings.support_chat_id))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.admin_port).start()
    log.info("admin panel on port %s, public address %s", settings.admin_port, settings.admin_url or "-")
    bot = Bot(settings.admin_bot_token)
    dp = Dispatcher()
    dp.include_router(AdminBot(settings, data).router)
    await set_panel_button(bot, settings)
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        data.close()


if __name__ == "__main__":
    asyncio.run(main())
