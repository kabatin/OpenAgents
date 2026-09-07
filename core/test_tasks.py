#!/usr/bin/env python3
"""追跡タスクの統一入口（tasks.py）と出口（stale）のユニットテスト。"""

import os
import tempfile
import unittest
from datetime import datetime

from core import action_items
from core import db
from core import honesty
from core import reminders
from core import tasks


class TasksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        self._orig_state = reminders.STATE_FILE
        reminders.STATE_FILE = os.path.join(self.tmp.name, "reminders.json")
        with db.connect(self.path) as conn:
            db.add_action_item(conn, agent_id="agent1", source_message_id=1,
                               channel_id=5, task="会場予約", owners="<@100>",
                               due_date="2026-09-10", urgent=0,
                               created_at="2026-09-01T10:00")
            conn.execute(
                """INSERT INTO homework_items(agent_id, source_message_id, channel_id,
                   owner, task, committed_date, follow_up_date, status, created_at)
                   VALUES('agent1', 2, 5, '<@200>', '資料を送る', '2026-09-01',
                          '2026-09-04', 'asked', 'x')""")
        reminders.add_reminder(channel_id=5, user_id="100", user_name="担当者",
                               content="準備", due=datetime(2099, 1, 5, 10, 0),
                               repeat="once", agent_id="agent1")

    def tearDown(self):
        reminders.STATE_FILE = self._orig_state
        self.tmp.cleanup()

    def test_parse_key(self):
        self.assertEqual(tasks.parse_key("A6"), ("action", 6))
        self.assertEqual(tasks.parse_key("h27"), ("homework", 27))
        self.assertEqual(tasks.parse_key("R-57"), ("reminder", 57))
        self.assertEqual(tasks.parse_key(6), ("action", 6))
        self.assertIsNone(tasks.parse_key("X1"))
        self.assertIsNone(tasks.parse_key("S1"))   # シート期日は未対応
        self.assertIsNone(tasks.parse_key(""))

    def test_list_all_unifies_three_kinds(self):
        rows = tasks.list_all(self.path, "agent1", actor_id="100",
                              is_admin=False, guild_id="9")
        keys = [r["key"] for r in rows]
        self.assertEqual(keys, ["H1", "A1", "R1"])   # 期日順
        self.assertIn("discord.com/channels/9/5/1", rows[1]["source_link"])
        self.assertTrue(all(r["editable"] for r in rows))
        line = tasks.format_line(rows[0])
        self.assertIn("H1 [宿題]", line)
        # 他人のリマインダーは見えない
        rows2 = tasks.list_all(self.path, "agent1", actor_id="200",
                               is_admin=False)
        self.assertNotIn("R1", [r["key"] for r in rows2])
        # 管理者は全員分
        rows3 = tasks.list_all(self.path, "agent1", actor_id="200",
                               is_admin=True)
        self.assertIn("R1", [r["key"] for r in rows3])

    def test_update_routes_by_kind(self):
        ok, notes = tasks.update(self.path, "agent1", "A1", "due",
                                 due="2026-09-12", actor_id="100",
                                 is_admin=False)
        self.assertTrue(ok, notes)
        self.assertTrue(honesty.SUCCESS_DEEDS["action"].search(notes[0]))
        no, notes = tasks.update(self.path, "agent1", "H1", "done",
                                 actor_id="100", is_admin=False)
        self.assertFalse(no)   # 宿題の本人でない
        self.assertTrue(honesty.FAIL_DEEDS["action"].search(notes[0]))
        ok, notes = tasks.update(self.path, "agent1", "H1", "due",
                                 due="2026-09-15", actor_id="200",
                                 is_admin=False)
        self.assertTrue(ok, notes)
        self.assertIn("2026-09-04 → 2026-09-15", notes[0])
        ok, notes = tasks.update(self.path, "agent1", "H1", "done",
                                 actor_id="1", is_admin=True)
        self.assertTrue(ok)
        self.assertTrue(honesty.SUCCESS_DEEDS["action"].search(notes[0]))
        self.assertNotIn("H1", [r["key"] for r in tasks.list_all(
            self.path, "agent1", is_admin=True)])
        no, notes = tasks.update(self.path, "agent1", "R1", "due",
                                 due="2026-09-12", actor_id="100", is_admin=False)
        self.assertFalse(no)
        ok, notes = tasks.update(self.path, "agent1", "R1", "cancel",
                                 actor_id="100", is_admin=False)
        self.assertTrue(ok, notes)
        bad, notes = tasks.update(self.path, "agent1", "zz", "done",
                                  actor_id="100", is_admin=False)
        self.assertFalse(bad)
        bad, notes = tasks.update(self.path, "agent1", "A1", "explode",
                                  actor_id="100", is_admin=False)
        self.assertFalse(bad)
        bad, notes = tasks.update(self.path, "agent1", "A1", "due", due="9/12",
                                  actor_id="100", is_admin=False)
        self.assertFalse(bad)

    def test_stale_exit_and_reopen(self):
        now = datetime(2026, 9, 20, 10, 0)
        # 超過の声かけを記録 → overdue_at が入る（before 段階では入らない）
        action_items.record_nudge(self.path, 1, "before", 998, now=now)
        with db.connect(self.path) as conn:
            row = conn.execute("SELECT overdue_at FROM action_items WHERE id=1").fetchone()
        self.assertIsNone(row[0])
        action_items.record_nudge(self.path, 1, "overdue", 999, now=now)
        with db.connect(self.path) as conn:
            row = conn.execute("SELECT overdue_at FROM action_items WHERE id=1").fetchone()
        self.assertEqual(row[0], "2026-09-20T10:00")
        # 二度目の超過声かけでも最初の日時を保つ
        action_items.record_nudge(self.path, 1, "overdue", 1000,
                                  now=datetime(2026, 9, 22, 10, 0))
        with db.connect(self.path) as conn:
            row = conn.execute("SELECT overdue_at FROM action_items WHERE id=1").fetchone()
        self.assertEqual(row[0], "2026-09-20T10:00")
        # 6日後はまだ / 7日後で手放し対象
        self.assertEqual(action_items.items_needing_stale(
            self.path, "agent1", datetime(2026, 9, 26, 10, 0)), [])
        stale = action_items.items_needing_stale(
            self.path, "agent1", datetime(2026, 9, 27, 10, 0))
        self.assertEqual([i["id"] for i in stale], [1])
        self.assertIn("A1 を open に", action_items.build_stale_text(stale[0]))
        self.assertTrue(action_items.mark_stale(self.path, 1, "agent1"))
        self.assertNotIn("A1", [r["key"] for r in tasks.list_all(self.path, "agent1")])
        # 手放し済みは再度拾わない
        self.assertEqual(action_items.items_needing_stale(
            self.path, "agent1", datetime(2026, 10, 27, 10, 0)), [])
        # 会話から再開
        ok, notes = tasks.update(self.path, "agent1", "A1", "open",
                                 actor_id="100", is_admin=False)
        self.assertTrue(ok, notes)
        self.assertIn("再開", notes[0])
        self.assertIn("A1", [r["key"] for r in tasks.list_all(self.path, "agent1")])
        # open のものは再開できない
        no, notes = tasks.update(self.path, "agent1", "A1", "open",
                                 actor_id="100", is_admin=False)
        self.assertFalse(no)

    def test_migration_adds_overdue_at(self):
        with db.connect(self.path) as conn:
            conn.execute("ALTER TABLE action_items DROP COLUMN overdue_at")
        db.init_db(self.path)   # migration が走る
        with db.connect(self.path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(action_items)")]
        self.assertIn("overdue_at", cols)


if __name__ == "__main__":
    unittest.main()
