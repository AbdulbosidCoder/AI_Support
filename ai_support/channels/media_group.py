"""Collects the messages of one Telegram album (media group) so they are answered once, together."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

M = TypeVar("M")


class MediaGroupCollector(Generic[M]):
    """Telegram delivers an album as separate messages sharing `media_group_id`.

    The first message starts a short timer; messages arriving before it fires join the group,
    then `handler` is called once with all of them.
    """

    def __init__(self, handler: Callable[[list[M]], Awaitable[None]], delay: float = 1.0):
        self._handler = handler
        self._delay = delay
        self._groups: dict[str, list[M]] = {}

    def add(self, group_id: str, message: M) -> None:
        if group_id in self._groups:
            self._groups[group_id].append(message)
            return
        self._groups[group_id] = [message]
        asyncio.get_running_loop().create_task(self._flush(group_id))

    async def _flush(self, group_id: str) -> None:
        await asyncio.sleep(self._delay)
        messages = self._groups.pop(group_id, [])
        if messages:
            await self._handler(messages)
