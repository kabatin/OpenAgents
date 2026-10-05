#!/usr/bin/env python3
"""過去ログをどこから記録するか（archive.since）のユニットテスト。

巨大なサーバーにセットアップしたとき、何年分もの履歴を無条件に取り込むと
数時間〜数日かかる。セットアップで「取り込まない／直近N日／すべて」を選ばせ、
その起点を archive.since に保存する。起点より前は取りに行かない。
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from core import archive_window as aw
from core import config
from core import db


class ParseSinceTest(unittest.TestCase):
    def test_missing_means_everything(self):
        # 既存の利用者は従来どおり全部（アップデートで急に過去ログを忘れない）
        self.assertIsNone(aw.parse_since(None))
        self.assertIsNone(aw.parse_since(""))

    def test_date_is_jst_midnight(self):
        got = aw.parse_since("2026-07-01")
        self.assertEqual(got, datetime(2026, 6, 30, 15, 0, tzinfo=timezone.utc))

    def test_iso_datetime_with_and_without_offset(self):
        self.assertEqual(aw.parse_since("2026-07-01T09:30:00+00:00"),
                         datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc))
        # オフセットなしは日本時間として読む
        self.assertEqual(aw.parse_since("2026-07-01T09:30:00"),
                         datetime(2026, 7, 1, 0, 30, tzinfo=timezone.utc))

    def test_invalid_raises(self):
        for bad in ("昨日", "2026/07/01", "90", 90):
            with self.assertRaises(ValueError, msg=bad):
                aw.parse_since(bad)


class NeedsOlderTest(unittest.TestCase):
    """起点を前にずらしたら、足りない古い分だけ取りに行く。"""
    A = aw.parse_since("2026-01-01")
    B = aw.parse_since("2026-06-01")

    def test_first_time_needs_scan(self):
        self.assertTrue(aw.needs_older(None, self.B))
        self.assertTrue(aw.needs_older(None, None))

    def test_widening_needs_scan(self):
        self.assertTrue(aw.needs_older(aw.since_key(self.B), self.A))
        self.assertTrue(aw.needs_older(aw.since_key(self.B), None))   # → すべて

    def test_same_or_narrower_does_not(self):
        self.assertFalse(aw.needs_older(aw.since_key(self.B), self.B))
        self.assertFalse(aw.needs_older(aw.since_key(self.A), self.B))
        self.assertFalse(aw.needs_older(aw.since_key(None), self.A))  # 既に全部


class CoverageDbTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_roundtrip_and_first_message(self):
        with db.connect(self.path) as conn:
            self.assertIsNone(db.get_archive_coverage(conn, 10))
            db.set_archive_coverage(conn, 10, "")
            self.assertEqual(db.get_archive_coverage(conn, 10), "")
            db.set_archive_coverage(conn, 10, "2026-06-01T00:00:00+00:00")
            self.assertEqual(db.get_archive_coverage(conn, 10),
                             "2026-06-01T00:00:00+00:00")
            self.assertIsNone(db.first_message_id(conn, 10))
            db.upsert_channel(conn, id=10, name="general", type="text")
            db.upsert_user(conn, id=1, name="u", display_name="人", is_bot=False)
            for mid in (500, 300, 400):
                db.insert_message(conn, id=mid, channel_id=10, author_id=1,
                                  content="x", created_at="2026-07-01T00:00:00+00:00")
            self.assertEqual(db.first_message_id(conn, 10), 300)


class ValidateTest(unittest.TestCase):
    def _cfg(self, since):
        return {"guild_id": 1, "agents": [{"id": "agent1", "name": "a", "token": "t",
                                           "home_channel_id": 1, "archiver": True,
                                           "persona_files": []}],
                "archive": {"since": since}}

    def test_bad_since_is_reported(self):
        problems = config.validate(self._cfg("昨日"))
        self.assertTrue(any("archive.since" in p for p in problems), problems)

    def test_good_since_passes(self):
        for ok in (None, "", "2026-07-01", "2026-07-01T09:30:00+09:00"):
            self.assertFalse(
                [p for p in config.validate(self._cfg(ok)) if "archive.since" in p], ok)


if __name__ == "__main__":
    unittest.main()
