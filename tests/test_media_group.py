import asyncio

from ai_support.channels.media_group import MediaGroupCollector


def test_album_items_are_answered_once_together():
    got = []

    async def handler(items):
        got.append(items)

    async def scenario():
        c = MediaGroupCollector(handler, delay=0.05)
        c.add("g1", "photo1")
        c.add("g1", "photo2")
        c.add("g2", "other")
        await asyncio.sleep(0.15)

    asyncio.run(scenario())
    assert sorted(got) == [["other"], ["photo1", "photo2"]]
