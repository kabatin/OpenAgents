#!/usr/bin/env python3
"""決定でずれるリマインダーの直し方（reminder_shift）のユニットテスト。

正解は、切り出し元で人が手作業した内容:
「定例は10/15から一時的に木曜開催」→ 毎週金曜18時のリマインダーを
10/9(金)は残す・10/15〜11/12 は木曜に臨時で5回・11/20(金)から毎週金曜に戻す。
"""

import os
import tempfile
import unittest
from datetime import datetime

from core import reminder_shift
from core import reminders

REM = {"id": 49, "channel_id": "1", "user_id": "2", "user_name": "担当者",
       "content": "毎週金曜日は定例です。", "due": "2026-10-09T18:00",
       "repeat": "weekly", "mention": "<@&3>", "mention_label": "@チーム",
       "channel_label": None}
NOW = datetime(2026, 10, 2, 12, 0)


class PlanTest(unittest.TestCase):
    def test_temporary_weekday_change_matches_manual_fix(self):
        plan = reminder_shift.plan(
            REM, now=NOW, weekday=3, start="2026-10-15", end="2026-11-12",
            content="今週は木曜日が定例です。")
        adds = [(o["due"], o["content"]) for o in plan["ops"] if o["op"] == "add_once"]
        self.assertEqual(adds, [
            ("2026-10-09T18:00", "毎週金曜日は定例です。"),   # 始まる前の週は元どおり
            ("2026-10-15T18:00", "今週は木曜日が定例です。"),
            ("2026-10-22T18:00", "今週は木曜日が定例です。"),
            ("2026-10-29T18:00", "今週は木曜日が定例です。"),
            ("2026-11-05T18:00", "今週は木曜日が定例です。"),
            ("2026-11-12T18:00", "今週は木曜日が定例です。"),
        ])
        resume = [o for o in plan["ops"] if o["op"] == "set_due"]
        self.assertEqual(resume, [{"op": "set_due", "id": 49,
                                   "due": "2026-11-20T18:00", "content": None}])

    def test_permanent_change_moves_the_weekly_itself(self):
        plan = reminder_shift.plan(
            REM, now=NOW, weekday=3, start="2026-10-15", end=None,
            content="毎週木曜日は定例です。")
        self.assertEqual(
            [(o["op"], o.get("due")) for o in plan["ops"]],
            [("add_once", "2026-10-09T18:00"), ("set_due", "2026-10-15T18:00")])
        self.assertEqual(plan["ops"][-1]["content"], "毎週木曜日は定例です。")

    def test_once_reminder_moves_within_its_week(self):
        once = dict(REM, repeat="once", due="2026-10-16T09:00")
        plan = reminder_shift.plan(once, now=NOW, weekday=3,
                                   start="2026-10-15", end=None, content=None)
        self.assertEqual(plan["ops"], [{"op": "set_due", "id": 49,
                                        "due": "2026-10-15T09:00", "content": None}])

    def test_unsupported_cases_are_left_to_people(self):
        self.assertIsNone(reminder_shift.plan(
            dict(REM, repeat="monthly"), now=NOW, weekday=3,
            start="2026-10-15", end=None, content=None))
        self.assertIsNone(reminder_shift.plan(       # 曜日が読めない
            REM, now=NOW, weekday=9, start="2026-10-15", end=None, content=None))
        self.assertIsNone(reminder_shift.plan(       # 半年分の臨時は多すぎる
            REM, now=NOW, weekday=3, start="2026-10-15", end="2027-04-01",
            content=None))

    def test_preview_lines_are_readable(self):
        plan = reminder_shift.plan(
            REM, now=NOW, weekday=3, start="2026-10-15", end="2026-11-12",
            content="今週は木曜日が定例です。")
        text = "\n".join(reminder_shift.preview(plan))
        self.assertIn("10/9(金) 18:00 いつもの文面（1回だけ）", text)
        self.assertIn("10/15(木)・10/22(木)・10/29(木)・11/5(木)・11/12(木) 18:00", text)
        self.assertIn("11/20(金)から元の繰り返しに戻す", text)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = reminders.STATE_FILE
        reminders.STATE_FILE = os.path.join(self.tmp.name, "reminders.json")
        e, _ = reminders.add_reminder("1", "2", "担当者", REM["content"],
                                      datetime(2026, 10, 9, 18, 0), "weekly",
                                      now=NOW)
        self.rid = e["id"]

    def tearDown(self):
        reminders.STATE_FILE = self.orig
        self.tmp.cleanup()

    def test_apply_registers_and_moves_with_backup(self):
        rem = dict(reminders.find_entry(self.rid))
        plan = reminder_shift.plan(rem, now=NOW, weekday=3, start="2026-10-15",
                                   end="2026-11-12", content="木曜です。")
        done = reminder_shift.apply(plan, now=NOW)
        self.assertEqual(done, {"added": 6, "moved": 1})
        backup = reminder_shift.last_backup()
        self.assertTrue(os.path.exists(backup))
        self.assertTrue(backup.startswith(self.tmp.name))
        self.assertEqual(reminders.find_entry(self.rid)["due"], "2026-11-20T18:00")
        dues = sorted(r["due"] for r in reminders.list_active() if r["id"] != self.rid)
        self.assertEqual(dues[0], "2026-10-09T18:00")
        self.assertEqual(len(dues), 6)

    def test_late_approval_never_fires_immediately(self):
        # ✅が遅れて案の日付が過ぎていても、その場で出さない（次の回へ送る）
        rem = dict(reminders.find_entry(self.rid))
        plan = reminder_shift.plan(rem, now=NOW, weekday=3, start="2026-10-15",
                                   end=None, content="木曜です。")
        late = datetime(2026, 10, 20, 12, 0)
        reminder_shift.apply(plan, now=late)
        self.assertEqual(reminders.find_entry(self.rid)["due"], "2026-10-22T18:00")


if __name__ == "__main__":
    unittest.main()
