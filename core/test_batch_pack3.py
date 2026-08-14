#!/usr/bin/env python3
"""バッチ3（RM#3 エピソード記憶 / #53 得意分野マップ / #12 A/B実験）のテスト。"""

import os
import tempfile
import unittest

from core import ab_test
from core import db
from core import episodes


class TestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)


class EpisodeTest(TestBase):
    def _seed(self):
        with db.connect(self.db_path) as conn:
            db.add_decision(conn, agent_id="agent1",
                            decision="賞品はしゃもじに決定", topic="グッズ",
                            source_kind="minutes", source_message_id=1,
                            channel_id=7, decided_on="2026-07-24",
                            created_at="t")
            aid = db.add_action_item(
                conn, agent_id="agent1", source_message_id=2, channel_id=7,
                task="グッズ発注", owners="<@1>", due_date="2026-08-08",
                urgent=False, created_at="t")
            conn.execute("UPDATE action_items SET status='done' WHERE id=?",
                         (aid,))
            db.add_event(conn, agent_id="agent1", name="サマーカップ",
                         event_date="2026-08-29", source_decision_id=99,
                         channel_id=7, milestones_json="[]",
                         status="planned", created_at="t")

    def test_sync_is_idempotent_and_builds_timeline(self):
        self._seed()
        self.assertEqual(episodes.sync(self.db_path), 3)
        self.assertEqual(episodes.sync(self.db_path), 0)   # 二重記録しない
        block = episodes.build_timeline_block(self.db_path, 7)
        self.assertIn("これまでの経緯", block)
        self.assertIn("[決定] 賞品はしゃもじに決定", block)
        self.assertIn("[完了] グッズ発注", block)
        self.assertIn("[イベント確定] サマーカップ", block)

    def test_timeline_is_newest_first(self):
        self._seed()
        episodes.sync(self.db_path)
        block = episodes.build_timeline_block(self.db_path, 7)
        lines = [l_ for l_ in block.splitlines() if l_.startswith("- ")]
        dates = [l_.split()[1] for l_ in lines]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_empty_channel_returns_blank(self):
        self.assertEqual(episodes.build_timeline_block(self.db_path, 99), "")


class ExpertiseMapTest(TestBase):
    def test_map_uses_first_profile_line(self):
        with db.connect(self.db_path) as conn:
            db.upsert_profile(conn, user_id=1, display_name="佐藤",
                              profile="- 役割: 代表・意思決定\n- 簡潔派",
                              covered_until=10, updated_at="t")
            db.upsert_profile(conn, user_id=2, display_name="山田",
                              profile="役割: グッズ制作の実務担当",
                              covered_until=10, updated_at="t")
        block = episodes.build_expertise_map(self.db_path)
        self.assertIn("佐藤: 役割: 代表・意思決定", block)
        self.assertIn("山田: 役割: グッズ制作の実務担当", block)
        self.assertNotIn("簡潔派", block)   # 1行目だけ

    def test_no_profiles_blank(self):
        self.assertEqual(episodes.build_expertise_map(self.db_path), "")


class AbTestTest(TestBase):
    def test_round_robin_pick(self):
        ab_test.ensure_variants(self.db_path)
        first = ab_test.pick(self.db_path)
        second = ab_test.pick(self.db_path)
        self.assertNotEqual(first[0], second[0])   # 交互に選ばれる
        third = ab_test.pick(self.db_path)
        self.assertEqual(third[0], first[0])

    def test_no_evaluation_until_enough_samples(self):
        ab_test.ensure_variants(self.db_path)
        for _ in range(4):
            vid, _b = ab_test.pick(self.db_path)
            ab_test.record_feedback(self.db_path, vid, "up")
        self.assertIsNone(ab_test.evaluate(self.db_path))

    def test_reports_only_on_clear_gap(self):
        ab_test.ensure_variants(self.db_path)
        ids = {}
        for _ in range(ab_test.MIN_SAMPLES * 2):
            vid, _b = ab_test.pick(self.db_path)
            ids.setdefault(vid, 0)
            ids[vid] += 1
        a_id, b_id = sorted(ids)
        for _ in range(8):        # A: 👍8/8=100%
            ab_test.record_feedback(self.db_path, a_id, "up")
        for _ in range(8):        # B: 👍0/8=0%
            ab_test.record_feedback(self.db_path, b_id, "down")
        result = ab_test.evaluate(self.db_path)
        self.assertIsNotNone(result)
        self.assertEqual(result["best"]["id"], a_id)
        report = ab_test.build_report(result)
        self.assertIn("👍率100%", report)
        self.assertIn("管理者判断", report)   # 自動採用しない

    def test_small_gap_no_report(self):
        ab_test.ensure_variants(self.db_path)
        ids = set()
        for _ in range(ab_test.MIN_SAMPLES * 2):
            vid, _b = ab_test.pick(self.db_path)
            ids.add(vid)
        for vid in ids:           # 両方 👍3/4 = 差ゼロ
            for _ in range(3):
                ab_test.record_feedback(self.db_path, vid, "up")
            ab_test.record_feedback(self.db_path, vid, "down")
        self.assertIsNone(ab_test.evaluate(self.db_path))


if __name__ == "__main__":
    unittest.main()
