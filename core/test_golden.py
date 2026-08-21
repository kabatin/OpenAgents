#!/usr/bin/env python3
"""ゴールデンセット（進化ロードマップ#16）のユニットテスト。

捕獲の入口の品質ゲート（2026-08-18）が中心。👍された投稿のうち
「一方的な発信」「相槌だけの質問」を評価データに混ぜないことを検証する。
"""

import os
import tempfile
import unittest

from core import db
from core import golden


class QualityGateTest(unittest.TestCase):
    """捕獲の入口の品質ゲート（2026-08-18）。23件中5件が壊れていた。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(self.db_path)
        with db.connect(self.db_path) as conn:
            db.upsert_channel(conn, id=7, name="g", type="text")
            db.upsert_user(conn, id=1, name="h", display_name="常谷",
                           is_bot=False)
            db.upsert_user(conn, id=99, name="senko", display_name="AI戦子",
                           is_bot=True)

    def tearDown(self):
        os.unlink(self.db_path)

    def _pair(self, q_id, a_id, question, answer, unsolicited=False):
        with db.connect(self.db_path) as conn:
            db.insert_message(conn, id=q_id, channel_id=7, author_id=1,
                              content=question, created_at="t")
            db.insert_message(conn, id=a_id, channel_id=7, author_id=99,
                              content=answer, created_at="t")
            if unsolicited:
                db.add_proactive_log(
                    conn, agent_id="senko", kind="weekly", action="posted",
                    channel_id=7, posted_message_id=a_id, created_at="t")

    def test_praise_only_detection(self):
        for t in ["加点！", "おお、いいね", "偉い", "ありがとう", "さすが！",
                  "草", ""]:
            self.assertTrue(golden.is_praise_only(t), t)
        for t in ["リマインドリスト", "8/6にレプユニの相談をリマインドして"]:
            self.assertFalse(golden.is_praise_only(t), t)

    def test_unsolicited_post_not_captured(self):
        """週次レポートへの👍は質問が存在しないので入れない。"""
        self._pair(10, 11, "このスプシにシート追加が良さそうですね",
                   "📊 今週の自発活動レポート", unsolicited=True)
        self.assertFalse(golden.capture(self.db_path, "senko", 11))
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.count_golden(conn), 0)

    def test_praise_question_not_captured(self):
        self._pair(20, 21, "加点！", "リマインダーの複数宛先対応が入ったっス")
        self.assertFalse(golden.capture(self.db_path, "senko", 21))

    def test_real_qa_still_captured(self):
        self._pair(30, 31, "リマインドリストを見せて",
                   "現在のリマインダーはこれっス📋 id=42…")
        self.assertTrue(golden.capture(self.db_path, "senko", 31))
        with db.connect(self.db_path) as conn:
            self.assertEqual(db.count_golden(conn, active_only=True), 1)

    def test_audit_disables_dirty_rows(self):
        """既存の汚れた行を仕分ける（第2段）。"""
        self._pair(40, 41, "正しい質問ですこれは長さも十分",
                   "正しい回答っス")
        self._pair(50, 51, "こちらも十分な長さの質問です",
                   "📰 今週の業界ニュース", unsolicited=True)
        with db.connect(self.db_path) as conn:   # ゲート前の状態を再現
            for q, a in ((40, 41), (50, 51)):
                db.add_golden(conn, agent_id="senko",
                              question=db.get_message(conn, q)["content"],
                              answer=db.get_message(conn, a)["content"],
                              source_answer_id=a, channel_id=7,
                              created_at="t")
            db.add_golden(conn, agent_id="senko", question="加点！",
                          answer="どうもっス", source_answer_id=999,
                          channel_id=7, created_at="t")
            self.assertEqual(db.count_golden(conn), 3)
        result = golden.audit_existing(self.db_path)
        self.assertEqual(result["kept"], 1)
        reasons = sorted(r for _i, r in result["disabled"])
        self.assertEqual(reasons, ["一方的投稿（質問なし）", "相槌・褒め言葉のみ"])
        # 二度目は何も落ちない（冪等）
        self.assertEqual(golden.audit_existing(self.db_path)["disabled"], [])


if __name__ == "__main__":
    unittest.main()


class AutoPostGateTest(unittest.TestCase):
    """proactive_logに載らない定時投稿も本文の書式で除外する（2026-08-18）。"""

    def test_auto_post_prefixes(self):
        for a in ["📊 今週の自発活動レポート（07/31〜）",
                  "⏰ <@1> リマインドっスよ: 告知画像作成",
                  "🌙 今日の自己監査っス",
                  "🗞 **ヤルキマン新聞**",
                  "🌊 新しい決定「…」で影響が出そうな記録があるっス"]:
            self.assertTrue(golden.is_auto_post(a), a)

    def test_normal_answers_pass(self):
        for a in ["了解っス、8/6朝にリマインドセットするっス📝",
                  "今わかる分だとこれっス📋\n| id | 頻度 |",
                  "ID:105のurlに追加するっスね。"]:
            self.assertFalse(golden.is_auto_post(a), a)
