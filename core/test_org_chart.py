#!/usr/bin/env python3
"""組織図の自動更新（RM#65）・人格の定期健診（RM#73）のユニットテスト。"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from core import db
from core import org_chart


class TestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)


class GateTest(TestBase):
    NOW = datetime(2026, 9, 1, 14, 20)

    def test_monthly_once(self):
        self.assertTrue(org_chart.should_post_chart(
            self.db_path, "agent1", now=self.NOW))
        org_chart.mark_chart_posted(self.db_path, "agent1", 900, now=self.NOW)
        self.assertFalse(org_chart.should_post_chart(
            self.db_path, "agent1", now=self.NOW))
        self.assertEqual(
            org_chart.previous_chart_id(self.db_path, "agent1"), 900)
        self.assertTrue(org_chart.should_post_chart(
            self.db_path, "agent1", now=datetime(2026, 10, 1, 14, 5)))

    def test_checkup_gate_is_independent(self):
        org_chart.mark_chart_posted(self.db_path, "agent1", 1, now=self.NOW)
        self.assertTrue(org_chart.should_checkup(
            self.db_path, "agent1", now=datetime(2026, 9, 1, 15, 10)))


class ChartTest(TestBase):
    NOW = datetime(2026, 9, 1, 14, 0)

    def _member(self, uid, name, profile, days_ago):
        created = (self.NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=1, name="g", type="text")
            db.upsert_user(conn, id=uid, name=f"u{uid}", display_name=name,
                           is_bot=False)
            db.insert_message(conn, id=uid * 100, channel_id=1,
                              author_id=uid, content="発言",
                              created_at=created)
            db.upsert_profile(conn, user_id=uid, display_name=name,
                              profile=profile, covered_until=1,
                              updated_at="t")

    def test_only_recent_humans_listed(self):
        self._member(1, "佐藤", "- 役割: 代表", days_ago=3)
        self._member(2, "退職者", "- 役割: 元スタッフ", days_ago=200)
        org = org_chart.collect_org(
            self.db_path, [{"name": "エージェント1", "role": "総務"}], now=self.NOW)
        names = [h["name"] for h in org["humans"]]
        self.assertIn("佐藤", names)
        self.assertNotIn("退職者", names)

    def test_chart_text(self):
        org = {"humans": [{"name": "佐藤", "role": "役割: 代表", "recent": 5}],
               "ai": [{"name": "エージェント1", "role": "総務"},
                      {"name": "AI経理", "role": "（試用枠）",
                       "trial": True}]}
        text = org_chart.build_chart(org, "2026-09-01")
        self.assertIn("エージェント1: 総務", text)
        self.assertIn("AI経理（試用枠）", text)
        self.assertIn("佐藤: 役割: 代表", text)
        self.assertIn("推定", text)   # 断定しない旨


class CheckupTest(TestBase):
    def test_sample_and_prompt(self):
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=1, name="g", type="text")
            db.upsert_user(conn, id=99, name="agent1", display_name="エージェント1",
                           is_bot=True)
            for i in range(3):
                db.insert_message(conn, id=i + 1, channel_id=1, author_id=99,
                                  content=f"了解っス{i}", created_at="t")
        utt = org_chart.sample_utterances(self.db_path, 99)
        self.assertEqual(len(utt), 3)
        prompt = org_chart.build_checkup_prompt("エージェント1", "語尾は〜っス",
                                                utt)
        self.assertIn("語尾は〜っス", prompt)
        self.assertIn("了解っス", prompt)
        self.assertIn("提案に留める", prompt)

    def test_checkup_without_samples_returns_none(self):
        def boom(p):
            raise AssertionError("サンプル無しで呼んだ")
        self.assertIsNone(org_chart.checkup("エージェント1", "p", [], model="x",
                                            invoke_fn=boom))

    def test_post_lists_each_agent(self):
        post = org_chart.build_checkup_post(
            [("エージェント1", "概ね一致"), ("エージェント2", None)])
        self.assertIn("エージェント1", post)
        self.assertIn("概ね一致", post)
        self.assertIn("判定できなかった", post)
        self.assertIn("管理者判断", post)


if __name__ == "__main__":
    unittest.main()
