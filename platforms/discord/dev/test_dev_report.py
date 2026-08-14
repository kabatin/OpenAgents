#!/usr/bin/env python3
"""開発BOTの週次開発レポート（dev_report / RM#60）のユニットテスト。"""

import datetime
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

from core import db
from platforms.discord.dev import dev_report
FRI_18 = datetime.datetime(2026, 8, 7, 18, 20)


class DevReportTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_should_send_friday_18_once(self):
        self.assertTrue(dev_report.should_send(self.db_path, now=FRI_18))
        dev_report.mark_sent(self.db_path, now=FRI_18)
        self.assertFalse(dev_report.should_send(self.db_path, now=FRI_18))
        self.assertFalse(dev_report.should_send(
            self.db_path, now=datetime.datetime(2026, 8, 6, 18, 0)))  # 木曜
        self.assertTrue(dev_report.should_send(
            self.db_path, now=FRI_18 + datetime.timedelta(days=7)))

    def test_collect_counts_this_week(self):
        now = FRI_18
        with db.connect(self.db_path) as conn:
            jid = db.add_dev_job(conn, cap_req_id=9, branch="b", worktree="w",
                                 channel_id=1,
                                 created_at="2026-08-05T10:00:00")
            db.update_dev_job(conn, jid, updated_at="2026-08-05T12:00:00",
                              status="deployed")
            old = db.add_dev_job(conn, cap_req_id=4, branch="b", worktree="w",
                                 channel_id=1,
                                 created_at="2026-07-01T10:00:00")
            db.update_dev_job(conn, old, updated_at="2026-07-01T12:00:00",
                              status="rejected")   # 期間外
            db.roadmap_seed_item(conn, id=1, title="t", description="d",
                                 category="c", tier="quiet", route="devbot",
                                 effect=3, cost=1, created_at="t")
        data = dev_report.collect(self.db_path, now=now)
        self.assertEqual(data["deployed"], [9])
        self.assertEqual(data["rejected"], [])
        self.assertEqual(data["roadmap"].get("pending"), 1)

    def test_build_report_mentions_deploys_and_quiet_week(self):
        text = dev_report.build_report(
            {"deployed": [8, 9], "rejected": [4], "failed": [],
             "roadmap": {"done": 12, "pending": 86}, "watching": 1})
        self.assertIn("デプロイ: 2件（起票#8、#9）", text)
        self.assertIn("完了12/98件", text)
        self.assertIn("見張り中: 1件", text)
        quiet = dev_report.build_report(
            {"deployed": [], "rejected": [], "failed": [],
             "roadmap": {}, "watching": 0})
        self.assertIn("静かな週でした", quiet)


if __name__ == "__main__":
    unittest.main()
