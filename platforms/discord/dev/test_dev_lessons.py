#!/usr/bin/env python3
"""dev_lessons（教訓帳）とジョブ検索ヘルパのDB入出力テスト。

実行: ../chatbot/venv/bin/python -m unittest test_dev_lessons -v
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# insert(0)だと同名モジュール（bot.py等）が archive 側に解決されて他テストを壊す。
# db は archive にしか無いので append で十分（devbot側の優先を保つ）。
from core import db
class DevLessonsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_newest_first_with_limit(self):
        with db.connect(self.path) as conn:
            for i in range(7):
                db.add_dev_lesson(conn, cap_req_id=i, job_id=i, kind="failed",
                                  text=f"教訓{i}", created_at="2026-07-29T00:00")
            got = db.recent_dev_lessons(conn, limit=5)
        self.assertEqual(len(got), 5)
        self.assertEqual(got[0], {"kind": "failed", "text": "教訓6"})  # 新しい順


class ClaimAndSupersedeTest(unittest.TestCase):
    """承認ゲートの排他（二重👍）と承認すり替え対策のDB側検証。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _built_job(self, conn, cap=7):
        jid = db.add_dev_job(conn, cap_req_id=cap, branch="b", worktree="w",
                             channel_id=1, created_at="t")
        db.update_dev_job(conn, jid, updated_at="t", status="built")
        return jid

    def test_claim_is_exclusive(self):
        with db.connect(self.path) as conn:
            jid = self._built_job(conn)
            self.assertTrue(db.claim_dev_job(
                conn, jid, from_status="built", to_status="deploying",
                updated_at="t2"))
            # 2回目（二重👍/👎の負け側）は失敗する
            self.assertFalse(db.claim_dev_job(
                conn, jid, from_status="built", to_status="rejected",
                updated_at="t3"))
            self.assertEqual(db.get_dev_job(conn, jid)["status"], "deploying")

    def test_supersede_only_touches_built(self):
        with db.connect(self.path) as conn:
            j_built = self._built_job(conn, cap=7)
            j_failed = db.add_dev_job(conn, cap_req_id=7, branch="b2",
                                      worktree="w2", channel_id=1,
                                      created_at="t")
            db.update_dev_job(conn, j_failed, updated_at="t", status="failed")
            j_other = self._built_job(conn, cap=8)   # 別起票は触らない
            db.supersede_built_jobs(conn, 7, updated_at="t2")
            self.assertEqual(db.get_dev_job(conn, j_built)["status"],
                             "superseded")
            self.assertEqual(db.get_dev_job(conn, j_failed)["status"], "failed")
            self.assertEqual(db.get_dev_job(conn, j_other)["status"], "built")


class LatestDevJobTest(unittest.TestCase):
    def test_returns_newest_job_or_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            db.init_db(path)
            with db.connect(path) as conn:
                db.add_dev_job(conn, cap_req_id=7, branch="b1", worktree="w1",
                               channel_id=1, created_at="t")
                j2 = db.add_dev_job(conn, cap_req_id=7, branch="b2",
                                    worktree="w2", channel_id=1, created_at="t")
                got = db.latest_dev_job_for_cap(conn, 7)
                self.assertEqual(got["id"], j2)
                self.assertEqual(got["branch"], "b2")
                self.assertIsNone(db.latest_dev_job_for_cap(conn, 99))


if __name__ == "__main__":
    unittest.main()
