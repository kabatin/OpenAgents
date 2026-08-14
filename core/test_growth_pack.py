#!/usr/bin/env python3
"""自己成長パック（勝ちパターン学習＋自己採点の週次蒸留）のテスト。

./venv/bin/python -m unittest test_growth_pack -v
"""

import os
import tempfile
import unittest
from datetime import datetime

from core import db
from core import proactive
from core import reminders
from core import selfreview_distill as svd


def _seed_spoke(conn, *, agent_id="agent1", message_id=100, kind="info",
                channel_id=5, content="良い感じの自発発言テキストっス"):
    """自発発言(spoke)＋投稿本文をDBへ植える。"""
    conn.execute(
        """INSERT INTO proactive_log(agent_id, kind, action, channel_id,
               posted_message_id, created_at)
           VALUES(?,?,'spoke',?,?,?)""",
        (agent_id, kind, channel_id, message_id, "2026-08-10T12:00"))
    conn.execute(
        """INSERT INTO messages(id, channel_id, author_id, content, created_at)
           VALUES(?,?,?,?,?)""",
        (message_id, channel_id, 1, content, "2026-08-10T12:00"))


def _add_feedback(conn, message_id, value, user_id="u1"):
    db.add_feedback(conn, message_id=message_id, agent_id="agent1",
                    kind="reaction", value=value, user_id=user_id,
                    created_at="2026-08-10T12:01")


class TestWinLessons(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init_db(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_up_on_spoke_records_win(self):
        with db.connect(self.tmp.name) as conn:
            _seed_spoke(conn, message_id=100)
            _add_feedback(conn, 100, "up")
        kind = proactive.record_win_from_feedback(self.tmp.name, 100)
        self.assertEqual(kind, "info")
        with db.connect(self.tmp.name) as conn:
            wins = db.recent_proactive_lessons(conn, "agent1", polarity="up")
        self.assertEqual(len(wins), 1)
        self.assertIn("良い感じ", wins[0]["text"])

    def test_up_on_normal_answer_is_ignored(self):
        """自発発言でない投稿への👍は勝ちパターンにしない（goldenの領分）。"""
        with db.connect(self.tmp.name) as conn:
            conn.execute(
                """INSERT INTO messages(id, channel_id, author_id, content,
                       created_at) VALUES(200, 5, 1, '普通の回答', 't')""")
        self.assertIsNone(
            proactive.record_win_from_feedback(self.tmp.name, 200))

    def test_lift_win_when_all_ups_removed(self):
        with db.connect(self.tmp.name) as conn:
            _seed_spoke(conn, message_id=100)
            _add_feedback(conn, 100, "up")
        proactive.record_win_from_feedback(self.tmp.name, 100)
        with db.connect(self.tmp.name) as conn:
            db.remove_feedback(conn, message_id=100, user_id="u1", value="up")
        self.assertTrue(proactive.lift_win_if_no_ups(self.tmp.name, 100))
        with db.connect(self.tmp.name) as conn:
            self.assertEqual(
                db.recent_proactive_lessons(conn, "agent1", polarity="up"), [])

    def test_win_does_not_leak_into_down_lessons(self):
        """極性の分離: 👍の勝ちパターンが👎教訓の取得に混ざらない。"""
        with db.connect(self.tmp.name) as conn:
            _seed_spoke(conn, message_id=100)
            _add_feedback(conn, 100, "up")
        proactive.record_win_from_feedback(self.tmp.name, 100)
        with db.connect(self.tmp.name) as conn:
            downs = db.recent_proactive_lessons(conn, "agent1")  # 既定=down
        self.assertEqual(downs, [])

    def test_lift_down_does_not_kill_win(self):
        """👎解除が👍由来の勝ちパターンを巻き添えにしない（極性つき解除）。"""
        with db.connect(self.tmp.name) as conn:
            _seed_spoke(conn, message_id=100)
            _add_feedback(conn, 100, "up")
        proactive.record_win_from_feedback(self.tmp.name, 100)
        # 👎ゼロの状態で解除判定が走っても（＝👎が付いて外れた後でも）
        proactive.lift_lesson_if_no_downs(self.tmp.name, 100)
        with db.connect(self.tmp.name) as conn:
            wins = db.recent_proactive_lessons(conn, "agent1", polarity="up")
        self.assertEqual(len(wins), 1)

    def test_build_wins_block(self):
        block = proactive.build_wins_block(
            [{"kind": "info", "text": "abc"}])
        self.assertIn("良い例", block)
        self.assertIn("abc", block)
        self.assertEqual(proactive.build_wins_block([]), "")


class TestSelfreviewDistill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init_db(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _seed_scores(self, details):
        now = reminders.fmt(reminders.now_jst())
        with db.connect(self.tmp.name) as conn:
            for d in details:
                conn.execute(
                    """INSERT INTO proactive_log(agent_id, kind, action,
                           detail, created_at)
                       VALUES('agent1','selfreview','score',?,?)""",
                    (d, now))

    def test_parse_score_detail(self):
        self.assertEqual(svd.parse_score_detail("2|根拠のない断定"),
                         {"score": 2, "issue": "根拠のない断定"})
        self.assertIsNone(svd.parse_score_detail("junk"))
        self.assertIsNone(svd.parse_score_detail("9|範囲外"))
        self.assertIsNone(svd.parse_score_detail(None))

    def test_collect_only_low_scores(self):
        self._seed_scores(["2|曖昧", "5|", "1|断定", "4|長い", "3|冗長"])
        issues = svd.collect_low_issues(self.tmp.name, "agent1")
        self.assertEqual(sorted(issues), ["断定", "曖昧"])

    def test_distill_skips_when_samples_thin(self):
        self._seed_scores(["2|曖昧", "1|断定"])  # MIN_SAMPLES(3)未満
        called = []
        advice = svd.distill(self.tmp.name, "agent1", model="m",
                             invoke_fn=lambda p: called.append(p) or "[]")
        self.assertEqual(advice, [])
        self.assertEqual(called, [])  # LLMすら呼ばない

    def test_distill_stores_and_replaces(self):
        self._seed_scores(["2|曖昧", "1|断定", "2|冗長"])
        advice = svd.distill(
            self.tmp.name, "agent1", model="m",
            invoke_fn=lambda p: '["根拠を確認してから断定する", "結論から書く"]')
        self.assertEqual(len(advice), 2)
        # 2回目の蒸留で古い助言が差し替わる（積もらない）
        svd.distill(self.tmp.name, "agent1", model="m",
                    invoke_fn=lambda p: '["新しい助言"]')
        with db.connect(self.tmp.name) as conn:
            rows = db.recent_proactive_lessons(conn, "agent1", limit=10,
                                               polarity="advice")
        self.assertEqual([r["text"] for r in rows], ["新しい助言"])

    def test_parse_clamps_and_rejects(self):
        self.assertEqual(svd.parse("junk"), [])
        self.assertEqual(svd.parse('{"a":1}'), [])
        four = svd.parse('["a","b","c","d"]')
        self.assertEqual(len(four), svd.MAX_ADVICE)
        long = svd.parse(f'["{"x" * 200}"]')
        self.assertEqual(len(long[0]), svd.MAX_ADVICE_LEN)

    def test_weekly_guard(self):
        mon9 = datetime(2026, 8, 17, 9, 30)  # 月曜9時台
        self.assertTrue(svd.should_run(self.tmp.name, "agent1", now=mon9))
        svd.mark_ran(self.tmp.name, "agent1", now=mon9)
        self.assertFalse(svd.should_run(self.tmp.name, "agent1", now=mon9))
        tue = datetime(2026, 8, 18, 9, 30)   # 火曜は曜日不一致
        self.assertFalse(svd.should_run(self.tmp.name, "agent1", now=tue))

    def test_advice_block(self):
        block = svd.build_advice_block([{"text": "結論から書く"}])
        self.assertIn("自己改善メモ", block)
        self.assertIn("結論から書く", block)
        self.assertEqual(svd.build_advice_block([]), "")


class TestMigration(unittest.TestCase):
    def test_polarity_added_to_existing_db(self):
        """polarity列の無い既存DBに冪等マイグレーションが効く。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            import sqlite3
            conn = sqlite3.connect(tmp.name)
            conn.execute(
                """CREATE TABLE proactive_lessons (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       agent_id TEXT, kind TEXT, channel_id INTEGER,
                       message_id INTEGER UNIQUE, text TEXT,
                       active INTEGER DEFAULT 1, created_at TEXT)""")
            conn.execute(
                """INSERT INTO proactive_lessons(agent_id, kind, text)
                   VALUES('agent1','handoff','既存の教訓')""")
            conn.commit()
            conn.close()
            db.init_db(tmp.name)  # 2回呼んでも冪等
            db.init_db(tmp.name)
            with db.connect(tmp.name) as c:
                rows = db.recent_proactive_lessons(c, "agent1")  # down扱い
            self.assertEqual([r["text"] for r in rows], ["既存の教訓"])
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
