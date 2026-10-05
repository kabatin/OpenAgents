#!/usr/bin/env python3
"""起動時の取り込み（backfill）が archive.since の起点を守るか。

Discord の履歴取得は偽物で代用し、どの範囲を取りに行ったか（after/before）を見る。
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest import mock

import discord

from core import archive_window as aw
from core import db
from platforms.discord import archiving

SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)
SINCE_SF = discord.utils.time_snowflake(SINCE)


class FakeChannel:
    """履歴取得の範囲（after/before）を記録する偽チャンネル。"""

    def __init__(self, cid, messages):
        self.id = cid
        self.name = f"ch{cid}"
        self._msgs = messages
        self.calls = []

    def permissions_for(self, _member):
        return NS(read_message_history=True)

    def history(self, *, limit=None, after=None, before=None, oldest_first=None):
        self.calls.append({"after": getattr(after, "id", None),
                           "before": getattr(before, "id", None)})
        lo = getattr(after, "id", None)
        hi = getattr(before, "id", None)
        picked = [m for m in self._msgs
                  if (lo is None or m.id > lo) and (hi is None or m.id < hi)]

        async def gen():
            for m in sorted(picked, key=lambda m: m.id, reverse=not oldest_first):
                yield m
        return gen()


class BackfillWindowTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.path)
        self.stored = []
        p = mock.patch.object(archiving, "store_message",
                              lambda conn, m: self._store(conn, m))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        os.unlink(self.path)

    def _store(self, conn, m):
        self.stored.append(m.id)
        db.upsert_channel(conn, id=m.channel_id, name="c", type="text")
        db.upsert_user(conn, id=1, name="u", display_name="人", is_bot=False)
        db.insert_message(conn, id=m.id, channel_id=m.channel_id, author_id=1,
                          content="x", created_at="2026-07-01T00:00:00+00:00")

    def _run(self, channel, since):
        asyncio.run(archiving.backfill_channel(channel, None, self.path, since))

    @staticmethod
    def _msgs(cid, ids):
        return [NS(id=i, channel_id=cid) for i in ids]

    def test_first_run_starts_at_since(self):
        old, new = SINCE_SF - 1000, SINCE_SF + 1000
        ch = FakeChannel(10, self._msgs(10, [old, new]))
        self._run(ch, SINCE)
        self.assertEqual(self.stored, [new])          # 起点より前は取らない
        self.assertEqual(ch.calls, [{"after": SINCE_SF, "before": None}])
        with db.connect(self.path) as conn:
            self.assertEqual(db.get_archive_coverage(conn, 10), aw.since_key(SINCE))

    def test_restart_only_fetches_the_gap(self):
        a, b = SINCE_SF + 1000, SINCE_SF + 2000
        ch = FakeChannel(10, self._msgs(10, [a]))
        self._run(ch, SINCE)
        ch._msgs.append(NS(id=b, channel_id=10))    # 停止中に増えた発言
        ch.calls.clear()
        self._run(ch, SINCE)
        self.assertEqual(self.stored, [a, b])
        # 2回目は差分だけ。遡りは済んでいるので古い方へは行かない
        self.assertEqual(ch.calls, [{"after": a, "before": None}])

    def test_widening_fetches_only_the_older_part(self):
        older, a = SINCE_SF - 1000, SINCE_SF + 1000
        ch = FakeChannel(10, self._msgs(10, [older, a]))
        self._run(ch, SINCE)
        wider = datetime(2026, 6, 1, tzinfo=timezone.utc)
        ch.calls.clear()
        self._run(ch, wider)
        self.assertEqual(sorted(self.stored), [older, a])
        self.assertIn({"after": discord.utils.time_snowflake(wider), "before": a},
                      ch.calls)

    def test_all_on_existing_archive_scans_once(self):
        # 既存の利用者（起点なし＝すべて）は、一度だけ古い方を確かめて以後は差分のみ
        a = SINCE_SF
        ch = FakeChannel(10, self._msgs(10, [a]))
        self._run(ch, None)
        ch.calls.clear()
        self._run(ch, None)
        self.assertEqual(ch.calls, [{"after": a, "before": None}])


if __name__ == "__main__":
    unittest.main()
