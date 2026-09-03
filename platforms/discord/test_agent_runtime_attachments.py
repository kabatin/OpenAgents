#!/usr/bin/env python3
"""agent_runtime の添付収集（リンク先投稿の添付を含める）のユニットテスト。"""

import asyncio
import unittest
from types import SimpleNamespace as NS

from platforms.discord import agent_runtime


class LinkedAttachmentsTest(unittest.TestCase):
    """本文中のメッセージリンクが指す投稿の添付を読解対象に含める。
    Discordオブジェクトは最小の偽物で代用する。"""

    def _fake_message(self, text, *, guild_id=1, channel_id=200):
        att = NS(id=9001, filename="日程.pdf", content_type="application/pdf",
                 size=1000)
        target = NS(id=555, attachments=[att])

        async def fetch_message(mid):
            if mid == 555:
                return target
            raise RuntimeError("not found")
        other_channel = NS(id=300, fetch_message=fetch_message)
        same_channel = NS(id=channel_id, fetch_message=fetch_message)

        async def fetch_channel(cid):
            raise RuntimeError("no such channel")
        guild = NS(id=guild_id,
                   get_channel=lambda cid: other_channel if cid == 300 else None,
                   fetch_channel=fetch_channel)
        msg = NS(id=1, clean_content=text, attachments=[], reference=None,
                 channel=same_channel, guild=guild)
        return msg, att

    def test_link_to_other_channel_pulls_attachment(self):
        msg, att = self._fake_message(
            "https://discord.com/channels/1/300/555 このpdf読んで")
        out = asyncio.run(agent_runtime._collect_attachments(msg))
        self.assertEqual([a.id for a in out], [att.id])

    def test_other_guild_link_ignored(self):
        msg, _att = self._fake_message(
            "https://discord.com/channels/999/300/555 これ")
        out = asyncio.run(agent_runtime._collect_attachments(msg))
        self.assertEqual(out, [])

    def test_missing_message_is_silent(self):
        msg, _att = self._fake_message(
            "https://discord.com/channels/1/300/777 消えた投稿")
        out = asyncio.run(agent_runtime._collect_attachments(msg))
        self.assertEqual(out, [])

    def test_self_link_skipped(self):
        msg, _att = self._fake_message(
            "https://discord.com/channels/1/200/1 自分自身")
        out = asyncio.run(agent_runtime._collect_attachments(msg))
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
