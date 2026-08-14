#!/usr/bin/env python3
"""会議前の予習パック（prep_pack / RM#43）と定例の前回比較（RM#36）のテスト。"""

import os
import tempfile
import unittest
from datetime import datetime

from core import action_items
from core import db
from core import prep_pack


class PrepTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)


class ShouldSendTest(PrepTestBase):
    FRI_18 = datetime(2026, 7, 31, 18, 30)   # 金曜・定例(19時)の1時間前枠

    def test_fires_hour_before_meeting_once(self):
        self.assertTrue(prep_pack.should_send(
            self.db_path, "agent1", weekday=4, hour=19, now=self.FRI_18))
        prep_pack.mark_sent(self.db_path, "agent1", now=self.FRI_18)
        self.assertFalse(prep_pack.should_send(
            self.db_path, "agent1", weekday=4, hour=19, now=self.FRI_18))
        # 翌週は再送できる
        self.assertTrue(prep_pack.should_send(
            self.db_path, "agent1", weekday=4, hour=19,
            now=datetime(2026, 8, 7, 18, 5)))

    def test_wrong_day_or_hour_is_silent(self):
        self.assertFalse(prep_pack.should_send(
            self.db_path, "agent1", weekday=4, hour=19,
            now=datetime(2026, 7, 30, 18, 30)))   # 木曜
        self.assertFalse(prep_pack.should_send(
            self.db_path, "agent1", weekday=4, hour=19,
            now=datetime(2026, 7, 31, 12, 0)))    # 昼


class BuildPackTest(PrepTestBase):
    def test_pack_contents_and_overdue_mark(self):
        data = {"open_items": [
                    {"id": 1, "task": "発注を進める", "owners": "<@1>",
                     "due_date": "2026-07-30"},
                    {"id": 2, "task": "画像素材の確認", "owners": "<@2>",
                     "due_date": "2026-08-02"}],
                "decisions": [{"decision": "賞品はしゃもじに決定",
                               "topic": "夏季"}],
                "last_minutes_id": 42}
        text = prep_pack.build_pack(data, guild_id="9",
                                    minutes_channel_id=555,
                                    today="2026-07-31", hour=19)
        self.assertIn("予習パック", text)
        self.assertIn("🔴期日超過", text)          # 7/30は超過
        self.assertIn("期日 2026-08-02", text)
        self.assertIn("[夏季] 賞品はしゃもじに決定", text)
        self.assertIn("discord.com/channels/9/555/42", text)

    def test_empty_data_returns_none(self):
        self.assertIsNone(prep_pack.build_pack(
            {"open_items": [], "decisions": [], "last_minutes_id": 0},
            guild_id="9", minutes_channel_id=555, today="2026-07-31",
            hour=19))


class CarryoverTest(unittest.TestCase):
    OPEN = [{"id": 1, "task": "グッズの最終デザイン確定と業者への発注",
             "due_date": "2026-07-30"},
            {"id": 2, "task": "会場の下見", "due_date": "2026-08-05"}]

    def test_similar_task_is_carried_not_retracked(self):
        new = [{"task": "グッズの最終デザイン確定と業者へ発注を急ぐ",
                "owners": "<@1>", "due_date": "2026-08-07", "urgent": False},
               {"task": "新しい告知バナーの入稿", "owners": "<@2>",
                "due_date": "2026-08-03", "urgent": False}]
        track, carried = action_items.match_carryover(new, self.OPEN)
        self.assertEqual([t["task"][:3] for t in track], ["新しい"])
        self.assertEqual(carried[0]["old"]["id"], 1)

    def test_no_open_items_tracks_all(self):
        new = [{"task": "x", "owners": "<@1>", "due_date": "2026-08-01",
                "urgent": False}]
        track, carried = action_items.match_carryover(new, [])
        self.assertEqual((len(track), carried), (1, []))

    def test_carryover_note_marks_overdue_and_again(self):
        carried = [{"new": {"task": "グッズ発注"}, "old": self.OPEN[0]}]
        note = action_items.build_carryover_note(self.OPEN, carried,
                                                 "2026-07-31")
        self.assertIn("持ち越し2件", note)
        self.assertIn("期日超過", note)
        self.assertIn("今週も議題に", note)
        self.assertEqual(action_items.build_carryover_note([], [], "d"), "")


if __name__ == "__main__":
    unittest.main()
