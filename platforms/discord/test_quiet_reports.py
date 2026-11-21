#!/usr/bin/env python3
"""読まれない週次の自己報告をチャンネルに出さない設定のテスト。

proactive.weekly_report_to_discord を OFF にすると、投稿もグラフ生成もせず、
文面を活動ログ（proactive_log）にだけ残す。既定は従来どおり投稿する。

実行: ./venv/bin/python -m unittest platforms.discord.test_quiet_reports
"""

import asyncio
import datetime
import os
import tempfile
import types
import unittest
from unittest import mock

from core import db
from platforms.discord import agent_loops

JST = datetime.timezone(datetime.timedelta(hours=9))
FRIDAY = datetime.datetime(2026, 11, 13, 17, 30, tzinfo=JST)


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kw):
        self.sent.append((text, kw))
        return types.SimpleNamespace(id=555)


class WeeklySelfReportTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.db_path)
        self.channel = FakeChannel()
        self.bot = types.SimpleNamespace(
            agent={"id": "agent1", "name": "エージェント"}, home_channel_id=1,
            proactive_cfg={}, get_channel=lambda _id: self.channel)
        p = mock.patch.object(agent_loops, "DB_PATH", self.db_path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def _log(self):
        with db.connect(self.db_path) as conn:
            return [dict(zip(("kind", "action", "detail"), tuple(r)))
                    for r in conn.execute("SELECT kind, action, detail "
                                          "FROM proactive_log ORDER BY id")]

    def _run(self, cfg):
        self.bot.proactive_cfg = cfg
        with mock.patch.object(agent_loops.reminders, "now_jst",
                               return_value=FRIDAY), \
             mock.patch.object(agent_loops.proactive,
                               "should_send_weekly_report",
                               return_value=True), \
             mock.patch.object(agent_loops.proactive, "audit_silences",
                               return_value=None), \
             mock.patch.object(agent_loops, "AGENTS", []), \
             mock.patch.object(agent_loops.dashboard, "build_chart_file",
                               return_value=None) as chart:
            asyncio.run(
                agent_loops.AgentLoopsMixin._maybe_weekly_report(self.bot))
        return chart

    def _marked(self):
        with db.connect(self.db_path) as conn:
            return db.get_proactive_state(
                conn, agent_loops.proactive.REPORT_STATE_PREFIX + "agent1")

    def test_off_keeps_it_out_of_the_channel(self):
        chart = self._run({"weekly_report_to_discord": False})
        self.assertEqual(self.channel.sent, [])
        chart.assert_not_called()
        log = self._log()
        self.assertEqual([(r["kind"], r["action"]) for r in log],
                         [("weekly_report", "shadow")])
        self.assertIn("レポート", log[0]["detail"])
        self.assertTrue(self._marked())     # 毎周期つくり直さない

    def test_default_still_posts(self):
        self._run({})
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self._log(), [])
        self.assertTrue(self._marked())


if __name__ == "__main__":
    unittest.main()
